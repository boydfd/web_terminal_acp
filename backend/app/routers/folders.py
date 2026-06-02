from uuid import UUID

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import SessionLocal, get_session
from app.models import Client
from app.repositories.clients import ensure_local_client, get_client
from app.repositories.folders import build_tree, get_or_create_folder_by_path, list_terminal_projects
from app.routers.ui_events import ui_event_hub_from_state
from app.schemas import (
    ClientWindowsActivityOut,
    FolderCreateIn,
    FolderOut,
    TerminalProjectOut,
    TreeFolderOut,
)
from app.services.polling_response_cache import (
    CachedJsonResponse,
    begin_response_cache_refresh,
    cached_or_stale_json_response,
    cached_json_response,
    finish_response_cache_refresh,
    response_cache_scope,
    store_json_response,
)
from app.services.terminal_time_ranges import terminal_visible_since
from app.services.window_activity_api import load_client_windows_activity

router = APIRouter(prefix="/api", tags=["folders"])
logger = logging.getLogger(__name__)


async def _require_client(session: AsyncSession, client_id: UUID) -> Client:
    client = await get_client(session, client_id)
    if client is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="client not found")
    return client


@router.get("/clients/{client_id}/tree", response_model=list[TreeFolderOut], response_model_exclude_none=True)
async def get_client_tree(
    client_id: UUID,
    time_range: str | None = Query(default=None, alias="range"),
    project_path: str | None = Query(default=None),
    session: AsyncSession = Depends(get_session),
):
    visible_since = terminal_visible_since(time_range)
    cache_project_path = _normalized_project_path(project_path)
    cache_key = ("tree", response_cache_scope(session), client_id, time_range or "all", cache_project_path)
    cached = _cached_or_stale_response(cache_key)
    if cached is not None and not cached.expired:
        return cached.response
    if cached is not None:
        _refresh_response_cache(
            cache_key,
            lambda refresh_session: _build_tree_response(
                refresh_session,
                client_id,
                require_client=True,
                visible_since=visible_since,
                project_path=cache_project_path,
                cache_key=cache_key,
            ),
        )
        return cached.response
    await _require_client(session, client_id)
    tree = await build_tree(session, client_id, visible_since=visible_since, project_path=cache_project_path)
    return _store_response(cache_key, tree, resources={"tree"}, client_id=client_id)


@router.get(
    "/clients/{client_id}/terminal-projects",
    response_model=list[TerminalProjectOut],
    response_model_exclude_none=True,
)
async def get_client_terminal_projects(
    client_id: UUID,
    time_range: str | None = Query(default=None, alias="range"),
    session: AsyncSession = Depends(get_session),
):
    visible_since = terminal_visible_since(time_range)
    cache_key = ("terminal-projects", response_cache_scope(session), client_id, time_range or "all")
    cached = _cached_or_stale_response(cache_key)
    if cached is not None and not cached.expired:
        return cached.response
    if cached is not None:
        _refresh_response_cache(
            cache_key,
            lambda refresh_session: _build_terminal_projects_response(
                refresh_session,
                client_id,
                require_client=True,
                visible_since=visible_since,
                cache_key=cache_key,
            ),
        )
        return cached.response
    await _require_client(session, client_id)
    projects = await list_terminal_projects(session, client_id, visible_since=visible_since)
    return _store_response(cache_key, projects, resources={"tree"}, client_id=client_id)


@router.get(
    "/clients/{client_id}/windows/activity",
    response_model=ClientWindowsActivityOut,
    response_model_exclude_none=True,
)
async def get_client_windows_activity(
    client_id: UUID,
    include_runtime_tags: bool = Query(default=False),
    time_range: str | None = Query(default=None, alias="range"),
    project_path: str | None = Query(default=None),
    session: AsyncSession = Depends(get_session),
) -> ClientWindowsActivityOut | Response:
    visible_since = terminal_visible_since(time_range)
    cache_project_path = _normalized_project_path(project_path)
    cache_key = (
        "activity",
        response_cache_scope(session),
        client_id,
        include_runtime_tags,
        time_range or "all",
        cache_project_path,
    )
    cached = _cached_or_stale_response(cache_key)
    if cached is not None and not cached.expired:
        return cached.response
    if cached is not None:
        _refresh_response_cache(
            cache_key,
            lambda refresh_session: _build_activity_response(
                refresh_session,
                client_id,
                include_runtime_tags=include_runtime_tags,
                visible_since=visible_since,
                project_path=cache_project_path,
                cache_key=cache_key,
            ),
        )
        return cached.response
    await _require_client(session, client_id)
    activity = await load_client_windows_activity(
        session,
        client_id,
        include_runtime_tags=include_runtime_tags,
        visible_since=visible_since,
        project_path=cache_project_path,
    )
    return _store_response(cache_key, activity, resources={"window", "tree"}, client_id=client_id)


