from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import hmac
import secrets
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Client, ClientRuntime, ClientStatus, LOCAL_CLIENT_ID
from app.version import __version__

LOCAL_CLIENT_NAME = "local"
LOCAL_CLIENT_UNUSABLE_TOKEN_HASH = (
    "sha256:9e3f0b2a4c1d8f6075b9e2c4a6d8f0137b5c9e1a2d4f6b8c0e3a5d7f9b1c4e6a"
)
_HASH_PREFIX = "sha256:"


def hash_client_token(token: str) -> str:
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    return f"{_HASH_PREFIX}{digest}"


def verify_client_token(token: str, token_hash: str) -> bool:
    if not token_hash.startswith(_HASH_PREFIX):
        return False
    expected = hash_client_token(token)
    return hmac.compare_digest(expected, token_hash)


def generate_client_token() -> str:
    return secrets.token_urlsafe(32)


async def get_client(session: AsyncSession, client_id: UUID) -> Client | None:
    return await session.get(Client, client_id)


async def get_local_client(session: AsyncSession) -> Client | None:
    return await get_client(session, LOCAL_CLIENT_ID)


async def create_client(
    session: AsyncSession,
    *,
    name: str,
    token: str | None = None,
    hostname: str | None = None,
    install_path: str | None = None,
    runtime: ClientRuntime = ClientRuntime.remote,
) -> tuple[Client, str]:
    token_value = token or generate_client_token()
    now = datetime.now(UTC)
    is_local = runtime is ClientRuntime.local
    client_kwargs = {
        "name": name,
        "status": ClientStatus.ONLINE if is_local else ClientStatus.OFFLINE,
        "token_hash": hash_client_token(token_value),
        "hostname": hostname,
        "install_path": install_path,
        "version": __version__ if is_local else None,
        "last_update_at": now if is_local else None,
        "runtime": runtime,
        "connected_at": now if is_local else None,
        "last_seen_at": now if is_local else None,
    }
    if is_local:
        client_kwargs["id"] = LOCAL_CLIENT_ID

    client = Client(**client_kwargs)
    session.add(client)
    await session.flush()
    return client, token_value


def _new_local_client(now: datetime) -> Client:
    return Client(
        id=LOCAL_CLIENT_ID,
        name=LOCAL_CLIENT_NAME,
        status=ClientStatus.ONLINE,
        token_hash=LOCAL_CLIENT_UNUSABLE_TOKEN_HASH,
        version=__version__,
        last_update_at=now,
        runtime=ClientRuntime.local,
        connected_at=now,
        last_seen_at=now,
    )


async def _mark_local_client_online(session: AsyncSession, client: Client, now: datetime) -> Client:
    client.status = ClientStatus.ONLINE
    client.runtime = ClientRuntime.local
    client.token_hash = LOCAL_CLIENT_UNUSABLE_TOKEN_HASH
    version_changed = client.version != __version__
    client.version = __version__
    if client.last_update_at is None or version_changed:
        client.last_update_at = now
    client.last_seen_at = now
    if client.connected_at is None:
        client.connected_at = now
    await session.flush()
    return client


async def ensure_local_client(session: AsyncSession) -> Client:
    now = datetime.now(UTC)
    client = await get_local_client(session)
    if client is None:
        try:
            async with session.begin_nested():
                client = _new_local_client(now)
                session.add(client)
                await session.flush()
        except IntegrityError:
            client = await get_local_client(session)
            if client is None:
                raise

    return await _mark_local_client_online(session, client, now)


async def list_clients(session: AsyncSession) -> list[Client]:
    return list(await session.scalars(select(Client).order_by(Client.name, Client.id)))


async def authenticate_client(session: AsyncSession, client_id: UUID, token: str) -> Client | None:
    client = await get_client(session, client_id)
    if client is None or not verify_client_token(token, client.token_hash):
        return None
    return client
