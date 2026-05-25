import asyncio
import contextlib

import pytest

import app.services.runtime.local as local_runtime
from app.services.runtime.local import LocalTerminalRuntime, _LocalTerminalSession
from app.services.runtime.types import RuntimeWindow


@pytest.mark.asyncio
async def test_resize_ignores_repeated_dimensions(monkeypatch) -> None:
    resizes: list[tuple[int, int, int]] = []
    shadow_resizes: list[tuple[RuntimeWindow, int, int]] = []
    keepalive = asyncio.create_task(asyncio.sleep(10))
    window = RuntimeWindow(session_id="web-terminal", window_id="@7")

    def fake_resize(fd: int, control) -> None:
        resizes.append((fd, control.cols, control.rows))

    class FakeTmuxManager:
        async def resize_shadow_window(self, target_window: RuntimeWindow, *, cols: int, rows: int) -> None:
            shadow_resizes.append((target_window, cols, rows))

    monkeypatch.setattr(local_runtime, "apply_pty_resize", fake_resize)
    runtime = LocalTerminalRuntime(FakeTmuxManager())
    runtime._sessions[window] = _LocalTerminalSession(master_fd=123, process=object(), task=keepalive)
    try:
        await runtime.resize(window, cols=80, rows=24)
        await runtime.resize(window, cols=80, rows=24)
        await runtime.resize(window, cols=81, rows=24)
    finally:
        keepalive.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await keepalive

    assert resizes == [(123, 80, 24), (123, 81, 24)]
    assert shadow_resizes == [(window, 80, 24), (window, 81, 24)]
