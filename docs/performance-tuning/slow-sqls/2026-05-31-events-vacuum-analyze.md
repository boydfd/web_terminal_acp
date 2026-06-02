# 2026-05-31: Events VACUUM ANALYZE

## Source

- Session: `~/.codex/sessions/2026/05/31/rollout-2026-05-31T14-35-49-019e7e76-6917-7f93-99ca-3611735ba34c.jsonl`
- Code change: none

## Evidence

The slow-query statistics still showed historical slow entries, mainly `recent_agent_events`, but recent backend logs did not show new application slow queries.

Database maintenance evidence from the recorded session:

- `events` table statistics were stale: estimated live rows were about `87k`, actual after refresh was about `1.677M`.
- `events` dead tuples dropped from about `53k` to about `168`.
- Maintenance statements such as `CREATE INDEX`, projection backfill, manual statistics SQL, and `VACUUM` appeared as slow entries but were not online request regressions.

## Root Cause

The active code/index path was already acceptable. The immediate issue was stale planner statistics plus table/index bloat on `events`.

## Handling

Ran `VACUUM (ANALYZE)` on:

- `events`
- `virtual_windows`
- `ai_sessions`
- `summary_jobs`

## Validation

- Key `events.created_at/source_type` query used `ix_events_agent_activity_window_created`.
- Recorded `Execution Time: 0.246 ms`.
- No new application-side slow SQL in the following 3 minutes.

## Operational Note

Parallel vacuum failed in this container because `/dev/shm` was too small. Use:

```sql
VACUUM (ANALYZE, VERBOSE, PARALLEL 0) events;
```
