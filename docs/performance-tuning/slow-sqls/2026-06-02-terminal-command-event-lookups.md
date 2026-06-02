# 2026-06-02: Terminal Command Event Lookups

## Source

- Session: `~/.codex/sessions/2026/06/02/rollout-2026-06-02T01-39-50-019e85fc-afab-7a32-95b4-88d5354ac54b.jsonl`
- Related commit: `6e10fa8 Optimize terminal command event lookups`

## Evidence

Recent 24-hour Postgres slow logs showed five statements over the `500ms` threshold:

- Event lookup by a large `events.id IN (...)` list: about `548ms`.
- `latest_window_event` LATERAL lookup for terminal input commands: about `646ms`.
- One `COMMIT`: about `542ms`.
- Two `terminal_command_finished` lookups by window/kind/time: about `518ms` and `1092ms`.

Current table size:

- `events`: about `2.7GB` total, about `1.1GB` heap.
- About `1.71M` event rows.

`EXPLAIN` before the final fix showed the `terminal_command_finished` query using `ix_events_agent_record_non_output_window`, then filtering by `kind`, removing 1295 rows. Current warm execution was about `22ms`, but the slow log showed this plan could exceed 1s under IO pressure.

## Root Cause

The rare terminal command event kinds were using broad event indexes. The query shape needed a tiny kind-specific access path so it would not scan non-output agent records and filter by kind.

## Handling

Added two partial indexes:

- `ix_events_terminal_input_window_created`
- `ix_events_terminal_finished_window_created`

Files changed:

- `backend/app/models.py`
- `backend/migrations/versions/20260602_0031_terminal_command_event_partial_indexes.py`
- `backend/tests/unit/test_models.py`

The migration uses `CREATE INDEX CONCURRENTLY IF NOT EXISTS` on PostgreSQL.

## Validation

Runtime database:

- Upgraded to Alembic `20260602_0031`.
- Index sizes:
  - `ix_events_terminal_finished_window_created`: `40 kB`
  - `ix_events_terminal_input_window_created`: `56 kB`

`EXPLAIN (ANALYZE, BUFFERS)` after the fix:

- `terminal_command_finished` lookup used `ix_events_terminal_finished_window_created`, `Execution Time: 0.166 ms`.
- latest terminal input lookup used `ix_events_terminal_input_window_created`, `Execution Time: 2.984 ms`.

Tests:

- `uv run pytest tests/unit/test_models.py tests/unit/test_terminal_work_status.py`: `68 passed`.
- `uv run ruff check app/models.py migrations/versions/20260602_0031_terminal_command_event_partial_indexes.py tests/unit/test_models.py`: passed.

## Notes

The investigation itself generated slow log entries for full-table diagnostic SQL and concurrent index creation. Those were separated from application SQL in the final assessment.
