from __future__ import annotations

from collections.abc import Awaitable, Callable
import logging
from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.models import Client, ClientRuntime
from app.repositories.clients import authenticate_client, get_client, list_clients
from app.routers.ui_events import ui_event_hub_from_state
from app.schemas import (
    BootstrapClientIn,
    BootstrapClientOut,
    ClientOut,
    ClientPatchIn,
    ClientUpdateCompleteIn,
    ClientUpdateOut,
)
from app.services.bootstrap.installer import (
    BootstrapConnectionError,
    BootstrapDependencyError,
    BootstrapResult,
    BootstrapSecretRedactor,
    bootstrap_client,
)
from app.services.client_update import (
    ClientUpdateStartResult,
    ClientUpdateUnavailable,
    build_client_update_package,
    start_client_update,
)
from app.services.runtime.client_connections import ClientConnectionRegistry

router = APIRouter(prefix="/api/clients", tags=["clients"])
logger = logging.getLogger(__name__)
BootstrapRunner = Callable[[AsyncSession, BootstrapClientIn], Awaitable[BootstrapResult]]
UpdateRunner = Callable[[UUID, ClientConnectionRegistry], Awaitable[ClientUpdateStartResult]]


def get_bootstrap_runner() -> BootstrapRunner:
    return bootstrap_client


def get_update_runner() -> UpdateRunner:
    async def runner(client_id: UUID, registry: ClientConnectionRegistry) -> ClientUpdateStartResult:
        return await start_client_update(client_id, registry=registry)

    return runner


def _bearer_token(authorization: str | None) -> str | None:
    if authorization is None:
        return None
    scheme, separator, token = authorization.partition(" ")
    if separator != " " or scheme.lower() != "bearer" or not token:
        return None
    return token


def _connection_registry(request: Request) -> ClientConnectionRegistry:
    registry = getattr(request.app.state, "client_connections", None)
    if registry is None:
        registry = ClientConnectionRegistry()
        request.app.state.client_connections = registry
    return registry


def _bootstrap_error_detail(payload: BootstrapClientIn, exc: Exception) -> str:
    return BootstrapSecretRedactor([payload.private_key, payload.passphrase]).redact(exc)


def to_client_out(client: Client) -> ClientOut:
    return ClientOut(
        id=client.id,
        name=client.name,
        status=client.status.value,
        hostname=client.hostname,
        install_path=client.install_path,
        version=client.version,
        last_update_at=client.last_update_at,
        runtime=client.runtime.value,
        last_seen_at=client.last_seen_at,
        connected_at=client.connected_at,
        created_at=client.created_at,
        updated_at=client.updated_at,
    )


@router.get("", response_model=list[ClientOut])
async def read_clients(session: AsyncSession = Depends(get_session)) -> list[ClientOut]:
    return [to_client_out(client) for client in await list_clients(session)]


@router.post("/bootstrap", response_model=BootstrapClientOut)
async def bootstrap_remote_client_route(
    request: Request,
    payload: BootstrapClientIn,
    session: AsyncSession = Depends(get_session),
    runner: BootstrapRunner = Depends(get_bootstrap_runner),
) -> BootstrapClientOut:
    return await bootstrap_remote_client(payload, session=session, runner=runner, request=request)


async def bootstrap_remote_client(
    payload: BootstrapClientIn,
    session: AsyncSession,
    runner: BootstrapRunner,
    request: Request | None = None,
) -> BootstrapClientOut:
    logger.info(
        "bootstrap client requested",
        extra={
            "client_name": payload.name,
            "host": payload.host,
            "port": payload.port,
            "username": payload.username,
            "server_url": payload.server_url,
        },
    )
    try:
        result = await runner(session, payload)
        await session.commit()
    except BootstrapDependencyError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=_bootstrap_error_detail(payload, exc),
        ) from None
    except BootstrapConnectionError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=_bootstrap_error_detail(payload, exc),
        ) from None

    if request is not None:
        await ui_event_hub_from_state(request.app.state).publish_invalidation(
            ["clients"],
            client_id=result.client_id,
            reason="client_bootstrapped",
        )
    logger.info(
        "bootstrap client completed",
        extra={
            "client_id": str(result.client_id),
            "client_name": result.name,
            "server_url": payload.server_url,
            "reused": result.reused,
        },
    )
    return BootstrapClientOut(
        client_id=result.client_id,
        name=result.name,
        status=result.status,
        reused=result.reused,
    )


@router.post("/{client_id}/update", response_model=ClientUpdateOut, status_code=status.HTTP_202_ACCEPTED)
async def update_remote_client(
    client_id: UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
    runner: UpdateRunner = Depends(get_update_runner),
) -> ClientUpdateOut:
    client = await get_client(session, client_id)
    if client is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="client not found")
    if client.runtime == ClientRuntime.local:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="local client update unsupported")

    try:
        result = await runner(client_id, _connection_registry(request))
    except ClientUpdateUnavailable as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from None

    await ui_event_hub_from_state(request.app.state).publish_invalidation(
        ["clients"],
        client_id=client_id,
        reason="client_update_started",
    )
    return ClientUpdateOut(
        client_id=result.client_id,
        job_id=result.job_id,
        status=result.status,
        method=result.method,
    )


@router.get("/{client_id}/update/package")
async def read_client_update_package(
    client_id: UUID,
    job_id: str | None = None,
    authorization: str | None = Header(default=None),
    session: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    token = _bearer_token(authorization)
    if token is None or await authenticate_client(session, client_id, token) is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid client token")
    return build_client_update_package(job_id)


@router.post("/{client_id}/update/complete")
async def complete_client_update(
    request: Request,
    client_id: UUID,
    payload: ClientUpdateCompleteIn,
    authorization: str | None = Header(default=None),
    session: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    token = _bearer_token(authorization)
    client = None if token is None else await authenticate_client(session, client_id, token)
    if client is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid client token")

    completed_at = datetime.now(UTC)
    client.last_update_at = completed_at
    await session.commit()
    await ui_event_hub_from_state(request.app.state).publish_invalidation(
        ["clients"],
        client_id=client_id,
        reason="client_update_completed",
    )
    logger.info(
        "client update completed",
        extra={
            "client_id": str(client_id),
            "job_id": payload.job_id,
            "completed_at": completed_at.isoformat(),
        },
    )
    return {"client_id": client_id, "job_id": payload.job_id, "completed_at": completed_at}


@router.get("/{client_id}", response_model=ClientOut)
async def read_client(client_id: UUID, session: AsyncSession = Depends(get_session)) -> ClientOut:
    client = await get_client(session, client_id)
    if client is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="client not found")
    return to_client_out(client)


@router.patch("/{client_id}", response_model=ClientOut)
async def update_client(
    request: Request,
    client_id: UUID,
    payload: ClientPatchIn,
    session: AsyncSession = Depends(get_session),
) -> ClientOut:
    client = await get_client(session, client_id)
    if client is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="client not found")

    if "name" in payload.model_fields_set and payload.name is not None:
        client.name = payload.name

    await session.commit()
    await session.refresh(client)
    await ui_event_hub_from_state(request.app.state).publish_invalidation(
        ["clients"],
        client_id=client_id,
        reason="client_updated",
    )
    return to_client_out(client)
