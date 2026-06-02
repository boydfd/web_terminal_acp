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
from app.agent_tools.types import AgentChatProjection
from app.config import get_settings
from app.models import Event, EventSourceType, SummaryJob, SummaryJobStatus, VirtualWindow
from app.repositories.folders import build_topic_tree_context

_ACTIVE_SUMMARY_JOB_STATUSES = (SummaryJobStatus.pending,)
MAX_SUMMARY_CONTEXT_EVENTS = 50
MAX_SUMMARY_CONTEXT_COMMANDS = 200
MAX_SUMMARY_CONTEXT_PAYLOAD_BYTES = 8192
MAX_SUMMARY_CONTEXT_TOTAL_BYTES = 32768
MAX_SUMMARY_JOB_ERROR_LENGTH = 2000
MAX_SUMMARY_JOB_ATTEMPTS = 3
SUMMARY_JOB_RETRY_DELAY_SECONDS = 30
_PROVIDER_ALIASES = {"claude": "claude_code"}
_ASK_USER_TOOL_NAME_MARKERS = (
    "request_user_input",
    "ask_user",
    "ask_question",
    "user_question",
    "clarifying_question",
)
_ASK_USER_QUESTION_KEYS = ("question", "prompt", "message")


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
    async def update_job(job: SummaryJob) -> None:
        job.status = SummaryJobStatus.pending
        job.trigger_reason = trigger_reason
        job.allow_title_folder_override = allow_title_folder_override
        job.input_generation = input_generation
        job.run_after = run_after
        await session.flush()

    existing_job = await session.scalar(_active_summary_job_query(virtual_window_id))
    if existing_job is not None:
        if update_existing:
            await update_job(existing_job)
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
            if update_existing:
                await update_job(existing_job)
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


async def mark_summary_job_succeeded(
    session: AsyncSession,
    job: SummaryJob,
    warning: BaseException | str | None = None,
) -> None:
    job.status = SummaryJobStatus.succeeded
    job.last_error = _bounded_error_message(warning) if warning is not None else None
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
    commands, session_messages = await collect_window_activity_context(session, window)

    return [_build_terminal_input_context(window, topic_tree, commands, session_messages)]


