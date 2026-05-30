from __future__ import annotations

from datetime import UTC, datetime

from app.models import Event


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
    if payload_text(payload.get("provider")) == "claude_code" and claude_completion_payload(payload):
        return True
    return codex_completion_payload(payload) or claude_completion_payload(payload)


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
    message = payload.get("message")
    if not isinstance(message, dict):
        message = payload
    stop_reason = payload_text(message.get("stop_reason")) or payload_text(payload.get("stop_reason"))
    if stop_reason != "end_turn":
        return False
    role = payload_text(message.get("role")) or payload_text(payload.get("type"))
    return role == "assistant" or payload_text(payload.get("type")) == "assistant"


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
