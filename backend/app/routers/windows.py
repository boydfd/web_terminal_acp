from __future__ import annotations

import contextlib
import logging
import posixpath
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import case, desc, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.agent_tools import get_agent_tool_registry
from app.agent_tools.common import fallback_projection
from app.agent_tools.types import AgentChatProjection, AgentEventProjection, AgentToolAdapter
from app.config import get_settings
from app.db import get_session
from app.models import AiSession, Client, ClientRuntime, Event, EventSourceType, SummaryJob, VirtualWindow
from app.repositories.clients import ensure_local_client, get_client
from app.repositories.summary_jobs import (
    enqueue_manual_summary_retry,
    get_latest_summary_job,
)
from app.repositories.git_worktree import get_window_git_binding, list_git_worktree_runs
from app.repositories.windows import (
    FolderNotFoundError,
    create_window,
    delete_window,
    get_window_for_client,
    patch_window,
)
from app.routers.ui_events import ui_event_hub_from_state
from app.schemas import (
    AgentChatMessageOut,
    AgentChatRecordOut,
    AgentEventOut,
    AgentEventProjectionOut,
    AgentRecordOut,
    AgentSessionOut,
    GitWorktreeRunListOut,
    GitWorktreeRunOut,
    SummaryJobOut,
    SummaryJobRetryIn,
    WindowCreateIn,
    WindowOut,
    WindowPatchIn,
    WorkStatusOut,
)
from app.services.runtime.client_connections import ClientConnectionRegistry
from app.services.runtime.remote import RemoteClientUnavailable, RemoteRuntime, RemoteTerminalError
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

AgentRecordLimit = Annotated[int, Query(ge=1, le=200)]
AgentRecordOffset = Annotated[int, Query(ge=0)]
_PROVIDER_ALIASES = {"claude": "claude_code"}


async def _require_client(session: AsyncSession, client_id: UUID) -> Client:
    client = await get_client(session, client_id)
    if client is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="client not found")
    return client


def _command_capture_supported(window: VirtualWindow) -> bool:
    shell = window.shell_command or get_settings().default_shell
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
) -> WindowOut:
    effective_runtime_tags = runtime_tags
    if effective_runtime_tags is None:
        effective_runtime_tags = runtime_tags_for_window(
            window,
            terminal_agent=agent_from_command(window.shell_command),
        )
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
        work_status=to_work_status_out(work_status or long_idle_work_status()),
        summary_job=to_summary_job_out(summary_job),
        created_at=window.created_at,
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


def _terminal_chat_projection(event: Event) -> AgentChatProjection | None:
    if event.kind != "terminal_input_command":
        return None
    body = _string_value(event.payload_json.get("command"))
    if body is None:
        return None
    return AgentChatProjection(role="user", body=body, dedupe_key=f"{event.source_id}:terminal:{body}")


def _project_chat(event: Event) -> AgentChatProjection | None:
    adapter = _adapter_for_event(event)
    chat: AgentChatProjection | None = None
    if adapter is not None:
        with contextlib.suppress(Exception):
            chat = adapter.project_chat(event)
    return chat or _terminal_chat_projection(event)


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


