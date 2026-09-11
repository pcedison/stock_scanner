"""Worker-cron scheduling helpers for the R2 seed refresh.

The Worker cron (cloudflare/wrangler.toml ``[triggers].crons``) is the only scheduled
trigger for ``.github/workflows/cloudflare-r2-seed-refresh.yml``; GitHub ``schedule``
events were delayed or dropped for hours at a time. Each tick therefore has to be
self-sufficient: recover jobs orphaned by a cancelled/timed-out run, retry a dispatch
GitHub did not acknowledge or that no workflow run claimed, and queue a refresh
ahead of the policy deadline so the seed never reaches ``nextRefreshAfter``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta


def parse_time(value):
    # Local copy of worker_support.parse_time so this module (and its tests) never need
    # the Pyodide-only ``js`` module that worker_support imports.
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


JOB_TYPE = "market_scan"
REFRESH_AHEAD_SECONDS = 120 * 60
DISPATCH_RETRY_SECONDS = 20 * 60
ORPHANED_RUNNING_SECONDS = 120 * 60
WORKER_CRON_CLIENT_KEY = "worker-cron"

READ_PENDING_DISPATCH_SQL = """
SELECT id, status, dispatch_status, dispatch_attempts, dispatch_error_code, updated_at
FROM refresh_jobs
WHERE job_type = ? AND status = 'queued' AND dispatch_status = 'pending'
ORDER BY queued_at ASC, id ASC
LIMIT 1
"""
READ_RETRYABLE_DISPATCH_SQL = """
SELECT id, status, dispatch_status, dispatch_attempts, dispatch_error_code, updated_at
FROM refresh_jobs
WHERE job_type = ? AND status = 'queued'
  AND dispatch_status IN ('failed', 'unknown', 'dispatched')
  AND julianday(COALESCE(NULLIF(updated_at, ''), queued_at)) <= julianday(?)
ORDER BY queued_at ASC, id ASC
LIMIT 1
"""
RESET_RETRYABLE_DISPATCH_SQL = """
UPDATE refresh_jobs
SET dispatch_status = 'pending', updated_at = ?
WHERE id = ? AND job_type = ? AND status = 'queued'
  AND dispatch_status IN ('failed', 'unknown', 'dispatched')
"""
RECOVER_ORPHANED_RUNNING_SQL = """
UPDATE refresh_jobs
SET status = 'queued', dispatch_status = 'pending', owner_run_id = NULL, started_at = NULL,
    finished_at = NULL, error = NULL, dispatch_error_code = NULL, updated_at = ?
WHERE job_type = ? AND status = 'running'
  AND julianday(COALESCE(NULLIF(started_at, ''), NULLIF(updated_at, ''), queued_at)) <= julianday(?)
"""


def utc_datetime(value) -> datetime:
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)
    if isinstance(value, (int, float)):
        timestamp = value / 1000 if value > 10_000_000_000 else value
        return datetime.fromtimestamp(timestamp, tz=UTC)
    return datetime.now(UTC)


def changes(result) -> int:
    if isinstance(result, dict):
        return int((result.get("meta") or {}).get("changes") or result.get("changes") or 0)
    return 0


def refresh_is_due(status, now, refresh_ahead_seconds=0) -> bool:
    """Stale per policy, or inside the refresh-ahead window before ``nextRefreshAfter``."""
    if status.get("isStale") is True:
        return True
    if int(refresh_ahead_seconds or 0) <= 0:
        return False
    next_refresh = parse_time(status.get("nextRefreshAfter"))
    if next_refresh is None:
        return True
    return utc_datetime(now) >= utc_datetime(next_refresh) - timedelta(seconds=int(refresh_ahead_seconds))


async def read_pending_dispatch(api):
    return await api.db_first(READ_PENDING_DISPATCH_SQL, JOB_TYPE)


async def recover_orphaned_running_jobs(api, now: datetime) -> int:
    """Re-queue ``running`` jobs whose workflow run was cancelled or timed out."""
    cutoff = (utc_datetime(now) - timedelta(seconds=ORPHANED_RUNNING_SECONDS)).isoformat()
    result = await api.db_run(RECOVER_ORPHANED_RUNNING_SQL, utc_datetime(now).isoformat(), JOB_TYPE, cutoff)
    return changes(result)


async def requeue_retryable_dispatch(api, now: datetime):
    """Reset a failed/unknown/unclaimed dispatch to ``pending`` after the retry delay."""
    cutoff = (utc_datetime(now) - timedelta(seconds=DISPATCH_RETRY_SECONDS)).isoformat()
    row = await api.db_first(READ_RETRYABLE_DISPATCH_SQL, JOB_TYPE, cutoff)
    if not row:
        return None
    result = await api.db_run(RESET_RETRYABLE_DISPATCH_SQL, utc_datetime(now).isoformat(), str(row["id"]), JOB_TYPE)
    return await read_pending_dispatch(api) if changes(result) == 1 else None


async def enqueue_when_due(api) -> dict:
    """Queue a refresh job when the deployed seed is stale or inside the refresh-ahead window."""
    ensure_refresh_job = getattr(api, "ensure_refresh_job", None)
    read_manifest = getattr(api, "r2_json", None)
    if ensure_refresh_job is None or read_manifest is None:
        return {"status": "unsupported"}
    manifest = await read_manifest("public/manifest.json", {})
    try:
        job = await ensure_refresh_job(
            manifest, force=False, client_key=WORKER_CRON_CLIENT_KEY, refresh_ahead_seconds=REFRESH_AHEAD_SECONDS
        )
    except Exception as exc:  # RefreshCooldownError and D1 failures both mean "not now"
        return {"status": "cooldown", "error": type(exc).__name__}
    if not isinstance(job, dict) or job.get("status") == "fresh":
        return {"status": "fresh"}
    return {"status": "queued", "jobId": job.get("id")}


async def prepare_pending_dispatch(api, now: datetime) -> tuple[dict | None, dict]:
    """Return the job to dispatch on this tick (if any) plus a small diagnostic summary."""
    summary = {"recovered": 0, "enqueue": "skipped"}
    pending = await read_pending_dispatch(api)
    if not pending:
        summary["recovered"] = await recover_orphaned_running_jobs(api, now)
        pending = await read_pending_dispatch(api) if summary["recovered"] else None
    if not pending:
        pending = await requeue_retryable_dispatch(api, now)
    if not pending:
        enqueue = await enqueue_when_due(api)
        summary["enqueue"] = enqueue.get("status", "unknown")
        if enqueue.get("status") == "queued":
            pending = await read_pending_dispatch(api)
    return pending, summary


__all__ = (
    "DISPATCH_RETRY_SECONDS",
    "ORPHANED_RUNNING_SECONDS",
    "REFRESH_AHEAD_SECONDS",
    "enqueue_when_due",
    "prepare_pending_dispatch",
    "read_pending_dispatch",
    "recover_orphaned_running_jobs",
    "refresh_is_due",
    "requeue_retryable_dispatch",
    "utc_datetime",
)
