from uuid import UUID

import pytest

from app.client_agent.tmux_runtime import ClientRuntimeWindow, ClientTmuxRuntime


CLIENT_ID = UUID("12345678-1234-5678-1234-567812345678")
WINDOW_ID = UUID("87654321-4321-8765-4321-876543218765")


@pytest.mark.asyncio
async def test_create_window_ensures_pool_and_returns_remote_target_when_pool_missing() -> None:
    calls: list[list[str]] = []

    async def fake_run(args: list[str]) -> str:
        calls.append(args)
        if args == ["tmux", "has-session", "-t", "client_pool"]:
            raise RuntimeError("missing session")
        if args[:3] == ["tmux", "new-window", "-P"]:
            return "@9\n"
        return ""

    runtime = ClientTmuxRuntime(
        client_id=CLIENT_ID,
        server_url="https://control.example.com",
        pool_session="client_pool",
        default_shell="/bin/bash",
        runner=fake_run,
    )

    target = await runtime.create_window(WINDOW_ID, cwd="/tmp/project")

    assert target == ClientRuntimeWindow(
        remote_session_id="client_pool",
        remote_window_id="@9",
        local_window_id=WINDOW_ID,
        cwd="/tmp/project",
        shell_command="/bin/bash",
        managed_agent_tools=True,
    )
    assert calls == [
        ["tmux", "has-session", "-t", "client_pool"],
        ["tmux", "new-session", "-d", "-s", "client_pool", "/bin/bash"],
        ["tmux", "set-option", "-t", "client_pool", "window-size", "manual"],
        ["tmux", "set-option", "-t", "client_pool", "mouse", "on"],
        ["tmux", "set-option", "-s", "set-clipboard", "external"],
        ["tmux", "show-options", "-s", "terminal-features"],
        ["tmux", "set-option", "-as", "terminal-features", ",xterm*:clipboard"],
        [
            "tmux",
            "new-window",
            "-P",
            "-F",
            "#{window_id}",
            "-t",
            "client_pool",
            "-c",
            "/tmp/project",
            runtime.managed_shell_command(WINDOW_ID, project_path="/tmp/project"),
        ],
        ["tmux", "set-option", "-p", "-t", "client_pool:@9", "allow-passthrough", "on"],
        ["tmux", "select-window", "-t", "client_pool:@9"],
        [
            "tmux",
            "set-option",
            "-w",
            "-t",
            "client_pool:@9",
            "@web-terminal-window-id",
            str(WINDOW_ID),
        ],
        [
            "tmux",
            "set-option",
            "-w",
            "-t",
            "client_pool:@9",
            "@web-terminal-managed-agent-tools",
            "1",
        ],
    ]


def test_managed_shell_command_injects_quoted_environment_and_execs_default_shell() -> None:
    runtime = ClientTmuxRuntime(
        client_id=CLIENT_ID,
        server_url="https://control.example.com/with space/it's-ok",
        pool_session="client_pool",
        default_shell="/opt/shells/custom shell",
    )

    command = runtime.managed_shell_command(WINDOW_ID, project_path="/workspace/project")

    assert 'PATH="$HOME/.web-terminal-acp/npm-global/bin:$PATH"' in command
    assert "WEB_TERMINAL_CLIENT_ID=12345678-1234-5678-1234-567812345678" in command
    assert "WEB_TERMINAL_WINDOW_ID=87654321-4321-8765-4321-876543218765" in command
    assert "WEB_TERMINAL_SERVER_URL='https://control.example.com/with space/it'\\''s-ok'" in command
    assert "WEB_TERMINAL_COMMAND_HOOK=1" in command
    assert "WEB_TERMINAL_PROJECT_PATH=/workspace/project" in command
    assert "WEB_TERMINAL_CODEX_HOME='~/.web-terminal-acp/codex-homes/87654321-4321-8765-4321-876543218765'" in command
    assert " CODEX_HOME='~/.web-terminal-acp/codex-homes/87654321-4321-8765-4321-876543218765'" not in command
    assert not command.startswith("CODEX_HOME='~/.web-terminal-acp/codex-homes/87654321-4321-8765-4321-876543218765'")
    assert "CLAUDE_CONFIG_DIR='~/.web-terminal-acp/claude-code-homes/87654321-4321-8765-4321-876543218765'" in command
    assert "CURSOR_AGENT_HOME='~/.web-terminal-acp/cursor-homes/87654321-4321-8765-4321-876543218765'" in command
    assert "/bin/sh -c" in command
    assert "exec '\\''/opt/shells/custom shell'\\''" in command


def test_managed_shell_command_adds_permission_flag_to_direct_codex_start() -> None:
    runtime = ClientTmuxRuntime(
        client_id=CLIENT_ID,
        server_url="https://control.example.com",
        pool_session="client_pool",
    )

    command = runtime.managed_shell_command(
        WINDOW_ID,
        shell_command="codex resume codex-session",
        project_path="/workspace/project",
    )

    assert "codex --dangerously-bypass-approvals-and-sandbox resume codex-session || __web_terminal_agent_exit=$?" in command
    assert "__web_terminal_load_zshrc_env" in command


