import pytest

from scripts import run_remote_smoke as remote_smoke
from scripts.run_remote_smoke import REMOTE_SMOKE_USER_AGENT, base_url_from_health_url, validate_public_smoke_payloads


class SuccessfulRemoteClient:
    def __init__(self, base_url, timeout):
        self.base_url = base_url
        self.timeout = timeout

    def request_json(self, path, method="GET", payload=None):
        responses = {
            "/api/health": {"runtime": "cloudflare-python-worker", "status": "degraded"},
            "/api/auth/me": {"authenticated": False, "user": None},
            "/api/app-status": {"dataSourceStatus": {}, "schedulerAutoScan": {"action": "sleep"}},
            "/api/data-sources/status": {"activeProvider": "CloudflareR2Seed"},
            "/api/scan/market": {
                "entry": [{"stockCode": str(index)} for index in range(1000)],
                "cacheStatus": {"cacheHit": True},
            },
        }
        return responses[path]

    def request_json_status(self, path, method="GET", payload=None):
        return 401, {"detail": "invalid credentials"}


def test_base_url_from_health_url_requires_https_api_health():
    assert base_url_from_health_url("https://worker.example/api/health") == "https://worker.example/"
    with pytest.raises(RuntimeError):
        base_url_from_health_url("http://worker.example/api/health")


def test_remote_smoke_uses_browser_like_user_agent():
    assert REMOTE_SMOKE_USER_AGENT.startswith("Mozilla/5.0 ")


def test_run_public_smoke_forwards_refresh_grace(monkeypatch):
    captured = {}

    def fake_validate(*args, **kwargs):
        captured["max_refresh_delay_minutes"] = kwargs.get("max_refresh_delay_minutes")

    monkeypatch.setattr(remote_smoke, "RemoteClient", SuccessfulRemoteClient)
    monkeypatch.setattr(remote_smoke, "validate_health_payload", fake_validate)

    remote_smoke.run_public_smoke(
        "https://worker.example/api/health",
        manifest=None,
        timeout=20,
        max_refresh_delay_minutes=15,
    )

    assert captured["max_refresh_delay_minutes"] == 15


def test_run_public_smoke_preserves_legacy_positional_arguments(monkeypatch):
    captured = {}

    def fake_validate(*args, **kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(remote_smoke, "RemoteClient", SuccessfulRemoteClient)
    monkeypatch.setattr(remote_smoke, "validate_health_payload", fake_validate)

    remote_smoke.run_public_smoke("https://worker.example/api/health", None, 20, 90, 36, True)

    assert captured["max_refresh_delay_minutes"] is None
    assert captured["reject_offline_seed"] is True


@pytest.mark.parametrize(
    ("extra_args", "expected"),
    [([], None), (["--max-refresh-delay-minutes", "15"], 15)],
)
def test_remote_smoke_cli_preserves_strict_default_and_explicit_grace(monkeypatch, extra_args, expected):
    captured = {}

    def fake_run_public_smoke(*args, **kwargs):
        captured["max_refresh_delay_minutes"] = kwargs.get("max_refresh_delay_minutes")
        return {"status": "ok"}

    monkeypatch.setattr(remote_smoke, "run_public_smoke", fake_run_public_smoke)

    result = remote_smoke.main(["--health-url", "https://worker.example/api/health", *extra_args])

    assert result == 0
    assert captured["max_refresh_delay_minutes"] == expected


def test_validate_public_smoke_payloads_accepts_expected_shapes():
    summary = validate_public_smoke_payloads(
        {
            "health": {"runtime": "cloudflare-python-worker", "status": "ok"},
            "authMe": {"authenticated": False, "user": None},
            "badLogin": {"status": 401},
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
                "badLogin": {"status": 401},
                "appStatus": {"dataSourceStatus": {}, "schedulerAutoScan": {}},
                "dataSources": {"activeProvider": "CloudflareR2Seed"},
                "marketScan": {"entry": [{"stockCode": str(index)} for index in range(1000)], "cacheStatus": {}},
            }
        )
