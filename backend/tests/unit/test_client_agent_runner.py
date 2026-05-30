import asyncio
from uuid import UUID

import pytest

from app.services.agent_config import AgentConfig, AgentConfigItem, AgentConfigSection
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
    assert supervisor.resume_calls == [(WINDOW_ID, True)]
    assert writer.messages[-1].payload["remote_window_id"] == "@9"


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
    assert supervisor.resume_calls == [(WINDOW_ID, True)]
    assert writer.messages[-1].payload["remote_window_id"] == "@9"


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
            window_id=WINDOW_ID,
            request_id="request-1",
            payload={"agent": "codex"},
        ),
    )

    assert writer.messages[-1].type == "agent_config_result"
    assert writer.messages[-1].payload["agent"] == "codex"
    assert writer.messages[-1].payload["sections"][0]["items"][0]["id"] == "docker"


class FakeWriter:
    def __init__(self) -> None:
        self.messages: list[AgentMessage] = []

    async def send(self, message: AgentMessage) -> None:
        self.messages.append(message)


class FakeBulkWriter:
    async def send_terminal_output(self, message: AgentMessage) -> None:
        return None

    async def send_ai_event(self, message: AgentMessage) -> None:
        return None


class FakeRuntime:
    def __init__(self, calls: list[str] | None = None, *, window_exists: bool = True) -> None:
        self.calls = calls
        self.window_exists = window_exists
        self.recreated: list[UUID] = []

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
        return ClientRuntimeWindow(
            remote_session_id="pool",
            remote_window_id="@9",
            local_window_id=window_id,
            cwd=cwd,
            shell_command=shell_command,
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

    def attach_view(self, view_id: UUID, window_id: UUID) -> None:
        self.calls.append("attach_view")

    def detach_view(self, view_id: UUID) -> None:
        self.calls.append("detach_view")

    def remove_window(self, window_id: UUID) -> None:
        self.calls.append("remove_window_supervisor")

    def register_window(self, window_id: UUID, project_path: str | None) -> None:
        self.calls.append("register_window_supervisor")

    async def resume_window(self, window_id: UUID, *, allow_latest_session: bool = False) -> None:
        self.calls.append("resume_window")
        self.resume_calls.append((window_id, allow_latest_session))


class FakeAgentToolWatcher:
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls
        self.watched: list[tuple[UUID, str | None]] = []
        self.removed: list[UUID] = []

    def watch_window(self, window_id: UUID, project_path: str | None) -> None:
        self.calls.append("watch_window")
        self.watched.append((window_id, project_path))

    def remove_window(self, window_id: UUID) -> None:
        self.calls.append("unwatch_window")
        self.removed.append(window_id)
