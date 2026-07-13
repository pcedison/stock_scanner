from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime

JOB_TYPE = "market_scan"
SAFE_ERROR_CODE = re.compile(r"[A-Z0-9_:-]{1,80}\Z", re.ASCII)

READ_PENDING_DISPATCH_SQL = """
SELECT id, status, dispatch_status
FROM refresh_jobs
WHERE job_type = ? AND status = 'queued' AND dispatch_status = 'pending'
ORDER BY queued_at ASC, id ASC
LIMIT 1
"""
CLAIM_DISPATCH_SQL = """
UPDATE refresh_jobs
SET dispatch_status = 'dispatching',
    dispatch_attempts = dispatch_attempts + 1,
    updated_at = ?
WHERE id = ? AND job_type = ? AND status = 'queued' AND dispatch_status = 'pending'
"""
READ_JOB_SQL = """
SELECT id, status, dispatch_status
FROM refresh_jobs
WHERE id = ? AND job_type = ?
LIMIT 1
"""
FINISH_DISPATCH_SQL = """
UPDATE refresh_jobs
SET dispatch_status = ?,
    dispatch_error_code = ?,
    dispatched_at = CASE WHEN ? = 'dispatched' THEN ? ELSE dispatched_at END,
    updated_at = ?
WHERE id = ? AND job_type = ? AND status = 'queued' AND dispatch_status = 'dispatching'
"""


@dataclass(frozen=True)
class DispatchResult:
    http_status: int | None = None
    error_code: str | None = None


def _utc_datetime(value) -> datetime:
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)
    if isinstance(value, (int, float)):
        timestamp = value / 1000 if value > 10_000_000_000 else value
        return datetime.fromtimestamp(timestamp, tz=UTC)
    return datetime.now(UTC)


def _iso(value) -> str:
    return _utc_datetime(value).isoformat()


def _changes(result) -> int:
    if isinstance(result, dict):
        return int((result.get("meta") or {}).get("changes") or result.get("changes") or 0)
    return 0


def _safe_error_code(value: str | None) -> str | None:
    text = str(value or "").strip().upper().replace(" ", "_")
    if not text:
        return None
    text = text[:80]
    return text if SAFE_ERROR_CODE.fullmatch(text) else "GITHUB_DISPATCH_ERROR"


def env_value(env, name: str, default=None):
    if env is None:
        return default
    return env.get(name, default) if isinstance(env, dict) else getattr(env, name, default)


def env_flag(env, name: str) -> bool:
    return str(env_value(env, name, "") or "").strip().lower() in {"1", "true", "yes", "on"}


def dispatch_enabled(env) -> bool:
    return env_flag(env, "GITHUB_DISPATCH_ENABLED")


def workflow_dispatch_payload(env) -> dict:
    ref = str(env_value(env, "GITHUB_REFRESH_WORKFLOW_REF", "main") or "main").strip()
    return {"ref": ref, "inputs": {"force": "true"}}


async def github_workflow_dispatch(env, payload) -> DispatchResult:
    token = str(env_value(env, "GITHUB_ACTIONS_DISPATCH_TOKEN", "") or "").strip()
    repository = str(env_value(env, "GITHUB_REPOSITORY", "") or "").strip()
    workflow_file = str(env_value(env, "GITHUB_REFRESH_WORKFLOW_FILE", "cloudflare-r2-seed-refresh.yml") or "").strip()
    if not token or not repository or "/" not in repository or not workflow_file:
        return DispatchResult(error_code="GITHUB_DISPATCH_NOT_CONFIGURED")
    try:
        import js  # type: ignore[import-not-found]

        response = await js.fetch(
            f"https://api.github.com/repos/{repository}/actions/workflows/{workflow_file}/dispatches",
            {
                "method": "POST",
                "headers": {
                    "authorization": f"Bearer {token}",
                    "accept": "application/vnd.github+json",
                    "content-type": "application/json",
                    "x-github-api-version": "2022-11-28",
                },
                "body": json.dumps(payload),
            },
        )
        return DispatchResult(http_status=int(response.status))
    except Exception:
        return DispatchResult(error_code="GITHUB_DISPATCH_NETWORK")


async def _finish_dispatch(api, job_id: str, status: str, error_code: str | None, now: str) -> None:
    await api.db_run(FINISH_DISPATCH_SQL, status, _safe_error_code(error_code), status, now, now, job_id, JOB_TYPE)


async def run_scheduled_refresh(api, scheduled_time=None, *, dispatch=github_workflow_dispatch) -> dict:
    now = _iso(scheduled_time)
    pending = await api.db_first(READ_PENDING_DISPATCH_SQL, JOB_TYPE)
    if not pending:
        return {"status": "idle"}

    job_id = str(pending["id"])
    if not dispatch_enabled(api.env):
        return {"status": "disabled", "jobId": job_id, "dispatchStatus": "pending"}

    result = await api.db_run(CLAIM_DISPATCH_SQL, now, job_id, JOB_TYPE)
    if _changes(result) != 1:
        row = await api.db_first(READ_JOB_SQL, job_id, JOB_TYPE)
        return {"status": "coalesced", "jobId": job_id, "dispatchStatus": (row or {}).get("dispatch_status", "unknown")}

    payload = workflow_dispatch_payload(api.env)
    response = await dispatch(api.env, payload)
    if response.http_status is not None and 200 <= int(response.http_status) < 300:
        await _finish_dispatch(api, job_id, "dispatched", None, now)
        return {"status": "dispatched", "jobId": job_id, "dispatchStatus": "dispatched"}
    if response.http_status is not None and 400 <= int(response.http_status) < 500:
        code = f"GITHUB_HTTP_{int(response.http_status)}"
        await _finish_dispatch(api, job_id, "failed", code, now)
        return {"status": "failed", "jobId": job_id, "dispatchStatus": "failed"}

    code = response.error_code or (
        f"GITHUB_HTTP_{int(response.http_status)}" if response.http_status is not None else "GITHUB_DISPATCH_UNKNOWN"
    )
    await _finish_dispatch(api, job_id, "unknown", code, now)
    return {"status": "unknown", "jobId": job_id, "dispatchStatus": "unknown"}


__all__ = ("DispatchResult", "dispatch_enabled", "github_workflow_dispatch", "run_scheduled_refresh")
