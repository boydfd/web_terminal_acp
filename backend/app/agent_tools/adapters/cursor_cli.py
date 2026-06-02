from __future__ import annotations

from pathlib import Path
from typing import Any

from app.agent_tools.common import content_text, fallback_projection, json_text, message_content, stable_hash, string_value
from app.client_agent.cursor_watcher import read_cursor_store_events as read_cursor_store_events
from app.agent_tools.types import AgentChatProjection, AgentEventProjection, AgentToolStorage
from app.agent_tools.user_input import extract_real_user_input
from app.models import Event, EventSourceType
from app.services.ingest.normalizers import NormalizedEvent

_MAX_SOURCE_ID_LENGTH = 512
_MAX_KIND_LENGTH = 128
_HASH_SUFFIX_LENGTH = 16
_MISSING_SOURCE_PATH = "<unknown-source-path>"


def _bounded_string(value: str, max_length: int) -> str:
    if len(value) <= max_length:
        return value

    suffix = f":{stable_hash(value)[:_HASH_SUFFIX_LENGTH]}"
    return f"{value[: max_length - len(suffix)]}{suffix}"


def _message_text(payload: dict[str, Any]) -> str | None:
    text = string_value(payload.get("text"))
    if text:
        return text
    if "content" in payload:
        content = content_text(payload.get("content"))
        if content:
            return content
    content = message_content(payload)
    return content if content else None


def _message_kind(role: str | None) -> str:
    if role == "user":
        return "user_message"
    if role == "assistant":
        return "assistant_message"
    if role == "system":
        return "system_message"
    return "message"


def _fingerprint_payload(payload: dict[str, Any]) -> dict[str, Any]:
    stable_payload = dict(payload)
    stable_payload.pop("root_blob_id", None)
    return stable_payload


class CursorCliAdapter:
    provider_id = "cursor_cli"
    source_types = (EventSourceType.agent_tool_record,)
    legacy_source_types = ()
    command_names = ("agent", "cursor")
    ai_activity = True

    def prepare_storage(self, window_id: str) -> AgentToolStorage:
        home = Path("~/.web-terminal-acp") / "cursor-homes" / window_id
        return AgentToolStorage(
            env={
                "CURSOR_AGENT_HOME": str(home),
                "CURSOR_CONFIG_DIR": str(home),
                "CURSOR_DATA_DIR": str(home),
            },
            directories=(home,),
        )

    def normalize(
        self, payload: dict[str, Any], *, source_path: str | None, cursor: str | int | None
    ) -> NormalizedEvent:
        source_id = _bounded_string(
            str(payload.get("agentId") or payload.get("session_id") or payload.get("source_id") or source_path or self.provider_id),
            _MAX_SOURCE_ID_LENGTH,
        )
        role = string_value(payload.get("role"))
        kind = _bounded_string(
            str(payload.get("kind") or payload.get("type") or _message_kind(role)), _MAX_KIND_LENGTH
        )
        payload_json = {**payload, "provider": self.provider_id}
        text = _message_text(payload_json) or json_text(payload_json)
        fingerprint = "agent_tool_record:cursor_cli:" + stable_hash(
            {
                "provider": self.provider_id,
                "source_path": source_path or _MISSING_SOURCE_PATH,
                "blob_id": payload.get("blob_id"),
                "payload_hash": stable_hash(_fingerprint_payload(payload)),
            }
        )
        return NormalizedEvent(
            source_type=EventSourceType.agent_tool_record,
            source_id=source_id,
            kind=kind,
            payload_json=payload_json,
            fingerprint=fingerprint,
            text=text,
        )

    def project_event(self, event: Event) -> AgentEventProjection:
        role = string_value(event.payload_json.get("role"))
        body = _message_text(event.payload_json)
        if body and role == "user":
            real_user_input = extract_real_user_input(body, provider=self.provider_id)
            if real_user_input is None:
                return AgentEventProjection("context", "Context", body, subtype=event.kind)
            body = real_user_input
            return AgentEventProjection("user-input", "User input", body, subtype=event.kind)
        if body and role == "assistant":
            return AgentEventProjection("agent", "Agent response", body, subtype=event.kind)
        if body and role == "system":
            return AgentEventProjection("system", "System message", body, subtype=event.kind)
        return fallback_projection(event)

    def project_chat(self, event: Event) -> AgentChatProjection | None:
        role = string_value(event.payload_json.get("role"))
        body = _message_text(event.payload_json)
        if not body:
            return None
        source = str(event.ai_session_id or event.source_id)
        if role == "user":
            body = extract_real_user_input(body, provider=self.provider_id)
            if body is None:
                return None
            return AgentChatProjection("user", body, dedupe_key=f"{source}:user:{body}")
        if role == "assistant":
            return AgentChatProjection("agent", body, dedupe_key=f"{source}:assistant:{body}")
        return None

    def is_completion(self, event: Event) -> bool:
        return string_value(event.payload_json.get("role")) == "assistant" and _message_text(event.payload_json) is not None

    def summary_text(self, event: Event) -> str:
        return _message_text(event.payload_json) or json_text(event.payload_json)

    def index_text(self, event: Event) -> str:
        return self.summary_text(event)
