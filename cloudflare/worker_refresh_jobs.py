from __future__ import annotations

import hashlib
import re
import secrets
from datetime import UTC, datetime

FALLBACK_BUCKET_SECONDS = 600
IDEMPOTENCY_KEY_PATTERN = re.compile(r"[A-Za-z0-9._:-]{1,80}\Z", re.ASCII)
JOB_TYPE = "market_scan"

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
    "RefreshJobReadBackError",
)
