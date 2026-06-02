import asyncio
from pathlib import Path
from uuid import UUID

import pytest

import app.client_agent.runner as client_agent_runner
from app.client_agent.config import ClientAgentConfig
from app.services.agent_config import AgentConfig, AgentConfigItem, AgentConfigSection
from app.services.agent_profiles import AgentProfile
from app.client_agent.runner import _handle_agent_message, _should_restore_agent_tool_watcher
from app.client_agent.tmux_runtime import ClientRuntimeWindow
from app.services.runtime.protocol import AgentMessage


WINDOW_ID = UUID("87654321-4321-8765-4321-876543218765")
VIEW_ID = UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")


async def handle_message_for_test(
    writer,
    bulk_writer,
    config,
    runtime,
    terminal,
    supervisor,
    watcher,
    attach_snapshot_tasks,
    terminal_view_window_ids,
    message: AgentMessage,
) -> bool:
    return await _handle_agent_message(
        writer,
        bulk_writer,
        config,
        runtime,
        terminal,
        supervisor,
        watcher,
        FakeAuxTerminal(),
        attach_snapshot_tasks,
        set(),
        asyncio.Semaphore(1),
        terminal_view_window_ids,
        message,
    )


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


@pytest.mark.asyncio
async def test_aux_terminal_attach_sends_aux_output_message() -> None:
    writer = FakeWriter()
    bulk_writer = FakeBulkWriter()
    aux_terminal = FakeAuxTerminal()

    await _handle_agent_message(
        writer,
        bulk_writer,
        object(),
        FakeRuntime(),
        FakeTerminal([]),
        FakeIdleSupervisor([]),
        FakeAgentToolWatcher([]),
        aux_terminal,
        {},
        set(),
        asyncio.Semaphore(1),
        {},
        AgentMessage(
            type="aux_terminal_attach",
            client_id=UUID("12345678-1234-5678-1234-567812345678"),
            window_id=WINDOW_ID,
            request_id="request-aux",
            payload={
                "aux_terminal_id": "aux-1",
                "view_id": str(VIEW_ID),
            },
        ),
    )

    senders = [call for call in aux_terminal.calls if call[0] == "attach"]
    assert senders == [("attach", "aux-1")]
    attach_sender = aux_terminal.attach_sender
    await attach_sender(b"aux output\n")

    assert writer.messages[-1].type == "aux_terminal_attach_result"
    assert bulk_writer.terminal_messages[-1].type == "aux_terminal_output"
    assert bulk_writer.terminal_messages[-1].payload["view_id"] == str(VIEW_ID)


@pytest.mark.asyncio
async def test_aux_terminal_kill_removes_aux_target() -> None:
    aux_terminal = FakeAuxTerminal()

    await _handle_agent_message(
        FakeWriter(),
        FakeBulkWriter(),
        object(),
        FakeRuntime(),
        FakeTerminal([]),
        FakeIdleSupervisor([]),
        FakeAgentToolWatcher([]),
        aux_terminal,
        {},
        set(),
        asyncio.Semaphore(1),
        {},
        AgentMessage(
            type="aux_terminal_kill",
            client_id=UUID("12345678-1234-5678-1234-567812345678"),
            window_id=WINDOW_ID,
            payload={"aux_terminal_id": "aux-1"},
        ),
    )

    assert aux_terminal.calls == [("kill", "aux-1")]


@pytest.mark.asyncio
async def test_run_client_agent_uses_capped_reconnect_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    client_id = UUID("12345678-1234-5678-1234-567812345678")
    attempts = 0
    sleeps: list[float] = []

    async def fake_run_once(config: ClientAgentConfig) -> bool:
        nonlocal attempts
        attempts += 1
        if attempts <= 7:
            raise OSError("network unavailable")
        return True

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(client_agent_runner, "_run_client_agent_once", fake_run_once)
    monkeypatch.setattr(client_agent_runner.asyncio, "sleep", fake_sleep)

    config = ClientAgentConfig(
        client_id=client_id,
        token="secret-token",
        server_url="http://control.example.com",
        name="edge-client",
        install_path=Path("/opt/web-terminal-acp-client"),
    )

    await client_agent_runner.run_client_agent(config)

    assert sleeps == [1, 2, 4, 8, 16, 30, 30]