def _dedupe_chat_messages(events: list[Event]) -> list[AgentChatMessageOut]:
    return [
        _chat_message_out(event, projection)
        for event, projection in _deduped_chat_projection_items(events)
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


def _client_connection_registry(request: Request) -> ClientConnectionRegistry:
    registry = getattr(request.app.state, "client_connections", None)
    if registry is None:
        registry = ClientConnectionRegistry()
        request.app.state.client_connections = registry
    return registry


async def _create_remote_virtual_window_for_client(
    client: Client,
    payload: WindowCreateIn,
    session: AsyncSession,
    registry: ClientConnectionRegistry,
) -> WindowOut:
    client_id = client.id
    try:
        window = await create_window(
            session,
            client_id,
            cwd=payload.cwd,
            shell_command=payload.shell_command,
        )
        remote_runtime = RemoteRuntime(client_id=client_id, registry=registry)
        runtime_window = await remote_runtime.create_window(
            cwd=payload.cwd,
            shell_command=payload.shell_command,
            window_id=window.id,
        )
        window.remote_session_id = runtime_window.session_id
        window.remote_window_id = runtime_window.window_id
        window.cwd = runtime_window.cwd
        window.shell_command = runtime_window.shell_command
        await session.commit()
        await session.refresh(window)
    except RemoteClientUnavailable as exc:
        with contextlib.suppress(Exception):
            await session.rollback()
        logger.warning(
            "remote runtime unavailable during window create",
            extra={
                "client_id": str(client_id),
                "reason": getattr(exc, "reason", "unknown"),
            },
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="remote runtime unavailable",
        ) from exc
    except Exception:
        with contextlib.suppress(Exception):
            await session.rollback()
        raise
    return to_window_out(window)


async def _create_virtual_window_for_client(
    client: Client,
    payload: WindowCreateIn,
    session: AsyncSession,
    tmux_manager: TmuxManager,
    registry: ClientConnectionRegistry | None = None,
) -> WindowOut:
    if client.runtime is not ClientRuntime.local:
        if registry is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="remote runtime unavailable",
            )
        return await _create_remote_virtual_window_for_client(client, payload, session, registry)

    tmux_target = None
    window_id = uuid4()
    effective_cwd = payload.cwd
    effective_shell = payload.shell_command or get_settings().default_shell
    try:
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
        await session.commit()
        await session.refresh(window)
    except Exception as exc:
        with contextlib.suppress(Exception):
            await session.rollback()
        if tmux_target is not None:
            try:
                await tmux_manager.kill_window(tmux_target)
            except Exception as cleanup_exc:
                exc.add_note(f"tmux cleanup failed: {cleanup_exc}")
        raise
    return to_window_out(window)


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
    client = await _require_client(session, client_id)
    created = await _create_virtual_window_for_client(
        client,
        payload,
        session,
        tmux_manager,
        _client_connection_registry(request),
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
    client = await ensure_local_client(session)
    await session.commit()
    created = await _create_virtual_window_for_client(client, payload, session, tmux_manager)
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
) -> WindowOut:
    await _require_client(session, client_id)
    window = await get_window_for_client(session, client_id, window_id)
    if window is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="window not found")
    summary_job = await get_latest_summary_job(session, window.id)
    runtime_tags = await runtime_tags_for_window_out(session, window)
    work_status = await load_work_status(session, client_id, window.id)
    return to_window_out(window, summary_job, runtime_tags, work_status)


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
    session: AsyncSession = Depends(get_session),
) -> AgentChatRecordOut:
    await _require_window_for_agent_record(session, client_id, window_id)
    event_filters = (
        Event.client_id == client_id,
        Event.virtual_window_id == window_id,
        or_(
            Event.kind.in_(("terminal_input_command", "user_message", "assistant_message")),
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
    messages = _dedupe_chat_messages(candidate_events)
    paged_messages = messages[messages_offset : messages_offset + messages_limit]
    return AgentChatRecordOut(
        window_id=window_id,
        messages=paged_messages,
        messages_total=len(messages),
        messages_limit=messages_limit,
        messages_offset=messages_offset,
        messages_has_more=messages_offset + len(paged_messages) < len(messages),
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


def _to_git_worktree_run_out(run) -> GitWorktreeRunOut:
    return GitWorktreeRunOut(
        id=run.id,
        virtual_window_id=run.virtual_window_id,
        command_sequence=run.command_sequence,
        agent_provider=run.agent_provider,
        status=run.status,
        worktree_root=run.worktree_root,
        main_repo_root=run.main_repo_root,
        discovery_method=run.discovery_method,
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
    client_id: UUID,
    window_id: UUID,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
    session: AsyncSession = Depends(get_session),
) -> GitWorktreeRunListOut:
    await _require_window_for_agent_record(session, client_id, window_id)
    binding = await get_window_git_binding(session, window_id)
    if binding is None:
        return GitWorktreeRunListOut(supported=False, runs=[], total=0, limit=limit, offset=offset)
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
    updated = to_window_out(window, summary_job, runtime_tags, work_status)
    await ui_event_hub_from_state(request.app.state).publish_invalidation(
        ["tree", "window", "search"],
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
    updated = to_window_out(window, summary_job, runtime_tags, work_status)
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
