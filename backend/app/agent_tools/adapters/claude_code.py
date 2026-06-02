from __future__ import annotations

from pathlib import Path
from typing import Any

from app.agent_tools.common import (
    agent_tool_record_event,
    content_text,
    fallback_projection,
    json_markdown,
    json_text,
    message_content,
    string_value,
)
from app.agent_tools.types import AgentChatProjection, AgentEventProjection, AgentToolStorage
from app.agent_tools.user_input import extract_real_user_input
from app.models import Event, EventSourceType
from app.services.ingest.normalizers import NormalizedEvent, normalize_claude_jsonl


def _message(payload: dict[str, Any]) -> dict[str, Any]:
    message = payload.get("message")
    return message if isinstance(message, dict) else {}


def _message_role(payload: dict[str, Any]) -> str | None:
    message_role = string_value(_message(payload).get("role"))
    return message_role or string_value(payload.get("type"))


def _raw_content(payload: dict[str, Any]) -> Any:
    message = _message(payload)
    if "content" in message:
        return message.get("content")
    return payload.get("content")


def _body_text(payload: dict[str, Any]) -> str | None:
    content = _raw_content(payload)
    if content is None:
        return None
    text = content_text(content)
    return text if text else None


def _chat_body_text(payload: dict[str, Any]) -> str | None:
    content = _raw_content(payload)
    if isinstance(content, str):
        return content if content else None
    if isinstance(content, dict):
        block_type = string_value(content.get("type"))
        if block_type in {"tool_use", "tool_result"}:
            return None
        text = string_value(content.get("text"))
        return text if text else None
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
        block_type = string_value(block.get("type"))
        if block_type in {"tool_use", "tool_result"}:
            continue
        if block_type and block_type != "text":
            continue
        text = string_value(block.get("text"))
        if text:
            parts.append(text)
    return "\n".join(parts) or None


def _tool_use_blocks(payload: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        block for block in _content_blocks(payload)
        if string_value(block.get("type")) == "tool_use"
    ]


def _tool_result_blocks(payload: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        block for block in _content_blocks(payload)
        if string_value(block.get("type")) == "tool_result"
    ]


def _agent_tool_use_block(payload: dict[str, Any]) -> dict[str, Any] | None:
    for block in _tool_use_blocks(payload):
        if string_value(block.get("name")) == "Agent":
            return block
    return None


def _agent_tool_result_block(payload: dict[str, Any]) -> dict[str, Any] | None:
    tool_use_id = _payload_subagent_tool_use_id(payload)
    for block in _tool_result_blocks(payload):
        if tool_use_id is None or string_value(block.get("tool_use_id")) == tool_use_id:
            return block
    return None


def _payload_subagent_tool_use_id(payload: dict[str, Any]) -> str | None:
    value = string_value(payload.get("subagent_tool_use_id")) or string_value(payload.get("subagentToolUseId"))
    if value:
        return value
    metadata = payload.get("subagent")
    if isinstance(metadata, dict):
        value = string_value(metadata.get("tool_use_id")) or string_value(metadata.get("toolUseId"))
        if value:
            return value
    tool_use_result = payload.get("toolUseResult")
    if isinstance(tool_use_result, dict):
        value = string_value(tool_use_result.get("toolUseId"))
        if value:
            return value
    for block in _tool_result_blocks(payload):
        value = string_value(block.get("tool_use_id"))
        if value:
            return value
    return None


def _payload_subagent_id(payload: dict[str, Any]) -> str | None:
    value = string_value(payload.get("agentId")) or string_value(payload.get("subagent_id")) or string_value(payload.get("subagentId"))
    if value:
        return value
    metadata = payload.get("subagent")
    if isinstance(metadata, dict):
        value = string_value(metadata.get("agent_id")) or string_value(metadata.get("agentId"))
        if value:
            return value
    tool_use_result = payload.get("toolUseResult")
    if isinstance(tool_use_result, dict):
        value = string_value(tool_use_result.get("agentId"))
        if value:
            return value
    return None


