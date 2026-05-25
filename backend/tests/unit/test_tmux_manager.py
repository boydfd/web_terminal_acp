import asyncio

import pytest

from app.services.tmux_manager import (
    TmuxAttachTarget,
    TmuxCommandError,
    TmuxManager,
    TmuxTarget,
    build_attach_command,
    shadow_session_name,
)


def test_shadow_session_name_is_stable():
    assert shadow_session_name("@42") == "web_terminal_view__42"


def test_shadow_session_name_sanitizes_unsafe_characters():
    assert shadow_session_name("pane:../weird id") == "web_terminal_view_pane____weird_id"


def test_build_attach_command_targets_shadow_session():
    target = TmuxAttachTarget(session="web_terminal_view__42")
    command = build_attach_command(target)
    assert command == ["tmux", "attach-session", "-t", "web_terminal_view__42"]


@pytest.mark.asyncio
async def test_create_window_uses_pool_and_returns_tmux_target():
    calls: list[list[str]] = []

    async def fake_run(args: list[str]) -> str:
        calls.append(args)
        if args[:3] == ["tmux", "new-window", "-P"]:
            return "@42\n"
        return ""

    manager = TmuxManager(pool_session="web_terminal_acp_pool", default_shell="/bin/bash", runner=fake_run)

    target = await manager.create_window("/tmp/project", "/bin/zsh")

    assert target == TmuxTarget(
        session="web_terminal_acp_pool",
        window_id="@42",
        cwd="/tmp/project",
        shell_command="/bin/zsh",
    )
    assert calls == [
        ["tmux", "has-session", "-t", "web_terminal_acp_pool"],
        ["tmux", "set-option", "-t", "web_terminal_acp_pool", "window-size", "manual"],
        ["tmux", "set-option", "-s", "set-clipboard", "external"],
        ["tmux", "show-options", "-s", "terminal-features"],
        ["tmux", "set-option", "-as", "terminal-features", ",xterm*:clipboard"],
        ["tmux", "new-window", "-P", "-F", "#{window_id}", "-t", "web_terminal_acp_pool", "-c", "/tmp/project", "/bin/zsh"],
    ]


@pytest.mark.asyncio
async def test_ensure_pool_creates_missing_pool_session():
    calls: list[list[str]] = []

    async def fake_run(args: list[str]) -> str:
        calls.append(args)
        if args == ["tmux", "has-session", "-t", "web_terminal_acp_pool"]:
            raise TmuxCommandError(args, 1, "missing session")
        return ""

    manager = TmuxManager(pool_session="web_terminal_acp_pool", default_shell="/bin/bash", runner=fake_run)

    await manager.ensure_pool()

    assert calls == [
        ["tmux", "has-session", "-t", "web_terminal_acp_pool"],
        ["tmux", "has-session", "-t", "web_terminal_acp_pool"],
        ["tmux", "new-session", "-d", "-s", "web_terminal_acp_pool", "/bin/bash"],
        ["tmux", "set-option", "-t", "web_terminal_acp_pool", "window-size", "manual"],
        ["tmux", "set-option", "-s", "set-clipboard", "external"],
        ["tmux", "show-options", "-s", "terminal-features"],
        ["tmux", "set-option", "-as", "terminal-features", ",xterm*:clipboard"],
    ]


@pytest.mark.asyncio
async def test_ensure_shadow_session_groups_to_pool_and_selects_window():
    calls: list[list[str]] = []

    async def fake_run(args: list[str]) -> str:
        calls.append(args)
        if args == ["tmux", "has-session", "-t", "web_terminal_view__42"]:
            raise TmuxCommandError(args, 1, "missing session")
        return ""

    manager = TmuxManager(pool_session="web_terminal_acp_pool", default_shell="/bin/bash", runner=fake_run)

    attach_target = await manager.ensure_shadow_session(
        TmuxTarget(session="web_terminal_acp_pool", window_id="@42")
    )

    assert attach_target == TmuxAttachTarget(session="web_terminal_view__42")
    assert calls == [
        ["tmux", "has-session", "-t", "web_terminal_acp_pool"],
        ["tmux", "set-option", "-t", "web_terminal_acp_pool", "window-size", "manual"],
        ["tmux", "set-option", "-s", "set-clipboard", "external"],
        ["tmux", "show-options", "-s", "terminal-features"],
        ["tmux", "set-option", "-as", "terminal-features", ",xterm*:clipboard"],
        ["tmux", "has-session", "-t", "web_terminal_view__42"],
        ["tmux", "has-session", "-t", "web_terminal_view__42"],
        ["tmux", "new-session", "-d", "-t", "web_terminal_acp_pool", "-s", "web_terminal_view__42"],
        ["tmux", "set-option", "-t", "web_terminal_view__42", "window-size", "manual"],
        ["tmux", "select-window", "-t", "web_terminal_view__42:@42"],
    ]


