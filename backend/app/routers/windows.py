from __future__ import annotations

import asyncio
import contextlib
import logging
import posixpath
import re
import shlex
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Annotated, Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from sqlalchemy import Text, and_, case, cast, desc, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload

from app.agent_plugins import get_agent_plugin_registry, list_agent_client_descriptors
from app.agent_tools import agent_activity_source_types, get_agent_tool_registry
from app.agent_tools.common import fallback_projection
from app.agent_tools.types import AgentChatProjection, AgentEventProjection, AgentToolAdapter
from app.config import get_settings
from app.db import SessionLocal, get_session
from app.models import (
    AiSession,
    Client,
    ClientRuntime,
    Event,
    EventSourceType,
    SummaryJob,
    TerminalRecentUsage,
    VirtualWindow,
    WindowStatus,
)
from app.repositories.clients import ensure_local_client, get_client
from app.repositories.summary_jobs import (
    enqueue_manual_summary_retry,
    get_latest_summary_job,
)
from app.repositories.git_worktree import get_window_git_binding, list_git_worktree_runs
from app.repositories.folders import get_or_create_folder_by_path
from app.repositories.windows import (
    FolderNotFoundError,
    create_window,
    delete_window,
    get_window_for_client,
    list_window_title_history,
    patch_window,
)
from app.routers.ui_events import ui_event_hub_from_state
from app.services import agent_config as agent_config_service
from app.services import agent_profiles as agent_profile_service
from app.services.aux_terminal import aux_terminal_registry_from_state, kill_remote_aux_terminal
from app.schemas import (
    AgentConfigOut,
    AgentConfigSelectionIn,
    AgentConfigToggleIn,
    AgentClientListOut,
    AgentChatMessageOut,
    AgentChatRecordOut,
    AgentEventOut,
    AgentEventProjectionOut,
    AgentRecordOut,
    AgentSessionOut,
    CommandHistoryItemOut,
    CommandHistoryOut,
    GitWorktreeRunListOut,
    GitWorktreeRunOut,
    SummaryJobOut,
    SummaryJobRetryIn,
    WindowCreateIn,
    WindowOut,
    WindowPatchIn,
    WindowTitleHistoryItemOut,
    WindowTitleHistoryOut,
)
from app.services.runtime.client_connections import ClientConnectionRegistry
from app.services.git_worktree_agent_markers import materialize_agent_worktree_markers
from app.services.git_worktree_coordinator import process_git_worktree_snapshot_refresh
from app.services.polling_response_cache import (
    CachedJsonResponse,
    begin_response_cache_refresh,
    cached_or_stale_json_response,
    invalidate_polling_response_cache,
    finish_response_cache_refresh,
    response_cache_scope,
    store_json_response,
)
from app.services.runtime.remote import RemoteClientUnavailable, RemoteRuntime, RemoteTerminalError
from app.services.event_kinds import AGENT_WORK_PRESENCE_KIND
from app.services.terminal_work_status import (
    TerminalWorkStatus,
    load_work_status,
    long_idle_work_status,
    to_work_status_out,
)
from app.services.tmux_manager import TmuxManager, TmuxTarget, get_tmux_manager
from app.services.window_runtime_tags import agent_from_command, runtime_tags_for_window

router = APIRouter(prefix="/api", tags=["windows"])
logger = logging.getLogger(__name__)
REMOTE_CREATE_WINDOW_TIMEOUT_SECONDS = 60.0

AgentRecordLimit = Annotated[int, Query(ge=1, le=200)]
AgentRecordOffset = Annotated[int, Query(ge=0)]
AgentChatRole = Literal["all", "user", "agent", "subagent_call", "subagent_result"]
AgentClientCapability = Literal["launch", "client_config", "window_config", "profile_config"]
CommandHistoryLimit = Annotated[int, Query(ge=1, le=200)]
CommandHistoryOffset = Annotated[int, Query(ge=0)]
TitleHistoryLimit = Annotated[int, Query(ge=1, le=200)]
TitleHistoryOffset = Annotated[int, Query(ge=0)]
_PROVIDER_ALIASES = {"claude": "claude_code", "cursor": "cursor_cli", "agent": "cursor_cli"}
_COMMAND_SEGMENT_PATTERN = re.compile(r"&&|\|\||[;|]")
_ENV_ASSIGNMENT_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=.*$")
_COMMAND_WRAPPERS = {"command", "env", "sudo"}
_REMOTE_CAPABILITY_DEFAULTS: dict[AgentClientCapability, bool] = {
    "launch": True,
    "client_config": True,
    "window_config": True,
    "profile_config": True,
}
AGENT_RECORD_CHAT_EVENT_BATCH_SIZE = 500
AGENT_RECORD_DETAIL_RELATED_EVENT_LIMIT = 500


@dataclass(frozen=True)
class _RuntimeClient:
    id: UUID
    runtime: ClientRuntime


@dataclass(frozen=True)
class _WindowOverviewTimestamps:
    last_terminal_command_at: datetime | None = None
    last_agent_event_at: datetime | None = None
    last_recent_usage_at: datetime | None = None
    last_agent_presence_at: datetime | None = None


@router.get("/clients/{client_id}/agent-clients", response_model=AgentClientListOut)
async def read_agent_clients(
    client_id: UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> AgentClientListOut:
    client = await _require_client(session, client_id)
    if client.runtime is not ClientRuntime.local:
        runtime = RemoteRuntime(
            client_id=client_id,
            registry=request.app.state.client_connections,
        )
        try:
            return AgentClientListOut.model_validate(await runtime.list_agent_clients())
        except RemoteClientUnavailable as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={"message": "remote client unavailable", "reason": exc.reason},
            ) from exc
        except RemoteTerminalError as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=str(exc),
            ) from exc

    return AgentClientListOut(
        agent_clients=[
            {
                "id": descriptor.id,
                "provider_id": descriptor.provider_id,
                "label": descriptor.label,
                "aliases": list(descriptor.aliases),
                "default_command": descriptor.default_command,
                "command_names": list(descriptor.command_names),
                "capabilities": asdict(descriptor.capabilities),
            }
            for descriptor in list_agent_client_descriptors()
        ]
    )


def _runtime_client_from_model(client: Client) -> _RuntimeClient:
    return _RuntimeClient(id=client.id, runtime=client.runtime)


async def _require_client(session: AsyncSession, client_id: UUID) -> Client:
    client = await get_client(session, client_id)
    if client is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="client not found")
    return client


def _command_capture_supported(window: VirtualWindow) -> bool:
    shell = window.shell_command or get_settings().default_shell
    if agent_from_command(shell) is not None:
        shell = get_settings().default_shell
    return posixpath.basename(shell) in {"bash", "zsh"}


def to_summary_job_out(job: SummaryJob | None) -> SummaryJobOut | None:
    if job is None:
        return None
    return SummaryJobOut(
        id=job.id,
        status=job.status.value,
        trigger_reason=job.trigger_reason,
        attempts=job.attempts,
        last_error=job.last_error,
        run_after=job.run_after,
        updated_at=job.updated_at,
        allow_title_folder_override=job.allow_title_folder_override,
    )


def to_window_out(
    window: VirtualWindow,
    summary_job: SummaryJob | None = None,
    runtime_tags: list[str] | None = None,
    work_status: TerminalWorkStatus | None = None,
    overview_timestamps: _WindowOverviewTimestamps | None = None,
) -> WindowOut:
    timestamps = overview_timestamps or _WindowOverviewTimestamps()
    effective_runtime_tags = runtime_tags
    if effective_runtime_tags is None:
        effective_runtime_tags = runtime_tags_for_window(
            window,
            terminal_agent=agent_from_command(window.shell_command),
        )
    effective_work_status = work_status or long_idle_work_status()
    return WindowOut(
        id=window.id,
        client_id=window.client_id,
        title=window.title,
        folder_id=window.folder_id,
        status=window.status.value,
        tmux_session=window.tmux_session,
        tmux_window_id=window.tmux_window_id,
        remote_session_id=window.remote_session_id,
        remote_window_id=window.remote_window_id,
        cwd=window.cwd,
        shell_command=window.shell_command,
        title_manually_overridden=window.title_manually_overridden,
        folder_manually_overridden=window.folder_manually_overridden,
        command_capture_supported=_command_capture_supported(window),
        summary=window.summary,
        title_tags=window.title_tags,
        runtime_tags=effective_runtime_tags,
        work_status=to_work_status_out(effective_work_status),
        summary_job=to_summary_job_out(summary_job),
        created_at=window.created_at,
        last_terminal_command_at=timestamps.last_terminal_command_at,
        last_agent_event_at=timestamps.last_agent_event_at,
        last_active_at=_latest_window_active_at(
            window,
            effective_work_status,
            timestamps,
        ),
    )


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _max_datetime(*values: datetime | None) -> datetime | None:
    candidates = [_aware_utc(value) for value in values if value is not None]
    if not candidates:
        return None
    return max(candidates)


def _latest_window_active_at(
    window: VirtualWindow,
    work_status: TerminalWorkStatus,
    timestamps: _WindowOverviewTimestamps,
) -> datetime:
    return _max_datetime(
        window.created_at,
        timestamps.last_recent_usage_at,
        timestamps.last_terminal_command_at,
        timestamps.last_agent_event_at,
        window.terminal_last_output_at,
        timestamps.last_agent_presence_at,
        work_status.last_activity_at,
        work_status.last_working_activity_at,
    ) or _aware_utc(window.created_at)


