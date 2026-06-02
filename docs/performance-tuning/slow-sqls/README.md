# Slow SQL Records

This directory records Web Terminal ACP slow SQL investigations. Add a dated record for every slow SQL check, even when the conclusion is "no code change".

## Records

- [2026-05-30: enable Postgres slow SQL monitoring](2026-05-30-postgres-slow-sql-monitoring.md)
- [2026-05-30: remove agent activity event scans from activity APIs](2026-05-30-agent-activity-event-scans.md)
- [2026-05-30: tune summary scheduler source-type lookups](2026-05-30-summary-scheduler-source-type-index.md)
- [2026-05-31: vacuum stale event table statistics](2026-05-31-events-vacuum-analyze.md)
- [2026-05-31: avoid user-input projection event scans](2026-05-31-user-input-projection-scans.md)
- [2026-06-01: reduce event write and COMMIT latency](2026-06-01-event-write-commit-latency.md)
- [2026-06-02: add terminal command event partial indexes](2026-06-02-terminal-command-event-lookups.md)

## Rule

Use the `slow-sql-analysis` project skill before future slow SQL work. The required output is a dated record in this directory with evidence, root cause, fix, and validation.
