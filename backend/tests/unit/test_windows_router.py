from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.models import LOCAL_CLIENT_ID, ClientRuntime, VirtualWindow, WindowStatus
from app.routers import windows
from app.schemas import WindowCreateIn
from app.services.tmux_manager import TmuxTarget


class FakeSession:
    async def commit(self) -> None:
        return None

    async def refresh(self, _instance) -> None:
        return None

    async def rollback(self) -> None:
        return None


@pytest.mark.asyncio
async def test_local_window_creation_runs_tmux_before_database_insert(monkeypatch) -> None:
    events: list[tuple[str, object]] = []
    folder_id = uuid4()

    class FakeTmuxManager:
        async def create_window(self, cwd, shell_command, *, client_id, window_id):
            events.append(("tmux", window_id))
            return TmuxTarget(
                session="web-terminal",
                window_id="@2",
                cwd=cwd,
                shell_command=shell_command,
            )

    async def fake_create_window(
        session,
        client_id,
        cwd,
        shell_command,
        *,
        window_id=None,
        tmux_session=None,
        tmux_window_id=None,
        remote_session_id=None,
        remote_window_id=None,
    ):
        persisted_window_id = window_id or uuid4()
        events.append(("db", window_id))
        return VirtualWindow(
            id=persisted_window_id,
            client_id=client_id,
            title="Terminal",
            folder_id=folder_id,
            status=WindowStatus.active,
            tmux_session=tmux_session,
            tmux_window_id=tmux_window_id,
            remote_session_id=remote_session_id,
            remote_window_id=remote_window_id,
            cwd=cwd,
            shell_command=shell_command,
            title_manually_overridden=False,
            folder_manually_overridden=False,
            created_at=datetime.now(UTC),
        )

    monkeypatch.setattr(windows, "create_window", fake_create_window)
    client = SimpleNamespace(id=LOCAL_CLIENT_ID, runtime=ClientRuntime.local)

    created = await windows._create_virtual_window_for_client(
        client,
        WindowCreateIn(cwd="/workspace", shell_command="/bin/bash"),
        FakeSession(),
        FakeTmuxManager(),
    )

    assert events[0][0] == "tmux"
    assert events[1] == ("db", events[0][1])
    assert created.tmux_session == "web-terminal"
    assert created.tmux_window_id == "@2"