async def _load_window_overview_timestamps(
    session: AsyncSession,
    client_id: UUID,
    window_id: UUID,
) -> _WindowOverviewTimestamps:
    latest_command_at = await session.scalar(
        select(Event.created_at)
        .where(
            Event.client_id == client_id,
            Event.virtual_window_id == window_id,
            Event.kind == "terminal_input_command",
        )
        .order_by(desc(Event.created_at), desc(Event.id))
        .limit(1)
    )
    latest_agent_event_at = await session.scalar(
        select(Event.created_at)
        .where(
            Event.client_id == client_id,
            Event.virtual_window_id == window_id,
            Event.source_type.in_(agent_activity_source_types()),
        )
        .order_by(desc(Event.created_at), desc(Event.id))
        .limit(1)
    )
    latest_recent_usage_at = await session.scalar(
        select(TerminalRecentUsage.last_used_at)
        .where(
            TerminalRecentUsage.client_id == client_id,
            TerminalRecentUsage.window_id == window_id,
        )
        .order_by(desc(TerminalRecentUsage.last_used_at), desc(TerminalRecentUsage.id))
        .limit(1)
    )
    latest_agent_presence_at = await session.scalar(
        select(Event.created_at)
        .where(
            Event.client_id == client_id,
            Event.virtual_window_id == window_id,
            Event.kind == AGENT_WORK_PRESENCE_KIND,
        )
        .order_by(desc(Event.created_at), desc(Event.id))
        .limit(1)
    )
    return _WindowOverviewTimestamps(
        last_terminal_command_at=latest_command_at,
        last_agent_event_at=latest_agent_event_at,
        last_recent_usage_at=latest_recent_usage_at,
        last_agent_presence_at=latest_agent_presence_at,
    )


def to_agent_session_out(ai_session: AiSession) -> AgentSessionOut:
    return AgentSessionOut(
        id=ai_session.id,
        provider=ai_session.provider,
        source_id=ai_session.source_id,
        source_path=ai_session.source_path,
        project_path=ai_session.project_path,
        virtual_window_id=ai_session.virtual_window_id,
        title=ai_session.title,
        tags=ai_session.tags,
        summary=ai_session.summary,
        created_at=ai_session.created_at,
        updated_at=ai_session.updated_at,
    )


def _canonical_provider(provider: str) -> str:
    return get_agent_plugin_registry().canonical_provider(_PROVIDER_ALIASES.get(provider, provider))


def _payload_provider(event: Event) -> str | None:
    provider = event.payload_json.get("provider")
    if isinstance(provider, str) and provider.strip():
        return _canonical_provider(provider.strip())
    return None


def _adapter_for_event(event: Event) -> AgentToolAdapter | None:
    registry = get_agent_tool_registry()

    if event.ai_session is not None and event.ai_session.provider:
        with contextlib.suppress(ValueError):
            return registry.by_provider(_canonical_provider(event.ai_session.provider))

    if event.source_type is EventSourceType.agent_tool_record:
        provider = _payload_provider(event)
        if provider is not None:
            with contextlib.suppress(ValueError):
                return registry.by_provider(provider)
        return None

    with contextlib.suppress(KeyError, ValueError):
        return registry.by_source_type(event.source_type)
    return None


def _projection_out(
    projection: AgentEventProjection,
    sessions_by_source_id: dict[str, AiSession] | None = None,
    subagent_targets_by_tool_use_id: dict[str, str] | None = None,
) -> AgentEventProjectionOut:
    sessions_by_source_id = sessions_by_source_id or {}
    target_session_source_id = _projection_target_source_id(
        projection,
        subagent_targets_by_tool_use_id or {},
    )
    return AgentEventProjectionOut(
        tone=projection.tone,
        label=projection.label,
        body=projection.body,
        body_format=projection.body_format,
        subtype=projection.subtype,
        agent_message_type=_agent_message_type(projection.agent_message_type),
        subagent_id=projection.subagent_id,
        subagent_tool_use_id=projection.subagent_tool_use_id,
        target_session_id=_target_session_id_for_source(target_session_source_id, sessions_by_source_id),
        target_session_source_id=target_session_source_id,
    )


def _project_event(
    event: Event,
    sessions_by_source_id: dict[str, AiSession] | None = None,
    subagent_targets_by_tool_use_id: dict[str, str] | None = None,
) -> AgentEventProjectionOut:
    adapter = _adapter_for_event(event)
    projection: AgentEventProjection | None = None
    if adapter is not None:
        with contextlib.suppress(Exception):
            projection = adapter.project_event(event)
    return _projection_out(
        projection or fallback_projection(event),
        sessions_by_source_id,
        subagent_targets_by_tool_use_id,
    )


def to_agent_event_out(
    event: Event,
    sessions_by_source_id: dict[str, AiSession] | None = None,
    subagent_targets_by_tool_use_id: dict[str, str] | None = None,
) -> AgentEventOut:
    return AgentEventOut(
        id=event.id,
        ai_session_id=event.ai_session_id,
        source_type=event.source_type.value,
        source_id=event.source_id,
        kind=event.kind,
        payload_json=event.payload_json,
        projection=_project_event(event, sessions_by_source_id, subagent_targets_by_tool_use_id),
        created_at=event.created_at,
    )


def _string_value(value: object) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _project_chat(event: Event) -> AgentChatProjection | None:
    adapter = _adapter_for_event(event)
    if adapter is not None:
        with contextlib.suppress(Exception):
            return adapter.project_chat(event)
    return None


def _agent_message_type(value: str | None) -> Literal["agent", "subagent_call", "subagent_result"] | None:
    if value in {"agent", "subagent_call", "subagent_result"}:
        return value
    return None


def _target_session_id_for_source(
    source_id: str | None,
    sessions_by_source_id: dict[str, AiSession],
) -> UUID | None:
    if source_id is None:
        return None
    return sessions_by_source_id.get(source_id).id if source_id in sessions_by_source_id else None


def _projection_target_source_id(
    projection: AgentEventProjection | AgentChatProjection,
    subagent_targets_by_tool_use_id: dict[str, str],
) -> str | None:
    if projection.target_session_source_id is not None:
        return projection.target_session_source_id
    if projection.subagent_tool_use_id is None:
        return None
    return subagent_targets_by_tool_use_id.get(projection.subagent_tool_use_id)


def _chat_message_out(
    event: Event,
    projection: AgentChatProjection,
    sessions_by_source_id: dict[str, AiSession] | None = None,
    subagent_targets_by_tool_use_id: dict[str, str] | None = None,
) -> AgentChatMessageOut:
    sessions_by_source_id = sessions_by_source_id or {}
    target_session_source_id = _projection_target_source_id(
        projection,
        subagent_targets_by_tool_use_id or {},
    )
    return AgentChatMessageOut(
        id=event.id,
        ai_session_id=event.ai_session_id,
        source_type=event.source_type.value,
        source_id=event.source_id,
        role=projection.role,
        body=projection.body,
        body_format=projection.body_format,
        agent_message_type=_agent_message_type(projection.agent_message_type),
        subagent_id=projection.subagent_id,
        subagent_tool_use_id=projection.subagent_tool_use_id,
        target_session_id=_target_session_id_for_source(target_session_source_id, sessions_by_source_id),
        target_session_source_id=target_session_source_id,
        created_at=event.created_at,
    )


def _payload_datetime(event: Event, key: str) -> datetime | None:
    value = event.payload_json.get(key)
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _payload_command(event: Event) -> str:
    command = _string_value(event.payload_json.get("command"))
    return command if command is not None else ""


def _payload_sequence(event: Event) -> int | str | None:
    sequence = event.payload_json.get("sequence")
    return sequence if isinstance(sequence, (int, str)) else None


def _payload_exit_status(event: Event) -> int | str | None:
    exit_status = event.payload_json.get("exit_status")
    return exit_status if isinstance(exit_status, (int, str)) else None


def _payload_sequence_key(event: Event) -> str | None:
    sequence = _payload_sequence(event)
    return str(sequence) if sequence is not None else None


def _command_history_item_out(event: Event, finished_by_sequence: dict[str, Event]) -> CommandHistoryItemOut:
    sequence_key = _payload_sequence_key(event)
    finished = finished_by_sequence.get(sequence_key) if sequence_key is not None else None
    finished_at = _payload_datetime(finished, "captured_at") if finished is not None else None
    return CommandHistoryItemOut(
        id=event.id,
        command=_payload_command(event),
        shell=_string_value(event.payload_json.get("shell")),
        cwd=_string_value(event.payload_json.get("cwd")),
        sequence=_payload_sequence(event),
        exit_status=_payload_exit_status(finished) if finished is not None else None,
        captured_at=_payload_datetime(event, "captured_at") or event.created_at,
        finished_at=finished_at,
        created_at=event.created_at,
    )


def _dedupe_chat_messages(
    events: list[Event],
    role: AgentChatRole = "all",
    sessions_by_source_id: dict[str, AiSession] | None = None,
) -> list[AgentChatMessageOut]:
    sessions_by_source_id = sessions_by_source_id or _sessions_by_source_id(events)
    subagent_targets_by_tool_use_id = _subagent_targets_by_tool_use_id(events)
    return [
        _chat_message_out(
            event,
            projection,
            sessions_by_source_id,
            subagent_targets_by_tool_use_id,
        )
        for event, projection in _deduped_chat_projection_items(events)
        if _chat_role_matches(projection, role)
    ]


@dataclass(frozen=True)
class _ChatMessagePage:
    messages: list[AgentChatMessageOut]
    total: int
    total_exact: bool
    has_more: bool


