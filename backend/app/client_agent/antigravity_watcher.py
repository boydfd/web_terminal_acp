from __future__ import annotations

from pathlib import Path
from uuid import UUID


def antigravity_home_for_window(window_id: UUID | str) -> Path:
    return Path.home() / ".web-terminal-acp" / "antigravity-cli-homes" / str(window_id)


def iter_antigravity_transcript_files(window_id: UUID | str) -> list[Path]:
    root = antigravity_home_for_window(window_id) / "brain"
    if not root.exists():
        return []
    return sorted(
        path
        for path in root.glob("*/.system_generated/logs/transcript.jsonl")
        if path.is_file()
    )


def antigravity_session_id_from_transcript_path(path: Path) -> str | None:
    try:
        brain_index = path.parts.index("brain")
    except ValueError:
        return None
    session_index = brain_index + 1
    if session_index >= len(path.parts):
        return None
    session_id = path.parts[session_index].strip()
    return session_id or None
