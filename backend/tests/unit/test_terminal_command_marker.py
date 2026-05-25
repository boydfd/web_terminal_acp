from __future__ import annotations

import base64
import json

from app.services.terminal_command_marker import CommandMarkerExtractor, extract_command_markers

WINDOW_ID = "87654321-4321-8765-4321-876543218765"


def _marker(command: str, *, sequence: int = 1, shell: str = "bash") -> bytes:
    payload = {
        "command": command,
        "shell": shell,
        "cwd": "/workspace/project",
        "captured_at": "2026-05-21T12:00:00+00:00",
        "sequence": sequence,
    }
    encoded = base64.b64encode(json.dumps(payload).encode("utf-8")).decode("ascii")
    return (
        f"\x1b]777;web-terminal-command;window_id={WINDOW_ID};payload={encoded}\x07"
    ).encode("ascii")


def test_extract_command_markers_parses_marker_and_removes_it_from_output() -> None:
    clean_data, commands = extract_command_markers(_marker("ls -la"))

    assert clean_data == b""
    assert commands == [
        {
            "window_id": WINDOW_ID,
            "command": "ls -la",
            "shell": "bash",
            "cwd": "/workspace/project",
            "captured_at": "2026-05-21T12:00:00+00:00",
            "sequence": 1,
        }
    ]


def test_extract_command_markers_parses_multiple_markers() -> None:
    clean_data, commands = extract_command_markers(
        _marker("pwd", sequence=1) + _marker("git status", sequence=2, shell="zsh")
    )

    assert clean_data == b""
    assert [command["command"] for command in commands] == ["pwd", "git status"]
    assert [command["sequence"] for command in commands] == [1, 2]
    assert commands[1]["shell"] == "zsh"


def test_extract_command_markers_preserves_normal_output_around_marker() -> None:
    clean_data, commands = extract_command_markers(b"before\n" + _marker("whoami") + b"after\n")

    assert clean_data == b"before\nafter\n"
    assert [command["command"] for command in commands] == ["whoami"]


def test_streaming_extractor_buffers_half_marker_without_leaking_control_sequence() -> None:
    marker = _marker("echo split")
    extractor = CommandMarkerExtractor()

    first_clean, first_commands = extractor.feed(b"prompt> " + marker[:20])
    second_clean, second_commands = extractor.feed(marker[20:] + b"done\n")

    assert b"\x1b]777;web-terminal-command" not in first_clean
    assert first_clean == b"prompt> "
    assert first_commands == []
    assert second_clean == b"done\n"
    assert [command["command"] for command in second_commands] == ["echo split"]


def test_extract_command_markers_drops_unfinished_marker_in_stateless_mode() -> None:
    clean_data, commands = extract_command_markers(
        b"visible" + b"\x1b]777;web-terminal-command;window_id=" + WINDOW_ID.encode("ascii")
    )

    assert clean_data == b"visible"
    assert commands == []
    assert b"\x1b]777;web-terminal-command" not in clean_data
