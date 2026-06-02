# 2026-05-30: Summary Scheduler Source-Type Index

## Source

- Session: `~/.codex/sessions/2026/05/30/rollout-2026-05-30T08-30-50-019e7801-e2b7-78c1-81e6-541cecb6cc60.jsonl`
- Related commit: `8e74453 Fix slow agent activity SQL`

## Evidence

The summary scheduler queried recent agent activity separately for three `source_type` values.

Recorded evidence:

- Existing index did not fit `source_type + created_at + id` ordering.
- Hot windows could filter more than ten thousand rows.
- Original single-source query was about `680ms`.

## Root Cause

The scheduler multiplied near-identical lookups by source type and lacked an index matching `client_id, virtual_window_id, source_type, created_at, id`.

## Handling

- Added migration `20260530_0026_event_window_source_created_id_index.py`.
- Added index `ix_events_client_window_source_created_id`.
- Collapsed three source-type queries into one `source_type IN (...)` query.
- Updated model index declarations and tests.

## Validation

Recorded validation:

- Original single-source query after index: about `1ms`, using index-only scan.
- New combined `IN` query: about `0.2ms`.
- Tests: `43 passed`; another related combination: `46 passed`.
- `alembic_version=20260530_0026`.
- Runtime containers healthy.

## Notes

`pg_stat_statements` preserves pre-fix cumulative slow entries until reset; verify current logs and post-fix query plans before treating old entries as active regressions.
