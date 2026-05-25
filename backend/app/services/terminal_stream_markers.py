from __future__ import annotations

from app.services.terminal_command_marker import CommandMarkerExtractor, ParsedCommandMarker
from app.services.terminal_worktree_marker import ParsedWorktreeMarker, WorktreeMarkerExtractor


class TerminalStreamMarkerExtractor:
    def __init__(self) -> None:
        self._command_extractor = CommandMarkerExtractor()
        self._worktree_extractor = WorktreeMarkerExtractor()

    def feed(self, data: bytes) -> tuple[bytes, list[ParsedCommandMarker], list[ParsedWorktreeMarker]]:
        clean_data, commands = self._command_extractor.feed(data)
        clean_data, worktrees = self._worktree_extractor.feed(clean_data)
        return clean_data, commands, worktrees

    def flush(self) -> bytes:
        pending = self._command_extractor.flush()
        pending += self._worktree_extractor.flush()
        return pending
