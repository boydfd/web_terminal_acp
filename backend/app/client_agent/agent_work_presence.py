from __future__ import annotations

import logging
import re
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from app.client_agent.terminal import ClientTerminalMultiplexer
from app.client_agent.tmux_runtime import ClientTmuxRuntime

logger = logging.getLogger(__name__)

PRESENCE_SEND_INTERVAL_SECONDS = 30.0
AGENT_WORK_PRESENCE_KIND = "agent_work_presence"

# Keep in sync with app.agent_tools registry command_names / provider_id pairs.
_PROVIDER_COMMAND_NAMES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("claude_code", ("claude",)),
    ("codex", ("codex",)),
    ("cursor_cli", ("agent", "cursor")),
)


@dataclass(frozen=True)
class AgentWorkPresenceSignal:
    providers: tuple[str, ...]
    reasons: tuple[str, ...]


def agent_command_tokens() -> frozenset[str]:
    names = {name for _, command_names in _PROVIDER_COMMAND_NAMES for name in command_names}
    names.add("acpx")
    return frozenset(names)


async def detect_agent_work_presence(
    window_id: UUID,
    *,
    terminal: ClientTerminalMultiplexer | None,
    runtime: ClientTmuxRuntime | None,
) -> AgentWorkPresenceSignal | None:
    providers: set[str] = set()
    reasons: set[str] = set()

    process_providers = await _detect_process_providers(window_id, terminal=terminal, runtime=runtime)
    providers.update(process_providers)
    if process_providers:
        reasons.add("process")

    if not providers:
        return None
    return AgentWorkPresenceSignal(
        providers=tuple(sorted(providers)),
        reasons=tuple(sorted(reasons)),
    )


async def _detect_process_providers(
    window_id: UUID,
    *,
    terminal: ClientTerminalMultiplexer | None,
    runtime: ClientTmuxRuntime | None,
) -> set[str]:
    if terminal is None or runtime is None or not terminal.is_registered(window_id):
        return set()

    tmux_target = terminal.tmux_target_for(window_id)
    if tmux_target is None:
        return set()

    try:
        output = await runtime._run(["tmux", "list-panes", "-t", tmux_target, "-F", "#{pane_pid}"])
    except Exception:
        logger.debug(
            "failed to list tmux pane pids for agent work presence",
            extra={"window_id": str(window_id), "tmux_target": tmux_target},
            exc_info=True,
        )
        return set()

    pane_pids = [int(line.strip()) for line in output.splitlines() if line.strip().isdigit()]
    if not pane_pids:
        return set()

    parent_map = _build_parent_map()
    watched_pids = _descendant_pids(pane_pids, parent_map)
    command_tokens = agent_command_tokens()
    providers: set[str] = set()
    for pid in watched_pids:
        provider = _provider_from_cmdline(_cmdline_for_pid(pid), command_tokens)
        if provider is not None:
            providers.add(provider)
    return providers


def _build_parent_map() -> dict[int, int]:
    parent_map: dict[int, int] = {}
    proc_root = Path("/proc")
    try:
        entries = list(proc_root.iterdir())
    except OSError:
        return parent_map

    for entry in entries:
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        try:
            status_text = (entry / "status").read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in status_text.splitlines():
            if line.startswith("PPid:"):
                parts = line.split()
                if len(parts) >= 2 and parts[1].isdigit():
                    parent_map[pid] = int(parts[1])
                break
    return parent_map


def _descendant_pids(root_pids: list[int], parent_map: dict[int, int]) -> set[int]:
    children: dict[int, list[int]] = defaultdict(list)
    for pid, ppid in parent_map.items():
        children[ppid].append(pid)

    seen: set[int] = set()
    stack = list(root_pids)
    while stack:
        pid = stack.pop()
        if pid in seen:
            continue
        seen.add(pid)
        stack.extend(children.get(pid, []))
    return seen


def _cmdline_for_pid(pid: int) -> str:
    try:
        raw = (Path("/proc") / str(pid) / "cmdline").read_bytes()
    except OSError:
        return ""
    return raw.replace(b"\0", b" ").decode(errors="replace").strip()


def _provider_from_cmdline(cmdline: str, command_tokens: frozenset[str]) -> str | None:
    if not cmdline:
        return None
    tokens = cmdline.split()
    basename = Path(tokens[0]).name.lower() if tokens else ""
    for provider_id, command_names in _PROVIDER_COMMAND_NAMES:
        if basename in command_names:
            return provider_id
        if any(re.search(rf"(?:^|\s){re.escape(name)}(?:\s|$)", cmdline) for name in command_names):
            return provider_id
    if basename == "acpx" or re.search(r"(?:^|\s)acpx(?:\s|$)", cmdline):
        for provider_id, command_names in _PROVIDER_COMMAND_NAMES:
            for name in command_names:
                if re.search(rf"(?:^|\s){re.escape(name)}(?:\s|$)", cmdline):
                    return provider_id
    if basename in command_tokens:
        for provider_id, command_names in _PROVIDER_COMMAND_NAMES:
            if basename in command_names:
                return provider_id
    return None
