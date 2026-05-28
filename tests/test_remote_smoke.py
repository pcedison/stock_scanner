import pytest

from scripts.run_remote_smoke import REMOTE_SMOKE_USER_AGENT, base_url_from_health_url, validate_public_smoke_payloads


def test_base_url_from_health_url_requires_https_api_health():
    assert base_url_from_health_url("https://worker.example/api/health") == "https://worker.example/"
    with pytest.raises(RuntimeError):
        base_url_from_health_url("http://worker.example/api/health")


def test_remote_smoke_uses_browser_like_user_agent():
    assert REMOTE_SMOKE_USER_AGENT.startswith("Mozilla/5.0 ")


def test_validate_public_smoke_payloads_accepts_expected_shapes():
    summary = validate_public_smoke_payloads(
        {
            "health": {"runtime": "cloudflare-python-worker", "status": "ok"},
            "authMe": {"authenticated": False, "user": None},
            "appStatus": {"dataSourceStatus": {}, "schedulerAutoScan": {"action": "sleep"}},
            "dataSources": {"activeProvider": "CloudflareR2Seed"},
            "marketScan": {
                "entry": [{"stockCode": str(index)} for index in range(10)],
                "watch": [{"stockCode": str(index)} for index in range(990)],
                "excluded": [],
                "cacheStatus": {"cacheHit": True},
            },
        }
    )

    assert summary["runtime"] == "cloudflare-python-worker"
    assert summary["activeProvider"] == "CloudflareR2Seed"
    assert summary["marketScanRows"] == 1000


def test_validate_public_smoke_payloads_rejects_wrong_runtime():
    with pytest.raises(RuntimeError, match="runtime"):
        validate_public_smoke_payloads(
            {
                "health": {"runtime": "fastapi", "status": "ok"},
                "authMe": {"authenticated": False, "user": None},
                "appStatus": {"dataSourceStatus": {}, "schedulerAutoScan": {}},
                "dataSources": {"activeProvider": "CloudflareR2Seed"},
                "marketScan": {"entry": [{"stockCode": str(index)} for index in range(1000)], "cacheStatus": {}},
            }
        )
