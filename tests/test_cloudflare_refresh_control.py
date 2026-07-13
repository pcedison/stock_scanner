from __future__ import annotations

import asyncio
import types
from datetime import UTC, datetime

from cloudflare import worker_refresh_control

FIXED_TIME = datetime(2026, 7, 13, 12, 15, tzinfo=UTC)


class FakeApi:
    def __init__(self, *, enabled=False, token="token"):
        self.env = types.SimpleNamespace(
            GITHUB_DISPATCH_ENABLED="true" if enabled else "false",
            GITHUB_ACTIONS_DISPATCH_TOKEN=token,
            GITHUB_REPOSITORY="pcedison/stock_scanner",
            GITHUB_REFRESH_WORKFLOW_FILE="cloudflare-r2-seed-refresh.yml",
            GITHUB_REFRESH_WORKFLOW_REF="main",
        )
        self.rows = []
        self.updates = []

    async def db_first(self, sql, *params):
        normalized = " ".join(sql.split())
        if "dispatch_status = 'pending'" in normalized:
            return next((dict(row) for row in self.rows if row["status"] == "queued" and row["dispatch_status"] == "pending"), None)
        if "WHERE id = ?" in normalized:
            return next((dict(row) for row in self.rows if row["id"] == params[0]), None)
        raise AssertionError(f"Unhandled first SQL: {sql}")

    async def db_run(self, sql, *params):
        normalized = " ".join(sql.split())
        self.updates.append(normalized)
        if "SET dispatch_status = 'dispatching'" in normalized:
            now, job_id = params[0], params[1]
            for row in self.rows:
                if row["id"] == job_id and row["status"] == "queued" and row["dispatch_status"] == "pending":
                    row.update({"dispatch_status": "dispatching", "dispatch_attempts": 1, "updated_at": now})
                    return {"meta": {"changes": 1}}
            return {"meta": {"changes": 0}}
        if "dispatch_status = ?" in normalized:
            status, error_code, _status_copy, _dispatched_at, now, job_id, _job_type = params
            for row in self.rows:
                if row["id"] == job_id:
                    row.update({"dispatch_status": status, "dispatch_error_code": error_code, "updated_at": now})
                    return {"meta": {"changes": 1}}
        raise AssertionError(f"Unhandled run SQL: {sql}")


def queued_job(job_id="job-1"):
    return {
        "id": job_id,
        "job_type": "market_scan",
        "cache_key": "cache",
        "status": "queued",
        "reason": "routine_refresh",
        "queued_at": "2026-07-13T12:00:00+00:00",
        "updated_at": "2026-07-13T12:00:00+00:00",
        "dispatch_status": "pending",
        "dispatch_attempts": 0,
    }


def test_dispatch_disabled_leaves_pending_job_unclaimed():
    api = FakeApi(enabled=False)
    api.rows.append(queued_job())

    result = asyncio.run(worker_refresh_control.run_scheduled_refresh(api, FIXED_TIME))

    assert result == {"status": "disabled", "jobId": "job-1", "dispatchStatus": "pending"}
    assert api.rows[0]["dispatch_status"] == "pending"
    assert api.updates == []


def test_dispatch_claims_pending_job_once_and_marks_2xx_dispatched():
    api = FakeApi(enabled=True)
    api.rows.append(queued_job("job-2"))
    calls = []

    async def dispatch(_env, payload):
        calls.append(payload)
        return worker_refresh_control.DispatchResult(http_status=204)

    result = asyncio.run(worker_refresh_control.run_scheduled_refresh(api, FIXED_TIME, dispatch=dispatch))

    assert result["status"] == "dispatched"
    assert result["jobId"] == "job-2"
    assert api.rows[0]["dispatch_status"] == "dispatched"
    assert api.rows[0]["dispatch_attempts"] == 1
    assert len(calls) == 1
    assert calls[0]["ref"] == "main"


def test_ambiguous_dispatch_keeps_job_queued_for_schedule_fallback_without_retry():
    api = FakeApi(enabled=True)
    api.rows.append(queued_job("job-3"))
    calls = []

    async def dispatch(_env, payload):
        calls.append(payload)
        return worker_refresh_control.DispatchResult(http_status=503, error_code="GITHUB_HTTP_503")

    result = asyncio.run(worker_refresh_control.run_scheduled_refresh(api, FIXED_TIME, dispatch=dispatch))

    assert result == {"status": "unknown", "jobId": "job-3", "dispatchStatus": "unknown"}
    assert api.rows[0]["status"] == "queued"
    assert api.rows[0]["dispatch_status"] == "unknown"
    assert api.rows[0]["dispatch_error_code"] == "GITHUB_HTTP_503"
    assert len(calls) == 1