@router.get("/tree", response_model=list[TreeFolderOut], response_model_exclude_none=True)
async def get_tree(
    time_range: str | None = Query(default=None, alias="range"),
    project_path: str | None = Query(default=None),
    session: AsyncSession = Depends(get_session),
):
    client = await ensure_local_client(session)
    visible_since = terminal_visible_since(time_range)
    cache_project_path = _normalized_project_path(project_path)
    cache_key = ("tree", response_cache_scope(session), client.id, time_range or "all", cache_project_path)
    cached = _cached_or_stale_response(cache_key)
    if cached is not None and not cached.expired:
        return cached.response
    if cached is not None:
        _refresh_response_cache(
            cache_key,
            lambda refresh_session: _build_tree_response(
                refresh_session,
                client.id,
                require_client=False,
                visible_since=visible_since,
                project_path=cache_project_path,
                cache_key=cache_key,
            ),
        )
        return cached.response
    tree = await build_tree(session, client.id, visible_since=visible_since, project_path=cache_project_path)
    await session.commit()
    return _store_response(cache_key, tree, resources={"tree"}, client_id=client.id)


def _normalized_project_path(project_path: str | None) -> str | None:
    if project_path is None:
        return None
    normalized = project_path.strip()
    return normalized or None


async def _get_or_create_folder_and_commit(session: AsyncSession, client_id: UUID, path: str):
    folder = await get_or_create_folder_by_path(session, client_id, path)
    await session.commit()
    return folder


async def _create_folder_for_client(
    session: AsyncSession, client_id: UUID, payload: FolderCreateIn
) -> FolderOut:
    try:
        folder = await _get_or_create_folder_and_commit(session, client_id, payload.path)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except IntegrityError:
        await session.rollback()
        try:
            folder = await _get_or_create_folder_and_commit(session, client_id, payload.path)
        except IntegrityError as retry_exc:
            await session.rollback()
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="folder path conflict; retry request",
            ) from retry_exc
        except ValueError as retry_exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=str(retry_exc),
            ) from retry_exc
    return FolderOut(id=folder.id, name=folder.name, path=folder.path)


def _cached_response(cache_key: tuple[object, ...]) -> Response | None:
    return cached_json_response(cache_key)


def _cached_or_stale_response(cache_key: tuple[object, ...]) -> CachedJsonResponse | None:
    return cached_or_stale_json_response(cache_key)


def _store_response(
    cache_key: tuple[object, ...],
    payload: object,
    *,
    resources: set[str],
    client_id: UUID,
) -> Response:
    return store_json_response(cache_key, payload, resources=resources, client_id=client_id)


async def _build_tree_response(
    session: AsyncSession,
    client_id: UUID,
    *,
    require_client: bool,
    visible_since: datetime | None,
    project_path: str | None,
    cache_key: tuple[object, ...],
) -> Response:
    if require_client:
        await _require_client(session, client_id)
    tree = await build_tree(session, client_id, visible_since=visible_since, project_path=project_path)
    return _store_response(cache_key, tree, resources={"tree"}, client_id=client_id)


async def _build_terminal_projects_response(
    session: AsyncSession,
    client_id: UUID,
    *,
    require_client: bool,
    visible_since: datetime | None,
    cache_key: tuple[object, ...],
) -> Response:
    if require_client:
        await _require_client(session, client_id)
    projects = await list_terminal_projects(session, client_id, visible_since=visible_since)
    return _store_response(cache_key, projects, resources={"tree"}, client_id=client_id)


async def _build_activity_response(
    session: AsyncSession,
    client_id: UUID,
    *,
    include_runtime_tags: bool,
    visible_since: datetime | None,
    project_path: str | None,
    cache_key: tuple[object, ...],
) -> Response:
    await _require_client(session, client_id)
    activity = await load_client_windows_activity(
        session,
        client_id,
        include_runtime_tags=include_runtime_tags,
        visible_since=visible_since,
        project_path=project_path,
    )
    return _store_response(
        cache_key,
        activity,
        resources={"window", "tree"},
        client_id=client_id,
    )


def _refresh_response_cache(
    cache_key: tuple[object, ...],
    refresh: Callable[[AsyncSession], Awaitable[Response]],
) -> None:
    if not begin_response_cache_refresh(cache_key):
        return

    async def refresh_task() -> None:
        try:
            async with SessionLocal() as refresh_session:
                await refresh(refresh_session)
        except Exception:
            logger.exception("polling response cache refresh failed", extra={"cache_key": repr(cache_key)})
        finally:
            finish_response_cache_refresh(cache_key)

    asyncio.create_task(refresh_task())


@router.post("/clients/{client_id}/folders", response_model=FolderOut)
async def create_client_folder(
    request: Request,
    client_id: UUID,
    payload: FolderCreateIn,
    session: AsyncSession = Depends(get_session),
):
    await _require_client(session, client_id)
    folder = await _create_folder_for_client(session, client_id, payload)
    await ui_event_hub_from_state(request.app.state).publish_invalidation(
        ["tree"],
        client_id=client_id,
        reason="folder_created",
    )
    return folder


@router.post("/folders", response_model=FolderOut)
async def create_folder(
    request: Request,
    payload: FolderCreateIn,
    session: AsyncSession = Depends(get_session),
):
    client = await ensure_local_client(session)
    folder = await _create_folder_for_client(session, client.id, payload)
    await ui_event_hub_from_state(request.app.state).publish_invalidation(
        ["tree"],
        client_id=client.id,
        reason="folder_created",
    )
    return folder
