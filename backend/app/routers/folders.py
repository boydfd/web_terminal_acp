from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.models import Client
from app.repositories.clients import ensure_local_client, get_client
from app.repositories.folders import build_tree, get_or_create_folder_by_path
from app.routers.ui_events import ui_event_hub_from_state
from app.schemas import ClientWindowsActivityOut, FolderCreateIn, FolderOut, TreeFolderOut
from app.services.runtime.client_connections import ClientConnectionRegistry
from app.services.window_activity_api import load_client_windows_activity

router = APIRouter(prefix="/api", tags=["folders"])


async def _require_client(session: AsyncSession, client_id: UUID) -> Client:
    client = await get_client(session, client_id)
    if client is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="client not found")
    return client


@router.get("/clients/{client_id}/tree", response_model=list[TreeFolderOut], response_model_exclude_none=True)
async def get_client_tree(client_id: UUID, session: AsyncSession = Depends(get_session)):
    await _require_client(session, client_id)
    return await build_tree(session, client_id)


@router.get(
    "/clients/{client_id}/windows/activity",
    response_model=ClientWindowsActivityOut,
    response_model_exclude_none=True,
)
async def get_client_windows_activity(
    client_id: UUID,
    request: Request,
    include_runtime_tags: bool = Query(default=False),
    session: AsyncSession = Depends(get_session),
) -> ClientWindowsActivityOut:
    await _require_client(session, client_id)
    registry = getattr(request.app.state, "client_connections", None)
    if registry is not None and not isinstance(registry, ClientConnectionRegistry):
        registry = None
    activity = await load_client_windows_activity(
        session,
        client_id,
        include_runtime_tags=include_runtime_tags,
        registry=registry,
    )
    await session.commit()
    return activity


@router.get("/tree", response_model=list[TreeFolderOut], response_model_exclude_none=True)
async def get_tree(session: AsyncSession = Depends(get_session)):
    client = await ensure_local_client(session)
    tree = await build_tree(session, client.id)
    await session.commit()
    return tree


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
