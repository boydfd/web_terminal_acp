from uuid import UUID

from app.client_agent.runner import _should_restore_agent_tool_watcher
from app.client_agent.tmux_runtime import ClientRuntimeWindow


WINDOW_ID = UUID("87654321-4321-8765-4321-876543218765")


def test_should_restore_agent_tool_watcher_requires_managed_marker() -> None:
    assert _should_restore_agent_tool_watcher(
        ClientRuntimeWindow(
            remote_session_id="client_pool",
            remote_window_id="@1",
            local_window_id=WINDOW_ID,
            managed_agent_tools=True,
        )
    )

    assert not _should_restore_agent_tool_watcher(
        ClientRuntimeWindow(
            remote_session_id="client_pool",
            remote_window_id="@2",
            local_window_id=WINDOW_ID,
            managed_agent_tools=False,
        )
    )
    assert not _should_restore_agent_tool_watcher(
        ClientRuntimeWindow(
            remote_session_id="client_pool",
            remote_window_id="@3",
            managed_agent_tools=True,
        )
    )
