# 2026-05-31: User Input Projection Scans

## Source

- Session: `~/.codex/sessions/2026/05/31/rollout-2026-05-31T17-55-36-019e7f2d-4f0f-7772-bf83-f2ff1f447b0f.jsonl`
- Related commit: `40f3b97 fix: avoid agent activity slow event scans`

## Evidence

Two separate problems were identified:

- Migration `20260531_0029` had heavy `UPDATE virtual_windows ... FROM events ... payload_json` statements. `pg_stat_statements` showed about `29.6s` and `26.0s`.
- Runtime window activity still used `recent_agent_events` scans to derive latest agent user input. With 309 windows this averaged up to about `3.1s`.

## Root Cause

The system still derived latest user input from event history in both a migration and a runtime path. That repeated large `events` scans and parsed JSON payloads online.

## Handling

- Added `virtual_windows.agent_activity_latest_user_input_at`.
- Added migration `20260531_0030_agent_activity_user_input_projection.py`.
- Maintained latest user input projection on agent event ingest.
- Stopped PostgreSQL hot path from scanning `recent_agent_events` to determine user input.
- Changed heavy migration `20260531_0029` reprojection to opt-in via `WEB_TERMINAL_RUN_HEAVY_ACTIVITY_REPROJECTION=1`.
- Bumped version to `2.18.6`.

## Validation

Recorded validation:

- `ruff check` passed.
- Key tests: `96 passed`.
- Applied `20260531_0030` to actual Postgres.
- Rebuilt and restarted backend.
- Runtime version: `2.18.6`.
- Loading 309 windows activity took about `497ms`.
- `pg_stat_statements` count for `recent_agent_events` was `0` after reset and validation.

## Notes

The session intentionally ran `pg_stat_statements_reset()` to verify new query behavior cleanly.
