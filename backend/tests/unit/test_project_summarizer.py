from __future__ import annotations

import json

import pytest

from app.services.project_summarizer import (
    build_project_summary_prompt,
    parse_project_summary_response,
    project_path_from_runtime_tags,
)


def test_project_path_from_runtime_tags_prefers_absolute_path() -> None:
    assert project_path_from_runtime_tags(["codex", "/workspace/project"]) == "/workspace/project"
    assert project_path_from_runtime_tags(["/tmp/demo"]) == "/tmp/demo"
    assert project_path_from_runtime_tags(["codex"]) is None


def test_parse_project_summary_response_accepts_name() -> None:
    result = parse_project_summary_response(json.dumps({"name": "终端编排"}))
    assert result.name == "终端编排"


def test_parse_project_summary_response_rejects_unknown_field() -> None:
    with pytest.raises(ValueError, match="unknown field"):
        parse_project_summary_response(json.dumps({"name": "ok", "extra": "nope"}))


def test_build_project_summary_prompt_uses_output_language() -> None:
    prompt = build_project_summary_prompt(
        {"project_path": "/workspace/demo", "directory_entries": ["src/"], "recent_user_inputs": ["npm test"]},
        "中文",
    )
    assert "中文" in prompt
    assert "/workspace/demo" in prompt