async def _load_chat_message_page(
    session: AsyncSession,
    *,
    event_filters: list[object],
    target_event_filters: list[object],
    role: AgentChatRole,
    sessions_by_source_id: dict[str, AiSession],
    messages_limit: int,
    messages_offset: int,
) -> _ChatMessagePage:
    raw_total = (
        await session.scalar(select(func.count()).select_from(Event).where(*event_filters))
    ) or 0
    batch_size = max(messages_limit + messages_offset + 1, AGENT_RECORD_CHAT_EVENT_BATCH_SIZE)
    target_count = messages_offset + messages_limit + 1
    chat_items: list[tuple[Event, AgentChatProjection]] = []
    pending_duplicate_items: list[tuple[Event, AgentChatProjection]] = []
    target_events: list[Event] = []
    raw_offset = 0
    exhausted = True
    deduped_items: list[tuple[Event, AgentChatProjection]] = []

    while raw_offset < raw_total:
        raw_events = list(
            await session.scalars(
                select(Event)
                .options(selectinload(Event.ai_session))
                .where(*event_filters)
                .order_by(
                    Event.created_at,
                    case((Event.source_type == EventSourceType.terminal, 1), else_=0),
                    Event.id,
                )
                .offset(raw_offset)
                .limit(batch_size)
            )
        )
        if not raw_events:
            break
        target_events.extend(raw_events)
        raw_offset += len(raw_events)
        next_items = pending_duplicate_items + _deduped_chat_projection_items(raw_events)
        pending_duplicate_items = _trailing_duplicate_chat_projection_items(next_items)
        chat_items = [*chat_items, *next_items[: len(next_items) - len(pending_duplicate_items)]]
        deduped_items = _dedupe_chat_projection_items(chat_items)
        if _matching_chat_item_count(deduped_items, role) >= target_count:
            exhausted = False
            break

    if exhausted:
        chat_items.extend(pending_duplicate_items)
        deduped_items = _dedupe_chat_projection_items(chat_items)
    target_events.extend(
        await _load_antigravity_subagent_target_events(
            session,
            event_filters=target_event_filters,
        )
    )
    subagent_targets_by_tool_use_id = _subagent_targets_by_tool_use_id(target_events)
    messages = [
        _chat_message_out(
            event,
            projection,
            sessions_by_source_id,
            subagent_targets_by_tool_use_id,
        )
        for event, projection in deduped_items
        if _chat_role_matches(projection, role)
    ]
    paged_messages = messages[messages_offset : messages_offset + messages_limit]
    has_more = len(messages) > messages_offset + len(paged_messages)
    total = len(messages) if exhausted else messages_offset + len(paged_messages) + (1 if has_more else 0)
    return _ChatMessagePage(
        messages=paged_messages,
        total=total,
        total_exact=exhausted,
        has_more=has_more,
    )


async def _load_antigravity_subagent_target_events(
    session: AsyncSession,
    *,
    event_filters: list[object],
) -> list[Event]:
    return list(
        await session.scalars(
            select(Event)
            .where(
                *event_filters,
                Event.kind == "tool_result",
                Event.source_type == EventSourceType.agent_tool_record,
                cast(Event.payload_json, Text).contains("antigravity_cli"),
                cast(Event.payload_json, Text).contains("INVOKE_SUBAGENT"),
            )
            .order_by(Event.created_at, Event.id)
            .limit(AGENT_RECORD_DETAIL_RELATED_EVENT_LIMIT)
        )
    )


def _matching_chat_item_count(items: list[tuple[Event, AgentChatProjection]], role: AgentChatRole) -> int:
    return sum(1 for _event, projection in items if _chat_role_matches(projection, role))


def _sessions_by_source_id(events: list[Event]) -> dict[str, AiSession]:
    sessions: dict[str, AiSession] = {}
    for event in events:
        if event.ai_session is not None:
            sessions[event.ai_session.source_id] = event.ai_session
    return sessions


def _subagent_targets_by_tool_use_id(events: list[Event]) -> dict[str, str]:
    targets: dict[str, str] = {}
    for event in events:
        payload = event.payload_json
        raw_target = _raw_antigravity_subagent_target(payload)
        if raw_target is not None:
            targets.setdefault(raw_target[0], raw_target[1])
        tool_use_result = payload.get("toolUseResult")
        if isinstance(tool_use_result, dict):
            tool_use_id = _string_value(tool_use_result.get("toolUseId"))
            agent_id = _string_value(tool_use_result.get("agentId"))
            if tool_use_id is not None and agent_id is not None:
                targets.setdefault(tool_use_id, f"agent-{agent_id}")
        subagent = payload.get("subagent")
        if isinstance(subagent, dict):
            tool_use_id = _string_value(subagent.get("toolUseId")) or _string_value(subagent.get("tool_use_id"))
            agent_id = _string_value(payload.get("agentId")) or _string_value(subagent.get("agentId")) or _string_value(subagent.get("agent_id"))
            if tool_use_id is not None and agent_id is not None:
                targets.setdefault(tool_use_id, f"agent-{agent_id}")
        matches = payload.get("subagent_tool_use_results")
        if isinstance(matches, list):
            for item in matches:
                if not isinstance(item, dict):
                    continue
                tool_use_id = _string_value(item.get("tool_use_id")) or _string_value(item.get("toolUseId"))
                agent_id = _string_value(item.get("agent_id")) or _string_value(item.get("agentId"))
                if tool_use_id is not None and agent_id is not None:
                    targets.setdefault(tool_use_id, f"agent-{agent_id}")
    return targets


def _raw_antigravity_subagent_target(payload: dict) -> tuple[str, str] | None:
    provider = _string_value(payload.get("provider"))
    if provider != "antigravity_cli":
        return None
    if _string_value(payload.get("type")) != "INVOKE_SUBAGENT":
        return None
    step_index = payload.get("step_index")
    if not isinstance(step_index, int):
        return None
    content = _string_value(payload.get("content"))
    if content is None:
        return None
    match = re.search(r'"conversationId"\s*:\s*"([^"]+)"', content)
    if match is None:
        return None
    agent_id = match.group(1).strip()
    if not agent_id:
        return None
    return f"step-{step_index - 1}", f"agent-{agent_id}"


def _chat_role_matches(projection: AgentChatProjection, role: AgentChatRole) -> bool:
    if role == "all":
        return True
    if role in {"subagent_call", "subagent_result"}:
        return projection.agent_message_type == role
    if role == "user":
        return projection.role == "user"
    return projection.role == role and (
        projection.agent_message_type in {None, "agent"} if role == "agent" else True
    )


def _dedupe_chat_projection_items(
    events: list[tuple[Event, AgentChatProjection]]
) -> list[tuple[Event, AgentChatProjection]]:
    canonical_keys = {
        projection.dedupe_key
        for _event, projection in events
        if projection.is_canonical and projection.dedupe_key is not None
    }
    return [
        (event, projection)
        for event, projection in events
        if not (
            projection.is_duplicate_candidate
            and projection.dedupe_key is not None
            and projection.dedupe_key in canonical_keys
        )
    ]


def _deduped_chat_projection_items(events: list[Event]) -> list[tuple[Event, AgentChatProjection]]:
    items: list[tuple[Event, AgentChatProjection]] = []
    for event in events:
        projection = _project_chat(event)
        if projection is None or projection.role not in {"user", "agent"}:
            continue
        items.append((event, projection))
    return _dedupe_chat_projection_items(items)


def _trailing_duplicate_chat_projection_items(
    items: list[tuple[Event, AgentChatProjection]],
) -> list[tuple[Event, AgentChatProjection]]:
    trailing: list[tuple[Event, AgentChatProjection]] = []
    for event, projection in reversed(items):
        if not projection.is_duplicate_candidate or projection.dedupe_key is None:
            break
        trailing.append((event, projection))
    trailing.reverse()
    return trailing


def _dedupe_detail_events(events: list[Event]) -> list[Event]:
    chat_items: list[tuple[Event, AgentChatProjection]] = []
    for event in events:
        projection = _project_chat(event)
        if projection is not None and projection.role in {"user", "agent"}:
            chat_items.append((event, projection))

    duplicate_event_ids = {event.id for event, _projection in chat_items} - {
        event.id for event, _projection in _dedupe_chat_projection_items(chat_items)
    }
    return [event for event in events if event.id not in duplicate_event_ids]


async def runtime_tags_for_window_out(session: AsyncSession, window: VirtualWindow) -> list[str]:
    latest_ai_session = await session.scalar(
        select(AiSession)
        .where(
            AiSession.client_id == window.client_id,
            AiSession.virtual_window_id == window.id,
        )
        .order_by(desc(AiSession.updated_at), desc(AiSession.created_at), desc(AiSession.id))
        .limit(1)
    )
    latest_command = await session.scalar(
        select(Event.payload_json)
        .where(
            Event.client_id == window.client_id,
            Event.virtual_window_id == window.id,
            Event.kind == "terminal_input_command",
        )
        .order_by(desc(Event.created_at), desc(Event.id))
        .limit(1)
    )
    terminal_agent = agent_from_command(latest_command.get("command") if latest_command else None)
    return runtime_tags_for_window(
        window,
        ai_session=latest_ai_session,
        terminal_agent=terminal_agent,
    )


