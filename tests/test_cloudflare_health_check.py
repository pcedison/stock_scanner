from datetime import UTC, datetime

import pytest

from scripts.check_cloudflare_health import CHECK_USER_AGENT, validate_health_payload, validate_health_url


def _fresh_financial_freshness() -> dict:
    return {
        "status": "ok",
        "blocksDeployment": False,
        "expectedFinancialPeriod": "2026Q1",
        "latestCachedFinancialPeriod": "2026Q1",
        "expectedPeriodCoverage": 1969,
    }


def _healthy_health_payload() -> dict:
    return {
        "status": "ok",
        "runtime": "cloudflare-python-worker",
        "cache": {
            "generatedAt": "2026-07-11T21:52:48+00:00",
            "sourceLastCheckedAt": "2026-07-11T21:52:48+00:00",
            "counts": {"companies": 1000, "analysis": 1000, "entry": 10, "watch": 980, "excluded": 10},
            "financialFreshness": _fresh_financial_freshness(),
        },
        "cacheQuality": {"ok": True},
    }


def test_health_check_uses_browser_like_user_agent():
    assert CHECK_USER_AGENT.startswith("Mozilla/5.0 ")


def test_validate_health_payload_accepts_matching_manifest_counts():
    payload = {
        "status": "ok",
        "runtime": "cloudflare-python-worker",
        "cache": {
            "counts": {"companies": 1000, "entry": 10, "watch": 980, "excluded": 10, "analysis": 1000},
            "financialFreshness": _fresh_financial_freshness(),
        },
        "cacheQuality": {"ok": True},
    }
    manifest = {"counts": {"companies": 1000, "entry": 10, "watch": 980, "excluded": 10, "analysis": 1000}}

    summary = validate_health_payload(payload, manifest)

    assert summary["status"] == "ok"
    assert summary["counts"]["analysis"] == 1000


def test_validate_health_payload_rejects_policy_stale_after_grace():
    payload = _healthy_health_payload()
    payload["status"] = "degraded"
    payload["cacheStatus"] = {
        "isStale": True,
        "nextRefreshAfter": "2026-07-12T00:52:48+00:00",
    }
    with pytest.raises(RuntimeError, match="refresh policy"):
        validate_health_payload(
            payload,
            max_cache_age_hours=36,
            max_refresh_delay_minutes=15,
            now=datetime(2026, 7, 12, 1, 8, tzinfo=UTC),
        )


def test_validate_health_payload_accepts_policy_stale_inside_grace():
    payload = _healthy_health_payload()
    payload["status"] = "degraded"
    payload["cacheStatus"] = {
        "isStale": True,
        "nextRefreshAfter": "2026-07-12T00:52:48+00:00",
    }
    summary = validate_health_payload(
        payload,
        max_cache_age_hours=36,
        max_refresh_delay_minutes=15,
        now=datetime(2026, 7, 12, 1, 0, tzinfo=UTC),
    )
    assert summary["refreshDelayMinutes"] == pytest.approx(7.2)


def test_validate_health_payload_accepts_exact_grace_boundary():
    payload = _healthy_health_payload()
    payload["status"] = "degraded"
    payload["cacheStatus"] = {
        "isStale": True,
        "nextRefreshAfter": "2026-07-12T00:52:48+00:00",
    }
    summary = validate_health_payload(
        payload,
        max_cache_age_hours=36,
        max_refresh_delay_minutes=15,
        now=datetime(2026, 7, 12, 1, 7, 48, tzinfo=UTC),
    )
    assert summary["refreshDelayMinutes"] == pytest.approx(15)


def test_validate_health_payload_rejects_stale_cache_reported_as_ok_inside_grace():
    payload = _healthy_health_payload()
    payload["cacheStatus"] = {
        "isStale": True,
        "nextRefreshAfter": "2026-07-12T00:52:48+00:00",
    }

    with pytest.raises(RuntimeError, match="expected 'degraded'"):
        validate_health_payload(
            payload,
            max_cache_age_hours=36,
            max_refresh_delay_minutes=15,
            now=datetime(2026, 7, 12, 1, 0, tzinfo=UTC),
        )


def test_validate_health_payload_rejects_passed_boundary_when_stale_flag_is_false():
    payload = _healthy_health_payload()
    payload["cacheStatus"] = {
        "isStale": False,
        "nextRefreshAfter": "2026-07-12T00:52:48+00:00",
    }

    with pytest.raises(RuntimeError, match="refresh policy"):
        validate_health_payload(
            payload,
            max_cache_age_hours=36,
            max_refresh_delay_minutes=15,
            now=datetime(2026, 7, 12, 1, 8, tzinfo=UTC),
        )


def test_validate_health_payload_keeps_cache_age_ceiling_during_refresh_grace():
    payload = _healthy_health_payload()
    payload["status"] = "degraded"
    payload["cache"]["generatedAt"] = "2026-07-10T12:00:00+00:00"
    payload["cache"]["sourceLastCheckedAt"] = "2026-07-10T12:00:00+00:00"
    payload["cacheStatus"] = {
        "isStale": True,
        "nextRefreshAfter": "2026-07-12T00:52:48+00:00",
    }

    with pytest.raises(RuntimeError, match="deployed cache is stale"):
        validate_health_payload(
            payload,
            max_cache_age_hours=36,
            max_refresh_delay_minutes=15,
            now=datetime(2026, 7, 12, 1, 0, tzinfo=UTC),
        )