def _subagent_source_id(agent_id: str | None) -> str | None:
    return f"agent-{agent_id}" if agent_id else None


def _subagent_id_for_tool_use(payload: dict[str, Any], tool_use_id: str | None) -> str | None:
    if tool_use_id is None:
        return None
    matches = payload.get("subagent_tool_use_results")
    if not isinstance(matches, list):
        return None
    for item in matches:
        if not isinstance(item, dict):
            continue
        item_tool_use_id = string_value(item.get("tool_use_id")) or string_value(item.get("toolUseId"))
        if item_tool_use_id == tool_use_id:
            return string_value(item.get("agent_id")) or string_value(item.get("agentId"))
    return None


def _subagent_call_projection(payload: dict[str, Any], event_kind: str) -> AgentEventProjection | None:
    block = _agent_tool_use_block(payload)
    if block is None:
        return None
    input_payload = block.get("input")
    input_dict = input_payload if isinstance(input_payload, dict) else {}
    prompt = string_value(input_dict.get("prompt"))
    description = string_value(input_dict.get("description"))
    subagent_type = string_value(input_dict.get("subagent_type"))
    tool_use_id = string_value(block.get("id"))
    agent_id = _subagent_id_for_tool_use(payload, tool_use_id)
    parts = []
    if description:
        parts.append(f"Description: {description}")
    if subagent_type:
        parts.append(f"Type: {subagent_type}")
    if prompt:
        parts.append(prompt)
    body = "\n\n".join(parts) or json_markdown(input_payload or {})
    return AgentEventProjection(
        tone="subagent-call",
        label="Subagent call",
        body=body,
        body_format="markdown" if parts else "json",
        subtype=event_kind,
        agent_message_type="subagent_call",
        subagent_id=agent_id,
        subagent_tool_use_id=tool_use_id,
        target_session_source_id=_subagent_source_id(agent_id),
    )


def _tool_result_content_text(block: dict[str, Any]) -> str:
    content = block.get("content")
    if isinstance(content, str):
        return content
    return content_text(content) if content is not None else json_markdown(block)


def _subagent_result_projection(payload: dict[str, Any], event_kind: str) -> AgentEventProjection | None:
    block = _agent_tool_result_block(payload)
    if block is None:
        return None
    agent_id = _payload_subagent_id(payload)
    if agent_id is None:
        return None
    tool_use_id = _payload_subagent_tool_use_id(payload) or string_value(block.get("tool_use_id"))
    body = _tool_result_content_text(block)
    if agent_id:
        body = _strip_subagent_usage_tail(body)
    return AgentEventProjection(
        tone="subagent-result",
        label="Subagent result",
        body=body,
        body_format="markdown",
        subtype=event_kind,
        agent_message_type="subagent_result",
        subagent_id=agent_id,
        subagent_tool_use_id=tool_use_id,
        target_session_source_id=_subagent_source_id(agent_id),
    )


def _strip_subagent_usage_tail(body: str) -> str:
    marker = "\nagentId:"
    index = body.find(marker)
    if index >= 0:
        return body[:index].strip()
    return body.strip()


def _subagent_call_chat_projection(event: Event) -> AgentChatProjection | None:
    projection = _subagent_call_projection(event.payload_json, event.kind)
    if projection is None:
        return None
    source = _subagent_call_source_id(event)
    key = f"{source}:subagent-call:{projection.subagent_tool_use_id or projection.body}"
    return AgentChatProjection(
        "agent",
        projection.body,
        projection.body_format,
        dedupe_key=key,
        agent_message_type="subagent_call",
        subagent_id=projection.subagent_id,
        subagent_tool_use_id=projection.subagent_tool_use_id,
        target_session_source_id=projection.target_session_source_id,
    )


def _subagent_prompt_chat_projection(event: Event, body: str) -> AgentChatProjection | None:
    payload = event.payload_json
    if payload.get("isSidechain") is not True:
        return None
    agent_id = _payload_subagent_id(payload)
    if agent_id is None:
        return None
    tool_use_id = _payload_subagent_tool_use_id(payload)
    source = _subagent_call_source_id(event)
    key = f"{source}:subagent-call:{tool_use_id or body}"
    return AgentChatProjection(
        "agent",
        body,
        dedupe_key=key,
        is_canonical=False,
        is_duplicate_candidate=True,
        agent_message_type="subagent_call",
        subagent_id=agent_id,
        subagent_tool_use_id=tool_use_id,
    )