async def _agent_provider_for_window(session: AsyncSession, window: VirtualWindow) -> str | None:
    latest_ai_session = await session.scalar(
        select(AiSession)
        .where(
            AiSession.client_id == window.client_id,
            AiSession.virtual_window_id == window.id,
        )
        .order_by(desc(AiSession.updated_at), desc(AiSession.created_at), desc(AiSession.id))
        .limit(1)
    )
    if latest_ai_session is not None and latest_ai_session.provider:
        return _canonical_provider(latest_ai_session.provider)

    latest_command = await session.scalar(
        select(Event.payload_json)
        .where(
            Event.client_id == window.client_id,
            Event.virtual_window_id == window.id,
            Event.kind == "terminal_input_command",
        )
        .order_by(desc(Event.created_at), desc(Event.id))
        .limit(1)
    )
    terminal_agent = agent_from_command(latest_command.get("command") if latest_command else None)
    if terminal_agent is not None:
        return _canonical_provider(terminal_agent)
    shell_agent = agent_from_command(window.shell_command)
    return _canonical_provider(shell_agent) if shell_agent is not None else None


async def _agent_command_for_window(session: AsyncSession, window: VirtualWindow) -> str | None:
    latest_command = await session.scalar(
        select(Event.payload_json)
        .where(
            Event.client_id == window.client_id,
            Event.virtual_window_id == window.id,
            Event.kind == "terminal_input_command",
        )
        .order_by(desc(Event.created_at), desc(Event.id))
        .limit(1)
    )
    if isinstance(latest_command, dict):
        command = latest_command.get("command")
        if isinstance(command, str) and command.strip():
            return command
    return window.shell_command


def _require_supported_agent_capability(provider: str | None, capability: AgentClientCapability) -> str:
    if provider is not None:
        try:
            plugin = get_agent_plugin_registry().by_provider(provider)
        except ValueError:
            pass
        else:
            if getattr(plugin.capabilities, capability):
                return plugin.agent_client_id
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"agent client does not support {capability}",
            )
    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail="agent config unavailable for this terminal",
    )


def _require_local_agent_capability(agent: str, capability: AgentClientCapability) -> str:
    try:
        plugin = get_agent_plugin_registry().by_agent_id(agent)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    if not getattr(plugin.capabilities, capability):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"agent client does not support {capability}",
        )
    return plugin.agent_client_id


def _require_supported_provider(provider: str | None) -> str:
    if provider is not None:
        try:
            return get_agent_plugin_registry().by_provider(provider).provider_id
        except ValueError:
            pass
    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail="agent config unavailable for this terminal",
    )


def _agent_config_out(payload: object) -> AgentConfigOut:
    if isinstance(payload, agent_config_service.AgentConfig):
        payload = {
            "agent": payload.agent,
            "sections": [
                {
                    "id": section.id,
                    "name": section.name,
                    "items": [
                        {
                            "id": item.id,
                            "name": item.name,
                            "enabled": item.enabled,
                            "path": item.path,
                        }
                        for item in section.items
                    ],
                }
                for section in payload.sections
            ],
        }
    return AgentConfigOut.model_validate(payload)


def _agent_selection_from_schema(
    selection: AgentConfigSelectionIn,
) -> agent_config_service.AgentConfigSelection:
    return agent_config_service.AgentConfigSelection(
        agent=selection.agent,
        sections=[
            agent_config_service.AgentConfigSectionSelection(
                id=section.id,
                items=[
                    agent_config_service.AgentConfigItemSelection(id=item.id, enabled=item.enabled)
                    for item in section.items
                ],
            )
            for section in selection.sections
        ],
    )


def _agent_selection_payload(selection: agent_config_service.AgentConfigSelection) -> dict[str, object]:
    return {
        "agent": selection.agent,
        "sections": [
            {
                "id": section.id,
                "items": [
                    {"id": item.id, "enabled": item.enabled}
                    for item in section.items
                ],
            }
            for section in selection.sections
        ],
    }


def _launch_agent_kind(payload: WindowCreateIn) -> str:
    if payload.agent_launch is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="agent launch is required")
    return _require_local_agent_capability(payload.agent_launch.agent, "launch")


def _agent_command_for_launch(payload: WindowCreateIn) -> str | None:
    launch = payload.agent_launch
    if launch is None:
        return payload.shell_command
    return launch.command or launch.agent


def _agent_for_launch(payload: WindowCreateIn) -> str | None:
    launch = payload.agent_launch
    if launch is None:
        return None
    try:
        return get_agent_plugin_registry().by_agent_id(launch.agent).provider_id
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


def _known_agent_for_launch(payload: WindowCreateIn) -> str | None:
    try:
        return _agent_for_launch(payload)
    except HTTPException:
        return None


def _agent_config_for_launch(payload: WindowCreateIn) -> AgentConfigSelectionIn | None:
    launch = payload.agent_launch
    if launch is None or launch.config is None:
        return None
    launch_agent = _launch_agent_kind(payload)
    config_agent = agent_config_service.normalize_agent_kind(launch.config.agent)
    if config_agent != launch_agent:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="agent launch config agent must match launch agent",
        )
    return launch.config


def _remote_agent_for_launch(payload: WindowCreateIn) -> str:
    launch = payload.agent_launch
    if launch is None or not launch.agent.strip():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="agent launch is required")
    return launch.agent.strip()


def _remote_agent_config_for_launch(payload: WindowCreateIn) -> AgentConfigSelectionIn | None:
    launch = payload.agent_launch
    if launch is None or launch.config is None:
        return None
    if launch.config.agent.strip().lower() != launch.agent.strip().lower():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="agent launch config agent must match launch agent",
        )
    if launch.config.agent.strip() != launch.agent.strip():
        launch.config.agent = launch.agent.strip()
    return launch.config


def _remote_agent_descriptors(payload: dict[str, object]) -> list[dict[str, object]]:
    agents = payload.get("agent_clients")
    if not isinstance(agents, list):
        return []
    return [descriptor for descriptor in agents if isinstance(descriptor, dict)]


def _remote_descriptor_agent_id(descriptor: dict[str, object]) -> str | None:
    agent_id = descriptor.get("id")
    if isinstance(agent_id, str) and agent_id.strip():
        return agent_id.strip()
    return None


def _remote_descriptor_alias_candidates(descriptor: dict[str, object]) -> list[str]:
    candidates: list[str] = []
    for key in ("id", "provider_id", "default_command"):
        value = descriptor.get(key)
        if isinstance(value, str) and value.strip():
            candidates.append(value.strip())
    for key in ("aliases", "command_names"):
        values = descriptor.get(key)
        if isinstance(values, list):
            candidates.extend(value.strip() for value in values if isinstance(value, str) and value.strip())
    return candidates


def _remote_descriptor_for_agent(payload: dict[str, object], agent: str) -> dict[str, object] | None:
    clean_agent = agent.strip().lower()
    if not clean_agent:
        return None
    for descriptor in _remote_agent_descriptors(payload):
        for candidate in _remote_descriptor_alias_candidates(descriptor):
            if candidate.lower() == clean_agent:
                return descriptor
    return None


def _remote_descriptor_supports_capability(
    descriptor: dict[str, object],
    capability: AgentClientCapability,
) -> bool:
    capabilities = descriptor.get("capabilities")
    if not isinstance(capabilities, dict):
        return _REMOTE_CAPABILITY_DEFAULTS[capability]
    value = capabilities.get(capability)
    if isinstance(value, bool):
        return value
    return _REMOTE_CAPABILITY_DEFAULTS[capability]


def _remote_agent_id_with_capability(
    descriptors: dict[str, object],
    agent: str,
    capability: AgentClientCapability,
) -> str:
    clean_agent = agent.strip()
    if not clean_agent:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="agent is required")
    local_provider_id: str | None = None
    with contextlib.suppress(HTTPException):
        local_provider_id = _require_supported_provider(_canonical_provider(clean_agent))
    descriptor = _remote_descriptor_for_agent(descriptors, clean_agent)
    if descriptor is None and local_provider_id is not None:
        descriptor = _remote_descriptor_for_agent(descriptors, local_provider_id)
    if descriptor is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="agent config unavailable for this terminal",
        )
    if not _remote_descriptor_supports_capability(descriptor, capability):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"agent client does not support {capability}",
        )
    if local_provider_id is not None and _remote_descriptor_for_agent(descriptors, local_provider_id) is descriptor:
        return local_provider_id
    agent_id = _remote_descriptor_agent_id(descriptor)
    if agent_id is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="agent config unavailable for this terminal",
        )
    return agent_id


async def _require_remote_agent_capability(
    remote_runtime: RemoteRuntime,
    agent: str,
    capability: AgentClientCapability,
) -> str:
    return _remote_agent_id_with_capability(
        await remote_runtime.list_agent_clients(),
        agent,
        capability,
    )


async def _require_remote_launch_capabilities(
    payload: WindowCreateIn,
    remote_runtime: RemoteRuntime,
) -> None:
    launch = payload.agent_launch
    if launch is None:
        return
    descriptors = await remote_runtime.list_agent_clients()
    _remote_agent_id_with_capability(descriptors, launch.agent, "launch")
    if launch.config is not None:
        _remote_agent_id_with_capability(descriptors, launch.config.agent, "client_config")
    if _agent_profile_for_launch(payload) is not None:
        _remote_agent_id_with_capability(descriptors, launch.agent, "profile_config")


async def _remote_agent_request_id_for_capability(
    remote_runtime: RemoteRuntime,
    agent: str,
    capability: AgentClientCapability,
) -> str:
    return await _require_remote_agent_capability(remote_runtime, agent, capability)


