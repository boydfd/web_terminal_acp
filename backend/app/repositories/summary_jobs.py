from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import Select, asc, desc, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased, selectinload

from app.agent_tools import agent_activity_source_types, get_agent_tool_registry
from app.config import get_settings
from app.models import Event, EventSourceType, SummaryJob, SummaryJobStatus, VirtualWindow
from app.repositories.folders import build_topic_tree_context

_ACTIVE_SUMMARY_JOB_STATUSES = (SummaryJobStatus.pending,)
MAX_SUMMARY_CONTEXT_EVENTS = 50
MAX_SUMMARY_CONTEXT_PAYLOAD_BYTES = 8192
MAX_SUMMARY_CONTEXT_TOTAL_BYTES = 32768
MAX_SUMMARY_JOB_ERROR_LENGTH = 2000
MAX_SUMMARY_JOB_ATTEMPTS = 3
SUMMARY_JOB_RETRY_DELAY_SECONDS = 30
_PROVIDER_ALIASES = {"claude": "claude_code"}


def _canonical_provider(provider: str) -> str:
    return _PROVIDER_ALIASES.get(provider, provider)


def _active_summary_job_query(virtual_window_id: UUID) -> Select[tuple[SummaryJob]]:
    return (
        select(SummaryJob)
        .where(
            SummaryJob.virtual_window_id == virtual_window_id,
            SummaryJob.status.in_(_ACTIVE_SUMMARY_JOB_STATUSES),
        )
        .order_by(SummaryJob.created_at, SummaryJob.id)
    )


async def enqueue_summary_job(
    session: AsyncSession,
    virtual_window_id: UUID,
    *,
    trigger_reason: str | None = None,
    allow_title_folder_override: bool = False,
    input_generation: int = 0,
    run_after: datetime | None = None,
    update_existing: bool = False,
) -> SummaryJob:
    existing_job = await session.scalar(_active_summary_job_query(virtual_window_id))
    if existing_job is not None:
        if update_existing:
            existing_job.status = SummaryJobStatus.pending
            existing_job.trigger_reason = trigger_reason
            existing_job.allow_title_folder_override = allow_title_folder_override
            existing_job.input_generation = input_generation
            existing_job.run_after = run_after
            await session.flush()
        return existing_job

    job = SummaryJob(
        virtual_window_id=virtual_window_id,
        status=SummaryJobStatus.pending,
        trigger_reason=trigger_reason,
        allow_title_folder_override=allow_title_folder_override,
        input_generation=input_generation,
        run_after=run_after,
    )
    try:
        async with session.begin_nested():
            session.add(job)
            await session.flush()
    except IntegrityError as exc:
        existing_job = await session.scalar(_active_summary_job_query(virtual_window_id))
        if existing_job is not None:
            return existing_job
        await session.rollback()
        raise exc
    return job


async def get_latest_summary_job(session: AsyncSession, virtual_window_id: UUID) -> SummaryJob | None:
    return await session.scalar(
        select(SummaryJob)
        .where(SummaryJob.virtual_window_id == virtual_window_id)
        .order_by(desc(SummaryJob.updated_at), desc(SummaryJob.created_at), desc(SummaryJob.id))
        .limit(1)
    )


async def enqueue_manual_summary_retry(
    session: AsyncSession,
    virtual_window_id: UUID,
    *,
    allow_title_folder_override: bool = False,
) -> SummaryJob:
    return await enqueue_summary_job(
        session,
        virtual_window_id,
        trigger_reason="manual_retry",
        allow_title_folder_override=allow_title_folder_override,
        run_after=datetime.now(timezone.utc),
        update_existing=True,
    )


async def claim_next_summary_job(session: AsyncSession) -> SummaryJob | None:
    now = datetime.now(timezone.utc)
    running_job = aliased(SummaryJob)
    running_same_window = (
        select(running_job.id)
        .where(
            running_job.virtual_window_id == SummaryJob.virtual_window_id,
            running_job.status == SummaryJobStatus.running,
        )
        .exists()
    )
    statement = (
        select(SummaryJob)
        .where(
            SummaryJob.status == SummaryJobStatus.pending,
            or_(SummaryJob.run_after.is_(None), SummaryJob.run_after <= now),
            ~running_same_window,
        )
        .order_by(
            asc(SummaryJob.run_after).nulls_first(),
            SummaryJob.created_at,
            SummaryJob.id,
        )
        .limit(1)
    )

    bind = session.get_bind()
    if bind.dialect.name == "postgresql":
        statement = statement.with_for_update(skip_locked=True)

    job = await session.scalar(statement)
    if job is None:
        return None

    job.status = SummaryJobStatus.running
    job.attempts += 1
    job.last_error = None
    await session.flush()
    return job


