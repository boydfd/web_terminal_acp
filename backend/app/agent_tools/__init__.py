from app.models import EventSourceType

from .registry import AgentToolRegistry, get_agent_tool_registry


def agent_activity_source_types() -> tuple[EventSourceType, ...]:
    return tuple(sorted(get_agent_tool_registry().agent_activity_source_types(), key=lambda item: item.value))


__all__ = ["AgentToolRegistry", "agent_activity_source_types", "get_agent_tool_registry"]
