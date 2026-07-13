from __future__ import annotations

import hashlib
import re
import secrets
from datetime import UTC, datetime

FALLBACK_BUCKET_SECONDS = 600
IDEMPOTENCY_KEY_PATTERN = re.compile(r"[A-Za-z0-9._:-]{1,80}\Z", re.ASCII)
JOB_ID_PATTERN = re.compile(r"[0-9a-f]{32}\Z", re.ASCII)
OWNER_RUN_ID_PATTERN = re.compile(r"[0-9]{1,20}\Z", re.ASCII)
JOB_TYPE = "market_scan"
JOB_STATUSES = frozenset({"queued", "running", "success", "failed"})
JOB_REASONS = frozenset({"financial_report_window", "monthly_revenue_window", "routine_refresh"})

INSERT_JOB_SQL = """
INSERT OR IGNORE INTO refresh_jobs
(id, job_type, cache_key, idempotency_key, status, reason, queued_at, updated_at)
VALUES (?, ?, ?, ?, 'queued', ?, ?, ?)
"""
READ_IDEMPOTENCY_SQL = """
SELECT id, status, reason, queued_at, owner_run_id
FROM refresh_jobs
WHERE idempotency_key = ?
LIMIT 1
"""
READ_ACTIVE_SQL = """
SELECT id, status, reason, queued_at, owner_run_id
FROM refresh_jobs
WHERE job_type = ? AND cache_key = ? AND status IN ('queued', 'running')
ORDER BY queued_at DESC, id DESC
LIMIT 1
"""
READ_STATUS_SQL = """
SELECT * FROM refresh_jobs
WHERE id = ? AND job_type = ?
LIMIT 1
"""


class RefreshJobReadBackError(RuntimeError):
    pass


def normalize_idempotency_key(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or IDEMPOTENCY_KEY_PATTERN.fullmatch(value) is None:
        raise ValueError("Idempotency-Key must be 1..80 ASCII letters, digits, '.', '_', ':', or '-'")
    return value


def _utc_datetime(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("now must be a timezone-aware datetime")
    return value.astimezone(UTC)


def derive_refresh_idempotency_key(job_type, cache_key, client_key, now) -> str:
    normalized = normalize_idempotency_key(client_key)
    scope = (
        f"client:{normalized}"
        if normalized is not None
        else f"bucket:{int(_utc_datetime(now).timestamp()) // FALLBACK_BUCKET_SECONDS}"
    )
    envelope = f"refresh-job-v1\0{job_type}\0{cache_key}\0{scope}"
    return hashlib.sha256(envelope.encode("utf-8")).hexdigest()


def _job_payload(api, row) -> dict:
    owner_run_id = row.get("owner_run_id")
    return {
        "status": row["status"],
        "reason": row["reason"],
        "jobId": row["id"],
        "queuedAt": row["queued_at"],
        "ownerRunId": owner_run_id,
        "ownerRunUrl": api.github_actions_run_url(owner_run_id),
    }


def _safe_text(value, maximum, pattern=None):
    text = str(value or "")
    if not text or len(text) > maximum or not text.isprintable():
        return None
    return text if pattern is None or pattern.fullmatch(text) else None


def _safe_timestamp(value):
    text = _safe_text(value, 128, re.compile(r"[0-9T: +.Z-]+\Z", re.ASCII))
    if text is None:
        return None
    try:
        datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return text


def refresh_command_payload(job, request_id) -> dict:
    job_id = _safe_text(job.get("jobId"), 32, JOB_ID_PATTERN)
    status = _safe_text(job.get("status"), 16)
    safe_request_id = _safe_text(request_id, 80, IDEMPOTENCY_KEY_PATTERN)
    if job_id is None or status not in JOB_STATUSES or safe_request_id is None:
        raise RefreshJobReadBackError("Refresh job command payload invariant failed")
    status_url = f"/api/scan/market/refresh/{job_id}"
    return {"jobId": job_id, "status": status, "requestId": safe_request_id, "statusUrl": status_url}


def _status_payload(api, row):
    job_id = _safe_text(row.get("id"), 32, JOB_ID_PATTERN)
    status = _safe_text(row.get("status"), 16)
    if job_id is None or status not in JOB_STATUSES:
        raise RefreshJobReadBackError("Refresh job status payload invariant failed")
    payload = {"jobId": job_id, "status": status}
    mappings = (("reason", "reason"), ("queuedAt", "queued_at"), ("startedAt", "started_at"),
                ("finishedAt", "finished_at"), ("updatedAt", "updated_at"))
    for public_key, row_key in mappings:
        value = _safe_timestamp(row.get(row_key)) if public_key.endswith("At") else row.get(row_key)
        if public_key == "reason" and value not in JOB_REASONS:
            value = None
        if value is not None:
            payload[public_key] = value
    owner_id = _safe_text(row.get("owner_run_id"), 20, OWNER_RUN_ID_PATTERN)
    if owner_id is not None:
        owner_url = str(api.github_actions_run_url(owner_id) or "")
        expected = rf"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/actions/runs/{re.escape(owner_id)}\Z"
        payload.update({"ownerRunId": owner_id, "ownerRunUrl": owner_url if re.fullmatch(expected, owner_url) else None})
    payload["hasError"] = bool(str(row.get("error") or "").strip())
    dispatch_status = _safe_text(row.get("dispatch_status"), 24)
    if dispatch_status in {"pending", "dispatching", "dispatched", "failed", "unknown", "workflow_claimed"}:
        payload["dispatchStatus"] = dispatch_status
    dispatch_error = _safe_text(row.get("dispatch_error_code"), 80, re.compile(r"[A-Z0-9_:-]{1,80}\Z", re.ASCII))
    if dispatch_error is not None:
        payload["dispatchErrorCode"] = dispatch_error
    return payload


async def refresh_job_status(api, job_id):
    if not isinstance(job_id, str) or JOB_ID_PATTERN.fullmatch(job_id) is None:
        return None
    row = await api.db_first(READ_STATUS_SQL, job_id, JOB_TYPE)
    return None if not row else _status_payload(api, row)


async def _read_back_job(api, idempotency_key, cache_key):
    row = await api.db_first(READ_IDEMPOTENCY_SQL, idempotency_key)
    if row:
        return row
    return await api.db_first(READ_ACTIVE_SQL, JOB_TYPE, cache_key)


async def enqueue_or_reuse_refresh_job(api, manifest, force, client_key, now) -> dict:
    normalized_client_key = normalize_idempotency_key(client_key)
    policy = api.cache_policy()
    status = api.cache_status_from_manifest(manifest, {"status": "checking", "reason": policy["reason"]})
    if not force and not status["isStale"]:
        return {"status": "fresh", "reason": policy["reason"]}

    cache_key = status["cacheKey"]
    idempotency_key = derive_refresh_idempotency_key(JOB_TYPE, cache_key, normalized_client_key, now)
    queued_at = _utc_datetime(now).isoformat()
    candidate_id = secrets.token_hex(16)

    for _attempt in range(2):
        await api.db_run(
            INSERT_JOB_SQL,
            candidate_id,
            JOB_TYPE,
            cache_key,
            idempotency_key,
            policy["reason"],
            queued_at,
            queued_at,
        )
        row = await _read_back_job(api, idempotency_key, cache_key)
        if row:
            return _job_payload(api, row)

    raise RefreshJobReadBackError("Refresh job read-back invariant failed")


__all__ = (
    "derive_refresh_idempotency_key",
    "enqueue_or_reuse_refresh_job",
    "normalize_idempotency_key",
    "refresh_command_payload",
    "refresh_job_status",
    "RefreshJobReadBackError",
)
