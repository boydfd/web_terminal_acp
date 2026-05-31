import re

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.repositories.clients as clients_repo
from app.db import Base
from app.models import Client, ClientRuntime, ClientStatus, LOCAL_CLIENT_ID
from app.repositories.clients import (
    LOCAL_CLIENT_NAME,
    authenticate_client,
    create_or_rotate_remote_client_by_name,
    create_client,
    ensure_local_client,
    generate_client_token,
    hash_client_token,
    verify_client_token,
)


@pytest.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    async with session_factory() as db_session:
        yield db_session

    await engine.dispose()


def test_hash_client_token_uses_sha256_hex_and_constant_time_verify():
    token = "client-token-secret"

    token_hash = hash_client_token(token)

    assert re.fullmatch(r"sha256:[0-9a-f]{64}", token_hash)
    assert verify_client_token(token, token_hash) is True
    assert verify_client_token("wrong-token", token_hash) is False
    assert verify_client_token(token, "md5:not-supported") is False


@pytest.mark.asyncio
async def test_create_remote_client_stores_metadata_and_returns_token(session):
    token = generate_client_token()

    client, plaintext_token = await create_client(
        session,
        name="Remote MacBook",
        token=token,
        hostname="macbook.local",
        install_path="/opt/web-terminal-agent",
        runtime=ClientRuntime.remote,
    )
    await session.commit()

    assert plaintext_token == token
    assert client.name == "Remote MacBook"
    assert client.status is ClientStatus.OFFLINE
    assert client.runtime is ClientRuntime.remote
    assert client.hostname == "macbook.local"
    assert client.install_path == "/opt/web-terminal-agent"
    assert client.token_hash.startswith("sha256:")
    assert client.token_hash != token
    assert verify_client_token(token, client.token_hash) is True


@pytest.mark.asyncio
async def test_authenticate_client_requires_matching_client_id_and_token(session):
    first_client, first_token = await create_client(session, name="first")
    second_client, _second_token = await create_client(session, name="second")
    await session.commit()

    authenticated = await authenticate_client(session, first_client.id, first_token)

    assert authenticated is not None
    assert authenticated.id == first_client.id
    assert await authenticate_client(session, second_client.id, first_token) is None
    assert await authenticate_client(session, first_client.id, "wrong-token") is None


@pytest.mark.asyncio
async def test_ensure_local_client_seed_does_not_authenticate_static_token(session):
    client = await ensure_local_client(session)
    await session.commit()

    assert client.token_hash.startswith("sha256:")
    assert client.token_hash != hash_client_token("local-client-token")
    assert verify_client_token("local-client-token", client.token_hash) is False
    assert await authenticate_client(session, client.id, "local-client-token") is None


@pytest.mark.asyncio
async def test_ensure_local_client_is_idempotent_and_online(session):
    first = await ensure_local_client(session)
    await session.commit()

    second = await ensure_local_client(session)
    await session.commit()

    clients = list(await session.scalars(select(Client)))
    assert len(clients) == 1
    assert first.id == second.id
    assert second.name == LOCAL_CLIENT_NAME
    assert second.status is ClientStatus.ONLINE
    assert second.runtime is ClientRuntime.local
    assert second.token_hash != hash_client_token("local-client-token")
    assert second.connected_at is not None
    assert second.last_seen_at is not None


@pytest.mark.asyncio
async def test_ensure_local_client_recovers_from_concurrent_insert(session, monkeypatch):
    existing = Client(
        id=LOCAL_CLIENT_ID,
        name=LOCAL_CLIENT_NAME,
        status=ClientStatus.OFFLINE,
        token_hash=hash_client_token("legacy-local-token"),
        runtime=ClientRuntime.remote,
    )
    session.add(existing)
    await session.commit()
    session.expunge(existing)

    original_get_local_client = clients_repo.get_local_client
    calls = 0

    async def race_get_local_client(db_session):
        nonlocal calls
        calls += 1
        if calls == 1:
            return None
        return await original_get_local_client(db_session)

    monkeypatch.setattr(clients_repo, "get_local_client", race_get_local_client)

    client = await ensure_local_client(session)
    await session.commit()

    assert client.id == LOCAL_CLIENT_ID
    assert client.status is ClientStatus.ONLINE
    assert client.runtime is ClientRuntime.local
    assert client.token_hash != hash_client_token("local-client-token")
    assert client.connected_at is not None
    assert client.last_seen_at is not None
    assert len(list(await session.scalars(select(Client)))) == 1


@pytest.mark.asyncio
async def test_create_or_rotate_remote_client_by_name_reuses_existing_client_and_rotates_token(session):
    first, first_token, first_reused = await create_or_rotate_remote_client_by_name(
        session,
        name="office-mac-mini",
        hostname="old-host",
        install_path="/old/path",
    )
    first_id = first.id
    first_hash = first.token_hash
    await session.commit()

    second, second_token, reused = await create_or_rotate_remote_client_by_name(
        session,
        name="office-mac-mini",
        hostname="new-host",
        install_path="/new/path",
    )
    await session.commit()

    clients = list(await session.scalars(select(Client)))
    assert first_reused is False
    assert reused is True
    assert second.id == first_id
    assert second_token != first_token
    assert second.token_hash != first_hash
    assert verify_client_token(second_token, second.token_hash) is True
    assert second.hostname == "new-host"
    assert second.install_path == "/new/path"
    assert second.status is ClientStatus.OFFLINE
    assert len(clients) == 1