def test_validate_health_payload_new_worker_default_is_strict():
    payload = _healthy_health_payload()
    payload["status"] = "degraded"
    payload["cacheStatus"] = {
        "isStale": True,
        "nextRefreshAfter": "2026-07-12T00:52:48+00:00",
    }
    with pytest.raises(RuntimeError, match="refresh policy"):
        validate_health_payload(
            payload,
            max_cache_age_hours=36,
            now=datetime(2026, 7, 12, 1, 0, tzinfo=UTC),
        )


def test_validate_health_payload_old_worker_uses_age_fallback():
    payload = _healthy_health_payload()
    payload.pop("cacheStatus", None)
    summary = validate_health_payload(
        payload,
        max_cache_age_hours=36,
        max_refresh_delay_minutes=15,
        now=datetime(2026, 7, 12, 1, 0, tzinfo=UTC),
    )
    assert summary["cacheAgeHours"] is not None


def test_validate_health_payload_prefers_top_level_cache_status_refresh_boundary():
    payload = _healthy_health_payload()
    payload["status"] = "degraded"
    payload["cache"]["nextRefreshAfter"] = "2026-07-12T02:00:00+00:00"
    payload["cacheStatus"] = {
        "isStale": True,
        "nextRefreshAfter": "2026-07-12T00:52:48+00:00",
    }

    with pytest.raises(RuntimeError, match="refresh policy"):
        validate_health_payload(
            payload,
            max_cache_age_hours=36,
            max_refresh_delay_minutes=15,
            now=datetime(2026, 7, 12, 1, 8, tzinfo=UTC),
        )


def test_validate_health_payload_rejects_degraded_or_mismatched_counts():
    payload = {
        "status": "degraded",
        "cache": {"counts": {"companies": 1, "analysis": 1}},
        "cacheQuality": {"ok": False},
    }

    with pytest.raises(RuntimeError, match="health status"):
        validate_health_payload(payload, {"counts": {"companies": 1000, "analysis": 1000}})


def test_validate_health_payload_rejects_stale_offline_seed():
    payload = {
        "status": "ok",
        "runtime": "cloudflare-python-worker",
        "cache": {
            "sourceLastCheckedAt": "2026-05-18T14:39:18+00:00",
            "counts": {"companies": 1000, "analysis": 1000},
            "qualityGates": {"buildMode": "offline"},
        },
        "cacheQuality": {"ok": True},
    }

    with pytest.raises(RuntimeError, match="stale|offline"):
        validate_health_payload(
            payload,
            max_cache_age_hours=36,
            now=datetime(2026, 5, 20, 5, 0, tzinfo=UTC),
            reject_offline_seed=True,
        )


def test_validate_health_payload_rejects_blocking_financial_freshness():
    payload = {
        "status": "ok",
        "runtime": "cloudflare-python-worker",
        "cache": {
            "counts": {"companies": 1000, "analysis": 1000},
            "financialFreshness": {
                "status": "stale",
                "blocksDeployment": True,
                "expectedFinancialPeriod": "2026Q1",
                "latestCachedFinancialPeriod": "2025Q4",
            },
        },
        "cacheQuality": {"ok": True},
    }

    with pytest.raises(RuntimeError, match="financial freshness"):
        validate_health_payload(payload)


def test_validate_health_payload_rejects_missing_financial_freshness():
    payload = {
        "status": "ok",
        "runtime": "cloudflare-python-worker",
        "cache": {"counts": {"companies": 1000, "analysis": 1000}},
        "cacheQuality": {"ok": True},
    }

    with pytest.raises(RuntimeError, match="financial freshness"):
        validate_health_payload(payload)


def test_validate_health_payload_accepts_fresh_online_seed():
    payload = {
        "status": "ok",
        "runtime": "cloudflare-python-worker",
        "cache": {
            "sourceLastCheckedAt": "2026-05-20T04:30:00Z",
            "counts": {"companies": 1000, "analysis": 1000},
            "qualityGates": {},
            "financialFreshness": _fresh_financial_freshness(),
        },
        "cacheQuality": {"ok": True},
    }

    summary = validate_health_payload(
        payload,
        max_cache_age_hours=36,
        now=datetime(2026, 5, 20, 5, 0, tzinfo=UTC),
        reject_offline_seed=True,
    )

    assert summary["cacheAgeHours"] == pytest.approx(0.5)


def test_validate_health_url_rejects_non_https_or_wrong_path():
    with pytest.raises(RuntimeError, match="https URL"):
        validate_health_url("file:///tmp/health.json")

    with pytest.raises(RuntimeError, match="/api/health"):
        validate_health_url("https://stock-scanner-beta-api.pcedison.workers.dev/status")

    assert (
        validate_health_url("https://stock-scanner-beta-api.pcedison.workers.dev/api/health")
        == "https://stock-scanner-beta-api.pcedison.workers.dev/api/health"
    )
