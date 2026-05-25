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
    assert "exec '/opt/shells/custom shell'" in command


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