async def _remote_agent_for_window(
    session: AsyncSession,
    window: VirtualWindow,
    remote_runtime: RemoteRuntime,
) -> str:
    provider = await _agent_provider_for_window(session, window)
    if provider is not None:
        with contextlib.suppress(HTTPException):
            return await _require_remote_agent_capability(remote_runtime, provider, "window_config")

    descriptors = await remote_runtime.list_agent_clients()
    remote_agent = _remote_agent_from_command(await _agent_command_for_window(session, window), descriptors)
    if remote_agent is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="agent config unavailable for this terminal",
        )
    return _remote_agent_id_with_capability(descriptors, remote_agent, "window_config")


def _remote_agent_from_command(command: str | None, payload: dict[str, object]) -> str | None:
    if not command:
        return None
    command_agents = _remote_command_agent_map(payload)
    for segment in _COMMAND_SEGMENT_PATTERN.split(command):
        tokens = _command_tokens(segment)
        while tokens:
            token = tokens.pop(0)
            if _ENV_ASSIGNMENT_PATTERN.match(token) or token in _COMMAND_WRAPPERS:
                continue
            agent = command_agents.get(posixpath.basename(token).lower())
            if agent is not None:
                return agent
            break
    return None


def _remote_command_agent_map(payload: dict[str, object]) -> dict[str, str]:
    result: dict[str, str] = {}
    for descriptor in _remote_agent_descriptors(payload):
        agent_id = _remote_descriptor_agent_id(descriptor)
        if agent_id is None:
            continue
        for candidate in _remote_descriptor_command_candidates(descriptor):
            result[posixpath.basename(candidate).lower()] = agent_id
    return result


def _remote_descriptor_command_candidates(descriptor: dict[str, object]) -> list[str]:
    candidates: list[str] = []
    for key in ("id", "provider_id", "default_command"):
        value = descriptor.get(key)
        if isinstance(value, str) and value.strip():
            candidates.append(value.strip())
    for key in ("aliases", "command_names"):
        values = descriptor.get(key)
        if isinstance(values, list):
            candidates.extend(value.strip() for value in values if isinstance(value, str) and value.strip())
    return candidates


def _command_tokens(segment: str) -> list[str]:
    try:
        return shlex.split(segment.strip())
    except ValueError:
        return segment.strip().split()


def _agent_profile_for_launch(payload: WindowCreateIn) -> str | None:
    launch = payload.agent_launch
    if launch is None:
        return None
    profile_id = launch.profile_id
    if profile_id is None or not profile_id.strip():
        return None
    return profile_id.strip()


async def _assign_window_folder_path(
    session: AsyncSession,
    client_id: UUID,
    window: VirtualWindow,
    folder_path: str | None,
) -> None:
    if not folder_path:
        return

    try:
        folder = await get_or_create_folder_by_path(session, client_id, folder_path)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    window.folder_id = folder.id
    window.folder_manually_overridden = True


def _client_connection_registry(request: Request) -> ClientConnectionRegistry:
    registry = getattr(request.app.state, "client_connections", None)
    if registry is None:
        registry = ClientConnectionRegistry()
        request.app.state.client_connections = registry
    return registry


def _background_session_factory_for(session: AsyncSession) -> Callable[[], object]:
    bind = getattr(session, "bind", None)
    if bind is None:
        return SessionLocal
    return async_sessionmaker(bind, expire_on_commit=False, class_=AsyncSession)


async def _create_remote_virtual_window_for_client(
    client: _RuntimeClient,
    payload: WindowCreateIn,
    session: AsyncSession,
    registry: ClientConnectionRegistry,
    *,
    session_factory: Callable[[], object] = SessionLocal,
    ui_event_hub=None,
) -> WindowOut:
    client_id = client.id
    effective_shell = _agent_command_for_launch(payload)
    agent_config_selection = None
    schema_selection = _remote_agent_config_for_launch(payload)
    if schema_selection is not None:
        agent_config_selection = _agent_selection_from_schema(schema_selection)
    agent_profile_id = _agent_profile_for_launch(payload)
    connection = registry.get(client_id)
    if connection is None or getattr(connection, "closed", False):
        logger.warning(
            "remote runtime unavailable during window create",
            extra={
                "client_id": str(client_id),
                "reason": "no_connection" if connection is None else "connection_closed",
            },
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="remote runtime unavailable",
        )
    remote_runtime = RemoteRuntime(client_id=client_id, registry=registry)
    try:
        await _require_remote_launch_capabilities(payload, remote_runtime)
    except RemoteClientUnavailable as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="remote runtime unavailable",
        ) from exc
    except RemoteTerminalError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc

    try:
        window = await create_window(
            session,
            client_id,
            cwd=payload.cwd,
            shell_command=effective_shell,
        )
        await _assign_window_folder_path(session, client_id, window, payload.folder_path)
        await session.commit()
        await session.refresh(window)
    except asyncio.CancelledError:
        with contextlib.suppress(Exception):
            await session.rollback()
        raise
    except Exception:
        with contextlib.suppress(Exception):
            await session.rollback()
        raise
    _schedule_remote_window_runtime_start(
        client_id=client_id,
        window_id=window.id,
        cwd=payload.cwd,
        shell_command=effective_shell,
        agent_config_selection=(
            _agent_selection_payload(agent_config_selection) if agent_config_selection else None
        ),
        agent_profile_id=agent_profile_id,
        agent_profile_agent=_remote_agent_for_launch(payload) if agent_profile_id is not None else None,
        registry=registry,
        session_factory=session_factory,
        ui_event_hub=ui_event_hub,
    )
    return to_window_out(
        window,
        runtime_tags=runtime_tags_for_window(
            window,
            terminal_agent=_known_agent_for_launch(payload) or agent_from_command(window.shell_command),
        ),
    )


async def _create_virtual_window_for_client(
    client: _RuntimeClient,
    payload: WindowCreateIn,
    session: AsyncSession,
    tmux_manager: TmuxManager,
    registry: ClientConnectionRegistry | None = None,
    *,
    session_factory: Callable[[], object] = SessionLocal,
    ui_event_hub=None,
) -> WindowOut:
    if client.runtime is not ClientRuntime.local:
        if registry is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="remote runtime unavailable",
            )
        return await _create_remote_virtual_window_for_client(
            client,
            payload,
            session,
            registry,
            session_factory=session_factory,
            ui_event_hub=ui_event_hub,
        )

    tmux_target = None
    window_id = uuid4()
    effective_cwd = payload.cwd
    effective_shell = _agent_command_for_launch(payload) or get_settings().default_shell
    try:
        agent_config_selection = _agent_config_for_launch(payload)
        agent_profile_id = _agent_profile_for_launch(payload)
        if agent_profile_id is not None:
            agent_profile_service.materialize_agent_profile_for_window(
                agent_profile_id,
                _launch_agent_kind(payload),
                window_id=str(window_id),
            )
        elif agent_config_selection is not None:
            agent_config_service.apply_agent_config_selection(
                _agent_selection_from_schema(agent_config_selection),
                window_id=str(window_id),
            )
        tmux_target = await tmux_manager.create_window(
            effective_cwd,
            effective_shell,
            client_id=client.id,
            window_id=window_id,
        )
        window = await create_window(
            session,
            client.id,
            cwd=getattr(tmux_target, "cwd", effective_cwd),
            shell_command=getattr(tmux_target, "shell_command", effective_shell),
            window_id=window_id,
            tmux_session=tmux_target.session,
            tmux_window_id=tmux_target.window_id,
        )
        await _assign_window_folder_path(session, client.id, window, payload.folder_path)
        await session.commit()
        await session.refresh(window)
    except HTTPException:
        with contextlib.suppress(Exception):
            await session.rollback()
        if tmux_target is not None:
            try:
                await tmux_manager.kill_window(tmux_target)
            except Exception:
                pass
        raise
    except asyncio.CancelledError:
        with contextlib.suppress(Exception):
            await session.rollback()
        if tmux_target is not None:
            with contextlib.suppress(Exception):
                await tmux_manager.kill_window(tmux_target)
        raise
    except Exception as exc:
        with contextlib.suppress(Exception):
            await session.rollback()
        if tmux_target is not None:
            try:
                await tmux_manager.kill_window(tmux_target)
            except Exception as cleanup_exc:
                exc.add_note(f"tmux cleanup failed: {cleanup_exc}")
        raise
    return to_window_out(
        window,
        runtime_tags=runtime_tags_for_window(
            window,
            terminal_agent=_agent_for_launch(payload) or agent_from_command(window.shell_command),
        ),
    )


def _schedule_remote_window_runtime_start(
    *,
    client_id: UUID,
    window_id: UUID,
    cwd: str | None,
    shell_command: str | None,
    agent_config_selection: dict[str, object] | None,
    agent_profile_id: str | None,
    agent_profile_agent: str | None,
    registry: ClientConnectionRegistry,
    session_factory: Callable[[], object],
    ui_event_hub,
) -> None:
    task = asyncio.create_task(
        _start_remote_window_runtime(
            client_id=client_id,
            window_id=window_id,
            cwd=cwd,
            shell_command=shell_command,
            agent_config_selection=agent_config_selection,
            agent_profile_id=agent_profile_id,
            agent_profile_agent=agent_profile_agent,
            registry=registry,
            session_factory=session_factory,
            ui_event_hub=ui_event_hub,
        )
    )
    task.add_done_callback(_log_remote_window_runtime_start_failure)


def _log_remote_window_runtime_start_failure(task: asyncio.Task[None]) -> None:
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.error(
            "remote window runtime start task crashed",
            exc_info=(type(exc), exc, exc.__traceback__),
        )


