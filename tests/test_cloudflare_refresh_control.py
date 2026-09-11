from __future__ import annotations

import asyncio
import types
from datetime import UTC, datetime, timedelta

from cloudflare import worker_refresh_control, worker_refresh_schedule

FIXED_TIME = datetime(2026, 7, 13, 12, 15, tzinfo=UTC)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


class FakeApi:
    """In-memory stand-in for the Worker Api: D1 rows, the R2 manifest, and enqueue."""

    def __init__(self, *, enabled=False, token="token", next_refresh_after=None, is_stale=False):
        self.env = types.SimpleNamespace(
            GITHUB_DISPATCH_ENABLED="true" if enabled else "false",
            GITHUB_APP_ID="123456",
            GITHUB_APP_INSTALLATION_ID="7891011",
            GITHUB_APP_PRIVATE_KEY=token,
            GITHUB_REPOSITORY="pcedison/stock_scanner",
            GITHUB_REFRESH_WORKFLOW_FILE="cloudflare-r2-seed-refresh.yml",
            GITHUB_REFRESH_WORKFLOW_REF="main",
        )
        self.rows = []
        self.updates = []
        self.enqueue_calls = []
        self.next_refresh_after = next_refresh_after
        self.is_stale = is_stale

    async def r2_json(self, key, default):
        assert key == "public/manifest.json"
        return {"generatedAt": "2026-07-13T09:00:00+00:00"}

    def cache_policy(self):
        return {"strategy": "stale_while_revalidate", "reason": "routine_refresh", "minIntervalSeconds": 43200}

    def cache_status_from_manifest(self, manifest, refresh_status):
        return {"isStale": self.is_stale, "nextRefreshAfter": self.next_refresh_after, "cacheKey": "cache"}

    async def ensure_refresh_job(self, manifest, force=False, client_key=None, *, refresh_ahead_seconds=0):
        self.enqueue_calls.append(
            {"force": force, "client_key": client_key, "refresh_ahead_seconds": refresh_ahead_seconds}
        )
        status = self.cache_status_from_manifest(manifest, {})
        if not worker_refresh_schedule.refresh_is_due(status, FIXED_TIME, refresh_ahead_seconds):
            return {"status": "fresh", "reason": "routine_refresh"}
        row = queued_job(f"job-enqueued-{len(self.enqueue_calls)}", queued_at=_iso(FIXED_TIME))
        self.rows.append(row)
        return {"id": row["id"], "status": "queued"}

    async def db_first(self, sql, *params):
        normalized = " ".join(sql.split())
        if "dispatch_status = 'pending'" in normalized and "WHERE job_type = ? AND status = 'queued'" in normalized:
            return next(
                (dict(row) for row in self.rows if row["status"] == "queued" and row["dispatch_status"] == "pending"),
                None,
            )
        if "dispatch_status IN ('failed', 'unknown', 'dispatched')" in normalized and normalized.startswith("SELECT"):
            _job_type, cutoff = params
            return next(
                (
                    dict(row)
                    for row in self.rows
                    if row["status"] == "queued"
                    and row["dispatch_status"] in {"failed", "unknown", "dispatched"}
                    and (row.get("updated_at") or row["queued_at"]) <= cutoff
                ),
                None,
            )
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
                    row.update(
                        {
                            "dispatch_status": "dispatching",
                            "dispatch_attempts": row.get("dispatch_attempts", 0) + 1,
                            "updated_at": now,
                        }
                    )
                    return {"meta": {"changes": 1}}
            return {"meta": {"changes": 0}}
        if "SET dispatch_status = 'pending', updated_at = ?" in normalized:
            now, job_id = params[0], params[1]
            for row in self.rows:
                if (
                    row["id"] == job_id
                    and row["status"] == "queued"
                    and row["dispatch_status"] in {"failed", "unknown", "dispatched"}
                ):
                    row.update({"dispatch_status": "pending", "updated_at": now})
                    return {"meta": {"changes": 1}}
            return {"meta": {"changes": 0}}
        if "SET status = 'queued', dispatch_status = 'pending'" in normalized:
            now, _job_type, cutoff = params
            changed = 0
            for row in self.rows:
                started = row.get("started_at") or row.get("updated_at") or row["queued_at"]
                if row["status"] == "running" and started <= cutoff:
                    row.update(
                        {
                            "status": "queued",
                            "dispatch_status": "pending",
                            "owner_run_id": None,
                            "started_at": None,
                            "updated_at": now,
                        }
                    )
                    changed += 1
            return {"meta": {"changes": changed}}
        if "dispatch_status = ?" in normalized:
            status, error_code, _status_copy, _dispatched_at, now, job_id, _job_type = params
            for row in self.rows:
                if row["id"] == job_id:
                    row.update({"dispatch_status": status, "dispatch_error_code": error_code, "updated_at": now})
                    return {"meta": {"changes": 1}}
        raise AssertionError(f"Unhandled run SQL: {sql}")


