import pytest

from scripts.run_wrangler_dev_smoke import validate_local_smoke_url, validate_worker_smoke_payload


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


def test_validate_local_smoke_url_rejects_non_loopback_urls():
    with pytest.raises(RuntimeError, match="loopback"):
        validate_local_smoke_url("https://stock-scanner-beta-api.pcedison.workers.dev/api/health")

    with pytest.raises(RuntimeError, match="loopback"):
        validate_local_smoke_url("file:///tmp/health.json")

    assert validate_local_smoke_url("http://127.0.0.1:8787/api/health") == "http://127.0.0.1:8787/api/health"