async def _start_remote_window_runtime(
    *,
    client_id: UUID,
    window_id: UUID,
    cwd: str | None,
    shell_command: str | None,
    agent_config_selection: dict[str, object] | None,
    agent_profile_id: str | None,
    agent_profile_agent: str | None,
    registry: ClientConnectionRegistry,
    session_factory: Callable[[], object],
    ui_event_hub,
) -> None:
    remote_runtime = RemoteRuntime(
        client_id=client_id,
        registry=registry,
        request_timeout=REMOTE_CREATE_WINDOW_TIMEOUT_SECONDS,
    )
    try:
        runtime_window = await remote_runtime.create_window(
            cwd=cwd,
            shell_command=shell_command,
            window_id=window_id,
            agent_config_selection=agent_config_selection,
            agent_profile_id=agent_profile_id,
            agent_profile_agent=agent_profile_agent,
        )
    except RemoteClientUnavailable as exc:
        logger.warning(
            "remote runtime unavailable during async window start",
            extra={
                "client_id": str(client_id),
                "window_id": str(window_id),
                "reason": getattr(exc, "reason", "unknown"),
            },
        )
        await _update_remote_window_status(
            session_factory,
            client_id,
            window_id,
            WindowStatus.disconnected,
            ui_event_hub=ui_event_hub,
            reason="window_runtime_unavailable",
        )
        return
    except RemoteTerminalError as exc:
        logger.warning(
            "remote runtime failed during async window start",
            extra={
                "client_id": str(client_id),
                "window_id": str(window_id),
                "error": str(exc),
            },
        )
        await _update_remote_window_status(
            session_factory,
            client_id,
            window_id,
            WindowStatus.error,
            ui_event_hub=ui_event_hub,
            reason="window_runtime_error",
        )
        return
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception(
            "remote runtime crashed during async window start",
            extra={"client_id": str(client_id), "window_id": str(window_id)},
        )
        await _update_remote_window_status(
            session_factory,
            client_id,
            window_id,
            WindowStatus.error,
            ui_event_hub=ui_event_hub,
            reason="window_runtime_error",
        )
        return

    updated = await _persist_remote_runtime_window(
        session_factory,
        client_id,
        window_id,
        runtime_window,
    )
    invalidate_polling_response_cache(["tree", "window"], client_id=client_id)
    if not updated:
        with contextlib.suppress(RemoteClientUnavailable, RemoteTerminalError):
            await remote_runtime.kill_window(
                window_id=window_id,
                remote_session_id=runtime_window.session_id,
                remote_window_id=runtime_window.window_id,
            )
        return

    if ui_event_hub is not None:
        with contextlib.suppress(Exception):
            await ui_event_hub.publish_invalidation(
                ["tree", "window"],
                client_id=client_id,
                window_id=window_id,
                reason="window_runtime_ready",
            )


async def _persist_remote_runtime_window(
    session_factory: Callable[[], object],
    client_id: UUID,
    window_id: UUID,
    runtime_window,
) -> bool:
    async with session_factory() as session:
        window = await get_window_for_client(session, client_id, window_id)
        if window is None:
            return False
        window.remote_session_id = runtime_window.session_id
        window.remote_window_id = runtime_window.window_id
        window.cwd = runtime_window.cwd
        window.shell_command = runtime_window.shell_command
        window.status = WindowStatus.active
        await session.commit()
        return True


async def _update_remote_window_status(
    session_factory: Callable[[], object],
    client_id: UUID,
    window_id: UUID,
    status_value: WindowStatus,
    *,
    ui_event_hub,
    reason: str,
) -> bool:
    async with session_factory() as session:
        window = await get_window_for_client(session, client_id, window_id)
        if window is None:
            return False
        window.status = status_value
        await session.commit()
    invalidate_polling_response_cache(["tree", "window"], client_id=client_id)
    if ui_event_hub is not None:
        with contextlib.suppress(Exception):
            await ui_event_hub.publish_invalidation(
                ["tree", "window"],
                client_id=client_id,
                window_id=window_id,
                reason=reason,
            )
    return True


async def _kill_runtime_window(
    client: Client,
    window: VirtualWindow,
    registry: ClientConnectionRegistry | None,
    *,
    tmux_manager: TmuxManager,
) -> None:
    if client.runtime is ClientRuntime.local:
        if window.tmux_session and window.tmux_window_id:
            with contextlib.suppress(Exception):
                await tmux_manager.kill_window(
                    TmuxTarget(
                        session=window.tmux_session,
                        window_id=window.tmux_window_id,
                        local_window_id=window.id,
                    )
                )
        return

    if registry is None:
        return
    if not window.remote_session_id or not window.remote_window_id:
        return
    remote_runtime = RemoteRuntime(client_id=client.id, registry=registry)
    with contextlib.suppress(RemoteClientUnavailable, RemoteTerminalError):
        await remote_runtime.kill_window(
            window_id=window.id,
            remote_session_id=window.remote_session_id,
            remote_window_id=window.remote_window_id,
        )


async def _delete_virtual_window_for_client(
    client: Client,
    window_id: UUID,
    session: AsyncSession,
    tmux_manager: TmuxManager,
    registry: ClientConnectionRegistry | None = None,
) -> None:
    window = await get_window_for_client(session, client.id, window_id)
    if window is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="window not found")

    await _kill_runtime_window(client, window, registry, tmux_manager=tmux_manager)
    deleted = await delete_window(session, client.id, window_id)
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="window not found")
    await session.commit()


@router.post("/clients/{client_id}/windows", response_model=WindowOut)
async def create_virtual_window(
    request: Request,
    client_id: UUID,
    payload: WindowCreateIn,
    session: AsyncSession = Depends(get_session),
    tmux_manager: TmuxManager = Depends(get_tmux_manager),
) -> WindowOut:
    client = _runtime_client_from_model(await _require_client(session, client_id))
    await session.commit()
    created = await _create_virtual_window_for_client(
        client,
        payload,
        session,
        tmux_manager,
        _client_connection_registry(request),
        session_factory=_background_session_factory_for(session),
        ui_event_hub=ui_event_hub_from_state(request.app.state),
    )
    await ui_event_hub_from_state(request.app.state).publish_invalidation(
        ["tree", "window", "search"],
        client_id=client_id,
        window_id=created.id,
        reason="window_created",
    )
    return created


@router.post("/windows", response_model=WindowOut)
async def create_local_virtual_window(
    request: Request,
    payload: WindowCreateIn,
    session: AsyncSession = Depends(get_session),
    tmux_manager: TmuxManager = Depends(get_tmux_manager),
) -> WindowOut:
    client = _runtime_client_from_model(await ensure_local_client(session))
    await session.commit()
    created = await _create_virtual_window_for_client(
        client,
        payload,
        session,
        tmux_manager,
        session_factory=_background_session_factory_for(session),
        ui_event_hub=ui_event_hub_from_state(request.app.state),
    )
    await ui_event_hub_from_state(request.app.state).publish_invalidation(
        ["tree", "window", "search"],
        client_id=client.id,
        window_id=created.id,
        reason="window_created",
    )
    return created


@router.get("/clients/{client_id}/windows/{window_id}", response_model=WindowOut)
async def read_virtual_window(
    client_id: UUID, window_id: UUID, session: AsyncSession = Depends(get_session)
) -> WindowOut | Response:
    cache_key = ("window", response_cache_scope(session), client_id, window_id)
    cached = _cached_or_stale_response(cache_key)
    if cached is not None and not cached.expired:
        return cached.response
    if cached is not None:
        _refresh_window_response_cache(cache_key, client_id, window_id)
        return cached.response

    return await _build_window_response(session, client_id, window_id, cache_key=cache_key)


async def _build_window_response(
    session: AsyncSession,
    client_id: UUID,
    window_id: UUID,
    *,
    cache_key: tuple[object, ...] | None = None,
) -> Response:
    await _require_client(session, client_id)
    window = await get_window_for_client(session, client_id, window_id)
    if window is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="window not found")
    summary_job = await get_latest_summary_job(session, window.id)
    runtime_tags = await runtime_tags_for_window_out(session, window)
    work_status = await load_work_status(session, client_id, window.id)
    timestamps = await _load_window_overview_timestamps(session, client_id, window.id)
    payload = to_window_out(window, summary_job, runtime_tags, work_status, timestamps)
    return store_json_response(
        cache_key or ("window", response_cache_scope(session), client_id, window_id),
        payload,
        resources={"window"},
        client_id=client_id,
    )


def _cached_or_stale_response(cache_key: tuple[object, ...]) -> CachedJsonResponse | None:
    return cached_or_stale_json_response(cache_key)


def _refresh_window_response_cache(
    cache_key: tuple[object, ...],
    client_id: UUID,
    window_id: UUID,
) -> None:
    if not begin_response_cache_refresh(cache_key):
        return

    async def refresh_window() -> None:
        try:
            async with SessionLocal() as refresh_session:
                await _build_window_response(refresh_session, client_id, window_id, cache_key=cache_key)
        except Exception:
            logger.exception("window response cache refresh failed", extra={"cache_key": repr(cache_key)})
        finally:
            finish_response_cache_refresh(cache_key)

    asyncio.create_task(refresh_window())


async def _require_window_for_agent_record(
    session: AsyncSession,
    client_id: UUID,
    window_id: UUID,
) -> VirtualWindow:
    await _require_client(session, client_id)
    window = await get_window_for_client(session, client_id, window_id)
    if window is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="window not found")
    return window


