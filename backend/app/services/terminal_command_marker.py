from __future__ import annotations

import base64
import json
from typing import Any

COMMAND_MARKER_PREFIX = b"\x1b]777;web-terminal-command;"
_COMMAND_MARKER_END = b"\x07"

ParsedCommandMarker = dict[str, Any]


def extract_command_markers(data: bytes) -> tuple[bytes, list[ParsedCommandMarker]]:
    clean_data, commands, _pending = _extract_command_markers(data, keep_incomplete=False)
    return clean_data, commands


class CommandMarkerExtractor:
    def __init__(self) -> None:
        self._pending = b""

    def feed(self, data: bytes) -> tuple[bytes, list[ParsedCommandMarker]]:
        clean_data, commands, pending = _extract_command_markers(
            self._pending + data,
            keep_incomplete=True,
        )
        self._pending = pending
        return clean_data, commands

    def flush(self) -> bytes:
        pending = self._pending
        self._pending = b""
        return pending


def _extract_command_markers(
    data: bytes,
    *,
    keep_incomplete: bool,
) -> tuple[bytes, list[ParsedCommandMarker], bytes]:
    clean_parts: list[bytes] = []
    commands: list[ParsedCommandMarker] = []
    pending = b""
    position = 0

    while position < len(data):
        marker_start = data.find(COMMAND_MARKER_PREFIX, position)
        if marker_start < 0:
            remainder = data[position:]
            if keep_incomplete:
                partial_length = _partial_prefix_suffix_length(remainder)
                if partial_length:
                    clean_parts.append(remainder[:-partial_length])
                    pending = remainder[-partial_length:]
                else:
                    clean_parts.append(remainder)
            else:
                clean_parts.append(remainder)
            break

        clean_parts.append(data[position:marker_start])
        marker_end = data.find(_COMMAND_MARKER_END, marker_start + len(COMMAND_MARKER_PREFIX))
        if marker_end < 0:
            if keep_incomplete:
                pending = data[marker_start:]
            break

        marker_body = data[marker_start + len(COMMAND_MARKER_PREFIX):marker_end]
        parsed = _parse_marker_body(marker_body)
        if parsed is not None:
            commands.append(parsed)
        position = marker_end + len(_COMMAND_MARKER_END)

    return b"".join(clean_parts), commands, pending


def _partial_prefix_suffix_length(data: bytes) -> int:
    max_length = min(len(data), len(COMMAND_MARKER_PREFIX) - 1)
    for length in range(max_length, 0, -1):
        if COMMAND_MARKER_PREFIX.startswith(data[-length:]):
            return length
    return 0


def _parse_marker_body(marker_body: bytes) -> ParsedCommandMarker | None:
    try:
        text = marker_body.decode("ascii")
    except UnicodeDecodeError:
        return None

    fields: dict[str, str] = {}
    for part in text.split(";"):
        key, separator, value = part.partition("=")
        if separator != "=" or not key:
            return None
        fields[key] = value

    window_id = fields.get("window_id")
    payload = fields.get("payload")
    if not window_id or not payload:
        return None

    try:
        decoded_payload = base64.b64decode(payload, validate=True)
        parsed_payload = json.loads(decoded_payload.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(parsed_payload, dict):
        return None

    command: ParsedCommandMarker = {"window_id": window_id}
    command.update(parsed_payload)
    return command
