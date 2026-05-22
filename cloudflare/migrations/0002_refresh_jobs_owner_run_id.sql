ALTER TABLE refresh_jobs ADD COLUMN owner_run_id TEXT;

CREATE INDEX IF NOT EXISTS idx_refresh_jobs_owner_run ON refresh_jobs(job_type, status, owner_run_id);