def queued_job(
    job_id="job-1",
    *,
    dispatch_status="pending",
    queued_at="2026-07-13T12:00:00+00:00",
    updated_at=None,
    status="queued",
):
    return {
        "id": job_id,
        "job_type": "market_scan",
        "cache_key": "cache",
        "status": status,
        "reason": "routine_refresh",
        "queued_at": queued_at,
        "updated_at": updated_at or queued_at,
        "started_at": None,
        "dispatch_status": dispatch_status,
        "dispatch_attempts": 0,
    }


def _run(api, dispatch=None):
    kwargs = {"dispatch": dispatch} if dispatch else {}
    return asyncio.run(worker_refresh_control.run_scheduled_refresh(api, FIXED_TIME, **kwargs))


def _ok_dispatch(calls):
    async def dispatch(_env, payload):
        calls.append(payload)
        return worker_refresh_control.DispatchResult(http_status=204)

    return dispatch


def test_dispatch_disabled_leaves_pending_job_unclaimed_and_never_enqueues():
    api = FakeApi(enabled=False, is_stale=True)
    api.rows.append(queued_job())

    result = _run(api)

    assert result == {"status": "disabled", "jobId": "job-1", "dispatchStatus": "pending"}
    assert api.rows[0]["dispatch_status"] == "pending"
    assert api.updates == []
    assert api.enqueue_calls == []


def test_dispatch_claims_pending_job_once_and_marks_2xx_dispatched():
    api = FakeApi(enabled=True)
    api.rows.append(queued_job("job-2"))
    calls: list[dict] = []

    result = _run(api, _ok_dispatch(calls))

    assert result["status"] == "dispatched"
    assert result["jobId"] == "job-2"
    assert api.rows[0]["dispatch_status"] == "dispatched"
    assert api.rows[0]["dispatch_attempts"] == 1
    assert calls == [{"ref": "main", "inputs": {"force": "false"}}]


def test_ambiguous_dispatch_marks_job_unknown_without_immediate_retry():
    api = FakeApi(enabled=True)
    api.rows.append(queued_job("job-3"))
    calls = []

    async def dispatch(_env, payload):
        calls.append(payload)
        return worker_refresh_control.DispatchResult(http_status=503, error_code="GITHUB_HTTP_503")

    result = _run(api, dispatch)

    assert result == {
        "status": "unknown",
        "jobId": "job-3",
        "dispatchStatus": "unknown",
        "errorCode": "GITHUB_HTTP_503",
    }
    assert api.rows[0]["status"] == "queued"
    assert api.rows[0]["dispatch_status"] == "unknown"
    assert api.rows[0]["dispatch_error_code"] == "GITHUB_HTTP_503"
    assert len(calls) == 1


def test_client_error_marks_dispatch_failed_with_http_code():
    api = FakeApi(enabled=True)
    api.rows.append(queued_job("job-4"))

    async def dispatch(_env, _payload):
        return worker_refresh_control.DispatchResult(http_status=401)

    result = _run(api, dispatch)

    assert result["status"] == "failed"
    assert result["errorCode"] == "GITHUB_HTTP_401"
    assert api.rows[0]["dispatch_status"] == "failed"
    assert api.rows[0]["dispatch_error_code"] == "GITHUB_HTTP_401"


def test_github_app_credential_fault_fails_the_job_instead_of_retrying():
    # No http_status at all: the dispatch never left the Worker because the app
    # credentials could not mint a token. Retrying cannot fix that, so the job is
    # failed at once and the monitor reports it on its next run.
    api = FakeApi(enabled=True)
    api.rows.append(queued_job("job-app"))

    async def dispatch(_env, _payload):
        return worker_refresh_control.DispatchResult(error_code="GITHUB_APP_SIGN_FAILED")

    result = _run(api, dispatch)

    assert result["status"] == "failed"
    assert result["errorCode"] == "GITHUB_APP_SIGN_FAILED"
    assert api.rows[0]["dispatch_status"] == "failed"


def test_transport_fault_stays_unknown_so_the_next_tick_retries():
    api = FakeApi(enabled=True)
    api.rows.append(queued_job("job-net"))

    async def dispatch(_env, _payload):
        return worker_refresh_control.DispatchResult(error_code="GITHUB_APP_TOKEN_NETWORK")

    result = _run(api, dispatch)

    assert result["status"] == "unknown"
    assert result["errorCode"] == "GITHUB_APP_TOKEN_NETWORK"


def test_unknown_dispatch_is_retried_after_the_retry_delay():
    api = FakeApi(enabled=True)
    stale_update = _iso(FIXED_TIME - timedelta(seconds=worker_refresh_schedule.DISPATCH_RETRY_SECONDS + 60))
    api.rows.append(queued_job("job-5", dispatch_status="unknown", updated_at=stale_update))
    calls: list[dict] = []

    result = _run(api, _ok_dispatch(calls))

    assert result["status"] == "dispatched"
    assert result["jobId"] == "job-5"
    assert api.rows[0]["dispatch_status"] == "dispatched"
    assert len(calls) == 1
    assert api.enqueue_calls == []


