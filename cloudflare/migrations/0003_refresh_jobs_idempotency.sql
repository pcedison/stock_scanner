ALTER TABLE refresh_jobs ADD COLUMN idempotency_key TEXT;

WITH ranked_active AS (
    SELECT
        id,
        ROW_NUMBER() OVER (
            PARTITION BY job_type, cache_key
            ORDER BY queued_at DESC, id DESC
        ) AS active_rank
    FROM refresh_jobs
    WHERE status IN ('queued', 'running')
)
UPDATE refresh_jobs
SET status = 'failed',
    error = 'superseded before active uniqueness migration',
    finished_at = COALESCE(finished_at, datetime('now')),
    updated_at = datetime('now')
WHERE id IN (SELECT id FROM ranked_active WHERE active_rank > 1);

CREATE UNIQUE INDEX IF NOT EXISTS ux_refresh_jobs_idempotency_key
ON refresh_jobs(idempotency_key)
WHERE idempotency_key IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS ux_refresh_jobs_active_cache
ON refresh_jobs(job_type, cache_key)
WHERE status IN ('queued', 'running');