async def mark_summary_job_succeeded(session: AsyncSession, job: SummaryJob) -> None:
    job.status = SummaryJobStatus.succeeded
    job.last_error = None
    job.run_after = None
    await session.flush()


async def mark_summary_job_failed(session: AsyncSession, job: SummaryJob, error: BaseException | str) -> None:
    job.status = SummaryJobStatus.failed
    job.last_error = _bounded_error_message(error)
    job.run_after = datetime.now(timezone.utc)
    await session.flush()


async def mark_summary_job_retryable(session: AsyncSession, job: SummaryJob, error: BaseException | str) -> None:
    if job.attempts >= MAX_SUMMARY_JOB_ATTEMPTS:
        await mark_summary_job_failed(session, job, error)
        return

    job.status = SummaryJobStatus.pending
    job.last_error = _bounded_error_message(error)
    job.run_after = datetime.now(timezone.utc) + timedelta(seconds=SUMMARY_JOB_RETRY_DELAY_SECONDS)
    await session.flush()


async def collect_summary_context(session: AsyncSession, window: VirtualWindow) -> list[dict[str, Any]]:
    topic_tree = await build_topic_tree_context(session, window.client_id)
    commands, ai_events = await collect_window_activity_context(session, window)

    return [_build_terminal_input_context(window, topic_tree, commands, ai_events)]


