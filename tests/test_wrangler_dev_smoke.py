import pytest

from scripts import run_wrangler_dev_smoke
from scripts.run_wrangler_dev_smoke import (
    build_wrangler_dev_command,
    validate_local_smoke_url,
    validate_worker_smoke_payload,
)


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


def test_wrangler_dev_command_enables_scheduled_smoke_and_disables_dispatch(tmp_path):
    command = build_wrangler_dev_command("npx", 8799, tmp_path / "state")

    assert "--test-scheduled" in command
    assert "--persist-to" in command
    assert str(tmp_path / "state") in command
    assert "GITHUB_DISPATCH_ENABLED:false" in command
    # The smoke never carries a real dispatch credential of any kind.
    assert "GITHUB_APP_PRIVATE_KEY" not in " ".join(command)


def test_refresh_smoke_uses_csrf_header_for_production_worker(monkeypatch):
    calls = []

    def fake_request_json(url, **kwargs):
        calls.append((url, kwargs))
        if kwargs.get("expected_status") == 404:
            return 404, {"cache-control": "no-store"}, {"detail": "Not found"}
        if url.endswith("/cdn-cgi/handler/scheduled?format=json"):
            return 200, {"cache-control": "no-store"}, {"status": "disabled"}
        if kwargs.get("method") == "POST":
            return 202, {"cache-control": "no-store", "location": "/api/scan/market/refresh/" + "a" * 32}, {
                "jobId": "a" * 32,
                "statusUrl": "/api/scan/market/refresh/" + "a" * 32,
            }
        return 200, {"cache-control": "no-store"}, {"jobId": "a" * 32, "status": "queued"}

    monkeypatch.setattr(run_wrangler_dev_smoke, "_request_json", fake_request_json)

    run_wrangler_dev_smoke.validate_refresh_smoke("http://127.0.0.1:8787")

    post_headers = [kwargs["headers"] for _url, kwargs in calls if kwargs.get("method") == "POST"]
    assert post_headers
    assert all(headers["x-stock-scanner-csrf"] == "1" for headers in post_headers)
