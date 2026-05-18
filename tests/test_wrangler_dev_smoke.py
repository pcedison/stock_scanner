import pytest

from scripts.run_wrangler_dev_smoke import validate_worker_smoke_payload


def test_validate_worker_smoke_payload_accepts_local_degraded_cache():
    summary = validate_worker_smoke_payload(
        {
            "status": "degraded",
            "runtime": "cloudflare-python-worker",
            "cache": {},
            "cacheQuality": {"ok": False},
        }
    )

    assert summary["status"] == "degraded"
    assert summary["cacheQualityOk"] is False


def test_validate_worker_smoke_payload_rejects_wrong_runtime():
    with pytest.raises(RuntimeError, match="unexpected runtime"):
        validate_worker_smoke_payload({"status": "ok", "runtime": "fastapi", "cache": {}, "cacheQuality": {}})
