from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.models import LOCAL_CLIENT_ID, ClientRuntime, VirtualWindow, WindowStatus
from app.routers import windows
from app.schemas import WindowCreateIn
from app.services import agent_config as agent_config_service
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


@pytest.mark.asyncio
async def test_local_agent_window_applies_config_before_tmux_create(monkeypatch) -> None:
    events: list[tuple[str, object]] = []

    class FakeTmuxManager:
        async def create_window(self, cwd, shell_command, *, client_id, window_id):
            events.append(("tmux", shell_command))
            return TmuxTarget(session="web-terminal", window_id="@2", cwd=cwd, shell_command=shell_command)

    async def fake_create_window(session, client_id, cwd, shell_command, **kwargs):
        window_id = kwargs["window_id"]
        events.append(("db", shell_command))
        return VirtualWindow(
            id=window_id,
            client_id=client_id,
            title="Terminal",
            folder_id=None,
            status=WindowStatus.active,
            tmux_session=kwargs["tmux_session"],
            tmux_window_id=kwargs["tmux_window_id"],
            remote_session_id=None,
            remote_window_id=None,
            cwd=cwd,
            shell_command=shell_command,
            title_manually_overridden=False,
            folder_manually_overridden=False,
            created_at=datetime.now(UTC),
        )

    def fake_apply(selection, *, window_id, home=None):
        events.append(("config", (selection.agent, window_id)))
        return agent_config_service.AgentConfig(agent="codex", sections=[])

    monkeypatch.setattr(windows, "create_window", fake_create_window)
    monkeypatch.setattr(windows.agent_config_service, "apply_agent_config_selection", fake_apply)

    client = SimpleNamespace(id=LOCAL_CLIENT_ID, runtime=ClientRuntime.local)
    created = await windows._create_virtual_window_for_client(
        client,
        WindowCreateIn.model_validate(
            {
                "cwd": "/workspace",
                "agent_launch": {
                    "agent": "codex",
                    "command": "codex",
                    "config": {"agent": "codex", "sections": [{"id": "skills", "items": [{"id": "docker", "enabled": False}]}]},
                },
            }
        ),
        FakeSession(),
        FakeTmuxManager(),
    )

    assert [event[0] for event in events] == ["config", "tmux", "db"]
    assert created.shell_command == "codex"
    assert "codex" in created.runtime_tags


@pytest.mark.asyncio
async def test_local_agent_window_materializes_profile_before_tmux_create(monkeypatch) -> None:
    events: list[tuple[str, object]] = []

    class FakeTmuxManager:
        async def create_window(self, cwd, shell_command, *, client_id, window_id):
            events.append(("tmux", shell_command))
            return TmuxTarget(session="web-terminal", window_id="@2", cwd=cwd, shell_command=shell_command)

    async def fake_create_window(session, client_id, cwd, shell_command, **kwargs):
        window_id = kwargs["window_id"]
        events.append(("db", shell_command))
        return VirtualWindow(
            id=window_id,
            client_id=client_id,
            title="Terminal",
            folder_id=None,
            status=WindowStatus.active,
            tmux_session=kwargs["tmux_session"],
            tmux_window_id=kwargs["tmux_window_id"],
            remote_session_id=None,
            remote_window_id=None,
            cwd=cwd,
            shell_command=shell_command,
            title_manually_overridden=False,
            folder_manually_overridden=False,
            created_at=datetime.now(UTC),
        )

    def fake_materialize(profile_id, agent, *, window_id, home=None):
        events.append(("profile", (profile_id, agent, window_id)))
        return agent_config_service.AgentConfig(agent="codex", sections=[])

    monkeypatch.setattr(windows, "create_window", fake_create_window)
    monkeypatch.setattr(windows.agent_profile_service, "materialize_agent_profile_for_window", fake_materialize)

    client = SimpleNamespace(id=LOCAL_CLIENT_ID, runtime=ClientRuntime.local)
    created = await windows._create_virtual_window_for_client(
        client,
        WindowCreateIn.model_validate(
            {
                "cwd": "/workspace",
                "agent_launch": {
                    "agent": "codex",
                    "command": "codex",
                    "profile_id": "builder",
                },
            }
        ),
        FakeSession(),
        FakeTmuxManager(),
    )

    assert [event[0] for event in events] == ["profile", "tmux", "db"]
    assert events[0][1] == ("builder", "codex", str(created.id))


def test_remote_agent_from_command_uses_remote_descriptor_after_shell_prefix() -> None:
    payload = {
        "agent_clients": [
            {
                "id": "future_agent",
                "provider_id": "future_provider",
                "aliases": ["future"],
                "default_command": "future-agent",
                "command_names": ["future-agent"],
            }
        ]
    }

    assert windows._remote_agent_from_command(
        "cd /workspace && FOO=bar future-agent --profile main",
        payload,
    ) == "future_agent"


