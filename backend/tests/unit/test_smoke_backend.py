import json
from uuid import uuid4

from scripts import smoke_backend


SEARCH_TEXT = getattr(smoke_backend, "SEARCH_TEXT", "nginx 403 permission denied")


def test_write_claude_smoke_jsonl_writes_run_specific_ingestable_event(tmp_path):
    path = tmp_path / "smoke.jsonl"
    window_id = uuid4()
    run = smoke_backend.SmokeRun.create("abc123")

    smoke_backend.write_claude_smoke_jsonl(path, window_id, run)

    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert payload["type"] == "assistant"
    assert payload["sessionId"] == run.session_id
    assert payload["virtual_window_id"] == str(window_id)
    assert run.token in payload["message"]["content"]


def test_smoke_run_uses_run_specific_paths_and_document_ids():
    first = smoke_backend.SmokeRun.create("first-token")
    second = smoke_backend.SmokeRun.create("second-token")

    assert first.token == "first-token"
    assert first.session_id == "web-terminal-acp-smoke-first-token"
    assert first.terminal_document_id == "web-terminal-acp-smoke-first-token"
    assert first.jsonl_path.name == "web-terminal-acp-smoke-first-token.jsonl"
    assert first.jsonl_path != second.jsonl_path
    assert first.terminal_document_id != second.terminal_document_id


def test_unversioned_existing_initial_schema_needs_head_stamp_for_legacy_create_all_recovery():
    tables = {"folders", "virtual_windows", "ai_sessions", "events", "summary_jobs"}

    assert smoke_backend.should_stamp_existing_initial_schema(tables, has_alembic_version=False)
    assert not smoke_backend.should_stamp_existing_initial_schema(tables, has_alembic_version=True)
    assert not smoke_backend.should_stamp_existing_initial_schema({"folders"}, has_alembic_version=False)


def test_smoke_search_result_matches_only_current_run_window_document_and_token():
    window_id = uuid4()
    stale_window_id = uuid4()
    run = smoke_backend.SmokeRun.create("token-123")
    current_result = {
        "id": run.terminal_document_id,
        "index": "terminal_chunks",
        "snippet": f"{SEARCH_TEXT} token={run.token}",
        "source": {"virtual_window_id": str(window_id)},
    }

    assert smoke_backend.smoke_search_result_matches_run(current_result, window_id, run)
    assert not smoke_backend.smoke_search_result_matches_run(
        {**current_result, "source": {"virtual_window_id": str(stale_window_id)}},
        window_id,
        run,
    )
    assert not smoke_backend.smoke_search_result_matches_run(
        {**current_result, "id": "stale-document"},
        window_id,
        run,
    )
    assert not smoke_backend.smoke_search_result_matches_run(
        {**current_result, "snippet": SEARCH_TEXT},
        window_id,
        run,
    )
