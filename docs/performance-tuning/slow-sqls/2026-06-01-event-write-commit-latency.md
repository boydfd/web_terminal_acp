# 2026-06-01: Event Write and COMMIT Latency

## Source

- Session: `~/.codex/sessions/2026/06/01/rollout-2026-06-01T06-46-45-019e81ef-5131-74f0-a5d2-363a68f318e9.jsonl`
- Related commits:
  - `fe3e3be fix: reduce event write latency`
  - `7b8a250 fix: keep local client commit durable for trace ingest`

## Evidence

The active slow point was no longer `recent_agent_events`. Recent slow logs showed event writes and `COMMIT` being delayed during Postgres checkpoints.

## Root Cause

High-frequency, replayable event writes were paying full synchronous commit cost. Checkpoint settings also caused write bursts to show up as COMMIT latency.

## Handling

- Added transaction-level deferred durability for replayable/low-priority event writes with `SET LOCAL synchronous_commit = OFF`.
- Covered agent events, agent presence, terminal command/output activity, trace ingest, and JSONL ingest.
- Kept client/window metadata commits durable.
- Added trace ingest boundary handling so local client creation stays durable before event writes are degraded.
- Updated Postgres Compose settings:
  - `checkpoint_timeout=15min`
  - `max_wal_size=2GB`
- Bumped version to `2.19.1`.

## Validation

Recorded validation:

- Unit tests: `21 passed`.
- Trace ingest integration: `12 passed`.
- Client-agent event / terminal output integration: `3 passed`.
- `docker compose config` passed.
- Rebuilt and restarted Postgres, backend, and frontend.
- Services healthy.
- Runtime parameters confirmed:
  - `checkpoint_timeout=15min`
  - `max_wal_size=2GB`
  - global `synchronous_commit=on`
- Backend version confirmed: `2.19.1`.

## Notes

Use transaction-local durability changes only for data that can be replayed or safely regenerated.