def test_recent_unknown_dispatch_waits_for_the_retry_delay():
    api = FakeApi(enabled=True, next_refresh_after=_iso(FIXED_TIME + timedelta(hours=6)))
    api.rows.append(queued_job("job-6", dispatch_status="unknown", updated_at=_iso(FIXED_TIME - timedelta(minutes=5))))
    calls: list[dict] = []

    result = _run(api, _ok_dispatch(calls))

    assert result["status"] == "idle"
    assert api.rows[0]["dispatch_status"] == "unknown"
    assert calls == []


def test_dispatched_job_never_claimed_by_a_workflow_run_is_redispatched():
    api = FakeApi(enabled=True)
    old = _iso(FIXED_TIME - timedelta(hours=1))
    api.rows.append(queued_job("job-7", dispatch_status="dispatched", updated_at=old))
    calls: list[dict] = []

    result = _run(api, _ok_dispatch(calls))

    assert result["status"] == "dispatched"
    assert len(calls) == 1


def test_orphaned_running_job_is_recovered_and_redispatched():
    api = FakeApi(enabled=True)
    row = queued_job("job-8", status="running", dispatch_status="workflow_claimed")
    row["started_at"] = _iso(FIXED_TIME - timedelta(seconds=worker_refresh_schedule.ORPHANED_RUNNING_SECONDS + 60))
    row["owner_run_id"] = "123"
    api.rows.append(row)
    calls: list[dict] = []

    result = _run(api, _ok_dispatch(calls))

    assert result["status"] == "dispatched"
    assert result["recovered"] == 1
    assert api.rows[0]["owner_run_id"] is None
    assert len(calls) == 1


def test_recently_started_running_job_is_left_alone():
    api = FakeApi(enabled=True, next_refresh_after=_iso(FIXED_TIME + timedelta(hours=6)))
    row = queued_job("job-9", status="running", dispatch_status="workflow_claimed")
    row["started_at"] = _iso(FIXED_TIME - timedelta(minutes=30))
    api.rows.append(row)

    result = _run(api, _ok_dispatch([]))

    assert result == {"status": "idle", "recovered": 0, "enqueue": "fresh"}
    assert api.rows[0]["status"] == "running"


def test_fresh_seed_outside_refresh_ahead_window_stays_idle():
    api = FakeApi(enabled=True, next_refresh_after=_iso(FIXED_TIME + timedelta(hours=6)))
    calls: list[dict] = []

    result = _run(api, _ok_dispatch(calls))

    assert result == {"status": "idle", "recovered": 0, "enqueue": "fresh"}
    assert calls == []
    assert api.enqueue_calls == [
        {
            "force": False,
            "client_key": "worker-cron",
            "refresh_ahead_seconds": worker_refresh_schedule.REFRESH_AHEAD_SECONDS,
        }
    ]


def test_seed_inside_refresh_ahead_window_is_enqueued_and_dispatched():
    api = FakeApi(enabled=True, next_refresh_after=_iso(FIXED_TIME + timedelta(minutes=90)))
    calls: list[dict] = []

    result = _run(api, _ok_dispatch(calls))

    assert result["status"] == "dispatched"
    assert result["enqueue"] == "queued"
    assert result["jobId"] == "job-enqueued-1"
    assert api.rows[0]["dispatch_status"] == "dispatched"
    assert calls == [{"ref": "main", "inputs": {"force": "false"}}]


def test_stale_seed_is_enqueued_and_dispatched():
    api = FakeApi(enabled=True, is_stale=True, next_refresh_after=_iso(FIXED_TIME - timedelta(hours=1)))
    calls: list[dict] = []

    result = _run(api, _ok_dispatch(calls))

    assert result["status"] == "dispatched"
    assert len(calls) == 1


def test_enqueue_cooldown_keeps_tick_idle():
    api = FakeApi(enabled=True, is_stale=True)

    async def ensure_refresh_job(*_args, **_kwargs):
        raise RuntimeError("cooldown")

    setattr(api, "ensure_refresh_job", ensure_refresh_job)

    result = _run(api, _ok_dispatch([]))

    assert result == {"status": "idle", "recovered": 0, "enqueue": "cooldown"}


def test_refresh_is_due_handles_policy_and_ahead_window():
    due = worker_refresh_schedule.refresh_is_due
    later = _iso(FIXED_TIME + timedelta(hours=3))
    assert due({"isStale": True, "nextRefreshAfter": later}, FIXED_TIME) is True
    assert due({"isStale": False, "nextRefreshAfter": later}, FIXED_TIME) is False
    assert due({"isStale": False, "nextRefreshAfter": later}, FIXED_TIME, 2 * 3600) is False
    assert due({"isStale": False, "nextRefreshAfter": later}, FIXED_TIME, 3 * 3600) is True
    assert due({"isStale": False, "nextRefreshAfter": None}, FIXED_TIME, 60) is True
    assert due({"isStale": False, "nextRefreshAfter": None}, FIXED_TIME) is False
