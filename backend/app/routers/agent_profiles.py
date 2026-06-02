from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ClientRuntime
from app.db import get_session
from app.repositories.clients import get_client
from app.schemas import (
    AgentConfigOut,
    AgentConfigToggleIn,
    AgentProfileCreateIn,
    AgentProfileListOut,
    AgentProfileOut,
    AgentProfileUpdateIn,
)
from app.services import agent_profiles as agent_profile_service
from app.services.runtime.remote import RemoteClientUnavailable, RemoteRuntime, RemoteTerminalError
from app.routers.windows import (
    _agent_config_out,
    _canonical_provider,
    _client_connection_registry,
    _remote_agent_request_id_for_capability,
    _require_local_agent_capability,
    _require_supported_agent_capability,
)

router = APIRouter(prefix="/api", tags=["agent-profiles"])
_UNSET = object()


def _profile_out(profile: agent_profile_service.AgentProfile) -> AgentProfileOut:
    return AgentProfileOut(
        id=profile.id,
        name=profile.name,
        description=profile.description,
        default_agent_client=profile.default_agent_client,
        agent_md=profile.agent_md,
        created_at=profile.created_at,
        updated_at=profile.updated_at,
    )


def _field_or_unset(payload: AgentProfileUpdateIn, field: str) -> object:
    return getattr(payload, field) if field in payload.model_fields_set else _UNSET


async def _require_client_runtime(session: AsyncSession, client_id: UUID) -> ClientRuntime:
    client = await get_client(session, client_id)
    if client is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="client not found")
    return client.runtime


async def _remote_runtime_for_client(
    request: Request,
    session: AsyncSession,
    client_id: UUID,
) -> RemoteRuntime | None:
    runtime = await _require_client_runtime(session, client_id)
    if runtime is ClientRuntime.local:
        return None
    return RemoteRuntime(client_id=client_id, registry=_client_connection_registry(request))


def _validate_local_profile_create_capabilities(payload: AgentProfileCreateIn) -> None:
    _require_local_agent_capability(payload.default_agent_client, "launch")
    _require_local_agent_capability(payload.source_agent_client or payload.default_agent_client, "profile_config")


def _validate_local_profile_update_capabilities(payload: AgentProfileUpdateIn) -> None:
    if "default_agent_client" in payload.model_fields_set and payload.default_agent_client is not None:
        _require_local_agent_capability(payload.default_agent_client, "launch")


async def _validate_remote_profile_create_capabilities(
    remote_runtime: RemoteRuntime,
    payload: AgentProfileCreateIn,
) -> None:
    await _remote_agent_request_id_for_capability(
        remote_runtime,
        payload.default_agent_client,
        "launch",
    )
    await _remote_agent_request_id_for_capability(
        remote_runtime,
        payload.source_agent_client or payload.default_agent_client,
        "profile_config",
    )


async def _validate_remote_profile_update_capabilities(
    remote_runtime: RemoteRuntime,
    payload: AgentProfileUpdateIn,
) -> None:
    if "default_agent_client" in payload.model_fields_set and payload.default_agent_client is not None:
        await _remote_agent_request_id_for_capability(
            remote_runtime,
            payload.default_agent_client,
            "launch",
        )


@router.get("/clients/{client_id}/agent-profiles", response_model=AgentProfileListOut)
async def read_client_agent_profiles(
    request: Request,
    client_id: UUID,
    session: AsyncSession = Depends(get_session),
) -> AgentProfileListOut:
    remote_runtime = await _remote_runtime_for_client(request, session, client_id)
    if remote_runtime is None:
        return AgentProfileListOut(
            profiles=[_profile_out(profile) for profile in agent_profile_service.list_agent_profiles()]
        )
    try:
        return AgentProfileListOut.model_validate(await remote_runtime.list_agent_profiles())
    except RemoteClientUnavailable as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="remote runtime unavailable") from exc
    except RemoteTerminalError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc


@router.get("/agent-profiles", response_model=AgentProfileListOut)
async def read_agent_profiles() -> AgentProfileListOut:
    return AgentProfileListOut(
        profiles=[_profile_out(profile) for profile in agent_profile_service.list_agent_profiles()]
    )


