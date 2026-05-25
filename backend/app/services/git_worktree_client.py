from __future__ import annotations

import uuid
from typing import Any
from uuid import UUID

from app.services.runtime.client_connections import ClientConnectionClosed, ClientConnectionRegistry
from app.services.runtime.protocol import AgentMessage


async def request_git_worktree_action(
    registry: ClientConnectionRegistry | None,
    client_id: UUID,
    *,
    action: str,
    timeout: float = 15.0,
    **payload: Any,
) -> dict[str, Any] | None:
    if registry is None:
        return None
    connection = registry.get(client_id)
    if connection is None or connection.closed:
        return None

    request_id = str(uuid.uuid4())
    message = AgentMessage(
        type="git_worktree_request",
        client_id=client_id,
        request_id=request_id,
        payload={"action": action, **payload},
    )
    try:
        response = await connection.request(message, timeout=timeout)
    except (ClientConnectionClosed, TimeoutError):
        return None
    if response.type != "git_worktree_result":
        return None
    result = response.payload
    if not isinstance(result, dict):
        return None
    return result
