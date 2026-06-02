from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.db import Base
from app.models import Event, EventSourceType
from app.routers.windows import (
    _load_antigravity_subagent_target_events,
    _subagent_targets_by_tool_use_id,
)


def test_subagent_targets_include_raw_antigravity_invoke_subagent_event() -> None:
    event = Event(
        client_id=uuid4(),
        source_type=EventSourceType.agent_tool_record,
        source_id="parent-session",
        kind="tool_result",
        payload_json={
            "provider": "antigravity_cli",
            "type": "INVOKE_SUBAGENT",
            "step_index": 9,
            "content": (
                "Created the following subagents:\n"
                "{\n"
                '  "conversationId": "subagent-session",\n'
                '  "workspaceUris": []\n'
                "}"
            ),
        },
        fingerprint=str(uuid4()),
    )

    assert _subagent_targets_by_tool_use_id([event]) == {
        "step-8": "agent-subagent-session"
    }


@pytest.mark.asyncio
async def test_load_antigravity_subagent_target_events_filters_raw_invoke_events() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    client_id = uuid4()
    window_id = uuid4()
    wanted = Event(
        client_id=client_id,
        virtual_window_id=window_id,
        source_type=EventSourceType.agent_tool_record,
        source_id="parent-session",
        kind="tool_result",
        payload_json={
            "provider": "antigravity_cli",
            "type": "INVOKE_SUBAGENT",
            "step_index": 3,
            "content": 'Created the following subagents:\n{"conversationId": "subagent-session"}',
        },
        fingerprint=str(uuid4()),
    )
    ignored = Event(
        client_id=client_id,
        virtual_window_id=window_id,
        source_type=EventSourceType.agent_tool_record,
        source_id="parent-session",
        kind="tool_result",
        payload_json={
            "provider": "antigravity_cli",
            "type": "RUN_COMMAND",
            "content": "pytest output",
        },
        fingerprint=str(uuid4()),
    )

    async with AsyncSession(engine, expire_on_commit=False) as session:
        session.add_all([wanted, ignored])
        await session.flush()

        events = await _load_antigravity_subagent_target_events(
            session,
            event_filters=[
                Event.client_id == client_id,
                Event.virtual_window_id == window_id,
            ],
        )

    await engine.dispose()

    assert [event.id for event in events] == [wanted.id]
