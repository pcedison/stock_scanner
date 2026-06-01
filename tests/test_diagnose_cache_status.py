from datetime import UTC, datetime

import pytest

from scripts.diagnose_cache_status import (
    CHECK_USER_AGENT,
    analyze_cache_status,
    validate_cache_status_url,
)

# 固定一個「現在」,讓年齡/堆積時間判讀可重現。
NOW = datetime(2026, 6, 1, 4, 52, 0, tzinfo=UTC)


def _market(stored_at: str, is_stale: bool = True, refresh_status: str = "not_requested") -> dict:
    return {
        "marketScan": {
            "source": "cloudflare_r2",
            "storedAt": stored_at,
            "isStale": is_stale,
            "refreshStatus": refresh_status,
            "latestRevenuePeriod": "2026-04",
            "latestFinancialPeriod": "2026Q1",
        }
    }


def _job(status: str, queued_at: str, owner_run_id=None, has_error: bool = False) -> dict:
    return {
        "id": "job-" + status,
        "job_type": "market_scan",
        "status": status,
        "queued_at": queued_at,
        "owner_run_id": owner_run_id,
        "hasError": has_error,
    }


def test_url_validation_rejects_wrong_path_or_scheme():
    good = "https://example.workers.dev/api/cache/status"
    assert validate_cache_status_url(good) == good
    with pytest.raises(RuntimeError):
        validate_cache_status_url("http://example.workers.dev/api/cache/status")
    with pytest.raises(RuntimeError):
        validate_cache_status_url("https://example.workers.dev/api/health")


def test_user_agent_is_browser_like():
    assert CHECK_USER_AGENT.startswith("Mozilla/5.0 ")


def test_fresh_cache_is_healthy():
    payload = _market("2026-06-01T03:00:00+00:00", is_stale=False)
    result = analyze_cache_status(payload, now=NOW)
    assert result["verdict"] == "ok"
    assert result["healthy"] is True
    assert result["action"] is None


def test_fresh_flag_but_overaged_is_not_healthy():
    # isStale=False 但資料時間已超過 max_cache_age_hours,仍應判為不健康。
    payload = _market("2026-05-28T00:00:00+00:00", is_stale=False)
    result = analyze_cache_status(payload, now=NOW, max_cache_age_hours=36)
    assert result["healthy"] is False
    assert result["verdict"] == "stale_manifest"


def test_queued_never_consumed_matches_production_symptom():
    # 線上實況:manifest 卡在 5/29 18:59 UTC,一堆 queued job 但 owner_run_id 全空。
    payload = _market("2026-05-29T18:59:51+00:00", is_stale=True)
    payload["recentJobs"] = [
        _job("queued", "2026-06-01T04:46:48+00:00"),
        _job("queued", "2026-06-01T03:45:10+00:00"),
        _job("queued", "2026-05-31T06:53:01+00:00"),
    ]
    result = analyze_cache_status(payload, now=NOW)
    assert result["verdict"] == "queued_never_consumed"
    assert result["healthy"] is False
    assert result["details"]["queuedCount"] == 3
    assert result["details"]["consumedAny"] is False
    assert result["details"]["oldestQueuedMinutes"] > 30


def test_recent_queued_under_threshold_is_not_flagged_as_unconsumed():
    # 剛 queued、還沒到堆積門檻,不該武斷判成消費端異常。
    payload = _market("2026-05-29T18:59:51+00:00", is_stale=True)
    payload["recentJobs"] = [_job("queued", "2026-06-01T04:40:00+00:00")]
    result = analyze_cache_status(payload, now=NOW, queued_stall_minutes=30)
    assert result["verdict"] == "stale_manifest"


def test_running_job_reports_in_progress():
    payload = _market("2026-05-31T18:59:51+00:00", is_stale=True)
    payload["recentJobs"] = [_job("running", "2026-06-01T04:40:00+00:00", owner_run_id="123456")]
    result = analyze_cache_status(payload, now=NOW)
    assert result["verdict"] == "refresh_in_progress"


def test_consumed_then_failed_reports_failing():
    payload = _market("2026-05-31T18:59:51+00:00", is_stale=True)
    payload["recentJobs"] = [
        _job("failed", "2026-06-01T04:00:00+00:00", owner_run_id="999", has_error=True),
    ]
    result = analyze_cache_status(payload, now=NOW)
    assert result["verdict"] == "refresh_failing"
    assert result["details"]["failedCount"] == 1


def test_invalid_payload_is_reported():
    result = analyze_cache_status({"unexpected": True}, now=NOW)
    assert result["verdict"] == "invalid_payload"
    assert result["healthy"] is False