async def collect_window_activity_context(
    session: AsyncSession,
    window: VirtualWindow,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    command_events = list(
        await session.scalars(
            select(Event)
            .where(
                Event.client_id == window.client_id,
                Event.virtual_window_id == window.id,
                Event.kind == "terminal_input_command",
            )
            .order_by(desc(Event.created_at), desc(Event.id))
            .limit(MAX_SUMMARY_CONTEXT_COMMANDS)
        )
    )
    commands = [_command_from_event(event, window) for event in reversed(command_events)]
    ai_event_rows = await _recent_ai_events_for_summary(session, window)
    session_messages = [
        session_message
        for event in reversed(ai_event_rows)
        for session_message in _session_messages_from_event(event)
    ]
    return commands, session_messages


async def _recent_ai_events_for_summary(
    session: AsyncSession,
    window: VirtualWindow,
) -> list[Event]:
    rows: list[Event] = []
    for source_type in agent_activity_source_types():
        rows.extend(
            await session.scalars(
                select(Event)
                .options(selectinload(Event.ai_session))
                .where(
                    Event.client_id == window.client_id,
                    Event.virtual_window_id == window.id,
                    Event.source_type == source_type,
                )
                .order_by(desc(Event.created_at), desc(Event.id))
                .limit(MAX_SUMMARY_CONTEXT_EVENTS)
            )
        )
    return sorted(rows, key=lambda event: (event.created_at, event.id), reverse=True)[
        :MAX_SUMMARY_CONTEXT_EVENTS
    ]


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


def _session_messages_from_event(event: Event) -> list[dict[str, Any]]:
    session_messages: list[dict[str, Any]] = []
    chat = _ai_event_chat(event)
    if chat is not None and chat.agent_message_type != "subagent_call":
        role = "assistant" if chat.role == "agent" else chat.role
        if role in {"user", "assistant"}:
            session_messages.append({"role": role, "content": _bounded_text(chat.body)})

    ask_user_question = _ask_user_question_from_tool_call(event)
    if ask_user_question is not None:
        session_messages.append(ask_user_question)
    return session_messages


def _ask_user_question_from_tool_call(event: Event) -> dict[str, Any] | None:
    tool_name = _tool_call_name(event)
    if tool_name is None or not _is_ask_user_tool_name(tool_name):
        return None

    question_text = _ask_user_question_text(_tool_call_arguments(event))
    if question_text is None:
        return None
    return {
        "role": "tool_call",
        "name": tool_name,
        "content": _bounded_text(question_text),
    }


def _tool_call_name(event: Event) -> str | None:
    payload = event.payload_json
    item = _nested_payload_item(payload)
    content_block = _first_tool_use_content_block(payload)
    candidates = (
        item.get("name"),
        item.get("tool_name"),
        item.get("tool"),
        content_block.get("name") if content_block is not None else None,
        payload.get("name"),
        payload.get("tool_name"),
        payload.get("tool"),
    )
    for candidate in candidates:
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()

    span = payload.get("span")
    if isinstance(span, dict):
        attributes = span.get("attributes")
        if isinstance(attributes, dict):
            for key in ("tool", "tool_name", "name"):
                value = attributes.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
    return None


def _tool_call_arguments(event: Event) -> Any:
    payload = event.payload_json
    item = _nested_payload_item(payload)
    for key in ("arguments", "input", "args", "parameters"):
        if key in item:
            return item[key]
    content_block = _first_tool_use_content_block(payload)
    if content_block is not None:
        for key in ("arguments", "input", "args", "parameters"):
            if key in content_block:
                return content_block[key]
    for key in ("arguments", "input", "args", "parameters"):
        if key in payload:
            return payload[key]

    span = payload.get("span")
    if isinstance(span, dict):
        attributes = span.get("attributes")
        if isinstance(attributes, dict):
            for key in ("arguments", "input", "args", "parameters"):
                if key in attributes:
                    return attributes[key]
    return {}


def _nested_payload_item(payload: dict[str, Any]) -> dict[str, Any]:
    nested = payload.get("payload")
    return nested if isinstance(nested, dict) else payload


def _first_tool_use_content_block(payload: dict[str, Any]) -> dict[str, Any] | None:
    message = payload.get("message")
    if isinstance(message, dict) and "content" in message:
        content = message.get("content")
    else:
        content = payload.get("content")

    if isinstance(content, dict):
        block_type = content.get("type")
        return content if block_type == "tool_use" else None
    if not isinstance(content, list):
        return None
    for block in content:
        if isinstance(block, dict) and block.get("type") == "tool_use":
            return block
    return None


def _is_ask_user_tool_name(name: str) -> bool:
    normalized = "".join(character.lower() if character.isalnum() else "_" for character in name)
    return any(marker in normalized for marker in _ASK_USER_TOOL_NAME_MARKERS)


def _ask_user_question_text(arguments: Any) -> str | None:
    if isinstance(arguments, str):
        try:
            parsed_arguments = json.loads(arguments)
        except json.JSONDecodeError:
            stripped = arguments.strip()
            return stripped or None
        return _ask_user_question_text(parsed_arguments)

    if isinstance(arguments, dict):
        for key in _ASK_USER_QUESTION_KEYS:
            value = arguments.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        questions = arguments.get("questions")
        if isinstance(questions, list):
            parts = [_ask_user_question_text(question) for question in questions]
            joined = "\n\n".join(part for part in parts if part)
            return joined or None
        return None

    if isinstance(arguments, list):
        parts = [_ask_user_question_text(item) for item in arguments]
        joined = "\n\n".join(part for part in parts if part)
        return joined or None

    return None


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


def _ai_event_chat(event: Event) -> AgentChatProjection | None:
    try:
        adapter = get_agent_tool_registry().by_source_type(
            event.source_type,
            provider=_adapter_provider_for_event(event),
        )
        return adapter.project_chat(event)
    except Exception:
        return None


def _bounded_text(text: str) -> str:
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
    session_messages: list[dict[str, Any]],
) -> dict[str, Any]:
    budget_bytes = get_settings().terminal_summary_input_context_max_bytes
    included_commands = list(commands)
    included_session_messages = list(session_messages)
    total_commands = len(commands)
    total_session_messages = len(session_messages)
    commands_truncated = False
    session_messages_truncated = False

    while True:
        item = _terminal_input_context_item(
            window,
            topic_tree,
            included_commands,
            included_session_messages,
            total_commands=total_commands,
            total_session_messages=total_session_messages,
            commands_truncated=commands_truncated,
            session_messages_truncated=session_messages_truncated,
            budget_bytes=budget_bytes,
        )
        if _serialized_size(item) <= budget_bytes:
            return item
        trimmed_session_messages = _without_oldest_non_user_session_message(included_session_messages)
        if len(trimmed_session_messages) < len(included_session_messages):
            included_session_messages = trimmed_session_messages
            session_messages_truncated = True
            continue
        if included_commands:
            included_commands = included_commands[1:]
            commands_truncated = True
            continue
        if included_session_messages:
            included_session_messages = included_session_messages[1:]
            session_messages_truncated = True
            continue
        return _terminal_input_context_item_with_pruned_topic_tree(
            window,
            topic_tree,
            included_commands,
            included_session_messages,
            total_commands=total_commands,
            total_session_messages=total_session_messages,
            commands_truncated=commands_truncated,
            session_messages_truncated=session_messages_truncated,
            budget_bytes=budget_bytes,
        )


def _terminal_input_context_item_with_pruned_topic_tree(
    window: VirtualWindow,
    topic_tree: list[dict[str, object]],
    commands: list[dict[str, Any]],
    session_messages: list[dict[str, Any]],
    *,
    total_commands: int,
    total_session_messages: int,
    commands_truncated: bool,
    session_messages_truncated: bool,
    budget_bytes: int,
) -> dict[str, Any]:
    pruned_topic_tree: list[dict[str, object]] = []

    def build_item() -> dict[str, Any]:
        return _terminal_input_context_item(
            window,
            pruned_topic_tree,
            commands,
            session_messages,
            total_commands=total_commands,
            total_session_messages=total_session_messages,
            commands_truncated=commands_truncated,
            session_messages_truncated=session_messages_truncated,
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


def _without_oldest_non_user_session_message(
    session_messages: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    for index, message in enumerate(session_messages):
        if message.get("role") != "user":
            return session_messages[:index] + session_messages[index + 1 :]
    return session_messages


def _terminal_input_context_item(
    window: VirtualWindow,
    topic_tree: list[dict[str, object]],
    commands: list[dict[str, Any]],
    session_messages: list[dict[str, Any]],
    *,
    total_commands: int,
    total_session_messages: int,
    commands_truncated: bool,
    session_messages_truncated: bool,
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
            "session_messages": session_messages,
            "truncation": {
                "total_commands": total_commands,
                "included_commands": len(commands),
                "truncated": commands_truncated or len(commands) < total_commands,
                "budget_bytes": budget_bytes,
            },
            "session_message_truncation": {
                "total_messages": total_session_messages,
                "included_messages": len(session_messages),
                "truncated": session_messages_truncated
                or len(session_messages) < total_session_messages,
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