@pytest.mark.asyncio
async def test_local_window_agent_config_reads_managed_window_home(monkeypatch) -> None:
    events: list[tuple[str, object]] = []
    client_id = LOCAL_CLIENT_ID
    window_id = uuid4()
    window = VirtualWindow(
        id=window_id,
        client_id=client_id,
        title="Agent",
        folder_id=None,
        status=WindowStatus.active,
        cwd="/workspace",
        shell_command="codex",
        title_manually_overridden=False,
        folder_manually_overridden=False,
        created_at=datetime.now(UTC),
    )

    async def fake_require_client(session, requested_client_id):
        assert requested_client_id == client_id
        return SimpleNamespace(id=client_id, runtime=ClientRuntime.local)

    async def fake_get_window_for_client(session, requested_client_id, requested_window_id):
        assert requested_client_id == client_id
        assert requested_window_id == window_id
        return window

    async def fake_agent_provider_for_window(session, requested_window):
        assert requested_window is window
        return "codex"

    def fake_list_window_agent_config(agent, *, window_id, home=None):
        events.append(("window_config", (agent, window_id)))
        return agent_config_service.AgentConfig(
            agent="codex",
            sections=[
                agent_config_service.AgentConfigSection(
                    id="skills",
                    name="Skills",
                    items=[
                        agent_config_service.AgentConfigItem(
                            id="docker",
                            name="docker",
                            enabled=False,
                        )
                    ],
                )
            ],
        )

    def fail_list_agent_config(*args, **kwargs):
        raise AssertionError("window config must not read the global agent home")

    monkeypatch.setattr(windows, "_require_client", fake_require_client)
    monkeypatch.setattr(windows, "get_window_for_client", fake_get_window_for_client)
    monkeypatch.setattr(windows, "_agent_provider_for_window", fake_agent_provider_for_window)
    monkeypatch.setattr(
        windows.agent_config_service,
        "list_window_agent_config",
        fake_list_window_agent_config,
        raising=False,
    )
    monkeypatch.setattr(windows.agent_config_service, "list_agent_config", fail_list_agent_config)

    result = await windows.read_window_agent_config(
        SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace())),
        client_id,
        window_id,
        FakeSession(),
    )

    assert events == [("window_config", ("codex", str(window_id)))]
    assert result.sections[0].items[0].enabled is False


@pytest.mark.asyncio
async def test_local_window_agent_config_update_writes_managed_window_home(monkeypatch) -> None:
    events: list[tuple[str, object]] = []
    client_id = LOCAL_CLIENT_ID
    window_id = uuid4()
    window = VirtualWindow(
        id=window_id,
        client_id=client_id,
        title="Agent",
        folder_id=None,
        status=WindowStatus.active,
        cwd="/workspace",
        shell_command="codex",
        title_manually_overridden=False,
        folder_manually_overridden=False,
        created_at=datetime.now(UTC),
    )

    async def fake_require_client(session, requested_client_id):
        assert requested_client_id == client_id
        return SimpleNamespace(id=client_id, runtime=ClientRuntime.local)

    async def fake_get_window_for_client(session, requested_client_id, requested_window_id):
        assert requested_client_id == client_id
        assert requested_window_id == window_id
        return window

    async def fake_agent_provider_for_window(session, requested_window):
        assert requested_window is window
        return "codex"

    def fake_set_window_agent_config_item_enabled(agent, section_id, item_id, enabled, *, window_id, home=None):
        events.append(("window_config", (agent, section_id, item_id, enabled, window_id)))
        return agent_config_service.AgentConfig(
            agent="codex",
            sections=[
                agent_config_service.AgentConfigSection(
                    id="skills",
                    name="Skills",
                    items=[
                        agent_config_service.AgentConfigItem(
                            id=item_id,
                            name=item_id,
                            enabled=enabled,
                        )
                    ],
                )
            ],
        )

    def fail_set_agent_config_item_enabled(*args, **kwargs):
        raise AssertionError("window config must not write the global agent home")

    monkeypatch.setattr(windows, "_require_client", fake_require_client)
    monkeypatch.setattr(windows, "get_window_for_client", fake_get_window_for_client)
    monkeypatch.setattr(windows, "_agent_provider_for_window", fake_agent_provider_for_window)
    monkeypatch.setattr(
        windows.agent_config_service,
        "set_window_agent_config_item_enabled",
        fake_set_window_agent_config_item_enabled,
        raising=False,
    )
    monkeypatch.setattr(
        windows.agent_config_service,
        "set_agent_config_item_enabled",
        fail_set_agent_config_item_enabled,
    )

    result = await windows.update_window_agent_config_item(
        SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace())),
        client_id,
        window_id,
        "skills",
        "docker",
        windows.AgentConfigToggleIn(enabled=False),
        FakeSession(),
    )

    assert events == [("window_config", ("codex", "skills", "docker", False, str(window_id)))]
    assert result.sections[0].items[0].enabled is False