async def collect_window_activity_context(
    session: AsyncSession,
    window: VirtualWindow,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    command_events = list(
        await session.scalars(
            select(Event)
            .where(
                Event.virtual_window_id == window.id,
                Event.kind == "terminal_input_command",
            )
            .order_by(Event.created_at, Event.id)
        )
    )
    commands = [_command_from_event(event, window) for event in command_events]
    ai_event_rows = list(
        await session.scalars(
            select(Event)
            .options(selectinload(Event.ai_session))
            .where(
                Event.virtual_window_id == window.id,
                Event.source_type.in_(agent_activity_source_types()),
            )
            .order_by(desc(Event.created_at), desc(Event.id))
            .limit(MAX_SUMMARY_CONTEXT_EVENTS)
        )
    )
    ai_events = [_ai_event_from_event(event) for event in reversed(ai_event_rows)]
    return commands, ai_events


def _command_from_event(event: Event, window: VirtualWindow) -> dict[str, Any]:
    payload = event.payload_json
    captured_at = payload.get("captured_at")
    if captured_at is None and event.created_at is not None:
        captured_at = event.created_at.isoformat()

    return {
        "sequence": payload.get("sequence"),
        "command": payload.get("command", ""),
        "shell": payload.get("shell") or window.shell_command,
        "cwd": payload.get("cwd") or window.cwd,
        "captured_at": captured_at,
    }


def _ai_event_from_event(event: Event) -> dict[str, Any]:
    return {
        "source_type": event.source_type.value,
        "provider": _ai_event_provider(event),
        "source_id": event.source_id,
        "kind": event.kind,
        "role": _ai_event_role(event),
        "text": _bounded_event_text(event),
        "created_at": event.created_at.isoformat() if event.created_at is not None else None,
    }


def _ai_event_provider(event: Event) -> str:
    if event.ai_session is not None:
        return event.ai_session.provider

    legacy_provider = _legacy_provider_for_source_type(event.source_type)
    if legacy_provider is not None:
        return legacy_provider

    payload_provider = event.payload_json.get("provider")
    if isinstance(payload_provider, str) and payload_provider.strip():
        return _canonical_provider(payload_provider.strip())

    return event.source_type.value


def _legacy_provider_for_source_type(source_type: EventSourceType) -> str | None:
    for adapter in get_agent_tool_registry().all():
        if source_type in adapter.legacy_source_types:
            return adapter.provider_id
    return None


def _ai_event_role(event: Event) -> str:
    kind = event.kind.lower()
    payload_role = event.payload_json.get("role")
    payload_type = event.payload_json.get("type")
    if kind in {"user", "user_message"} or payload_role == "user" or payload_type == "user":
        return "user"
    if (
        kind in {"assistant", "assistant_message"}
        or payload_role == "assistant"
        or payload_type == "assistant"
    ):
        return "assistant"
    if "tool" in kind:
        return "tool"
    return "event"


def _bounded_event_text(event: Event) -> str:
    try:
        adapter = get_agent_tool_registry().by_source_type(
            event.source_type,
            provider=_adapter_provider_for_event(event),
        )
        text = adapter.summary_text(event)
    except Exception:
        text = json.dumps(event.payload_json, sort_keys=True, ensure_ascii=False)
    if len(text.encode("utf-8")) <= MAX_SUMMARY_CONTEXT_PAYLOAD_BYTES:
        return text
    encoded = text.encode("utf-8")[:MAX_SUMMARY_CONTEXT_PAYLOAD_BYTES]
    return encoded.decode("utf-8", errors="ignore") + "\n[TRUNCATED]"


def _adapter_provider_for_event(event: Event) -> str | None:
    if event.source_type is not EventSourceType.agent_tool_record:
        return None
    provider = _ai_event_provider(event)
    try:
        get_agent_tool_registry().by_provider(provider)
    except ValueError:
        return None
    return provider


def _build_terminal_input_context(
    window: VirtualWindow,
    topic_tree: list[dict[str, object]],
    commands: list[dict[str, Any]],
    ai_events: list[dict[str, Any]],
) -> dict[str, Any]:
    budget_bytes = get_settings().terminal_summary_input_context_max_bytes
    included_commands = list(commands)
    included_ai_events = list(ai_events)
    total_commands = len(commands)
    total_ai_events = len(ai_events)
    commands_truncated = False
    ai_events_truncated = False

    while True:
        item = _terminal_input_context_item(
            window,
            topic_tree,
            included_commands,
            included_ai_events,
            total_commands=total_commands,
            total_ai_events=total_ai_events,
            commands_truncated=commands_truncated,
            ai_events_truncated=ai_events_truncated,
            budget_bytes=budget_bytes,
        )
        if _serialized_size(item) <= budget_bytes:
            return item
        trimmed_ai_events = _without_oldest_non_user_ai_event(included_ai_events)
        if len(trimmed_ai_events) < len(included_ai_events):
            included_ai_events = trimmed_ai_events
            ai_events_truncated = True
            continue
        if included_commands:
            included_commands = included_commands[1:]
            commands_truncated = True
            continue
        if included_ai_events:
            included_ai_events = included_ai_events[1:]
            ai_events_truncated = True
            continue
        return _terminal_input_context_item_with_pruned_topic_tree(
            window,
            topic_tree,
            included_commands,
            included_ai_events,
            total_commands=total_commands,
            total_ai_events=total_ai_events,
            commands_truncated=commands_truncated,
            ai_events_truncated=ai_events_truncated,
            budget_bytes=budget_bytes,
        )


def _terminal_input_context_item_with_pruned_topic_tree(
    window: VirtualWindow,
    topic_tree: list[dict[str, object]],
    commands: list[dict[str, Any]],
    ai_events: list[dict[str, Any]],
    *,
    total_commands: int,
    total_ai_events: int,
    commands_truncated: bool,
    ai_events_truncated: bool,
    budget_bytes: int,
) -> dict[str, Any]:
    pruned_topic_tree: list[dict[str, object]] = []

    def build_item() -> dict[str, Any]:
        return _terminal_input_context_item(
            window,
            pruned_topic_tree,
            commands,
            ai_events,
            total_commands=total_commands,
            total_ai_events=total_ai_events,
            commands_truncated=commands_truncated,
            ai_events_truncated=ai_events_truncated,
            budget_bytes=budget_bytes,
            topic_tree_truncated=True,
        )

    def fits_budget() -> bool:
        return _serialized_size(build_item()) <= budget_bytes

    for root in topic_tree:
        pruned_root = _topic_tree_node_without_children(root)
        pruned_topic_tree.append(pruned_root)
        if not fits_budget():
            pruned_topic_tree.pop()
            break
        _include_topic_tree_children_until_budget(
            pruned_root,
            _topic_tree_node_children(root),
            fits_budget,
        )

    return build_item()


def _include_topic_tree_children_until_budget(
    pruned_parent: dict[str, object],
    original_children: list[dict[str, object]],
    fits_budget: Callable[[], bool],
) -> None:
    pruned_children = pruned_parent["children"]
    if not isinstance(pruned_children, list):
        return

    for child in original_children:
        pruned_child = _topic_tree_node_without_children(child)
        pruned_children.append(pruned_child)
        if not fits_budget():
            pruned_children.pop()
            break
        _include_topic_tree_children_until_budget(
            pruned_child,
            _topic_tree_node_children(child),
            fits_budget,
        )


def _topic_tree_node_without_children(node: dict[str, object]) -> dict[str, object]:
    return {
        "path": node["path"],
        "name": node["name"],
        "is_leaf": node["is_leaf"],
        "terminal_count": node["terminal_count"],
        "children": [],
    }


def _topic_tree_node_children(node: dict[str, object]) -> list[dict[str, object]]:
    children = node.get("children")
    if not isinstance(children, list):
        return []
    return [child for child in children if isinstance(child, dict)]


def _without_oldest_non_user_ai_event(ai_events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    for index, event in enumerate(ai_events):
        if event.get("role") != "user":
            return ai_events[:index] + ai_events[index + 1 :]
    return ai_events


def _terminal_input_context_item(
    window: VirtualWindow,
    topic_tree: list[dict[str, object]],
    commands: list[dict[str, Any]],
    ai_events: list[dict[str, Any]],
    *,
    total_commands: int,
    total_ai_events: int,
    commands_truncated: bool,
    ai_events_truncated: bool,
    budget_bytes: int,
    topic_tree_truncated: bool = False,
    today: Any | None = None,
) -> dict[str, Any]:
    current_date = today or datetime.now(timezone.utc).date()
    year_month_day = current_date.strftime("%Y-%m-%d")
    year_month = current_date.strftime("%Y-%m")
    return {
        "source_type": "terminal",
        "kind": "terminal_input_context",
        "payload": {
            "window": {
                "id": str(window.id),
                "title": window.title,
                "status": window.status.value,
                "cwd": window.cwd,
                "shell_command": window.shell_command,
                "summary": window.summary,
                "title_tags": window.title_tags,
            },
            "date": {
                "current_date": year_month_day,
                "year_month": year_month,
                "year_month_day": year_month_day,
            },
            "topic_tree": topic_tree,
            "topic_tree_truncation": {
                "truncated": topic_tree_truncated,
                "budget_bytes": budget_bytes,
            },
            "summary_output_language": get_settings().summary_output_language,
            "commands": commands,
            "ai_events": ai_events,
            "truncation": {
                "total_commands": total_commands,
                "included_commands": len(commands),
                "truncated": commands_truncated or len(commands) < total_commands,
                "budget_bytes": budget_bytes,
            },
            "ai_event_truncation": {
                "total_events": total_ai_events,
                "included_events": len(ai_events),
                "truncated": ai_events_truncated or len(ai_events) < total_ai_events,
                "budget_bytes": budget_bytes,
            },
        },
    }


def _bounded_error_message(error: BaseException | str) -> str:
    message = str(error)
    if len(message) <= MAX_SUMMARY_JOB_ERROR_LENGTH:
        return message
    return message[:MAX_SUMMARY_JOB_ERROR_LENGTH]


def _cap_context_total(context_items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    selected_items: list[dict[str, Any]] = []
    selected_size = 2  # Serialized [] brackets.

    for item in reversed(context_items):
        item_size = _serialized_size(item)
        separator_size = 1 if selected_items else 0
        if selected_size + separator_size + item_size > MAX_SUMMARY_CONTEXT_TOTAL_BYTES:
            break
        selected_items.append(item)
        selected_size += separator_size + item_size

    return list(reversed(selected_items))


def _serialized_size(item: dict[str, Any]) -> int:
    return len(json.dumps(item, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def _cap_payload(payload: dict[str, Any]) -> dict[str, Any]:
    payload_bytes = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    payload_size = len(payload_bytes)
    if payload_size <= MAX_SUMMARY_CONTEXT_PAYLOAD_BYTES:
        return payload
    return {"_truncated": True, "size_bytes": payload_size}