@router.get("/clients/{client_id}/windows/{window_id}/agent-record/chat", response_model=AgentChatRecordOut)
async def read_window_agent_record_chat(
    client_id: UUID,
    window_id: UUID,
    messages_limit: AgentRecordLimit = 30,
    messages_offset: AgentRecordOffset = 0,
    role: AgentChatRole = "all",
    session_id: UUID | None = None,
    session: AsyncSession = Depends(get_session),
) -> AgentChatRecordOut:
    await _require_window_for_agent_record(session, client_id, window_id)
    ai_sessions = list(
        await session.scalars(
            select(AiSession)
            .where(AiSession.client_id == client_id, AiSession.virtual_window_id == window_id)
            .order_by(AiSession.created_at, AiSession.id)
        )
    )
    base_event_filters = [
        Event.client_id == client_id,
        Event.virtual_window_id == window_id,
    ]
    event_filters = [
        *base_event_filters,
        or_(
            Event.kind.in_(("user_message", "assistant_message")),
            and_(
                Event.kind == "system_message",
                Event.source_type == EventSourceType.agent_tool_record,
            ),
            Event.kind.in_(("response_item", "event_msg")),
        ),
    ]
    if session_id is not None:
        base_event_filters.append(Event.ai_session_id == session_id)
        event_filters.append(Event.ai_session_id == session_id)
    page = await _load_chat_message_page(
        session,
        event_filters=event_filters,
        target_event_filters=base_event_filters,
        role=role,
        sessions_by_source_id={ai_session.source_id: ai_session for ai_session in ai_sessions},
        messages_limit=messages_limit,
        messages_offset=messages_offset,
    )
    return AgentChatRecordOut(
        window_id=window_id,
        messages=page.messages,
        messages_total=page.total,
        messages_total_exact=page.total_exact,
        messages_limit=messages_limit,
        messages_offset=messages_offset,
        messages_has_more=page.has_more,
    )


@router.get("/clients/{client_id}/windows/{window_id}/command-history", response_model=CommandHistoryOut)
async def read_window_command_history(
    client_id: UUID,
    window_id: UUID,
    commands_limit: CommandHistoryLimit = 100,
    commands_offset: CommandHistoryOffset = 0,
    session: AsyncSession = Depends(get_session),
) -> CommandHistoryOut:
    await _require_window_for_agent_record(session, client_id, window_id)
    command_filters = (
        Event.client_id == client_id,
        Event.virtual_window_id == window_id,
        Event.kind == "terminal_input_command",
    )
    commands_total = await session.scalar(select(func.count()).select_from(Event).where(*command_filters))
    command_events = list(
        await session.scalars(
            select(Event)
            .where(*command_filters)
            .order_by(desc(Event.created_at), desc(Event.id))
            .offset(commands_offset)
            .limit(commands_limit)
        )
    )
    sequence_keys = [
        sequence_key
        for event in command_events
        if (sequence_key := _payload_sequence_key(event)) is not None
    ]
    finished_by_sequence: dict[str, Event] = {}
    if sequence_keys:
        finished_fingerprints = [
            f"terminal_command_finished:{window_id}:{sequence_key}"
            for sequence_key in sequence_keys
        ]
        finished_events = list(
            await session.scalars(
                select(Event)
                .where(
                    Event.client_id == client_id,
                    Event.virtual_window_id == window_id,
                    Event.kind == "terminal_command_finished",
                    Event.fingerprint.in_(finished_fingerprints),
                )
                .order_by(desc(Event.created_at), desc(Event.id))
            )
        )
        wanted = set(sequence_keys)
        for event in finished_events:
            sequence_key = _payload_sequence_key(event)
            if sequence_key is not None and sequence_key in wanted and sequence_key not in finished_by_sequence:
                finished_by_sequence[sequence_key] = event
    raw_commands_total = int(commands_total or 0)
    return CommandHistoryOut(
        window_id=window_id,
        commands=[_command_history_item_out(event, finished_by_sequence) for event in command_events],
        commands_total=raw_commands_total,
        commands_limit=commands_limit,
        commands_offset=commands_offset,
        commands_has_more=commands_offset + commands_limit < raw_commands_total,
    )


@router.get("/clients/{client_id}/windows/{window_id}/title-history", response_model=WindowTitleHistoryOut)
async def read_window_title_history(
    client_id: UUID,
    window_id: UUID,
    limit: TitleHistoryLimit = 100,
    offset: TitleHistoryOffset = 0,
    session: AsyncSession = Depends(get_session),
) -> WindowTitleHistoryOut:
    await _require_window_for_agent_record(session, client_id, window_id)
    items, total = await list_window_title_history(
        session,
        client_id,
        window_id,
        limit=limit,
        offset=offset,
    )
    return WindowTitleHistoryOut(
        window_id=window_id,
        items=[
            WindowTitleHistoryItemOut(
                id=item.id,
                title=item.title,
                summary=item.summary,
                source=item.source,
                created_at=item.created_at,
            )
            for item in items
        ],
        total=total,
        limit=limit,
        offset=offset,
        has_more=offset + limit < total,
    )


@router.get("/clients/{client_id}/windows/{window_id}/agent-record/detail", response_model=AgentRecordOut)
async def read_window_agent_record_detail(
    client_id: UUID,
    window_id: UUID,
    events_limit: AgentRecordLimit = 100,
    events_offset: AgentRecordOffset = 0,
    session_id: UUID | None = None,
    session: AsyncSession = Depends(get_session),
) -> AgentRecordOut:
    await _require_window_for_agent_record(session, client_id, window_id)

    ai_sessions = list(
        await session.scalars(
            select(AiSession)
            .where(AiSession.client_id == client_id, AiSession.virtual_window_id == window_id)
            .order_by(AiSession.created_at, AiSession.id)
        )
    )
    event_filters = [
        Event.client_id == client_id,
        Event.virtual_window_id == window_id,
        Event.kind != "terminal_output",
    ]
    if session_id is not None:
        event_filters.append(Event.ai_session_id == session_id)
    events_total = await session.scalar(select(func.count()).select_from(Event).where(*event_filters))
    events = list(
        await session.scalars(
            select(Event)
            .options(selectinload(Event.ai_session))
            .where(*event_filters)
            .order_by(
                Event.created_at,
                case((Event.source_type == EventSourceType.terminal, 1), else_=0),
                Event.id,
            )
            .offset(events_offset)
            .limit(events_limit)
        )
    )
    events = _dedupe_detail_events(events)
    if events and session_id is None:
        event_ids = {event.id for event in events}
        sessions_by_source_id = {ai_session.source_id: ai_session for ai_session in ai_sessions}
        target_session_ids = {
            target_session.id
            for event in events
            if (projection := _project_chat(event)) is not None
            and projection.agent_message_type == "subagent_call"
            and projection.target_session_source_id is not None
            and (target_session := sessions_by_source_id.get(projection.target_session_source_id)) is not None
        }
        if target_session_ids:
            related_events = list(
                await session.scalars(
                    select(Event)
                    .options(selectinload(Event.ai_session))
                    .where(
                        Event.client_id == client_id,
                        Event.virtual_window_id == window_id,
                        Event.ai_session_id.in_(target_session_ids),
                        Event.kind != "terminal_output",
                    )
                    .order_by(
                        Event.created_at,
                        case((Event.source_type == EventSourceType.terminal, 1), else_=0),
                        Event.id,
                    )
                    .limit(AGENT_RECORD_DETAIL_RELATED_EVENT_LIMIT)
                )
            )
            for event in related_events:
                if event.id in event_ids:
                    continue
                events.append(event)
                event_ids.add(event.id)
    raw_events_total = events_total or 0
    sessions_by_source_id = {ai_session.source_id: ai_session for ai_session in ai_sessions}
    subagent_targets_by_tool_use_id = _subagent_targets_by_tool_use_id(events)
    return AgentRecordOut(
        window_id=window_id,
        sessions=[to_agent_session_out(ai_session) for ai_session in ai_sessions],
        events=[
            to_agent_event_out(event, sessions_by_source_id, subagent_targets_by_tool_use_id)
            for event in events
        ],
        events_total=raw_events_total,
        events_limit=events_limit,
        events_offset=events_offset,
        events_has_more=events_offset + events_limit < raw_events_total,
    )


@router.get("/clients/{client_id}/windows/{window_id}/agent-record", response_model=AgentRecordOut)
async def read_window_agent_record(
    client_id: UUID,
    window_id: UUID,
    events_limit: AgentRecordLimit = 100,
    events_offset: AgentRecordOffset = 0,
    session_id: UUID | None = None,
    session: AsyncSession = Depends(get_session),
) -> AgentRecordOut:
    return await read_window_agent_record_detail(client_id, window_id, events_limit, events_offset, session_id, session)


@router.get("/clients/{client_id}/windows/{window_id}/agent-config", response_model=AgentConfigOut)
async def read_window_agent_config(
    request: Request,
    client_id: UUID,
    window_id: UUID,
    session: AsyncSession = Depends(get_session),
) -> AgentConfigOut:
    client = await _require_client(session, client_id)
    window = await get_window_for_client(session, client_id, window_id)
    if window is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="window not found")
    if client.runtime is ClientRuntime.local:
        provider = await _agent_provider_for_window(session, window)
        local_agent = _require_supported_agent_capability(provider, "window_config")
        return _agent_config_out(
            agent_config_service.list_window_agent_config(local_agent, window_id=str(window_id))
        )

    remote_runtime = RemoteRuntime(client_id=client_id, registry=_client_connection_registry(request))
    try:
        remote_agent = await _remote_agent_for_window(session, window, remote_runtime)
        payload = await remote_runtime.get_agent_config(window_id=window_id, agent=remote_agent)
    except RemoteClientUnavailable as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="remote runtime unavailable",
        ) from exc
    except RemoteTerminalError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
    return _agent_config_out(payload)


