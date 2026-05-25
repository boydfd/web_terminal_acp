from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from app.models import EventSourceType

MAX_FINGERPRINT_LENGTH = 128
MAX_SOURCE_ID_LENGTH = 512
MAX_KIND_LENGTH = 128
_HASH_SUFFIX_LENGTH = 16

_CLAUDE_KIND_BY_TYPE = {
    "user": "user_message",
    "assistant": "assistant_message",
    "tool_use": "tool_call",
    "tool_result": "tool_result",
}


@dataclass(frozen=True)
class NormalizedEvent:
    source_type: EventSourceType
    source_id: str
    kind: str
    payload_json: dict[str, Any]
    fingerprint: str
    text: str


def _stable_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _stable_hash(value: Any) -> str:
    return hashlib.sha256(_stable_json(value).encode("utf-8")).hexdigest()


def _bounded_fingerprint(prefix: str, *components: Any) -> str:
    candidate = f"{prefix}:{':'.join(str(component) for component in components)}"
    if len(candidate) <= MAX_FINGERPRINT_LENGTH:
        return candidate
    return f"{prefix}:{_stable_hash(components)}"


def _bounded_provider_string(value: str, max_length: int) -> str:
    if len(value) <= max_length:
        return value

    suffix = f":{_stable_hash(value)[:_HASH_SUFFIX_LENGTH]}"
    prefix_length = max_length - len(suffix)
    return f"{value[:prefix_length]}{suffix}"


def _event_source_id(value: Any) -> str:
    return _bounded_provider_string(str(value), MAX_SOURCE_ID_LENGTH)


def _event_kind(value: Any) -> str:
    return _bounded_provider_string(str(value), MAX_KIND_LENGTH)


def _json_text(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False)


def _content_text(content: Any, fallback: Any) -> str:
    if isinstance(content, str):
        return content

    if isinstance(content, list):
        text_parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                text_parts.append(block)
                continue
            if not isinstance(block, dict):
                continue
            text = block.get("text")
            if isinstance(text, str):
                text_parts.append(text)
        if text_parts:
            return "\n".join(text_parts)
        return _json_text(content)

    if content is not None:
        return _json_text(content)
    return _json_text(fallback)


def normalize_claude_jsonl(raw: dict[str, Any], source_path: str, offset: int) -> NormalizedEvent:
    raw_type = raw.get("type")
    raw_kind = _CLAUDE_KIND_BY_TYPE.get(raw_type, raw_type) if isinstance(raw_type, str) else "event"
    kind = _event_kind(raw_kind)
    source_id = _event_source_id(raw.get("sessionId") or raw.get("session_id") or source_path)

    message = raw.get("message")
    content = message.get("content") if isinstance(message, dict) else None

    return NormalizedEvent(
        source_type=EventSourceType.claude_jsonl,
        source_id=source_id,
        kind=kind,
        payload_json=raw,
        fingerprint=_bounded_fingerprint("claude_jsonl", source_path, offset, _stable_hash(raw)),
        text=_content_text(content, raw),
    )


def normalize_codex_trace(raw: dict[str, Any]) -> NormalizedEvent:
    source_id = _event_source_id(raw.get("trace_id") or raw.get("id") or _stable_hash(raw))
    span = raw.get("span")
    span_name = span.get("name") if isinstance(span, dict) else None
    raw_name = raw.get("name")
    raw_kind = span_name if isinstance(span_name, str) else raw_name if isinstance(raw_name, str) else "trace"
    kind = _event_kind(raw_kind)

    attributes = span.get("attributes") if isinstance(span, dict) else None
    text_source = attributes if isinstance(attributes, dict) else raw

    return NormalizedEvent(
        source_type=EventSourceType.codex_trace,
        source_id=source_id,
        kind=kind,
        payload_json=raw,
        fingerprint=_bounded_fingerprint("codex_trace", source_id, _stable_hash(raw)),
        text=_json_text(text_source),
    )
