from __future__ import annotations

from datetime import UTC, datetime
from hashlib import sha256
import logging
import re
from uuid import UUID
from time import monotonic

from elasticsearch import AsyncElasticsearch
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import prefer_deferred_commit
from app.models import Event, EventSourceType, VirtualWindow
from app.services.search_index import index_terminal_chunk_without_event
from app.services.summary_scheduler import schedule_summary_after_terminal_input
from app.services.terminal_command_marker import ParsedCommandMarker, extract_command_markers
from app.services.window_runtime_tags import agent_from_command

logger = logging.getLogger(__name__)
TERMINAL_OUTPUT_ACTIVITY_TOUCH_INTERVAL_SECONDS = 1.0
_terminal_output_activity_touched_at: dict[tuple[UUID, UUID], float] = {}

_SECRET_ASSIGNMENT_PATTERN = re.compile(
    r"\b(password|passwd|token|api[_-]?key|secret|access[_-]?token)=('[^']*'|\"[^\"]*\"|[^\s&]+)",
    re.IGNORECASE,
)
_SECRET_FLAG_PATTERN = re.compile(
    r"(--(?:password|passwd|token|api-key|secret|access-token)(?:=|\s+))('[^']*'|\"[^\"]*\"|\S+)",
    re.IGNORECASE,
)
_BEARER_PATTERN = re.compile(r"\bBearer\s+[^\s'\"]+", re.IGNORECASE)
_AUTO_RESUME_MARKER = "WEB_TERMINAL_AUTO_RESUME=1"


async def record_terminal_input_command(
    session: AsyncSession,
    client_id: UUID,
    window_id: UUID,
    raw_command: str,
    shell: str | None,
    cwd: str | None,
    captured_at: datetime,
    sequence: int | str | None,
) -> Event | None:
    if is_auto_resume_command(raw_command):
        return None
    await prefer_deferred_commit(session)
    redacted_command = redact_terminal_command(raw_command)
    captured_at_value = _serialize_datetime(captured_at)
    fingerprint = _terminal_input_fingerprint(window_id, redacted_command, captured_at_value, sequence)

    existing_event = await _select_event_by_fingerprint(session, client_id, fingerprint)
    if existing_event is not None:
        return existing_event

    event = Event(
        client_id=client_id,
        source_type=EventSourceType.terminal,
        source_id=str(window_id),
        kind="terminal_input_command",
        virtual_window_id=window_id,
        payload_json={
            "command": redacted_command,
            "shell": shell,
            "cwd": cwd,
            "captured_at": captured_at_value,
            "sequence": sequence,
        },
        fingerprint=fingerprint,
        created_at=captured_at,
    )
    try:
        async with session.begin_nested():
            session.add(event)
            await session.flush()
    except IntegrityError:
        existing_event = await _select_event_by_fingerprint(session, client_id, fingerprint)
        if existing_event is not None:
            return existing_event
        raise

    window = await session.get(VirtualWindow, window_id)
    if window is not None:
        if cwd:
            window.cwd = cwd
        if agent_from_command(redacted_command) is None:
            await schedule_summary_after_terminal_input(session, window)

    await session.commit()
    await session.refresh(event)
    return event


async def record_terminal_command_markers(
    session: AsyncSession,
    client_id: UUID,
    window_id: UUID,
    commands: list[ParsedCommandMarker],
) -> list[Event]:
    events: list[Event] = []
    for command in commands:
        if _marker_window_id(command) != window_id:
            continue
        phase = command.get("phase")
        if phase == "finished":
            event = await record_terminal_command_finished(
                session,
                client_id,
                window_id,
                _marker_command(command),
                _marker_shell(command),
                _marker_cwd(command),
                _marker_captured_at(command),
                _marker_sequence(command),
                _marker_exit_status(command),
            )
            if event is not None:
                events.append(event)
            continue
        raw_command = command.get("command")
        if not isinstance(raw_command, str) or raw_command == "":
            continue
        if is_auto_resume_command(raw_command):
            continue
        event = await record_terminal_input_command(
            session,
            client_id,
            window_id,
            raw_command,
            _marker_shell(command),
            _marker_cwd(command),
            _marker_captured_at(command),
            _marker_sequence(command),
        )
        if event is not None:
            events.append(event)
    return events


async def record_terminal_command_finished(
    session: AsyncSession,
    client_id: UUID,
    window_id: UUID,
    raw_command: str | None,
    shell: str | None,
    cwd: str | None,
    captured_at: datetime,
    sequence: int | str | None,
    exit_status: int | str | None,
) -> Event | None:
    if sequence is None:
        return None

    if raw_command is not None and is_auto_resume_command(raw_command):
        return None

    await prefer_deferred_commit(session)
    redacted_command = redact_terminal_command(raw_command or "")
    captured_at_value = _serialize_datetime(captured_at)
    fingerprint = f"terminal_command_finished:{window_id}:{sequence}"
    existing_event = await _select_event_by_fingerprint(session, client_id, fingerprint)
    if existing_event is not None:
        return existing_event

    event = Event(
        client_id=client_id,
        source_type=EventSourceType.terminal,
        source_id=str(window_id),
        kind="terminal_command_finished",
        virtual_window_id=window_id,
        payload_json={
            "command": redacted_command,
            "shell": shell,
            "cwd": cwd,
            "captured_at": captured_at_value,
            "sequence": sequence,
            "exit_status": exit_status,
        },
        fingerprint=fingerprint,
        created_at=captured_at,
    )
    try:
        async with session.begin_nested():
            session.add(event)
            await session.flush()
    except IntegrityError:
        existing_event = await _select_event_by_fingerprint(session, client_id, fingerprint)
        if existing_event is not None:
            return existing_event
        raise

    window = await session.get(VirtualWindow, window_id)
    if window is not None and cwd:
        window.cwd = cwd

    await session.commit()
    await session.refresh(event)
    return event