def _subagent_call_source_id(event: Event) -> str:
    return string_value(event.payload_json.get("sessionId")) or str(event.source_id)


def _subagent_result_chat_projection(event: Event) -> AgentChatProjection | None:
    projection = _subagent_result_projection(event.payload_json, event.kind)
    if projection is None:
        return None
    source = str(event.ai_session_id or event.source_id)
    key = f"{source}:subagent-result:{projection.subagent_tool_use_id or event.id}"
    return AgentChatProjection(
        "agent",
        projection.body,
        projection.body_format,
        dedupe_key=key,
        agent_message_type="subagent_result",
        subagent_id=projection.subagent_id,
        subagent_tool_use_id=projection.subagent_tool_use_id,
        target_session_source_id=projection.target_session_source_id,
    )


def _has_tool_result_content(payload: dict[str, Any]) -> bool:
    content = _raw_content(payload)
    if isinstance(content, dict):
        return string_value(content.get("type")) == "tool_result"
    if not isinstance(content, list):
        return False
    return any(isinstance(block, dict) and string_value(block.get("type")) == "tool_result" for block in content)


def _content_blocks(payload: dict[str, Any]) -> list[dict[str, Any]]:
    content = _raw_content(payload)
    if not isinstance(content, list):
        return []
    return [block for block in content if isinstance(block, dict)]


def _tool_blocks(payload: dict[str, Any]) -> list[dict[str, Any]]:
    blocks = []
    for block in _content_blocks(payload):
        block_type = string_value(block.get("type"))
        if block_type in {"tool_use", "tool_result"}:
            blocks.append(block)
    return blocks


