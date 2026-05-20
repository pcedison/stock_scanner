from datetime import UTC, datetime

import pytest

from scripts.check_cloudflare_health import validate_health_payload


def test_validate_health_payload_accepts_matching_manifest_counts():
    payload = {
        "status": "ok",
        "runtime": "cloudflare-python-worker",
        "cache": {"counts": {"companies": 1000, "entry": 10, "watch": 980, "excluded": 10, "analysis": 1000}},
        "cacheQuality": {"ok": True},
    }
    manifest = {"counts": {"companies": 1000, "entry": 10, "watch": 980, "excluded": 10, "analysis": 1000}}

    summary = validate_health_payload(payload, manifest)

    assert summary["status"] == "ok"
    assert summary["counts"]["analysis"] == 1000


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


def test_validate_health_payload_accepts_fresh_online_seed():
    payload = {
        "status": "ok",
        "runtime": "cloudflare-python-worker",
        "cache": {
            "sourceLastCheckedAt": "2026-05-20T04:30:00Z",
            "counts": {"companies": 1000, "analysis": 1000},
            "qualityGates": {},
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
