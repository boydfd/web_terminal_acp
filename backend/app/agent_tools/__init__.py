from app.models import EventSourceType


def agent_activity_source_types() -> tuple[EventSourceType, ...]:
    from .registry import get_agent_tool_registry

    return tuple(sorted(get_agent_tool_registry().agent_activity_source_types(), key=lambda item: item.value))


def get_agent_tool_registry():
    from .registry import get_agent_tool_registry as _get_agent_tool_registry

    return _get_agent_tool_registry()


__all__ = ["agent_activity_source_types", "get_agent_tool_registry"]