def redact_terminal_command(command: str) -> str:
    redacted = _BEARER_PATTERN.sub("Bearer [REDACTED]", command)
    redacted = _SECRET_FLAG_PATTERN.sub(lambda match: f"{match.group(1)}[REDACTED]", redacted)
    redacted = _SECRET_ASSIGNMENT_PATTERN.sub(lambda match: f"{match.group(1)}=[REDACTED]", redacted)
    return redacted


def is_auto_resume_command(command: str) -> bool:
    return (
        command.startswith(f"{_AUTO_RESUME_MARKER} ")
        or f"&& {_AUTO_RESUME_MARKER} " in command
    )


async def _select_event_by_fingerprint(session: AsyncSession, client_id: UUID, fingerprint: str) -> Event | None:
    return await session.scalar(
        select(Event).where(Event.client_id == client_id, Event.fingerprint == fingerprint)
    )


def _terminal_input_fingerprint(
    window_id: UUID,
    redacted_command: str,
    captured_at: str,
    sequence: int | str | None,
) -> str:
    if sequence is not None:
        return f"terminal_input_command:{window_id}:{sequence}"
    digest = sha256(f"{window_id}:{captured_at}:{redacted_command}".encode("utf-8")).hexdigest()[:16]
    return f"terminal_input_command:{window_id}:no-sequence:{digest}"


def _marker_window_id(command: ParsedCommandMarker) -> UUID | None:
    try:
        return UUID(str(command.get("window_id")))
    except (TypeError, ValueError):
        return None


def _marker_shell(command: ParsedCommandMarker) -> str | None:
    shell = command.get("shell")
    return shell if isinstance(shell, str) else None


def _marker_cwd(command: ParsedCommandMarker) -> str | None:
    cwd = command.get("cwd")
    return cwd if isinstance(cwd, str) and cwd else None


def _marker_command(command: ParsedCommandMarker) -> str | None:
    raw_command = command.get("command")
    return raw_command if isinstance(raw_command, str) else None


def _marker_sequence(command: ParsedCommandMarker) -> int | str | None:
    sequence = command.get("sequence")
    return sequence if isinstance(sequence, (int, str)) else None


def _marker_exit_status(command: ParsedCommandMarker) -> int | str | None:
    exit_status = command.get("exit_status")
    return exit_status if isinstance(exit_status, (int, str)) else None


def _marker_captured_at(command: ParsedCommandMarker) -> datetime:
    captured_at = command.get("captured_at")
    if isinstance(captured_at, str):
        try:
            return datetime.fromisoformat(captured_at.replace("Z", "+00:00"))
        except ValueError:
            pass
    return datetime.now(UTC)


def _serialize_datetime(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.isoformat()


async def record_terminal_output_chunk(
    session: AsyncSession,
    client_id: UUID,
    window_id: UUID,
    data: bytes,
    es_client: AsyncElasticsearch | None = None,
) -> bool:
    clean_data, commands = extract_command_markers(data)
    await record_terminal_command_markers(session, client_id, window_id, commands)
    text = clean_data.decode("utf-8", errors="replace")
    if text == "":
        return False

    activity_recorded = await _touch_terminal_output_activity(session, client_id, window_id)

    try:
        if es_client is None:
            return activity_recorded
        await index_terminal_chunk_without_event(
            es_client,
            client_id,
            window_id,
            text,
        )
    except Exception:
        logger.exception(
            "failed to index terminal output",
            extra={"client_id": str(client_id), "window_id": str(window_id)},
        )
        return activity_recorded

    return True


async def _touch_terminal_output_activity(
    session: AsyncSession,
    client_id: UUID,
    window_id: UUID,
) -> bool:
    now = monotonic()
    key = (client_id, window_id)
    last_touched_at = _terminal_output_activity_touched_at.get(key)
    if (
        last_touched_at is not None
        and now - last_touched_at < TERMINAL_OUTPUT_ACTIVITY_TOUCH_INTERVAL_SECONDS
    ):
        return True

    window = await session.get(VirtualWindow, window_id)
    if window is None or window.client_id != client_id:
        return False

    await prefer_deferred_commit(session)
    window.terminal_last_output_at = datetime.now(UTC)
    await session.commit()
    _terminal_output_activity_touched_at[key] = now
    return True
