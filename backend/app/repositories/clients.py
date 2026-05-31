from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import hmac
import secrets
from uuid import UUID

from sqlalchemy import delete as sa_delete
from sqlalchemy import select
from sqlalchemy import update as sa_update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    AiSession,
    Client,
    ClientRegistrationKey,
    ClientRuntime,
    ClientStatus,
    Event,
    Folder,
    FolderSplitJob,
    GitWorktreeRun,
    LOCAL_CLIENT_ID,
    ProjectSummary,
    SummaryJob,
    TerminalNotificationState,
    TerminalRecentUsage,
    VirtualWindow,
    WindowGitBinding,
    WindowTitleHistory,
)
from app.version import __version__

LOCAL_CLIENT_NAME = "local"
LOCAL_CLIENT_UNUSABLE_TOKEN_HASH = (
    "sha256:9e3f0b2a4c1d8f6075b9e2c4a6d8f0137b5c9e1a2d4f6b8c0e3a5d7f9b1c4e6a"
)
_HASH_PREFIX = "sha256:"


class ClientNameUnavailable(RuntimeError):
    """Raised when a requested remote client name is reserved by a non-remote client."""


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


async def get_client_by_name(session: AsyncSession, name: str) -> Client | None:
    return await session.scalar(select(Client).where(Client.name == name))


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


def _rotate_remote_client_token(
    client: Client,
    *,
    token: str,
    hostname: str | None,
    install_path: str | None,
    now: datetime,
) -> None:
    if client.runtime == ClientRuntime.local:
        raise ClientNameUnavailable("client name is reserved for local client")
    client.token_hash = hash_client_token(token)
    client.hostname = hostname
    client.install_path = install_path
    client.runtime = ClientRuntime.remote
    client.status = ClientStatus.OFFLINE
    client.connected_at = None
    client.last_seen_at = None
    client.last_update_at = now


async def create_or_rotate_remote_client_by_name(
    session: AsyncSession,
    *,
    name: str,
    hostname: str | None = None,
    install_path: str | None = None,
    token: str | None = None,
) -> tuple[Client, str, bool]:
    token_value = token or generate_client_token()
    now = datetime.now(UTC)
    statement = select(Client).where(Client.name == name)
    if session.bind is not None and session.bind.dialect.name != "sqlite":
        statement = statement.with_for_update()
    client = await session.scalar(statement)
    if client is not None:
        _rotate_remote_client_token(
            client,
            token=token_value,
            hostname=hostname,
            install_path=install_path,
            now=now,
        )
        await session.flush()
        return client, token_value, True

    try:
        async with session.begin_nested():
            client, token_value = await create_client(
                session,
                name=name,
                token=token_value,
                hostname=hostname,
                install_path=install_path,
                runtime=ClientRuntime.remote,
            )
    except IntegrityError:
        client = await get_client_by_name(session, name)
        if client is None:
            raise
        _rotate_remote_client_token(
            client,
            token=token_value,
            hostname=hostname,
            install_path=install_path,
            now=now,
        )
        await session.flush()
        return client, token_value, True

    client.last_update_at = now
    await session.flush()
    return client, token_value, False


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


async def delete_remote_client(session: AsyncSession, client_id: UUID) -> bool | None:
    client = await get_client(session, client_id)
    if client is None:
        return False
    if client.runtime is ClientRuntime.local:
        return None

    window_ids = list(
        await session.scalars(select(VirtualWindow.id).where(VirtualWindow.client_id == client_id))
    )
    folder_ids = list(await session.scalars(select(Folder.id).where(Folder.client_id == client_id)))
    ai_session_ids = list(
        await session.scalars(select(AiSession.id).where(AiSession.client_id == client_id))
    )

    if window_ids:
        await session.execute(
            sa_delete(SummaryJob).where(SummaryJob.virtual_window_id.in_(window_ids))
        )
        await session.execute(
            sa_delete(TerminalRecentUsage).where(TerminalRecentUsage.window_id.in_(window_ids))
        )
        await session.execute(
            sa_delete(WindowTitleHistory).where(
                WindowTitleHistory.virtual_window_id.in_(window_ids)
            )
        )
        await session.execute(
            sa_delete(WindowGitBinding).where(WindowGitBinding.virtual_window_id.in_(window_ids))
        )
        await session.execute(
            sa_delete(GitWorktreeRun).where(GitWorktreeRun.virtual_window_id.in_(window_ids))
        )

    if folder_ids:
        await session.execute(
            sa_delete(FolderSplitJob).where(FolderSplitJob.folder_id.in_(folder_ids))
        )

    if ai_session_ids:
        await session.execute(
            sa_update(Event)
            .where(Event.ai_session_id.in_(ai_session_ids))
            .values(ai_session_id=None)
        )

    await session.execute(
        sa_update(ClientRegistrationKey)
        .where(ClientRegistrationKey.used_client_id == client_id)
        .values(used_client_id=None)
    )
    await session.execute(
        sa_delete(TerminalNotificationState).where(TerminalNotificationState.client_id == client_id)
    )
    await session.execute(sa_delete(TerminalRecentUsage).where(TerminalRecentUsage.client_id == client_id))
    await session.execute(sa_delete(WindowTitleHistory).where(WindowTitleHistory.client_id == client_id))
    await session.execute(sa_delete(WindowGitBinding).where(WindowGitBinding.client_id == client_id))
    await session.execute(sa_delete(GitWorktreeRun).where(GitWorktreeRun.client_id == client_id))
    await session.execute(sa_delete(ProjectSummary).where(ProjectSummary.client_id == client_id))
    await session.execute(sa_delete(FolderSplitJob).where(FolderSplitJob.client_id == client_id))
    await session.execute(sa_delete(Event).where(Event.client_id == client_id))
    await session.execute(sa_delete(AiSession).where(AiSession.client_id == client_id))
    await session.execute(sa_delete(VirtualWindow).where(VirtualWindow.client_id == client_id))
    await session.execute(sa_delete(Folder).where(Folder.client_id == client_id))
    result = await session.execute(sa_delete(Client).where(Client.id == client_id))
    await session.flush()
    return result.rowcount == 1
