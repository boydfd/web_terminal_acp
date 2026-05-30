from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from app.client_agent.updater import package_checksum
from app.models import Client, ClientRuntime, ClientStatus
from app.repositories.client_registration_keys import consume_registration_key
from app.repositories.clients import create_client
from app.services.bootstrap.installer import (
    AGENT_REQUIREMENTS,
    DEFAULT_INSTALL_PATH,
    build_client_config_payload,
    client_app_file_contents,
)


class ClientRegistrationKeyInvalid(RuntimeError):
    """Raised when a direct registration key is missing, invalid, or already used."""


async def register_direct_client(
    session: AsyncSession,
    *,
    registration_key: str,
    name: str,
    hostname: str | None,
    install_path: str | None,
    server_url: str,
) -> tuple[Client, str, dict[str, str], dict[str, object]]:
    effective_install_path = install_path or DEFAULT_INSTALL_PATH
    client, token = await create_client(
        session,
        name=name,
        hostname=hostname,
        install_path=effective_install_path,
        runtime=ClientRuntime.remote,
    )
    consumed = await consume_registration_key(
        session,
        registration_key=registration_key,
        client_id=client.id,
    )
    if consumed is None:
        await session.delete(client)
        await session.flush()
        raise ClientRegistrationKeyInvalid("registration key is invalid or already used")

    client.status = ClientStatus.OFFLINE
    client.last_update_at = datetime.now(UTC)
    await session.flush()
    config = build_client_config_payload(
        client,
        token=token,
        server_url=server_url,
        install_path=effective_install_path,
    )
    package = build_direct_registration_package()
    return client, token, config, package


def build_direct_registration_package(job_id: str | None = None) -> dict[str, object]:
    files = client_app_file_contents()
    requirements = AGENT_REQUIREMENTS
    return {
        "job_id": job_id or str(uuid4()),
        "files": files,
        "requirements": requirements,
        "checksum": package_checksum(files, requirements),
    }
