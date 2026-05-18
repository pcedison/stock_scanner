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