@pytest.mark.asyncio
async def test_terminal_attach_resumes_suspended_agent_before_attach() -> None:
    calls: list[str] = []
    terminal = FakeTerminal(calls)
    supervisor = FakeIdleSupervisor(calls)
    writer = FakeWriter()

    await handle_message_for_test(
        writer,
        FakeBulkWriter(),
        object(),
        FakeRuntime(),
        terminal,
        supervisor,
        FakeAgentToolWatcher(calls),
        {},
        {},
        AgentMessage(
            type="terminal_attach",
            client_id=UUID("12345678-1234-5678-1234-567812345678"),
            window_id=WINDOW_ID,
            request_id="request-1",
            payload={
                "remote_session_id": "pool",
                "remote_window_id": "@7",
                "view_id": str(VIEW_ID),
            },
        ),
    )

    assert calls[:4] == [
        "attach_view",
        "register_window",
        "register_window_supervisor",
        "resume_window",
    ]
    assert calls[4] == "attach_with_selection"
    assert supervisor.resume_calls == [(WINDOW_ID, False)]
    assert writer.messages[-1].type == "terminal_attach_result"


@pytest.mark.asyncio
async def test_terminal_select_window_resumes_suspended_agent_before_select() -> None:
    calls: list[str] = []
    terminal = FakeTerminal(calls)
    supervisor = FakeIdleSupervisor(calls)
    writer = FakeWriter()

    await handle_message_for_test(
        writer,
        FakeBulkWriter(),
        object(),
        FakeRuntime(),
        terminal,
        supervisor,
        FakeAgentToolWatcher(calls),
        {},
        {},
        AgentMessage(
            type="terminal_select_window",
            client_id=UUID("12345678-1234-5678-1234-567812345678"),
            window_id=WINDOW_ID,
            request_id="request-1",
            payload={
                "remote_session_id": "pool",
                "remote_window_id": "@7",
                "view_id": str(VIEW_ID),
            },
        ),
    )

    assert calls == [
        "register_window",
        "register_window_supervisor",
        "attach_view",
        "resume_window",
        "select_window",
    ]
    assert supervisor.resume_calls == [(WINDOW_ID, False)]
    assert writer.messages[-1].type == "terminal_attach_result"


@pytest.mark.asyncio
async def test_create_window_registers_existing_unified_agent_tool_watcher() -> None:
    calls: list[str] = []
    terminal = FakeTerminal(calls)
    supervisor = FakeIdleSupervisor(calls)
    watcher = FakeAgentToolWatcher(calls)
    writer = FakeWriter()

    await handle_message_for_test(
        writer,
        FakeBulkWriter(),
        object(),
        FakeRuntime(calls),
        terminal,
        supervisor,
        watcher,
        {},
        {},
        AgentMessage(
            type="create_window",
            client_id=UUID("12345678-1234-5678-1234-567812345678"),
            window_id=WINDOW_ID,
            request_id="request-1",
            payload={"cwd": "/workspace/project"},
        ),
    )

    assert calls == [
        "create_window",
        "register_window",
        "register_window_supervisor",
        "watch_window",
    ]
    assert watcher.watched == [(WINDOW_ID, "/workspace/project")]
    assert writer.messages[-1].type == "create_window_result"


@pytest.mark.asyncio
async def test_create_window_scopes_agent_tool_watcher_to_detected_provider() -> None:
    watcher = FakeAgentToolWatcher([])

    await handle_message_for_test(
        FakeWriter(),
        FakeBulkWriter(),
        object(),
        FakeRuntime([]),
        FakeTerminal([]),
        FakeIdleSupervisor([]),
        watcher,
        {},
        {},
        AgentMessage(
            type="create_window",
            client_id=UUID("12345678-1234-5678-1234-567812345678"),
            window_id=WINDOW_ID,
            request_id="request-1",
            payload={"cwd": "/workspace/project", "shell_command": "env FOO=1 cursor-agent"},
        ),
    )

    assert watcher.watched == [(WINDOW_ID, "/workspace/project", frozenset({"cursor_cli"}))]


