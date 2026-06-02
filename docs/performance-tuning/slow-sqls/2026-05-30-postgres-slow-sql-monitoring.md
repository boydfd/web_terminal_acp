# 2026-05-30: Postgres Slow SQL Monitoring

## Source

- Session: `~/.codex/sessions/2026/05/30/rollout-2026-05-30T06-50-37-019e77a6-25db-7541-92bf-82fb1a9ec5df.jsonl`
- Related commits: `b148c3e Enable Postgres slow query monitoring`, merge commits `a9b058c`, `7213eab`

## Evidence

Before this work, Postgres had no useful slow query visibility:

- `log_min_duration_statement = -1`
- `pg_stat_statements` was not enabled.

## Handling

Enabled runtime observability:

- `log_min_duration_statement=500ms`
- `shared_preload_libraries=pg_stat_statements`
- `pg_stat_statements.track=all`
- `track_io_timing=on`
- `log_lock_waits=on`
- `deadlock_timeout=1s`

Added migration `20260530_0021_enable_pg_stat_statements.py`.

## Validation

- `pg_stat_statements` installed, version reported as `1.10`.
- Database migrated to `20260530_0022` during the recorded session.
- Postgres, backend, frontend, Redis, and Elasticsearch were healthy.
- Recorded test result: `23 passed`.

## Notes

Future slow SQL checks should start with Postgres logs and `pg_stat_statements`, not broad repository searches.
