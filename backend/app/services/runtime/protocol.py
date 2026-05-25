from __future__ import annotations

import base64
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field


class AgentMessage(BaseModel):
    type: str
    client_id: UUID
    window_id: UUID | None = None
    request_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


class TerminalPayload(BaseModel):
    window_id: UUID
    data: str

    @classmethod
    def from_bytes(cls, window_id: UUID, data: bytes) -> TerminalPayload:
        return cls(window_id=window_id, data=base64.b64encode(data).decode("ascii"))

    def to_bytes(self) -> bytes:
        return base64.b64decode(self.data.encode("ascii"))


def encode_agent_message(message: AgentMessage) -> str:
    return message.model_dump_json()


def decode_agent_message(data: str | bytes | dict[str, Any]) -> AgentMessage:
    if isinstance(data, dict):
        return AgentMessage.model_validate(data)
    if isinstance(data, bytes):
        data = data.decode("utf-8")
    return AgentMessage.model_validate_json(data)
