---
name: slow-sql-analysis
description: Analyze Web Terminal ACP PostgreSQL slow SQL efficiently and record every investigation. Use when Codex is asked to check recent slow SQL, inspect database performance, review pg_stat_statements, investigate Postgres duration logs, tune SQLAlchemy queries or indexes, or document slow SQL fixes for this project.
---

# Slow SQL Analysis

## Overview

Use this skill to avoid broad codebase searches before reading the database evidence. Start from Postgres slow logs and `pg_stat_statements`, map only confirmed query shapes back to code, then write a dated record under `docs/performance-tuning/slow-sqls/`.

## Workflow

1. Confirm the running stack:

   ```bash
   docker ps --format '{{.Names}}\t{{.Image}}\t{{.Status}}\t{{.Ports}}' | rg 'web_terminal|postgres|backend'
   ```

   Prefer the Web Terminal ACP Postgres container. Do not scan unrelated local or LAN services.

2. Read recent slow logs first:

   ```bash
   docker logs --since 24h <postgres-container> 2>&1 | rg -n 'duration:|statement:|execute '
   ```

   Treat `psql` statements and agent-triggered diagnostic SQL separately from application SQL.

3. Read cumulative statistics second:

   ```bash
   docker exec <postgres-container> psql -U web_terminal -d web_terminal_acp -P pager=off -c "
   SELECT calls,
          round(total_exec_time::numeric, 2) AS total_ms,
          round(mean_exec_time::numeric, 2) AS mean_ms,
          round(max_exec_time::numeric, 2) AS max_ms,
          rows,
          left(regexp_replace(query, '\s+', ' ', 'g'), 500) AS query
   FROM pg_stat_statements
   WHERE dbid = (SELECT oid FROM pg_database WHERE datname = current_database())
   ORDER BY max_exec_time DESC
   LIMIT 20;"
   ```

4. Map only confirmed query shapes back to code with targeted `rg` searches. Good anchors in this repo include:

   - `recent_agent_events`, `latest_window_event`, `terminal_command_finished`
   - `Event.kind ==`, `Event.source_type`, `Event.virtual_window_id`
   - migration index names from `backend/migrations/versions/`

5. Use `EXPLAIN (ANALYZE, BUFFERS)` only for the specific query shape and realistic parameters. Record before and after plans when changing indexes or query shape.

6. Fix root causes conservatively:

   - Prefer narrow partial indexes for rare event kinds over another broad `events` index.
   - Prefer projected window state over repeatedly scanning large event ranges.
   - Use concurrent Postgres index creation in migrations for production-sized tables.
   - Update `backend/app/models.py`, Alembic migrations, and focused tests together.
   - If the change touches client-visible behavior or client code, follow the project version bump rules.

7. Verify:

   - Rerun `EXPLAIN (ANALYZE, BUFFERS)` for the fixed query.
   - Run focused backend tests and `ruff` for touched files.
   - Check recent Postgres logs after the fix.

## Required Record

Always create or update a Markdown file under:

```text
docs/performance-tuning/slow-sqls/
```

Use a date-prefixed filename such as `2026-06-02-terminal-command-event-lookups.md`. Include:

- Trigger/request and session source when known.
- Slow SQL evidence: timestamps, durations, query shape, affected table sizes if checked.
- Root cause, not just symptoms.
- Code changes, migration names, commit hashes.
- Before/after `EXPLAIN` or log evidence.
- Validation commands and results.
- Follow-ups explicitly separated from completed work.

When scanning `~/.codex/sessions`, do not dump whole JSONL files. Extract only matching filenames, timestamps, final answers, and command outputs around slow SQL keywords.

## Known Project Patterns

- Current Postgres slow threshold is controlled by `POSTGRES_LOG_MIN_DURATION_STATEMENT_MS` in `docker-compose.yml`.
- `pg_stat_statements` is enabled by migration `20260530_0021_enable_pg_stat_statements.py`.
- The `events` table is the recurring hotspot; verify whether a query is scanning terminal output, agent activity, or rare command marker events.
- Existing useful indexes include `ix_events_agent_activity_window_created`, `ix_events_client_window_kind_created_id`, and `ix_events_client_window_source_created_id`.
- Keep investigation SQL out of the "application regression" bucket; full-table diagnostic counts can appear in slow logs.