class ClaudeCodeAdapter:
    provider_id = "claude_code"
    source_types = (EventSourceType.agent_tool_record,)
    legacy_source_types = (EventSourceType.claude_jsonl,)
    command_names = ("claude",)
    ai_activity = True

    def prepare_storage(self, window_id: str) -> AgentToolStorage:
        home = Path("~/.web-terminal-acp") / "claude-code-homes" / window_id
        return AgentToolStorage(env={"CLAUDE_CONFIG_DIR": str(home)}, directories=(home,))

    def normalize(
        self, payload: dict[str, Any], *, source_path: str | None, cursor: str | int | None
    ) -> NormalizedEvent:
        resolved_source_path = str(
            payload.get("source_path") or payload.get("path") or source_path or self.provider_id
        )
        offset = cursor if isinstance(cursor, int) else payload.get("offset")
        if not isinstance(offset, int):
            offset = int(offset) if isinstance(offset, str) and offset.isdigit() else 0
        legacy_event = normalize_claude_jsonl(payload, source_path=resolved_source_path, offset=offset)
        subagent_source_id = _subagent_source_id(_payload_subagent_id(payload))
        if subagent_source_id is not None and payload.get("isSidechain") is True:
            legacy_event = NormalizedEvent(
                source_type=legacy_event.source_type,
                source_id=subagent_source_id,
                kind=legacy_event.kind,
                payload_json=legacy_event.payload_json,
                fingerprint=legacy_event.fingerprint,
                text=legacy_event.text,
            )
        return agent_tool_record_event(
            self.provider_id,
            legacy_event,
        )

    def project_event(self, event: Event) -> AgentEventProjection:
        payload = event.payload_json
        subagent_call = _subagent_call_projection(payload, event.kind)
        if subagent_call is not None:
            return subagent_call

        subagent_result = _subagent_result_projection(payload, event.kind)
        if subagent_result is not None:
            return subagent_result

        tool_blocks = _tool_blocks(payload)
        if tool_blocks:
            first = tool_blocks[0]
            block_type = string_value(first.get("type"))
            if block_type == "tool_use":
                name = string_value(first.get("name")) or "tool"
                return AgentEventProjection(
                    tone="tool-call",
                    label="Tool call",
                    body=f"{name}\n\n{json_markdown(first.get('input') or {})}",
                    subtype=event.kind,
                )
            if block_type == "tool_result":
                content = first.get("content")
                if isinstance(content, str):
                    body = content
                    body_format = "markdown"
                else:
                    body = content_text(content) if content is not None else None
                    if body:
                        body_format = "markdown"
                    else:
                        body = json_markdown(content if content is not None else first)
                        body_format = "json"
                return AgentEventProjection(
                    tone="tool-result",
                    label="Tool response",
                    body=body,
                    body_format=body_format,
                    subtype=event.kind,
                )

        role = _message_role(payload)
        body = _body_text(payload)
        body_format = "markdown"
        if not body:
            body = json_markdown(payload)
            body_format = "json"
        if role == "user" or event.kind == "user_message":
            if payload.get("isSidechain") is True and _payload_subagent_id(payload):
                return AgentEventProjection(
                    tone="subagent-context",
                    label="Subagent prompt",
                    body=body,
                    body_format=body_format,
                    subtype=event.kind,
                )
            real_user_input = extract_real_user_input(body if body_format == "markdown" else None, provider=self.provider_id)
            if real_user_input is None and (body_format == "markdown" or _has_tool_result_content(payload)):
                label = "Tool response" if _has_tool_result_content(payload) else "Context"
                tone = "tool-result" if _has_tool_result_content(payload) else "context"
                return AgentEventProjection(
                    tone=tone,
                    label=label,
                    body=body,
                    body_format=body_format,
                    subtype=event.kind,
                )
            if real_user_input is not None:
                body = real_user_input
                body_format = "markdown"
            return AgentEventProjection(
                tone="user-input",
                label="User input",
                body=body,
                body_format=body_format,
                subtype=event.kind,
            )
        if role == "assistant" or event.kind == "assistant_message":
            return AgentEventProjection(
                tone="agent",
                label="Agent response",
                body=body,
                body_format=body_format,
                subtype=event.kind,
            )
        return fallback_projection(event)

    def project_chat(self, event: Event) -> AgentChatProjection | None:
        subagent_call = _subagent_call_chat_projection(event)
        if subagent_call is not None:
            return subagent_call

        subagent_result = _subagent_result_chat_projection(event)
        if subagent_result is not None:
            return subagent_result

        role = _message_role(event.payload_json)
        body = _chat_body_text(event.payload_json)
        if not body:
            return None
        source = str(event.ai_session_id or event.source_id)
        if role == "user":
            subagent_prompt = _subagent_prompt_chat_projection(event, body)
            if subagent_prompt is not None:
                return subagent_prompt
            body = extract_real_user_input(body, provider=self.provider_id)
            if body is None:
                return None
            return AgentChatProjection("user", body, dedupe_key=f"{source}:user:{body}")
        if role == "assistant":
            return AgentChatProjection(
                "agent",
                body,
                dedupe_key=f"{source}:agent:{body}",
                agent_message_type="agent",
                subagent_id=_payload_subagent_id(event.payload_json),
            )
        return None

    def is_completion(self, event: Event) -> bool:
        payload = event.payload_json
        if (
            string_value(payload.get("type")) == "system"
            and string_value(payload.get("subtype")) == "turn_duration"
        ):
            return True

        message = _message(payload)
        stop_reason = string_value(message.get("stop_reason")) or string_value(payload.get("stop_reason"))
        if stop_reason != "end_turn":
            return False
        role = string_value(message.get("role")) or string_value(payload.get("type"))
        return role == "assistant" or string_value(payload.get("type")) == "assistant"

    def summary_text(self, event: Event) -> str:
        chat = self.project_chat(event)
        if chat is not None:
            return chat.body
        return message_content(event.payload_json) or self.normalize(
            event.payload_json, source_path=event.source_id, cursor=None
        ).text or json_text(event.payload_json)

    def index_text(self, event: Event) -> str:
        return self.summary_text(event)
