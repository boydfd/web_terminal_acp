from __future__ import annotations

import asyncio
import contextlib
import logging
import posixpath
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from sqlalchemy import case, desc, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload

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
from app.schemas import (
    AgentConfigOut,
    AgentConfigSelectionIn,
    AgentConfigToggleIn,
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
AgentChatRole = Literal["all", "user", "agent"]
CommandHistoryLimit = Annotated[int, Query(ge=1, le=200)]
CommandHistoryOffset = Annotated[int, Query(ge=0)]
TitleHistoryLimit = Annotated[int, Query(ge=1, le=200)]
TitleHistoryOffset = Annotated[int, Query(ge=0)]
_PROVIDER_ALIASES = {"claude": "claude_code", "cursor": "cursor_cli", "agent": "cursor_cli"}
_AGENT_PROVIDER_BY_KIND = {
    "codex": "codex",
    "claude": "claude_code",
    "cursor": "cursor_cli",
}


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
    return _PROVIDER_ALIASES.get(provider, provider)


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


def _projection_out(projection: AgentEventProjection) -> AgentEventProjectionOut:
    return AgentEventProjectionOut(
        tone=projection.tone,
        label=projection.label,
        body=projection.body,
        body_format=projection.body_format,
        subtype=projection.subtype,
    )


def _project_event(event: Event) -> AgentEventProjectionOut:
    adapter = _adapter_for_event(event)
    projection: AgentEventProjection | None = None
    if adapter is not None:
        with contextlib.suppress(Exception):
            projection = adapter.project_event(event)
    return _projection_out(projection or fallback_projection(event))


def to_agent_event_out(event: Event) -> AgentEventOut:
    return AgentEventOut(
        id=event.id,
        ai_session_id=event.ai_session_id,
        source_type=event.source_type.value,
        source_id=event.source_id,
        kind=event.kind,
        payload_json=event.payload_json,
        projection=_project_event(event),
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


def _chat_message_out(event: Event, projection: AgentChatProjection) -> AgentChatMessageOut:
    return AgentChatMessageOut(
        id=event.id,
        ai_session_id=event.ai_session_id,
        source_type=event.source_type.value,
        source_id=event.source_id,
        role=projection.role,
        body=projection.body,
        body_format=projection.body_format,
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


def _dedupe_chat_messages(events: list[Event], role: AgentChatRole = "all") -> list[AgentChatMessageOut]:
    return [
        _chat_message_out(event, projection)
        for event, projection in _deduped_chat_projection_items(events)
        if role == "all" or projection.role == role
    ]


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
    return _canonical_provider(agent_from_command(window.shell_command)) if window.shell_command else None


def _require_supported_agent(provider: str | None) -> str:
    if provider in {"codex", "claude_code", "cursor_cli"}:
        return provider
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


def _agent_command_for_launch(payload: WindowCreateIn) -> str | None:
    launch = payload.agent_launch
    if launch is None:
        return payload.shell_command
    return launch.command or launch.agent


def _agent_for_launch(payload: WindowCreateIn) -> str | None:
    launch = payload.agent_launch
    if launch is None:
        return None
    return _AGENT_PROVIDER_BY_KIND[launch.agent]


def _agent_config_for_launch(payload: WindowCreateIn) -> AgentConfigSelectionIn | None:
    launch = payload.agent_launch
    if launch is None or launch.config is None:
        return None
    if launch.config.agent != launch.agent:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="agent launch config agent must match launch agent",
        )
    return launch.config


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
    agent_config_selection = _agent_config_for_launch(payload)
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
            agent_config_selection.model_dump(mode="json") if agent_config_selection else None
        ),
        registry=registry,
        session_factory=session_factory,
        ui_event_hub=ui_event_hub,
    )
    return to_window_out(
        window,
        runtime_tags=runtime_tags_for_window(
            window,
            terminal_agent=_agent_for_launch(payload) or agent_from_command(window.shell_command),
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
        if agent_config_selection is not None:
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
                    TmuxTarget(session=window.tmux_session, window_id=window.tmux_window_id)
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
    session: AsyncSession = Depends(get_session),
) -> AgentChatRecordOut:
    await _require_window_for_agent_record(session, client_id, window_id)
    event_filters = (
        Event.client_id == client_id,
        Event.virtual_window_id == window_id,
        or_(
            Event.kind.in_(("user_message", "assistant_message")),
            Event.kind.in_(("response_item", "event_msg")),
        ),
    )
    candidate_events = list(
        await session.scalars(
            select(Event)
            .options(selectinload(Event.ai_session))
            .where(*event_filters)
            .order_by(
                Event.created_at,
                case((Event.source_type == EventSourceType.terminal, 1), else_=0),
                Event.id,
            )
        )
    )
    messages = _dedupe_chat_messages(candidate_events, role)
    paged_messages = messages[messages_offset : messages_offset + messages_limit]
    return AgentChatRecordOut(
        window_id=window_id,
        messages=paged_messages,
        messages_total=len(messages),
        messages_limit=messages_limit,
        messages_offset=messages_offset,
        messages_has_more=messages_offset + len(paged_messages) < len(messages),
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
    event_filters = (
        Event.client_id == client_id,
        Event.virtual_window_id == window_id,
        Event.kind != "terminal_output",
    )
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
    raw_events_total = events_total or 0
    return AgentRecordOut(
        window_id=window_id,
        sessions=[to_agent_session_out(ai_session) for ai_session in ai_sessions],
        events=[to_agent_event_out(event) for event in events],
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
    session: AsyncSession = Depends(get_session),
) -> AgentRecordOut:
    return await read_window_agent_record_detail(client_id, window_id, events_limit, events_offset, session)


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
    agent = _require_supported_agent(await _agent_provider_for_window(session, window))
    if client.runtime is ClientRuntime.local:
        return _agent_config_out(agent_config_service.list_agent_config(agent))

    remote_runtime = RemoteRuntime(client_id=client_id, registry=_client_connection_registry(request))
    try:
        payload = await remote_runtime.get_agent_config(window_id=window_id, agent=agent)
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
    supported_agent = _require_supported_agent(_canonical_provider(agent))
    if client.runtime is ClientRuntime.local:
        return _agent_config_out(agent_config_service.list_agent_config(supported_agent))

    remote_runtime = RemoteRuntime(client_id=client_id, registry=_client_connection_registry(request))
    try:
        payload = await remote_runtime.get_agent_config(agent=supported_agent)
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
    agent = _require_supported_agent(await _agent_provider_for_window(session, window))
    if client.runtime is ClientRuntime.local:
        try:
            return _agent_config_out(
                agent_config_service.set_agent_config_item_enabled(
                    agent,
                    section_id,
                    item_id,
                    payload.enabled,
                )
            )
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    remote_runtime = RemoteRuntime(client_id=client_id, registry=_client_connection_registry(request))
    try:
        response_payload = await remote_runtime.set_agent_config_enabled(
            window_id=window_id,
            agent=agent,
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
