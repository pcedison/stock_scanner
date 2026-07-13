ALTER TABLE refresh_jobs ADD COLUMN dispatch_status TEXT NOT NULL DEFAULT 'pending';
ALTER TABLE refresh_jobs ADD COLUMN dispatch_attempts INTEGER NOT NULL DEFAULT 0;
ALTER TABLE refresh_jobs ADD COLUMN dispatched_at TEXT;
ALTER TABLE refresh_jobs ADD COLUMN dispatch_error_code TEXT;

UPDATE refresh_jobs
SET dispatch_status = 'workflow_claimed'
WHERE owner_run_id IS NOT NULL
  AND status IN ('running', 'success', 'failed');

CREATE INDEX IF NOT EXISTS idx_refresh_jobs_dispatch ON refresh_jobs(job_type, status, dispatch_status, queued_at);
