-- Task leases — scope crash recovery to the worker that actually owns a task.
--
-- pgdb.reset_stuck_tasks() used to requeue EVERY task in 'processing' across all orgs and
-- delete each one's partial document. With a single worker that is correct: anything left
-- 'processing' at startup belongs to the process that just died. With two workers it is
-- data loss — the second one's startup deletes the document the first is actively writing.
--
-- `locked_by` records which worker claimed the task. Recovery now reclaims only:
--   * tasks this worker claimed in a previous life (safe: the process has just started),
--   * tasks with no owner recorded (claimed by a build from before this migration),
--   * tasks whose lease has gone stale — no progress update for PDFSPECS_TASK_LEASE_MINUTES.
--
-- No separate heartbeat column is needed: ingest._progress() UPDATEs the task row at every
-- phase (download, each parsed page, each embedding batch, storing), and the existing
-- trg_tasks_updated_at trigger refreshes updated_at on any UPDATE. A live-but-slow task
-- therefore keeps renewing its own lease, so it is never reaped mid-document.

alter table tasks add column if not exists locked_by text;

-- Recovery scans by status and lease age.
create index if not exists idx_tasks_status_updated on tasks (status, updated_at);