@router.post("/clients/{client_id}/agent-profiles", response_model=AgentProfileOut)
async def create_client_agent_profile(
    request: Request,
    client_id: UUID,
    payload: AgentProfileCreateIn,
    session: AsyncSession = Depends(get_session),
) -> AgentProfileOut:
    remote_runtime = await _remote_runtime_for_client(request, session, client_id)
    if remote_runtime is not None:
        try:
            await _validate_remote_profile_create_capabilities(remote_runtime, payload)
            return AgentProfileOut.model_validate(
                await remote_runtime.create_agent_profile(payload.model_dump(mode="json"))
            )
        except RemoteClientUnavailable as exc:
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="remote runtime unavailable") from exc
        except RemoteTerminalError as exc:
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
    try:
        _validate_local_profile_create_capabilities(payload)
        return _profile_out(
            agent_profile_service.create_agent_profile(
                name=payload.name,
                description=payload.description,
                default_agent_client=payload.default_agent_client,
                source_agent_client=payload.source_agent_client,
            )
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.post("/agent-profiles", response_model=AgentProfileOut)
async def create_agent_profile(
    payload: AgentProfileCreateIn,
) -> AgentProfileOut:
    try:
        _validate_local_profile_create_capabilities(payload)
        return _profile_out(
            agent_profile_service.create_agent_profile(
                name=payload.name,
                description=payload.description,
                default_agent_client=payload.default_agent_client,
                source_agent_client=payload.source_agent_client,
            )
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.get("/clients/{client_id}/agent-profiles/{profile_id}", response_model=AgentProfileOut)
async def read_client_agent_profile(
    request: Request,
    client_id: UUID,
    profile_id: str,
    session: AsyncSession = Depends(get_session),
) -> AgentProfileOut:
    remote_runtime = await _remote_runtime_for_client(request, session, client_id)
    if remote_runtime is not None:
        profiles = await read_client_agent_profiles(request, client_id, session)
        for profile in profiles.profiles:
            if profile.id == profile_id:
                return profile
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="agent profile not found")
    try:
        return _profile_out(agent_profile_service.get_agent_profile(profile_id))
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


@router.get("/agent-profiles/{profile_id}", response_model=AgentProfileOut)
async def read_agent_profile(
    profile_id: str,
) -> AgentProfileOut:
    try:
        return _profile_out(agent_profile_service.get_agent_profile(profile_id))
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


@router.patch("/clients/{client_id}/agent-profiles/{profile_id}", response_model=AgentProfileOut)
async def update_client_agent_profile(
    request: Request,
    client_id: UUID,
    profile_id: str,
    payload: AgentProfileUpdateIn,
    session: AsyncSession = Depends(get_session),
) -> AgentProfileOut:
    remote_runtime = await _remote_runtime_for_client(request, session, client_id)
    if remote_runtime is not None:
        try:
            await _validate_remote_profile_update_capabilities(remote_runtime, payload)
            patch = {key: value for key, value in payload.model_dump(mode="json").items() if value is not None}
            return AgentProfileOut.model_validate(
                await remote_runtime.update_agent_profile(profile_id, patch)
            )
        except RemoteClientUnavailable as exc:
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="remote runtime unavailable") from exc
        except RemoteTerminalError as exc:
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
    try:
        _validate_local_profile_update_capabilities(payload)
        return _profile_out(
            agent_profile_service.update_agent_profile(
                profile_id,
                name=payload.name if "name" in payload.model_fields_set else None,
                description=_field_or_unset(payload, "description"),
                default_agent_client=payload.default_agent_client if "default_agent_client" in payload.model_fields_set else None,
                agent_md=_field_or_unset(payload, "agent_md"),
            )
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.patch("/agent-profiles/{profile_id}", response_model=AgentProfileOut)
async def update_agent_profile(
    profile_id: str,
    payload: AgentProfileUpdateIn,
) -> AgentProfileOut:
    try:
        _validate_local_profile_update_capabilities(payload)
        return _profile_out(
            agent_profile_service.update_agent_profile(
                profile_id,
                name=payload.name if "name" in payload.model_fields_set else None,
                description=_field_or_unset(payload, "description"),
                default_agent_client=payload.default_agent_client if "default_agent_client" in payload.model_fields_set else None,
                agent_md=_field_or_unset(payload, "agent_md"),
            )
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.delete("/clients/{client_id}/agent-profiles/{profile_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_client_agent_profile(
    request: Request,
    client_id: UUID,
    profile_id: str,
    session: AsyncSession = Depends(get_session),
) -> Response:
    remote_runtime = await _remote_runtime_for_client(request, session, client_id)
    if remote_runtime is not None:
        try:
            await remote_runtime.delete_agent_profile(profile_id)
            return Response(status_code=status.HTTP_204_NO_CONTENT)
        except RemoteClientUnavailable as exc:
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="remote runtime unavailable") from exc
        except RemoteTerminalError as exc:
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
    try:
        agent_profile_service.delete_agent_profile(profile_id)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete("/agent-profiles/{profile_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_agent_profile(
    profile_id: str,
) -> Response:
    try:
        agent_profile_service.delete_agent_profile(profile_id)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/clients/{client_id}/agent-profiles/{profile_id}/agent-config/{agent}",
    response_model=AgentConfigOut,
)
async def read_agent_profile_config(
    request: Request,
    client_id: UUID,
    profile_id: str,
    agent: str,
    session: AsyncSession = Depends(get_session),
) -> AgentConfigOut:
    runtime = await _require_client_runtime(session, client_id)
    if runtime is ClientRuntime.local:
        supported_agent = _require_supported_agent_capability(_canonical_provider(agent), "profile_config")
        try:
            return _agent_config_out(
                agent_profile_service.list_agent_profile_config(profile_id, supported_agent)
            )
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    remote_runtime = RemoteRuntime(client_id=client_id, registry=_client_connection_registry(request))
    try:
        payload = await remote_runtime.get_agent_profile_config(
            profile_id=profile_id,
            agent=await _remote_agent_request_id_for_capability(remote_runtime, agent, "profile_config"),
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
    "/clients/{client_id}/agent-profiles/{profile_id}/agent-config/{agent}/{section_id}/{item_id:path}",
    response_model=AgentConfigOut,
)
async def update_agent_profile_config_item(
    request: Request,
    client_id: UUID,
    profile_id: str,
    agent: str,
    section_id: str,
    item_id: str,
    payload: AgentConfigToggleIn,
    session: AsyncSession = Depends(get_session),
) -> AgentConfigOut:
    runtime = await _require_client_runtime(session, client_id)
    if runtime is not ClientRuntime.local:
        remote_runtime = RemoteRuntime(client_id=client_id, registry=_client_connection_registry(request))
        try:
            response_payload = await remote_runtime.set_agent_profile_config_enabled(
                profile_id=profile_id,
                agent=await _remote_agent_request_id_for_capability(remote_runtime, agent, "profile_config"),
                section_id=section_id,
                item_id=item_id,
                enabled=payload.enabled,
            )
        except RemoteClientUnavailable as exc:
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="remote runtime unavailable") from exc
        except RemoteTerminalError as exc:
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
        return _agent_config_out(response_payload)
    try:
        return _agent_config_out(
            agent_profile_service.set_agent_profile_config_item_enabled(
                profile_id,
                _require_supported_agent_capability(_canonical_provider(agent), "profile_config"),
                section_id,
                item_id,
                payload.enabled,
            )
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.patch(
    "/agent-profiles/{profile_id}/agent-config/{agent}/{section_id}/{item_id:path}",
    response_model=AgentConfigOut,
)
async def update_local_agent_profile_config_item(
    profile_id: str,
    agent: str,
    section_id: str,
    item_id: str,
    payload: AgentConfigToggleIn,
) -> AgentConfigOut:
    supported_agent = _require_supported_agent_capability(_canonical_provider(agent), "profile_config")
    try:
        return _agent_config_out(
            agent_profile_service.set_agent_profile_config_item_enabled(
                profile_id,
                supported_agent,
                section_id,
                item_id,
                payload.enabled,
            )
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