@pytest.mark.asyncio
async def test_create_window_can_run_in_background_without_blocking_control_messages() -> None:
    create_started = asyncio.Event()
    create_continue = asyncio.Event()
    calls: list[str] = []
    writer = FakeWriter()
    runtime = BlockingCreateRuntime(calls, create_started, create_continue)

    await _handle_agent_message(
        writer,
        FakeBulkWriter(),
        object(),
        runtime,
        FakeTerminal(calls),
        FakeIdleSupervisor(calls),
        FakeAgentToolWatcher(calls),
        FakeAuxTerminal(),
        {},
        set(),
        asyncio.Semaphore(1),
        {},
        AgentMessage(
            type="create_window",
            client_id=UUID("12345678-1234-5678-1234-567812345678"),
            window_id=WINDOW_ID,
            request_id="create-1",
            payload={"cwd": "/workspace/project"},
        ),
        create_window_tasks=set(),
    )
    await asyncio.wait_for(create_started.wait(), timeout=1.0)

    await _handle_agent_message(
        writer,
        FakeBulkWriter(),
        object(),
        runtime,
        FakeTerminal(calls),
        FakeIdleSupervisor(calls),
        FakeAgentToolWatcher(calls),
        FakeAuxTerminal(),
        {},
        set(),
        asyncio.Semaphore(1),
        {},
        AgentMessage(
            type="agent_config_get",
            client_id=UUID("12345678-1234-5678-1234-567812345678"),
            window_id=WINDOW_ID,
            request_id="config-1",
            payload={"agent": "codex"},
        ),
        create_window_tasks=set(),
    )

    assert writer.messages[-1].type == "agent_config_result"
    assert calls == ["create_window"]

    create_continue.set()
    for _ in range(20):
        if any(message.type == "create_window_result" for message in writer.messages):
            break
        await asyncio.sleep(0.01)
    assert [message.type for message in writer.messages] == [
        "agent_config_result",
        "create_window_result",
    ]