@pytest.mark.asyncio
async def test_has_window_returns_true_when_tmux_target_exists():
    calls: list[list[str]] = []

    async def fake_run(args: list[str]) -> str:
        calls.append(args)
        return "@42\n"

    manager = TmuxManager(pool_session="web_terminal_acp_pool", default_shell="/bin/bash", runner=fake_run)

    assert await manager.has_window(TmuxTarget(session="web_terminal_acp_pool", window_id="@42")) is True
    assert calls == [
        ["tmux", "display-message", "-p", "-t", "web_terminal_acp_pool:@42", "#{window_id}"],
    ]


@pytest.mark.asyncio
async def test_current_window_id_reads_active_window_for_session():
    calls: list[list[str]] = []

    async def fake_run(args: list[str]) -> str:
        calls.append(args)
        return "@43\n"

    manager = TmuxManager(pool_session="web_terminal_acp_pool", default_shell="/bin/bash", runner=fake_run)

    assert await manager.current_window_id("web_terminal_view__42") == "@43"
    assert calls == [
        ["tmux", "display-message", "-p", "-t", "web_terminal_view__42", "#{window_id}"],
    ]


@pytest.mark.asyncio
async def test_has_window_returns_false_when_tmux_target_is_missing():
    async def fake_run(args: list[str]) -> str:
        raise TmuxCommandError(args, 1, "missing window")

    manager = TmuxManager(pool_session="web_terminal_acp_pool", default_shell="/bin/bash", runner=fake_run)

    assert await manager.has_window(TmuxTarget(session="web_terminal_acp_pool", window_id="@42")) is False


@pytest.mark.asyncio
async def test_ensure_pool_is_idempotent_when_concurrent_creation_races():
    sessions: set[str] = set()
    calls: list[list[str]] = []
    create_lock = asyncio.Lock()

    async def fake_run(args: list[str]) -> str:
        calls.append(args)
        if args == ["tmux", "has-session", "-t", "web_terminal_acp_pool"]:
            if "web_terminal_acp_pool" not in sessions:
                await asyncio.sleep(0)
                raise TmuxCommandError(args, 1, "missing session")
            return ""
        if args == ["tmux", "new-session", "-d", "-s", "web_terminal_acp_pool", "/bin/bash"]:
            async with create_lock:
                if "web_terminal_acp_pool" in sessions:
                    raise TmuxCommandError(args, 1, "duplicate session")
                await asyncio.sleep(0)
                sessions.add("web_terminal_acp_pool")
            return ""
        return ""

    manager = TmuxManager(pool_session="web_terminal_acp_pool", default_shell="/bin/bash", runner=fake_run)

    await asyncio.gather(*(manager.ensure_pool() for _ in range(2)))

    assert "web_terminal_acp_pool" in sessions
    assert calls.count(["tmux", "new-session", "-d", "-s", "web_terminal_acp_pool", "/bin/bash"]) <= 2


@pytest.mark.asyncio
async def test_ensure_shadow_session_is_idempotent_when_concurrent_creation_races():
    sessions = {"web_terminal_acp_pool"}
    calls: list[list[str]] = []
    create_lock = asyncio.Lock()

    async def fake_run(args: list[str]) -> str:
        calls.append(args)
        if args == ["tmux", "has-session", "-t", "web_terminal_acp_pool"]:
            return ""
        if args == ["tmux", "has-session", "-t", "web_terminal_view__42"]:
            if "web_terminal_view__42" not in sessions:
                await asyncio.sleep(0)
                raise TmuxCommandError(args, 1, "missing session")
            return ""
        if args == ["tmux", "new-session", "-d", "-t", "web_terminal_acp_pool", "-s", "web_terminal_view__42"]:
            async with create_lock:
                if "web_terminal_view__42" in sessions:
                    raise TmuxCommandError(args, 1, "duplicate session")
                await asyncio.sleep(0)
                sessions.add("web_terminal_view__42")
            return ""
        if args in [
            ["tmux", "set-option", "-t", "web_terminal_acp_pool", "window-size", "manual"],
            ["tmux", "set-option", "-t", "web_terminal_view__42", "window-size", "manual"],
            ["tmux", "set-option", "-s", "set-clipboard", "external"],
            ["tmux", "show-options", "-s", "terminal-features"],
            ["tmux", "set-option", "-as", "terminal-features", ",xterm*:clipboard"],
        ]:
            return ""
        if args == ["tmux", "select-window", "-t", "web_terminal_view__42:@42"]:
            return ""
        raise AssertionError(f"unexpected tmux command: {args}")

    manager = TmuxManager(pool_session="web_terminal_acp_pool", default_shell="/bin/bash", runner=fake_run)

    targets = await asyncio.gather(
        *(manager.ensure_shadow_session(TmuxTarget(session="web_terminal_acp_pool", window_id="@42")) for _ in range(2))
    )

    assert targets == [TmuxAttachTarget(session="web_terminal_view__42"), TmuxAttachTarget(session="web_terminal_view__42")]
    assert "web_terminal_view__42" in sessions
    assert calls.count(["tmux", "select-window", "-t", "web_terminal_view__42:@42"]) == 2
