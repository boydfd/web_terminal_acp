# 2026-05-30: Agent Activity Event Scans

## Source

- Sessions:
  - `~/.codex/sessions/2026/05/30/rollout-2026-05-30T07-29-57-019e77ca-278f-7092-8f07-88a01870b046.jsonl`
  - `~/.codex/sessions/2026/05/30/rollout-2026-05-30T06-27-33-019e7791-062f-7e43-864e-c3ff5e078a3e.jsonl`
- Related commits include `18fb60d`, `972d40f`, `805f524`, and later hardening commits `bb9b8ef`, `40f3b97`.

## Evidence

The cold activity path used `recent_agent_events` LATERAL queries. For each window it fetched up to 200 agent events, including large `payload_json` rows.

Observed from the recorded session:

- Around 254 windows caused repeated per-window scans.
- `pg_stat_statements` showed this query shape reaching about `4295ms`.
- A real cold request took seconds before the fix.

## Root Cause

Activity state was derived by repeatedly scanning `events`. The query scaled with window count and event volume, and it did too much row materialization before application-side filtering.

## Handling

Moved agent activity to projected window state:

- Added `virtual_windows.agent_activity_latest_completed_at`.
- Backfilled `agent_activity_latest_at/latest_event_id` in migration `20260530_0025`.
- Maintained activity/completion projection on new event ingest.
- Changed activity APIs to read projected `virtual_windows` state on PostgreSQL instead of scanning recent events.

## Validation

Recorded results after clearing Redis cache and restarting backend:

- Cold request: `35.4ms`
- Hot requests: `5.0ms - 21.1ms`
- Stale-after-cache-expiry response: `17.1ms`
- Background refresh result: `14.0ms`
- Later sample: `11.7ms`
- No new `recent_agent_events` slow SQL in recent Postgres logs.
- `ruff check` passed.
- Related unit tests: `86 passed`.
- Activity/window integration tests: `14 passed`.

## Follow-Up

Historical completion backfill avoided old JSON with NUL bytes; new completion state is maintained incrementally.
