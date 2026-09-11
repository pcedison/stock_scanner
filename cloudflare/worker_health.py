from __future__ import annotations


def health_payload(manifest, manifest_quality, cache_status_from_manifest, cache_policy, utc_now, refresh_dispatch=None):
    quality = manifest_quality(manifest)
    policy = cache_policy()
    cache_status = cache_status_from_manifest(
        manifest,
        {"status": "not_requested", "reason": policy["reason"]},
    )
    payload = {
        "status": "ok" if quality["ok"] and not cache_status["isStale"] else "degraded",
        "runtime": "cloudflare-python-worker",
        "time": utc_now(),
        "cache": manifest,
        "cacheStatus": cache_status,
        "cacheQuality": quality,
    }
    if refresh_dispatch is not None:
        payload["refreshDispatch"] = refresh_dispatch
    return payload


LATEST_ACTIVE_JOB_SQL = """
SELECT status, dispatch_status, dispatch_error_code, dispatch_attempts, queued_at, updated_at
FROM refresh_jobs
WHERE job_type = ? AND status IN ('queued', 'running')
ORDER BY queued_at DESC, id DESC
LIMIT 1
"""


def summarize_refresh_dispatch(dispatch_enabled, latest_job):
    """Summarize Worker-cron dispatch state so an expired GitHub token (``GITHUB_HTTP_401``)
    or a missing workflow (``GITHUB_HTTP_404``) is visible before the seed goes stale."""
    summary = {"enabled": bool(dispatch_enabled), "jobStatus": None, "dispatchStatus": None, "dispatchErrorCode": None, "dispatchAttempts": 0}
    if isinstance(latest_job, dict) and latest_job:
        summary["jobStatus"] = latest_job.get("status")
        summary["dispatchStatus"] = latest_job.get("dispatch_status")
        summary["dispatchErrorCode"] = latest_job.get("dispatch_error_code") or None
        summary["dispatchAttempts"] = int(latest_job.get("dispatch_attempts") or 0)
        summary["queuedAt"] = latest_job.get("queued_at")
        summary["updatedAt"] = latest_job.get("updated_at")
    return summary


async def refresh_dispatch_status(api, dispatch_enabled):
    try:
        latest = await api.db_first(LATEST_ACTIVE_JOB_SQL, "market_scan")
    except Exception as exc:
        # Health must keep reporting seed freshness even when D1 is briefly unavailable.
        return {"enabled": bool(dispatch_enabled), "jobStatus": None, "dispatchStatus": "unavailable", "dispatchErrorCode": type(exc).__name__.upper(), "dispatchAttempts": 0}
    return summarize_refresh_dispatch(dispatch_enabled, latest)
