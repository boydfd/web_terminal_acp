from __future__ import annotations

from datetime import UTC, datetime

from app.agent_tools import get_agent_tool_registry
from app.agent_tools.user_input import extract_real_user_input
from app.models import Event, EventSourceType

_PROVIDER_ALIASES = {
    "agent": "cursor_cli",
    "claude": "claude_code",
    "cursor": "cursor_cli",
}
_CLAUDE_LOCAL_COMMAND_PREFIXES = (
    "<local-command-caveat>",
    "<bash-input>",
    "<bash-stdout>",
    "<bash-stderr>",
)


def event_activity_time(event: Event) -> datetime:
    payload_time = payload_datetime(
        event.payload_json.get("timestamp")
        or event.payload_json.get("created_at")
        or event.payload_json.get("createdAt")
    )
    return payload_time or aware_utc(event.created_at)


def event_is_agent_completion(event: Event) -> bool:
    payload = event.payload_json
    if payload_text(payload.get("provider")) == "codex" and codex_completion_payload(payload):
        return True
    if payload_text(payload.get("provider")) in {"claude", "claude_code"} and claude_completion_payload(payload):
        return True
    return codex_completion_payload(payload) or claude_completion_payload(payload)


def event_is_agent_activity(event: Event) -> bool:
    payload = event.payload_json
    provider = payload_text(payload.get("provider"))
    canonical_provider = _canonical_provider(provider)
    if event_is_agent_user_input(event) or event_is_agent_completion(event):
        return True
    if canonical_provider == "codex" or event.source_type == EventSourceType.codex_trace:
        return codex_work_activity_payload(payload)
    if canonical_provider == "claude_code" or event.source_type == EventSourceType.claude_jsonl:
        return claude_work_activity_payload(payload)
    if canonical_provider == "cursor_cli":
        return cursor_work_activity_payload(payload)
    return True


def event_is_agent_user_input(event: Event) -> bool:
    payload = event.payload_json
    provider = _canonical_provider(payload_text(payload.get("provider")))
    try:
        adapter = get_agent_tool_registry().by_source_type(event.source_type, provider)
    except (KeyError, ValueError):
        body = (
            payload_text(payload.get("content"))
            or payload_text(payload.get("text"))
            or payload_text(payload.get("message"))
        )
        message = payload.get("message")
        if isinstance(message, dict):
            body = body or payload_text(message.get("content")) or payload_text(message.get("text"))
        role = _payload_role(payload)
        if event.kind not in {"user", "user_message"} and role != "user":
            return False
        return extract_real_user_input(body, provider=provider) is not None

    chat = adapter.project_chat(event)
    return chat is not None and chat.role == "user"


def codex_work_activity_payload(payload: dict) -> bool:
    raw_type = (
        payload_text(payload.get("raw_type"))
        or payload_text(payload.get("name"))
        or payload_text(payload.get("type"))
    )
    item = payload.get("payload")
    if not isinstance(item, dict):
        item = payload
    item_type = payload_text(item.get("type"))
    role = payload_text(item.get("role"))

    if raw_type in {"session_meta", "turn_context"}:
        return False
    if raw_type == "event_msg" and item_type == "token_count":
        return False
    if raw_type == "response_item" and item_type == "message" and role in {"system", "developer"}:
        return False
    if raw_type == "response_item" and item_type == "message" and role == "user":
        text = _codex_message_text(item)
        return extract_real_user_input(text, provider="codex") is not None
    if raw_type == "event_msg" and item_type == "user_message":
        return extract_real_user_input(payload_text(item.get("message")), provider="codex") is not None
    return True


def claude_work_activity_payload(payload: dict) -> bool:
    if claude_local_command_payload(payload):
        return False
    payload_type = payload_text(payload.get("type"))
    if payload_type == "system":
        return claude_completion_payload(payload)
    if payload_type == "user":
        return event_is_plain_user_payload(payload, provider="claude_code")
    return True


def cursor_work_activity_payload(payload: dict) -> bool:
    role = _payload_role(payload)
    if role == "system":
        return False
    if role == "user":
        return event_is_plain_user_payload(payload, provider="cursor_cli")
    return True


def codex_completion_payload(payload: dict) -> bool:
    raw_type = (
        payload_text(payload.get("raw_type"))
        or payload_text(payload.get("name"))
        or payload_text(payload.get("type"))
    )
    item = payload.get("payload")
    if not isinstance(item, dict):
        item = payload
    item_type = payload_text(item.get("type"))
    return raw_type == "event_msg" and item_type in {
        "task_complete",
        "task_completed",
        "turn_completed",
    }


def claude_completion_payload(payload: dict) -> bool:
    if (
        payload_text(payload.get("type")) == "system"
        and payload_text(payload.get("subtype")) == "turn_duration"
    ):
        return True

    message = payload.get("message")
    if not isinstance(message, dict):
        message = payload
    stop_reason = payload_text(message.get("stop_reason")) or payload_text(payload.get("stop_reason"))
    if stop_reason != "end_turn":
        return False
    role = payload_text(message.get("role")) or payload_text(payload.get("type"))
    return role == "assistant" or payload_text(payload.get("type")) == "assistant"


def claude_local_command_payload(payload: dict) -> bool:
    provider = payload_text(payload.get("provider"))
    if provider not in {None, "claude", "claude_code"}:
        return False
    if payload_text(payload.get("type")) != "user":
        return False

    message = payload.get("message")
    if not isinstance(message, dict):
        return False
    role = payload_text(message.get("role"))
    if role not in {None, "user"}:
        return False

    content = payload_text(message.get("content"))
    if content is None:
        return payload.get("isMeta") is True

    stripped = content.lstrip()
    return payload.get("isMeta") is True or any(
        stripped.startswith(prefix) for prefix in _CLAUDE_LOCAL_COMMAND_PREFIXES
    )


def event_is_plain_user_payload(payload: dict, *, provider: str) -> bool:
    body = payload_text(payload.get("content")) or payload_text(payload.get("text"))
    message = payload.get("message")
    if isinstance(message, dict):
        body = body or payload_text(message.get("content")) or payload_text(message.get("text"))
    return extract_real_user_input(body, provider=provider) is not None


def _codex_message_text(item: dict) -> str | None:
    content = item.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return None

    parts: list[str] = []
    for block in content:
        if isinstance(block, str):
            if block:
                parts.append(block)
            continue
        if not isinstance(block, dict):
            continue
        text = payload_text(block.get("text"))
        if text:
            parts.append(text)
    return "\n\n".join(parts) or None


def _payload_role(payload: dict) -> str | None:
    role = payload_text(payload.get("role")) or payload_text(payload.get("type"))
    if role is not None:
        return role
    message = payload.get("message")
    if isinstance(message, dict):
        return payload_text(message.get("role"))
    return None


def _canonical_provider(provider: str | None) -> str | None:
    if provider is None:
        return None
    return _PROVIDER_ALIASES.get(provider, provider)


def payload_text(value: object) -> str | None:
    if isinstance(value, str):
        value = value.strip()
        return value or None
    return None


def payload_datetime(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value:
        return None
    try:
        return aware_utc(
            datetime.fromisoformat(value.removesuffix("Z") + ("+00:00" if value.endswith("Z") else ""))
        )
    except ValueError:
        return None


def aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
