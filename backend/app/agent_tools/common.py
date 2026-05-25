from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import asdict, is_dataclass
from typing import Any

from app.models import Event, EventSourceType
from app.services.ingest.normalizers import NormalizedEvent

from .types import AgentEventProjection


def as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    return {}


def string_value(value: Any) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)):
        return str(value)
    return None


def json_text(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False)


def stable_hash(value: Any) -> str:
    stable_json = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(stable_json.encode("utf-8")).hexdigest()


def provider_scoped_fingerprint(
    legacy_fingerprint: str, *, legacy_prefix: str, provider: str
) -> str:
    prefix = f"agent_tool_record:{provider}:"
    legacy_body = legacy_fingerprint
    if legacy_fingerprint.startswith(f"{legacy_prefix}:"):
        legacy_body = legacy_fingerprint.removeprefix(f"{legacy_prefix}:")

    candidate = prefix + legacy_body
    if len(candidate) <= 128:
        return candidate
    return prefix + stable_hash(legacy_fingerprint)


def agent_tool_record_event(provider: str, legacy_event: NormalizedEvent) -> NormalizedEvent:
    return NormalizedEvent(
        source_type=EventSourceType.agent_tool_record,
        source_id=legacy_event.source_id,
        kind=legacy_event.kind,
        payload_json={**legacy_event.payload_json, "provider": provider},
        fingerprint=provider_scoped_fingerprint(
            legacy_event.fingerprint,
            legacy_prefix=legacy_event.source_type.value,
            provider=provider,
        ),
        text=legacy_event.text,
    )


def json_markdown(value: Any) -> str:
    return f"```json\n{json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False)}\n```"


def block_text(block: Any) -> str | None:
    if isinstance(block, str):
        return block
    if not isinstance(block, Mapping):
        return string_value(block)

    text = string_value(block.get("text"))
    if text is not None:
        return text

    content = block.get("content")
    if content is not None:
        return content_text(content)

    return None


def content_text(content: Any) -> str:
    if isinstance(content, str):
        return content

    if isinstance(content, list):
        parts = [part for item in content if (part := block_text(item))]
        if parts:
            return "\n".join(parts)
        return json_text(content)

    text = string_value(content)
    if text is not None:
        return text

    return json_text(content)


def message_content(payload: Mapping[str, Any]) -> str | None:
    message = payload.get("message")
    if isinstance(message, Mapping) and "content" in message:
        return content_text(message.get("content"))

    if "content" in payload:
        return content_text(payload.get("content"))

    return None


def fallback_projection(event: Event) -> AgentEventProjection:
    raw_payload = event.payload_json
    payload = raw_payload if isinstance(raw_payload, Mapping) else {"payload": raw_payload}
    body = message_content(payload) or string_value(payload.get("text"))
    body_format = "markdown"

    if event.kind == "terminal_input_command":
        body = string_value(payload.get("command")) or body
        if body is None:
            body = json_markdown(raw_payload)
            body_format = "json"
        return AgentEventProjection(
            tone="terminal",
            label="Terminal command",
            body=body,
            body_format=body_format,
            subtype="command",
        )

    if body is None:
        body = json_markdown(raw_payload)
        body_format = "json"

    if event.kind in {"user", "user_message"}:
        return AgentEventProjection(
            tone="user", label="User", body=body, body_format=body_format, subtype="message"
        )

    if event.kind in {"assistant", "assistant_message"}:
        return AgentEventProjection(
            tone="assistant",
            label="Assistant",
            body=body,
            body_format=body_format,
            subtype="message",
        )

    if event.kind in {"tool", "tool_call", "tool_use"} or event.kind.startswith("tool_"):
        return AgentEventProjection(
            tone="tool", label="Tool", body=body, body_format=body_format, subtype=event.kind
        )

    return AgentEventProjection(
        tone="event", label="Event", body=body, body_format=body_format, subtype=event.kind
    )