@router.get("/clients/{client_id}/agent-config/{agent}", response_model=AgentConfigOut)
async def read_client_agent_config(
    request: Request,
    client_id: UUID,
    agent: str,
    session: AsyncSession = Depends(get_session),
) -> AgentConfigOut:
    client = await _require_client(session, client_id)
    if client.runtime is ClientRuntime.local:
        supported_agent = _require_supported_agent_capability(_canonical_provider(agent), "client_config")
        return _agent_config_out(agent_config_service.list_agent_config(supported_agent))

    remote_runtime = RemoteRuntime(client_id=client_id, registry=_client_connection_registry(request))
    try:
        payload = await remote_runtime.get_agent_config(
            agent=await _remote_agent_request_id_for_capability(remote_runtime, agent, "client_config")
        )
    except RemoteClientUnavailable as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="remote runtime unavailable",
        ) from exc
    except RemoteTerminalError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
    return _agent_config_out(payload)


@router.patch(
    "/clients/{client_id}/windows/{window_id}/agent-config/{section_id}/{item_id:path}",
    response_model=AgentConfigOut,
)
async def update_window_agent_config_item(
    request: Request,
    client_id: UUID,
    window_id: UUID,
    section_id: str,
    item_id: str,
    payload: AgentConfigToggleIn,
    session: AsyncSession = Depends(get_session),
) -> AgentConfigOut:
    client = await _require_client(session, client_id)
    window = await get_window_for_client(session, client_id, window_id)
    if window is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="window not found")
    if client.runtime is ClientRuntime.local:
        provider = await _agent_provider_for_window(session, window)
        local_agent = _require_supported_agent_capability(provider, "window_config")
        try:
            return _agent_config_out(
                agent_config_service.set_window_agent_config_item_enabled(
                    local_agent,
                    section_id,
                    item_id,
                    payload.enabled,
                    window_id=str(window_id),
                )
            )
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    remote_runtime = RemoteRuntime(client_id=client_id, registry=_client_connection_registry(request))
    try:
        remote_agent = await _remote_agent_for_window(session, window, remote_runtime)
        response_payload = await remote_runtime.set_agent_config_enabled(
            window_id=window_id,
            agent=remote_agent,
            section_id=section_id,
            item_id=item_id,
            enabled=payload.enabled,
        )
    except RemoteClientUnavailable as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="remote runtime unavailable",
        ) from exc
    except RemoteTerminalError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
    return _agent_config_out(response_payload)


def _to_git_worktree_run_out(run) -> GitWorktreeRunOut:
    run_type = "tracking" if str(run.command_sequence).startswith("worktree:") else "agent"
    return GitWorktreeRunOut(
        id=run.id,
        virtual_window_id=run.virtual_window_id,
        command_sequence=run.command_sequence,
        agent_provider=run.agent_provider,
        status=run.status,
        run_type=run_type,
        worktree_root=run.worktree_root,
        main_repo_root=run.main_repo_root,
        discovery_method=run.discovery_method,
        start_snapshot_json=run.start_snapshot_json,
        end_snapshot_json=run.end_snapshot_json,
        session_diff_json=run.session_diff_json,
        pending_commit=run.pending_commit,
        resolved_at=run.resolved_at,
        started_at=run.started_at,
        ended_at=run.ended_at,
    )


@router.get(
    "/clients/{client_id}/windows/{window_id}/git-runs",
    response_model=GitWorktreeRunListOut,
)
async def read_window_git_runs(
    request: Request,
    client_id: UUID,
    window_id: UUID,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
    session: AsyncSession = Depends(get_session),
) -> GitWorktreeRunListOut:
    await _require_window_for_agent_record(session, client_id, window_id)
    registry = _client_connection_registry(request)
    client_runtime = await session.scalar(select(Client.runtime).where(Client.id == client_id))
    binding = await get_window_git_binding(session, window_id)
    if binding is None:
        materialized = await materialize_agent_worktree_markers(
            session,
            client_id=client_id,
            window_ids=(window_id,),
            registry=registry,
        )
        if materialized:
            await process_git_worktree_snapshot_refresh(
                session,
                client_id=client_id,
                window_id=window_id,
                registry=registry,
                client_runtime=client_runtime,
            )
            await session.commit()
            binding = await get_window_git_binding(session, window_id)
    if binding is None:
        return GitWorktreeRunListOut(supported=False, runs=[], total=0, limit=limit, offset=offset)
    refreshed = await process_git_worktree_snapshot_refresh(
        session,
        client_id=client_id,
        window_id=window_id,
        registry=registry,
        client_runtime=client_runtime,
    )
    if refreshed:
        await session.commit()
    runs, total = await list_git_worktree_runs(session, window_id, limit=limit, offset=offset)
    return GitWorktreeRunListOut(
        supported=True,
        runs=[_to_git_worktree_run_out(run) for run in runs],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/windows/{window_id}", response_model=WindowOut)
async def read_local_virtual_window(
    window_id: UUID, session: AsyncSession = Depends(get_session)
) -> WindowOut:
    client = await ensure_local_client(session)
    return await read_virtual_window(client.id, window_id, session)


@router.patch("/clients/{client_id}/windows/{window_id}", response_model=WindowOut)
async def update_virtual_window(
    request: Request,
    client_id: UUID,
    window_id: UUID,
    payload: WindowPatchIn,
    session: AsyncSession = Depends(get_session),
) -> WindowOut:
    await _require_client(session, client_id)
    patch_values = {
        field_name: getattr(payload, field_name)
        for field_name in payload.model_fields_set
    }
    try:
        window = await patch_window(session, client_id, window_id, **patch_values)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except FolderNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    if window is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="window not found")

    await session.commit()
    await session.refresh(window)
    summary_job = await get_latest_summary_job(session, window.id)
    runtime_tags = await runtime_tags_for_window_out(session, window)
    work_status = await load_work_status(session, client_id, window.id)
    timestamps = await _load_window_overview_timestamps(session, client_id, window.id)
    updated = to_window_out(window, summary_job, runtime_tags, work_status, timestamps)
    await ui_event_hub_from_state(request.app.state).publish_invalidation(
        ["tree", "window", "search", "title_history"],
        client_id=client_id,
        window_id=window_id,
        reason="window_updated",
    )
    return updated


@router.patch("/windows/{window_id}", response_model=WindowOut)
async def update_local_virtual_window(
    request: Request,
    window_id: UUID,
    payload: WindowPatchIn,
    session: AsyncSession = Depends(get_session),
) -> WindowOut:
    client = await ensure_local_client(session)
    return await update_virtual_window(request, client.id, window_id, payload, session)


@router.delete("/clients/{client_id}/windows/{window_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_virtual_window(
    request: Request,
    client_id: UUID,
    window_id: UUID,
    session: AsyncSession = Depends(get_session),
    tmux_manager: TmuxManager = Depends(get_tmux_manager),
) -> None:
    client = await _require_client(session, client_id)
    await _delete_virtual_window_for_client(
        client,
        window_id,
        session,
        tmux_manager,
        _client_connection_registry(request),
    )
    if client.runtime is ClientRuntime.local:
        await aux_terminal_registry_from_state(request.app.state).remove(client_id, window_id)
    else:
        await kill_remote_aux_terminal(
            client_id=client_id,
            parent_window_id=window_id,
            registry=_client_connection_registry(request),
        )
    await ui_event_hub_from_state(request.app.state).publish_invalidation(
        ["tree", "window", "search"],
        client_id=client_id,
        window_id=window_id,
        reason="window_deleted",
    )


@router.delete("/windows/{window_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_local_virtual_window(
    request: Request,
    window_id: UUID,
    session: AsyncSession = Depends(get_session),
    tmux_manager: TmuxManager = Depends(get_tmux_manager),
) -> None:
    client = await ensure_local_client(session)
    await delete_virtual_window(request, client.id, window_id, session, tmux_manager)


@router.post("/clients/{client_id}/windows/{window_id}/summary_jobs", response_model=WindowOut)
async def retry_summary_job(
    request: Request,
    client_id: UUID,
    window_id: UUID,
    payload: SummaryJobRetryIn | None = None,
    session: AsyncSession = Depends(get_session),
) -> WindowOut:
    await _require_client(session, client_id)
    window = await get_window_for_client(session, client_id, window_id)
    if window is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="window not found")

    allow_override = False if payload is None else payload.allow_title_folder_override
    summary_job = await enqueue_manual_summary_retry(
        session,
        window.id,
        allow_title_folder_override=allow_override,
    )
    await session.commit()
    await session.refresh(window)
    await session.refresh(summary_job)
    runtime_tags = await runtime_tags_for_window_out(session, window)
    work_status = await load_work_status(session, client_id, window.id)
    timestamps = await _load_window_overview_timestamps(session, client_id, window.id)
    updated = to_window_out(window, summary_job, runtime_tags, work_status, timestamps)
    await ui_event_hub_from_state(request.app.state).publish_invalidation(
        ["window"],
        client_id=client_id,
        window_id=window_id,
        reason="summary_retry_queued",
    )
    return updated


@router.post("/windows/{window_id}/summary_jobs", response_model=WindowOut)
async def retry_local_summary_job(
    request: Request,
    window_id: UUID,
    payload: SummaryJobRetryIn | None = None,
    session: AsyncSession = Depends(get_session),
) -> WindowOut:
    client = await ensure_local_client(session)
    return await retry_summary_job(request, client.id, window_id, payload, session)
