from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.agent_tools.common import (
    agent_tool_record_event,
    as_dict,
    fallback_projection,
    json_markdown,
    json_text,
    message_content,
    string_value,
)
from app.agent_tools.types import AgentChatProjection, AgentEventProjection, AgentToolStorage
from app.agent_tools.user_input import extract_real_user_input
from app.models import Event, EventSourceType
from app.services.ingest.normalizers import NormalizedEvent, normalize_codex_trace

_EVENT_LABELS = {
    "base-instructions": "Base instructions",
    "context": "Context",
    "user-input": "User input",
    "agent": "Agent response",
    "system": "System message",
    "developer": "Developer instructions",
    "reasoning": "Agent reasoning",
    "tool-call": "Tool call",
    "tool-result": "Tool response",
    "lifecycle": "Lifecycle",
    "event": "Event",
}


def _raw_type(payload: dict[str, Any]) -> str | None:
    # Raw watcher-shaped events keep the Codex event discriminator on the wrapper;
    # nested payload["type"] is the response item or event_msg subtype.
    return (
        string_value(payload.get("raw_type"))
        or string_value(payload.get("name"))
        or string_value(payload.get("type"))
    )


def _item(payload: dict[str, Any]) -> dict[str, Any]:
    nested = payload.get("payload")
    return nested if isinstance(nested, dict) else payload


def _subtype(*parts: str | None) -> str | None:
    value = " · ".join(part for part in parts if part)
    return value or None


def _content_text(value: Any) -> str | None:
    if isinstance(value, str):
        return value if value else None
    if not isinstance(value, list):
        return None

    parts: list[str] = []
    for block in value:
        if isinstance(block, str) and block:
            parts.append(block)
            continue
        if not isinstance(block, dict):
            continue
        text = string_value(block.get("text"))
        if text:
            parts.append(text)
            continue
        nested = _content_text(block.get("content"))
        if nested:
            parts.append(nested)
    return "\n\n".join(parts) or None


def _json_markdown_string(value: str) -> str:
    try:
        return json_markdown(json.loads(value))
    except json.JSONDecodeError:
        return json_markdown(value)


def _tool_result_body(item: dict[str, Any]) -> tuple[str, str]:
    if "output" in item:
        output = item["output"]
    elif "content" in item:
        output = item["content"]
    elif "result" in item:
        output = item["result"]
    else:
        output = item

    if isinstance(output, str):
        try:
            return json_markdown(json.loads(output)), "json"
        except json.JSONDecodeError:
            return output, "markdown"
    return json_markdown(output), "json"


