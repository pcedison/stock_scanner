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


def test_deployment_grace_accepts_delayed_refresh_below_hard_age_ceiling():
    payload = _healthy_health_payload()
    payload["status"] = "degraded"
    payload["cache"]["generatedAt"] = "2026-07-26T21:22:00+00:00"
    payload["cache"]["sourceLastCheckedAt"] = "2026-07-26T21:22:00+00:00"
    payload["cacheStatus"] = {
        "isStale": True,
        "nextRefreshAfter": "2026-07-27T06:48:04+00:00",
    }

    summary = validate_health_payload(
        payload,
        max_cache_age_hours=36,
        max_refresh_delay_minutes=1440,
        now=datetime(2026, 7, 27, 8, 7, 22, tzinfo=UTC),
    )

    assert summary["refreshDelayMinutes"] == pytest.approx(79.3)


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


def test_validate_health_payload_ignores_overdue_raw_manifest_boundary_when_top_level_policy_is_fresh():
    payload = _healthy_health_payload()
    payload["cache"]["nextRefreshAfter"] = "2026-07-12T00:30:00+00:00"
    payload["cacheStatus"] = {
        "isStale": False,
        "nextRefreshAfter": "2026-07-12T02:00:00+00:00",
    }

    summary = validate_health_payload(
        payload,
        max_cache_age_hours=36,
        max_refresh_delay_minutes=15,
        now=datetime(2026, 7, 12, 1, 0, tzinfo=UTC),
    )

    assert summary["status"] == "ok"
    assert summary["refreshDelayMinutes"] is None


def test_validate_health_payload_reports_quality_and_count_problems_without_misleading_status():
    payload = {
        "status": "degraded",
        "cache": {"counts": {"companies": 1, "analysis": 1}},
        "cacheQuality": {"ok": False},
    }

    with pytest.raises(RuntimeError) as exc_info:
        validate_health_payload(payload, {"counts": {"companies": 1000, "analysis": 1000}})

    message = str(exc_info.value)
    assert "cacheQuality.ok is not true" in message
    assert "deployed manifest counts" in message
    assert "expected 'ok'" not in message


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


def _dispatch(**overrides) -> dict:
    payload = {"enabled": True, "jobStatus": None, "dispatchStatus": None, "dispatchErrorCode": None, "dispatchAttempts": 0}
    payload.update(overrides)
    return payload


def test_validate_health_payload_ignores_dispatch_state_unless_required():
    payload = _healthy_health_payload()
    payload["refreshDispatch"] = _dispatch(jobStatus="queued", dispatchStatus="failed", dispatchErrorCode="GITHUB_HTTP_401")

    summary = validate_health_payload(payload, now=datetime(2026, 7, 12, 0, 0, tzinfo=UTC))

    assert summary["refreshDispatch"]["dispatchErrorCode"] == "GITHUB_HTTP_401"


def test_validate_health_payload_fails_fast_on_a_rejected_installation_token():
    payload = _healthy_health_payload()
    payload["refreshDispatch"] = _dispatch(jobStatus="queued", dispatchStatus="failed", dispatchErrorCode="GITHUB_HTTP_401")

    with pytest.raises(RuntimeError, match=r"refresh dispatch failed: GITHUB_HTTP_401 - GitHub rejected the app"):
        validate_health_payload(payload, now=datetime(2026, 7, 12, 0, 0, tzinfo=UTC), require_dispatch_healthy=True)


def test_validate_health_payload_fails_fast_on_a_github_app_credential_fault():
    # A GitHub App key does not expire, but it can still be revoked or mis-copied; the
    # monitor has to name the secret to fix rather than report a stale seed.
    payload = _healthy_health_payload()
    payload["refreshDispatch"] = _dispatch(
        jobStatus="queued", dispatchStatus="failed", dispatchErrorCode="GITHUB_APP_SIGN_FAILED"
    )

    with pytest.raises(RuntimeError, match=r"GITHUB_APP_SIGN_FAILED - GITHUB_APP_PRIVATE_KEY is not a readable"):
        validate_health_payload(payload, now=datetime(2026, 7, 12, 0, 0, tzinfo=UTC), require_dispatch_healthy=True)


def test_validate_health_payload_requires_dispatch_enabled_and_reported():
    now = datetime(2026, 7, 12, 0, 0, tzinfo=UTC)
    missing = _healthy_health_payload()
    with pytest.raises(RuntimeError, match="refreshDispatch is missing"):
        validate_health_payload(missing, now=now, require_dispatch_healthy=True)

    disabled = _healthy_health_payload()
    disabled["refreshDispatch"] = _dispatch(enabled=False)
    with pytest.raises(RuntimeError, match="refresh dispatch is disabled"):
        validate_health_payload(disabled, now=now, require_dispatch_healthy=True)


def test_validate_health_payload_tolerates_transient_dispatch_states():
    now = datetime(2026, 7, 12, 0, 0, tzinfo=UTC)
    for state in (
        _dispatch(),
        _dispatch(jobStatus="queued", dispatchStatus="pending"),
        _dispatch(jobStatus="queued", dispatchStatus="dispatched", dispatchAttempts=1),
        _dispatch(jobStatus="running", dispatchStatus="workflow_claimed", dispatchAttempts=1),
        _dispatch(jobStatus="queued", dispatchStatus="unknown", dispatchErrorCode="GITHUB_HTTP_503", dispatchAttempts=2),
    ):
        payload = _healthy_health_payload()
        payload["refreshDispatch"] = state
        validate_health_payload(payload, now=now, require_dispatch_healthy=True)

    stuck = _healthy_health_payload()
    stuck["refreshDispatch"] = _dispatch(jobStatus="queued", dispatchStatus="unknown", dispatchErrorCode="GITHUB_DISPATCH_NETWORK", dispatchAttempts=3)
    with pytest.raises(RuntimeError, match="not been acknowledged after 3 attempts"):
        validate_health_payload(stuck, now=now, require_dispatch_healthy=True)
