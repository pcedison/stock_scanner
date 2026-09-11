from __future__ import annotations

import json
import re
from dataclasses import dataclass

try:
    import worker_github_app as github_app
except ModuleNotFoundError:
    from cloudflare import worker_github_app as github_app

try:
    import worker_refresh_schedule as schedule
except ModuleNotFoundError:
    from cloudflare import worker_refresh_schedule as schedule

JOB_TYPE = "market_scan"
SAFE_ERROR_CODE = re.compile(r"[A-Z0-9_:-]{1,80}\Z", re.ASCII)

READ_PENDING_DISPATCH_SQL = schedule.READ_PENDING_DISPATCH_SQL
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


def _iso(value) -> str:
    return schedule.utc_datetime(value).isoformat()


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
    # force=false keeps the workflow's own D1/freshness guards active, so a duplicate
    # dispatch (retry after an ambiguous response) skips instead of rebuilding twice.
    return {"ref": ref, "inputs": {"force": "false"}}


async def github_workflow_dispatch(env, payload) -> DispatchResult:
    repository = str(env_value(env, "GITHUB_REPOSITORY", "") or "").strip()
    workflow_file = str(env_value(env, "GITHUB_REFRESH_WORKFLOW_FILE", "cloudflare-r2-seed-refresh.yml") or "").strip()
    if not repository or "/" not in repository or not workflow_file:
        return DispatchResult(error_code="GITHUB_DISPATCH_NOT_CONFIGURED")
    # A GitHub App installation token is minted per dispatch and lasts an hour, so
    # there is no long-lived credential here for anyone to rotate or leak.
    token, token_error = await github_app.installation_access_token(
        env_value(env, "GITHUB_APP_ID", ""),
        env_value(env, "GITHUB_APP_INSTALLATION_ID", ""),
        env_value(env, "GITHUB_APP_PRIVATE_KEY", ""),
    )
    if not token:
        return DispatchResult(error_code=token_error)
    try:
        import js

        response = await js.fetch(
            f"{github_app.GITHUB_API_ROOT}/repos/{repository}/actions/workflows/{workflow_file}/dispatches",
            {
                "method": "POST",
                "headers": {
                    "authorization": f"Bearer {token}",
                    "accept": "application/vnd.github+json",
                    "content-type": "application/json",
                    "user-agent": github_app.USER_AGENT,
                    "x-github-api-version": github_app.GITHUB_API_VERSION,
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
    now_dt = schedule.utc_datetime(scheduled_time)
    now = now_dt.isoformat()
    enabled = dispatch_enabled(api.env)
    summary: dict = {}
    if enabled:
        pending, summary = await schedule.prepare_pending_dispatch(api, now_dt)
    else:
        pending = await schedule.read_pending_dispatch(api)
    if not pending:
        return {"status": "idle", **summary}

    job_id = str(pending["id"])
    if not enabled:
        return {"status": "disabled", "jobId": job_id, "dispatchStatus": "pending"}

    result = await api.db_run(CLAIM_DISPATCH_SQL, now, job_id, JOB_TYPE)
    if schedule.changes(result) != 1:
        row = await api.db_first(READ_JOB_SQL, job_id, JOB_TYPE)
        return {"status": "coalesced", "jobId": job_id, "dispatchStatus": (row or {}).get("dispatch_status", "unknown")}

    payload = workflow_dispatch_payload(api.env)
    response = await dispatch(api.env, payload)
    if response.http_status is not None and 200 <= int(response.http_status) < 300:
        await _finish_dispatch(api, job_id, "dispatched", None, now)
        return {"status": "dispatched", "jobId": job_id, "dispatchStatus": "dispatched", **summary}
    if response.http_status is not None and 400 <= int(response.http_status) < 500:
        code = f"GITHUB_HTTP_{int(response.http_status)}"
        await _finish_dispatch(api, job_id, "failed", code, now)
        return {"status": "failed", "jobId": job_id, "dispatchStatus": "failed", "errorCode": code}

    # A credential fault never clears on retry, so record it as failed immediately;
    # the health monitor fails the next run instead of waiting out three attempts.
    if github_app.is_permanent_error(response.error_code):
        permanent = _safe_error_code(response.error_code) or "GITHUB_DISPATCH_ERROR"
        await _finish_dispatch(api, job_id, "failed", permanent, now)
        return {"status": "failed", "jobId": job_id, "dispatchStatus": "failed", "errorCode": permanent}

    code = response.error_code or (
        f"GITHUB_HTTP_{int(response.http_status)}" if response.http_status is not None else "GITHUB_DISPATCH_UNKNOWN"
    )
    await _finish_dispatch(api, job_id, "unknown", code, now)
    return {"status": "unknown", "jobId": job_id, "dispatchStatus": "unknown", "errorCode": _safe_error_code(code)}


__all__ = (
    "DispatchResult",
    "dispatch_enabled",
    "github_workflow_dispatch",
    "run_scheduled_refresh",
    "workflow_dispatch_payload",
)