class CodexAdapter:
    provider_id = "codex"
    source_types = (EventSourceType.agent_tool_record,)
    legacy_source_types = (EventSourceType.codex_trace,)
    command_names = ("codex",)
    ai_activity = True

    def prepare_storage(self, window_id: str) -> AgentToolStorage:
        home = Path("~/.web-terminal-acp") / "codex-homes" / window_id
        return AgentToolStorage(env={"CODEX_HOME": str(home)}, directories=(home,))

    def normalize(
        self, payload: dict[str, Any], *, source_path: str | None, cursor: str | int | None
    ) -> NormalizedEvent:
        return agent_tool_record_event(self.provider_id, normalize_codex_trace(payload))

    def project_event(self, event: Event) -> AgentEventProjection:
        wrapper = event.payload_json
        raw_type = _raw_type(wrapper)
        item = _item(wrapper)
        item_type = string_value(item.get("type"))
        role = string_value(item.get("role"))
        subtype = _subtype(raw_type, item_type, role)

        if raw_type == "session_meta":
            base_instructions = as_dict(item.get("base_instructions"))
            text = string_value(base_instructions.get("text"))
            if text:
                return AgentEventProjection(
                    tone="base-instructions",
                    label=_EVENT_LABELS["base-instructions"],
                    body=text,
                    subtype=subtype,
                )
            return AgentEventProjection(
                tone="context",
                label=_EVENT_LABELS["context"],
                body=json_markdown(item),
                body_format="json",
                subtype=subtype,
            )

        if raw_type == "turn_context":
            return AgentEventProjection(
                tone="context",
                label=_EVENT_LABELS["context"],
                body=json_markdown(item),
                body_format="json",
                subtype=subtype,
            )

        if raw_type == "response_item" and item_type == "message":
            content_body = _content_text(item.get("content"))
            body = content_body or json_markdown(item)
            body_format = "markdown" if content_body else "json"
            if role == "user":
                real_user_input = extract_real_user_input(content_body, provider=self.provider_id)
                if real_user_input is None and content_body:
                    return AgentEventProjection(
                        tone="context",
                        label=_EVENT_LABELS["context"],
                        body=body,
                        body_format=body_format,
                        subtype=subtype,
                    )
                body = real_user_input or body
                body_format = "markdown" if real_user_input is not None else body_format
                return AgentEventProjection(
                    tone="user-input",
                    label=_EVENT_LABELS["user-input"],
                    body=body,
                    body_format=body_format,
                    subtype=subtype,
                )
            if role == "assistant":
                return AgentEventProjection(
                    tone="agent",
                    label=_EVENT_LABELS["agent"],
                    body=body,
                    body_format=body_format,
                    subtype=subtype,
                )
            if role in {"system", "developer"}:
                return AgentEventProjection(
                    tone=role,
                    label=_EVENT_LABELS[role],
                    body=body,
                    body_format=body_format,
                    subtype=subtype,
                )

        if raw_type == "response_item" and item_type == "function_call":
            name = string_value(item.get("name")) or "tool"
            args = item.get("arguments") if "arguments" in item else item.get("input", {})
            args_body = _json_markdown_string(args) if isinstance(args, str) else json_markdown(args)
            return AgentEventProjection(
                tone="tool-call",
                label=_EVENT_LABELS["tool-call"],
                body=f"{name}\n\n{args_body}",
                subtype=subtype,
            )

        if raw_type == "response_item" and item_type == "function_call_output":
            body, body_format = _tool_result_body(item)
            return AgentEventProjection(
                tone="tool-result",
                label=_EVENT_LABELS["tool-result"],
                body=body,
                body_format=body_format,
                subtype=subtype,
            )

        if raw_type == "response_item" and item_type == "reasoning":
            body = (
                _content_text(item.get("summary"))
                or _content_text(item.get("content"))
                or json_markdown(item)
            )
            body_format = "markdown" if not body.startswith("```json") else "json"
            return AgentEventProjection(
                tone="reasoning",
                label=_EVENT_LABELS["reasoning"],
                body=body,
                body_format=body_format,
                subtype=subtype,
            )

        if raw_type == "event_msg" and item_type == "user_message":
            body = string_value(item.get("message")) or json_markdown(item)
            real_user_input = extract_real_user_input(body, provider=self.provider_id)
            if real_user_input is None:
                return AgentEventProjection(
                    tone="context",
                    label=_EVENT_LABELS["context"],
                    body=body,
                    subtype=subtype,
                )
            body = real_user_input
            return AgentEventProjection(
                tone="user-input",
                label=_EVENT_LABELS["user-input"],
                body=body,
                subtype=subtype,
            )

        if raw_type == "event_msg" and item_type == "agent_message":
            return AgentEventProjection(
                tone="agent",
                label=_EVENT_LABELS["agent"],
                body=string_value(item.get("message")) or json_markdown(item),
                subtype=subtype,
            )

        if raw_type == "event_msg" and item_type and "exec_command" in item_type:
            if item_type.endswith("end"):
                body, body_format = _tool_result_body(item)
                tone = "tool-result"
            else:
                body, body_format = json_markdown(item), "json"
                tone = "tool-call"
            return AgentEventProjection(
                tone=tone,
                label=_EVENT_LABELS[tone],
                body=body,
                body_format=body_format,
                subtype=subtype,
            )

        if raw_type == "event_msg" and item_type and (
            item_type.startswith("task_") or item_type == "token_count"
        ):
            return AgentEventProjection(
                tone="lifecycle",
                label=_EVENT_LABELS["lifecycle"],
                body=json_markdown(item),
                body_format="json",
                subtype=subtype,
            )

        return fallback_projection(event)

    def project_chat(self, event: Event) -> AgentChatProjection | None:
        payload = event.payload_json
        raw_type = _raw_type(payload)
        item = _item(payload)
        item_type = string_value(item.get("type"))
        source = str(event.ai_session_id or event.source_id)

        if raw_type == "response_item" and item_type == "message":
            role = string_value(item.get("role"))
            body = _content_text(item.get("content"))
            if role == "user" and body:
                body = extract_real_user_input(body, provider=self.provider_id)
                if body is None:
                    return None
                return AgentChatProjection(
                    role="user",
                    body=body,
                    dedupe_key=f"{source}:user:{body}",
                    is_canonical=True,
                )
            if role == "assistant" and body:
                return AgentChatProjection(
                    role="agent",
                    body=body,
                    dedupe_key=f"{source}:assistant:{body}",
                    is_canonical=True,
                )

        if raw_type == "event_msg" and item_type in {"user_message", "agent_message"}:
            body = string_value(item.get("message"))
            if not body:
                return None
            semantic_role = "user" if item_type == "user_message" else "assistant"
            role = "user" if item_type == "user_message" else "agent"
            if role == "user":
                body = extract_real_user_input(body, provider=self.provider_id)
                if body is None:
                    return None
            return AgentChatProjection(
                role=role,
                body=body,
                dedupe_key=f"{source}:{semantic_role}:{body}",
                is_canonical=False,
                is_duplicate_candidate=True,
            )

        return None

    def is_completion(self, event: Event) -> bool:
        raw_type = _raw_type(event.payload_json)
        item_type = string_value(_item(event.payload_json).get("type"))
        return raw_type == "event_msg" and item_type in {
            "task_complete",
            "task_completed",
            "turn_completed",
        }

    def summary_text(self, event: Event) -> str:
        chat = self.project_chat(event)
        if chat is not None:
            return chat.body
        projection = self.project_event(event)
        if projection.body_format == "markdown":
            return projection.body
        return message_content(event.payload_json) or normalize_codex_trace(event.payload_json).text or json_text(
            event.payload_json
        )

    def index_text(self, event: Event) -> str:
        return self.summary_text(event)