@pytest.mark.asyncio
async def test_list_windows_ensures_pool_and_returns_runtime_windows_from_tmux_output() -> None:
    calls: list[list[str]] = []

    async def fake_run(args: list[str]) -> str:
        calls.append(args)
        if args == [
            "tmux",
            "list-windows",
            "-t",
            "client_pool",
            "-F",
            "#{window_id}\t#{@web-terminal-window-id}\t#{pane_current_path}\t#{@web-terminal-managed-agent-tools}",
        ]:
            return f"@1\t{WINDOW_ID}\t/workspace/project\t1\n@2\t\t\t\n\n"
        return ""

    runtime = ClientTmuxRuntime(
        client_id=CLIENT_ID,
        server_url="https://control.example.com",
        pool_session="client_pool",
        runner=fake_run,
    )

    windows = await runtime.list_windows()

    assert windows == [
        ClientRuntimeWindow(
            remote_session_id="client_pool",
            remote_window_id="@1",
            local_window_id=WINDOW_ID,
            cwd="/workspace/project",
            managed_agent_tools=True,
        ),
        ClientRuntimeWindow(remote_session_id="client_pool", remote_window_id="@2"),
    ]
    assert calls == [
        ["tmux", "has-session", "-t", "client_pool"],
        ["tmux", "set-option", "-t", "client_pool", "window-size", "manual"],
        ["tmux", "set-option", "-t", "client_pool", "mouse", "on"],
        ["tmux", "set-option", "-s", "set-clipboard", "external"],
        ["tmux", "show-options", "-s", "terminal-features"],
        ["tmux", "set-option", "-as", "terminal-features", ",xterm*:clipboard"],
        [
            "tmux",
            "list-windows",
            "-t",
            "client_pool",
            "-F",
            "#{window_id}\t#{@web-terminal-window-id}\t#{pane_current_path}\t#{@web-terminal-managed-agent-tools}",
        ],
    ]


@pytest.mark.asyncio
async def test_has_window_checks_remote_tmux_target() -> None:
    calls: list[list[str]] = []

    async def fake_run(args: list[str]) -> str:
        calls.append(args)
        return "@9\n"

    runtime = ClientTmuxRuntime(
        client_id=CLIENT_ID,
        server_url="https://control.example.com",
        pool_session="client_pool",
        runner=fake_run,
    )

    assert await runtime.has_window("@9", remote_session_id="client_pool")
    assert calls == [
        ["tmux", "display-message", "-p", "-t", "client_pool:@9", "#{window_id}"],
    ]


@pytest.mark.asyncio
async def test_create_window_launches_direct_agent_inside_default_shell_with_literal_send_keys() -> None:
    calls: list[list[str]] = []

    async def fake_run(args: list[str]) -> str:
        calls.append(args)
        if args[:3] == ["tmux", "new-window", "-P"]:
            return "@9\n"
        return ""

    runtime = ClientTmuxRuntime(
        client_id=CLIENT_ID,
        server_url="https://control.example.com",
        pool_session="client_pool",
        default_shell="/bin/bash",
        runner=fake_run,
    )

    target = await runtime.create_window(
        WINDOW_ID,
        cwd="/workspace/project",
        shell_command="codex resume codex-session",
    )

    new_window_call = next(call for call in calls if call[:3] == ["tmux", "new-window", "-P"])
    assert new_window_call[-1] == runtime.managed_shell_command(WINDOW_ID, project_path="/workspace/project")
    assert target.remote_window_id == "@9"
    assert target.shell_command == "codex resume codex-session"
    assert [
        "tmux",
        "send-keys",
        "-l",
        "-t",
        "client_pool:@9",
        "--",
        "codex --dangerously-bypass-approvals-and-sandbox resume codex-session",
    ] in calls
    assert ["tmux", "send-keys", "-t", "client_pool:@9", "Enter"] in calls


@pytest.mark.asyncio
async def test_has_window_returns_false_for_missing_remote_tmux_target() -> None:
    async def fake_run(args: list[str]) -> str:
        raise RuntimeError("missing window")

    runtime = ClientTmuxRuntime(
        client_id=CLIENT_ID,
        server_url="https://control.example.com",
        pool_session="client_pool",
        runner=fake_run,
    )

    assert not await runtime.has_window("@9")


@pytest.mark.asyncio
async def test_has_window_returns_false_when_tmux_resolves_different_remote_window() -> None:
    async def fake_run(args: list[str]) -> str:
        return "@10\n"

    runtime = ClientTmuxRuntime(
        client_id=CLIENT_ID,
        server_url="https://control.example.com",
        pool_session="client_pool",
        runner=fake_run,
    )

    assert not await runtime.has_window("@9")


@pytest.mark.asyncio
async def test_kill_window_skips_stale_remote_tmux_target() -> None:
    calls: list[list[str]] = []

    async def fake_run(args: list[str]) -> str:
        calls.append(args)
        if args[:4] == ["tmux", "has-session", "-t", "client_pool"]:
            return ""
        if args[:4] == ["tmux", "list-windows", "-t", "client_pool"]:
            return f"@9\t{WINDOW_ID}\t/tmp\t1\n"
        if args[:4] == ["tmux", "display-message", "-p", "-t"]:
            return "@10\n"
        if args[:2] == ["tmux", "kill-window"]:
            raise AssertionError("stale target must not be killed")
        return ""

    runtime = ClientTmuxRuntime(
        client_id=CLIENT_ID,
        server_url="https://control.example.com",
        pool_session="client_pool",
        runner=fake_run,
    )

    await runtime.kill_window(WINDOW_ID)

    assert calls[-1] == ["tmux", "display-message", "-p", "-t", "client_pool:@9", "#{window_id}"]
    assert not any(call[:2] == ["tmux", "kill-window"] for call in calls)
