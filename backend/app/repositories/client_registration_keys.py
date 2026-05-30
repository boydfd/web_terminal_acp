from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import hmac
import secrets
from uuid import UUID

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ClientRegistrationKey, ClientRegistrationKeyStatus

_HASH_PREFIX = "sha256:"


def hash_registration_key(registration_key: str) -> str:
    digest = hashlib.sha256(registration_key.encode("utf-8")).hexdigest()
    return f"{_HASH_PREFIX}{digest}"


def verify_registration_key(registration_key: str, key_hash: str) -> bool:
    expected = hash_registration_key(registration_key)
    return hmac.compare_digest(expected, key_hash)


def generate_registration_key() -> str:
    return f"wtr_{secrets.token_urlsafe(32)}"


async def create_registration_key(
    session: AsyncSession,
    *,
    label: str | None = None,
) -> tuple[ClientRegistrationKey, str]:
    key = generate_registration_key()
    registration_key = ClientRegistrationKey(
        key_hash=hash_registration_key(key),
        status=ClientRegistrationKeyStatus.active,
        label=label,
    )
    session.add(registration_key)
    await session.flush()
    return registration_key, key


async def consume_registration_key(
    session: AsyncSession,
    *,
    registration_key: str,
    client_id: UUID,
) -> ClientRegistrationKey | None:
    key_hash = hash_registration_key(registration_key)
    statement: Select[tuple[ClientRegistrationKey]] = select(ClientRegistrationKey).where(
        ClientRegistrationKey.key_hash == key_hash,
        ClientRegistrationKey.status == ClientRegistrationKeyStatus.active,
    )
    if session.bind is not None and session.bind.dialect.name != "sqlite":
        statement = statement.with_for_update(skip_locked=True)
    key = await session.scalar(statement)
    if key is None or not verify_registration_key(registration_key, key.key_hash):
        return None
    key.status = ClientRegistrationKeyStatus.used
    key.used_client_id = client_id
    key.used_at = datetime.now(UTC)
    await session.flush()
    return key