@pytest.mark.asyncio
async def test_create_window_applies_agent_config_selection(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    applied: list[tuple[str, str]] = []

    def fake_apply(selection, *, window_id, home=None):
        applied.append((selection.agent, window_id))
        calls.append("apply_config")
        return AgentConfig(agent=selection.agent, sections=[])

    monkeypatch.setattr(
        "app.client_agent.runner.agent_config_service.apply_agent_config_selection",
        fake_apply,
    )

    await handle_message_for_test(
        FakeWriter(),
        FakeBulkWriter(),
        object(),
        FakeRuntime(calls),
        FakeTerminal(calls),
        FakeIdleSupervisor(calls),
        FakeAgentToolWatcher(calls),
        {},
        {},
        AgentMessage(
            type="create_window",
            client_id=UUID("12345678-1234-5678-1234-567812345678"),
            window_id=WINDOW_ID,
            request_id="request-1",
            payload={
                "cwd": "/workspace/project",
                "shell_command": "codex",
                "agent_config_selection": {
                    "agent": "codex",
                    "sections": [
                        {"id": "skills", "items": [{"id": "docker", "enabled": False}]}
                    ],
                },
            },
        ),
    )

    assert calls[:2] == ["apply_config", "create_window"]
    assert applied == [("codex", str(WINDOW_ID))]


@pytest.mark.asyncio
async def test_create_window_materializes_agent_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    materialized: list[tuple[str, str, str]] = []

    def fake_materialize(profile_id, agent, *, window_id, home=None):
        materialized.append((profile_id, agent, window_id))
        calls.append("materialize_profile")
        return AgentConfig(agent="codex", sections=[])

    monkeypatch.setattr(
        "app.client_agent.runner.agent_profile_service.materialize_agent_profile_for_window",
        fake_materialize,
    )

    await handle_message_for_test(
        FakeWriter(),
        FakeBulkWriter(),
        object(),
        FakeRuntime(calls),
        FakeTerminal(calls),
        FakeIdleSupervisor(calls),
        FakeAgentToolWatcher(calls),
        {},
        {},
        AgentMessage(
            type="create_window",
            client_id=UUID("12345678-1234-5678-1234-567812345678"),
            window_id=WINDOW_ID,
            request_id="request-1",
            payload={
                "cwd": "/workspace/project",
                "shell_command": "codex",
                "agent_profile_id": "builder",
                "agent_profile_agent": "codex",
            },
        ),
    )

    assert calls[:2] == ["materialize_profile", "create_window"]
    assert materialized == [("builder", "codex", str(WINDOW_ID))]


@pytest.mark.asyncio
async def test_kill_window_removes_window_from_unified_agent_tool_watcher_first() -> None:
    calls: list[str] = []
    terminal = FakeTerminal(calls)
    supervisor = FakeIdleSupervisor(calls)
    watcher = FakeAgentToolWatcher(calls)
    writer = FakeWriter()

    await handle_message_for_test(
        writer,
        FakeBulkWriter(),
        object(),
        FakeRuntime(calls),
        terminal,
        supervisor,
        watcher,
        {},
        {},
        AgentMessage(
            type="kill_window",
            client_id=UUID("12345678-1234-5678-1234-567812345678"),
            window_id=WINDOW_ID,
            request_id="request-1",
            payload={},
        ),
    )

    assert calls == ["unwatch_window", "remove_window_supervisor", "remove_window", "kill_window"]
    assert watcher.removed == [WINDOW_ID]
    assert writer.messages[-1].type == "kill_window_result"


@pytest.mark.asyncio
async def test_terminal_attach_recreates_missing_tmux_window_before_resume() -> None:
    calls: list[str] = []
    terminal = FakeTerminal(calls)
    supervisor = FakeIdleSupervisor(calls)
    writer = FakeWriter()
    runtime = FakeRuntime(window_exists=False)

    await handle_message_for_test(
        writer,
        FakeBulkWriter(),
        object(),
        runtime,
        terminal,
        supervisor,
        FakeAgentToolWatcher(calls),
        {},
        {},
        AgentMessage(
            type="terminal_attach",
            client_id=UUID("12345678-1234-5678-1234-567812345678"),
            window_id=WINDOW_ID,
            request_id="request-1",
            payload={
                "remote_session_id": "pool",
                "remote_window_id": "@7",
                "view_id": str(VIEW_ID),
                "cwd": "/workspace/project",
                "shell_command": "/bin/bash",
            },
        ),
    )

    assert runtime.recreated == [WINDOW_ID]
    assert calls[:5] == [
        "attach_view",
        "unregister_window",
        "register_window",
        "register_window_supervisor",
        "resume_window",
    ]
    assert calls[5] == "attach_with_selection"
    assert supervisor.resume_calls == [(WINDOW_ID, False)]
    assert writer.messages[-1].payload["remote_window_id"] == "@9"


@pytest.mark.asyncio
async def test_terminal_attach_recreates_with_default_shell_when_resume_is_available() -> None:
    calls: list[str] = []
    terminal = FakeTerminal(calls)
    supervisor = FakeIdleSupervisor(calls)
    supervisor.resumable_session = True
    writer = FakeWriter()
    runtime = FakeRuntime(window_exists=False)

    await handle_message_for_test(
        writer,
        FakeBulkWriter(),
        object(),
        runtime,
        terminal,
        supervisor,
        FakeAgentToolWatcher(calls),
        {},
        {},
        AgentMessage(
            type="terminal_attach",
            client_id=UUID("12345678-1234-5678-1234-567812345678"),
            window_id=WINDOW_ID,
            request_id="request-1",
            payload={
                "remote_session_id": "pool",
                "remote_window_id": "@7",
                "view_id": str(VIEW_ID),
                "cwd": "/workspace/project",
                "shell_command": "codex",
            },
        ),
    )

    assert runtime.recreate_shell_commands == [None]
    assert supervisor.resume_calls == [(WINDOW_ID, True)]
    assert writer.messages[-1].payload["shell_command"] == "codex"


@pytest.mark.asyncio
async def test_terminal_select_window_recreates_missing_tmux_window_before_latest_resume() -> None:
    calls: list[str] = []
    terminal = FakeTerminal(calls)
    supervisor = FakeIdleSupervisor(calls)
    writer = FakeWriter()
    runtime = FakeRuntime(window_exists=False)

    await handle_message_for_test(
        writer,
        FakeBulkWriter(),
        object(),
        runtime,
        terminal,
        supervisor,
        FakeAgentToolWatcher(calls),
        {},
        {},
        AgentMessage(
            type="terminal_select_window",
            client_id=UUID("12345678-1234-5678-1234-567812345678"),
            window_id=WINDOW_ID,
            request_id="request-1",
            payload={
                "remote_session_id": "pool",
                "remote_window_id": "@7",
                "view_id": str(VIEW_ID),
                "cwd": "/workspace/project",
                "shell_command": "/bin/bash",
            },
        ),
    )

    assert runtime.recreated == [WINDOW_ID]
    assert calls == [
        "unregister_window",
        "register_window",
        "register_window_supervisor",
        "attach_view",
        "resume_window",
        "select_window",
    ]
    assert supervisor.resume_calls == [(WINDOW_ID, False)]
    assert writer.messages[-1].payload["remote_window_id"] == "@9"


@pytest.mark.asyncio
async def test_terminal_select_window_recreates_with_default_shell_when_resume_is_available() -> None:
    calls: list[str] = []
    terminal = FakeTerminal(calls)
    supervisor = FakeIdleSupervisor(calls)
    supervisor.resumable_session = True
    writer = FakeWriter()
    runtime = FakeRuntime(window_exists=False)

    await handle_message_for_test(
        writer,
        FakeBulkWriter(),
        object(),
        runtime,
        terminal,
        supervisor,
        FakeAgentToolWatcher(calls),
        {},
        {},
        AgentMessage(
            type="terminal_select_window",
            client_id=UUID("12345678-1234-5678-1234-567812345678"),
            window_id=WINDOW_ID,
            request_id="request-1",
            payload={
                "remote_session_id": "pool",
                "remote_window_id": "@7",
                "view_id": str(VIEW_ID),
                "cwd": "/workspace/project",
                "shell_command": "codex",
            },
        ),
    )

    assert runtime.recreate_shell_commands == [None]
    assert supervisor.resume_calls == [(WINDOW_ID, True)]
    assert writer.messages[-1].payload["shell_command"] == "codex"


@pytest.mark.asyncio
async def test_agent_config_get_returns_serialized_config(monkeypatch: pytest.MonkeyPatch) -> None:
    writer = FakeWriter()

    def fake_list_agent_config(agent: str):
        assert agent == "codex"
        return AgentConfig(
            agent="codex",
            sections=[
                AgentConfigSection(
                    id="skills",
                    name="Skills",
                    items=[AgentConfigItem(id="docker", name="docker", enabled=True)],
                ),
                AgentConfigSection(id="plugins", name="Plugins", items=[]),
                AgentConfigSection(id="hooks", name="Hooks", items=[]),
            ],
        )

    monkeypatch.setattr(
        "app.client_agent.runner.agent_config_service.list_agent_config",
        fake_list_agent_config,
    )

    await handle_message_for_test(
        writer,
        FakeBulkWriter(),
        object(),
        FakeRuntime(),
        FakeTerminal([]),
        FakeIdleSupervisor([]),
        FakeAgentToolWatcher([]),
        {},
        {},
        AgentMessage(
            type="agent_config_get",
            client_id=UUID("12345678-1234-5678-1234-567812345678"),
            request_id="request-1",
            payload={"agent": "codex"},
        ),
    )

    assert writer.messages[-1].type == "agent_config_result"
    assert writer.messages[-1].payload["agent"] == "codex"
    assert writer.messages[-1].payload["sections"][0]["items"][0]["id"] == "docker"


@pytest.mark.asyncio
async def test_agent_clients_list_returns_serialized_descriptors() -> None:
    writer = FakeWriter()

    await handle_message_for_test(
        writer,
        FakeBulkWriter(),
        object(),
        FakeRuntime(),
        FakeTerminal([]),
        FakeIdleSupervisor([]),
        FakeAgentToolWatcher([]),
        {},
        {},
        AgentMessage(
            type="agent_clients_list",
            client_id=UUID("12345678-1234-5678-1234-567812345678"),
            request_id="request-1",
            payload={},
        ),
    )

    assert writer.messages[-1].type == "agent_client_result"
    clients = {client["id"]: client for client in writer.messages[-1].payload["agent_clients"]}
    assert clients["codex"]["provider_id"] == "codex"
    assert clients["codex"]["capabilities"]["agent_records"] is True
    assert clients["cursor"]["command_names"] == ["agent", "cursor", "cursor-agent"]


@pytest.mark.asyncio
async def test_agent_profile_list_returns_serialized_profiles(monkeypatch: pytest.MonkeyPatch) -> None:
    writer = FakeWriter()

    def fake_list_agent_profiles():
        return [
            AgentProfile(
                id="builder",
                name="Builder",
                description=None,
                default_agent_client="codex",
                agent_md="Rules",
                created_at="2026-01-01T00:00:00+00:00",
                updated_at="2026-01-01T00:00:00+00:00",
            )
        ]

    monkeypatch.setattr(
        "app.client_agent.runner.agent_profile_service.list_agent_profiles",
        fake_list_agent_profiles,
    )

    await handle_message_for_test(
        writer,
        FakeBulkWriter(),
        object(),
        FakeRuntime(),
        FakeTerminal([]),
        FakeIdleSupervisor([]),
        FakeAgentToolWatcher([]),
        {},
        {},
        AgentMessage(
            type="agent_profile_list",
            client_id=UUID("12345678-1234-5678-1234-567812345678"),
            request_id="request-1",
            payload={},
        ),
    )

    assert writer.messages[-1].type == "agent_profile_result"
    assert writer.messages[-1].payload["profiles"][0]["id"] == "builder"


@pytest.mark.asyncio
async def test_agent_config_get_uses_window_managed_home(monkeypatch: pytest.MonkeyPatch) -> None:
    writer = FakeWriter()
    captured: list[tuple[str, str]] = []

    def fail_list_agent_config(*args, **kwargs):
        raise AssertionError("window config must not read the global agent home")

    def fake_list_window_agent_config(agent: str, *, window_id: str, home=None):
        captured.append((agent, window_id))
        return AgentConfig(
            agent="codex",
            sections=[
                AgentConfigSection(
                    id="skills",
                    name="Skills",
                    items=[AgentConfigItem(id="docker", name="docker", enabled=False)],
                )
            ],
        )

    monkeypatch.setattr(
        "app.client_agent.runner.agent_config_service.list_agent_config",
        fail_list_agent_config,
    )
    monkeypatch.setattr(
        "app.client_agent.runner.agent_config_service.list_window_agent_config",
        fake_list_window_agent_config,
        raising=False,
    )

    await handle_message_for_test(
        writer,
        FakeBulkWriter(),
        object(),
        FakeRuntime(),
        FakeTerminal([]),
        FakeIdleSupervisor([]),
        FakeAgentToolWatcher([]),
        {},
        {},
        AgentMessage(
            type="agent_config_get",
            client_id=UUID("12345678-1234-5678-1234-567812345678"),
            window_id=WINDOW_ID,
            request_id="request-1",
            payload={"agent": "codex"},
        ),
    )

    assert captured == [("codex", str(WINDOW_ID))]
    assert writer.messages[-1].payload["sections"][0]["items"][0]["enabled"] is False


@pytest.mark.asyncio
async def test_agent_config_set_enabled_uses_window_managed_home(monkeypatch: pytest.MonkeyPatch) -> None:
    writer = FakeWriter()
    captured: list[tuple[str, str, str, bool, str]] = []

    def fail_set_agent_config_item_enabled(*args, **kwargs):
        raise AssertionError("window config must not write the global agent home")

    def fake_set_window_agent_config_item_enabled(
        agent: str,
        section_id: str,
        item_id: str,
        enabled: bool,
        *,
        window_id: str,
        home=None,
    ):
        captured.append((agent, section_id, item_id, enabled, window_id))
        return AgentConfig(
            agent="codex",
            sections=[
                AgentConfigSection(
                    id="skills",
                    name="Skills",
                    items=[AgentConfigItem(id=item_id, name=item_id, enabled=enabled)],
                )
            ],
        )

    monkeypatch.setattr(
        "app.client_agent.runner.agent_config_service.set_agent_config_item_enabled",
        fail_set_agent_config_item_enabled,
    )
    monkeypatch.setattr(
        "app.client_agent.runner.agent_config_service.set_window_agent_config_item_enabled",
        fake_set_window_agent_config_item_enabled,
        raising=False,
    )

    await handle_message_for_test(
        writer,
        FakeBulkWriter(),
        object(),
        FakeRuntime(),
        FakeTerminal([]),
        FakeIdleSupervisor([]),
        FakeAgentToolWatcher([]),
        {},
        {},
        AgentMessage(
            type="agent_config_set_enabled",
            client_id=UUID("12345678-1234-5678-1234-567812345678"),
            window_id=WINDOW_ID,
            request_id="request-1",
            payload={
                "agent": "codex",
                "section_id": "skills",
                "item_id": "docker",
                "enabled": False,
            },
        ),
    )

    assert captured == [("codex", "skills", "docker", False, str(WINDOW_ID))]
    assert writer.messages[-1].payload["sections"][0]["items"][0]["enabled"] is False


class FakeWriter:
    def __init__(self) -> None:
        self.messages: list[AgentMessage] = []

    async def send(self, message: AgentMessage) -> None:
        self.messages.append(message)


class FakeBulkWriter:
    def __init__(self) -> None:
        self.terminal_messages: list[AgentMessage] = []

    async def send_terminal_output(self, message: AgentMessage) -> None:
        self.terminal_messages.append(message)
        return None

    async def send_ai_event(self, message: AgentMessage) -> None:
        return None


class FakeRuntime:
    def __init__(self, calls: list[str] | None = None, *, window_exists: bool = True) -> None:
        self.calls = calls
        self.window_exists = window_exists
        self.recreated: list[UUID] = []
        self.recreate_shell_commands: list[str | None] = []

    async def create_window(self, window_id, *, cwd=None, shell_command=None):
        if self.calls is not None:
            self.calls.append("create_window")
        return ClientRuntimeWindow(
            remote_session_id="pool",
            remote_window_id="@9",
            local_window_id=window_id,
            cwd=cwd,
            managed_agent_tools=True,
        )

    async def kill_window(self, window_id) -> None:
        if self.calls is not None:
            self.calls.append("kill_window")

    async def has_window(self, remote_window_id: str, *, remote_session_id: str | None = None) -> bool:
        return self.window_exists

    async def recreate_window(
        self,
        window_id: UUID,
        *,
        cwd: str | None = None,
        shell_command: str | None = None,
    ) -> ClientRuntimeWindow:
        self.recreated.append(window_id)
        self.recreate_shell_commands.append(shell_command)
        return ClientRuntimeWindow(
            remote_session_id="pool",
            remote_window_id="@9",
            local_window_id=window_id,
            cwd=cwd,
            shell_command=shell_command,
            managed_agent_tools=True,
        )


class BlockingCreateRuntime(FakeRuntime):
    def __init__(
        self,
        calls: list[str],
        started: asyncio.Event,
        should_continue: asyncio.Event,
    ) -> None:
        super().__init__(calls)
        self.started = started
        self.should_continue = should_continue

    async def create_window(self, window_id, *, cwd=None, shell_command=None):
        if self.calls is not None:
            self.calls.append("create_window")
        self.started.set()
        await self.should_continue.wait()
        return ClientRuntimeWindow(
            remote_session_id="pool",
            remote_window_id="@9",
            local_window_id=window_id,
            cwd=cwd,
            managed_agent_tools=True,
        )


class FakeTerminal:
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls

    def register_window(self, window_id, remote_session_id, remote_window_id) -> None:
        self.calls.append("register_window")

    def unregister_window(self, window_id) -> None:
        self.calls.append("unregister_window")

    async def attach_with_selection(self, *args, **kwargs) -> None:
        self.calls.append("attach_with_selection")

    async def select_window(self, *args, **kwargs) -> None:
        self.calls.append("select_window")

    async def remove_window(self, window_id) -> None:
        self.calls.append("remove_window")


class FakeIdleSupervisor:
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls
        self.resume_calls: list[tuple[UUID, bool]] = []
        self.resumable_session = False

    def attach_view(self, view_id: UUID, window_id: UUID) -> None:
        self.calls.append("attach_view")

    def detach_view(self, view_id: UUID) -> None:
        self.calls.append("detach_view")

    def remove_window(self, window_id: UUID) -> None:
        self.calls.append("remove_window_supervisor")

    def register_window(self, window_id: UUID, project_path: str | None) -> None:
        self.calls.append("register_window_supervisor")

    def has_resumable_session(self, window_id: UUID, *, project_path: str | None = None) -> bool:
        return self.resumable_session

    async def resume_window(self, window_id: UUID, *, allow_latest_session: bool = False) -> None:
        self.calls.append("resume_window")
        self.resume_calls.append((window_id, allow_latest_session))


class FakeAgentToolWatcher:
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls
        self.watched: list[tuple[UUID, str | None] | tuple[UUID, str | None, frozenset[str] | None]] = []
        self.removed: list[UUID] = []

    def watch_window(
        self,
        window_id: UUID,
        project_path: str | None,
        *,
        providers: frozenset[str] | None = None,
    ) -> None:
        self.calls.append("watch_window")
        if providers is None:
            self.watched.append((window_id, project_path))
        else:
            self.watched.append((window_id, project_path, providers))

    def remove_window(self, window_id: UUID) -> None:
        self.calls.append("unwatch_window")
        self.removed.append(window_id)


class FakeAuxTerminal:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []
        self.attach_sender = None

    async def ensure_terminal(self, aux_terminal_id: str, *, cwd=None, shell_command=None):
        self.calls.append(("ensure_terminal", aux_terminal_id))
        return type(
            "AuxTarget",
            (),
            {
                "aux_terminal_id": aux_terminal_id,
                "cwd": cwd,
                "shell_command": shell_command,
            },
        )()

    async def attach(self, aux_terminal_id: str, sender, *, view_id) -> None:
        self.calls.append(("attach", aux_terminal_id))
        self.attach_sender = sender

    async def detach(self, aux_terminal_id: str, *, view_id) -> None:
        self.calls.append(("detach", aux_terminal_id))

    async def send_input(self, aux_terminal_id: str, data: bytes, *, view_id) -> None:
        self.calls.append(("send_input", data))

    async def resize(self, aux_terminal_id: str, *, cols: int, rows: int, view_id) -> None:
        self.calls.append(("resize", (cols, rows)))

    async def kill(self, aux_terminal_id: str) -> None:
        self.calls.append(("kill", aux_terminal_id))
