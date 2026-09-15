import pytest

from scripts import run_remote_smoke as remote_smoke
from scripts.run_remote_smoke import REMOTE_SMOKE_USER_AGENT, base_url_from_health_url, validate_public_smoke_payloads


def _market_index(*, entry: int = 0, watch: int = 0, excluded: int = 0, generation_id: str = "a" * 24) -> dict:
    return {
        "schemaVersion": 2,
        "generationId": generation_id,
        "pageSize": 100,
        "counts": {
            "universeSize": entry + watch + excluded,
            "announced": entry + watch + excluded,
            "pending": 0,
            "categories": {"entry": entry, "watch": watch, "excluded": excluded},
        },
        "cacheStatus": {"cacheHit": True},
    }


def _market_report_csv(counts: dict[str, int] | None = None, **kwargs: int) -> str:
    counts = {**(counts or {}), **kwargs}
    lines = ["category,stockCode,companyName,status,summary"]
    for category, count in counts.items():
        for index in range(count):
            lines.append(f"{category},{index:06d},Example Co,{category.upper()},summary")
    return "\n".join(lines) + "\n"


class SuccessfulRemoteClient:
    def __init__(self, base_url, timeout):
        self.base_url = base_url
        self.timeout = timeout

    def request_json(self, path, method="GET", payload=None):
        responses = {
            "/api/health": {"runtime": "cloudflare-python-worker", "status": "degraded"},
            "/api/auth/me": {"authenticated": False, "user": None},
            "/api/app-status": {"dataSourceStatus": {}, "schedulerAutoScan": {"action": "sleep"}},
            "/api/runtime-config": {"schemaVersion": 1, "marketScanApiVersion": "v2", "edgeCacheEnabled": False},
            "/api/data-sources/status": {"activeProvider": "CloudflareR2Seed"},
            "/api/scan/market/index": _market_index(entry=1000),
        }
        return responses[path]

    def request_json_status(self, path, method="GET", payload=None):
        return 401, {"detail": "invalid credentials"}

    def request_text(self, path, method="GET", payload=None):
        return _market_report_csv(entry=1000)


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
            "runtimeConfig": {"schemaVersion": 1, "marketScanApiVersion": "v2", "edgeCacheEnabled": False},
            "dataSources": {"activeProvider": "CloudflareR2Seed"},
            "marketIndex": _market_index(entry=10, watch=990, generation_id="b" * 24),
            "marketReportCsv": _market_report_csv(entry=10, watch=990),
        }
    )

    assert summary["runtime"] == "cloudflare-python-worker"
    assert summary["activeProvider"] == "CloudflareR2Seed"
    assert summary["marketScanRows"] == 1000
    assert summary["marketReportRows"] == 1000
    assert summary["marketGenerationId"] == "b" * 24
    assert summary["marketScanApiVersion"] == "v2"


def test_validate_public_smoke_payloads_rejects_a_non_v2_runtime_config():
    with pytest.raises(RuntimeError, match="marketScanApiVersion"):
        validate_public_smoke_payloads(
            {
                "health": {"runtime": "cloudflare-python-worker", "status": "ok"},
                "authMe": {"authenticated": False, "user": None},
                "badLogin": {"status": 401},
                "appStatus": {"dataSourceStatus": {}, "schedulerAutoScan": {"action": "sleep"}},
                "runtimeConfig": {"schemaVersion": 1, "marketScanApiVersion": "v1", "edgeCacheEnabled": False},
                "dataSources": {"activeProvider": "CloudflareR2Seed"},
                "marketIndex": _market_index(entry=1000),
                "marketReportCsv": _market_report_csv(entry=1000),
            }
        )


def test_validate_public_smoke_payloads_rejects_an_index_without_a_generation():
    with pytest.raises(RuntimeError, match="generationId"):
        validate_public_smoke_payloads(
            {
                "health": {"runtime": "cloudflare-python-worker", "status": "ok"},
                "authMe": {"authenticated": False, "user": None},
                "badLogin": {"status": 401},
                "appStatus": {"dataSourceStatus": {}, "schedulerAutoScan": {"action": "sleep"}},
                "runtimeConfig": {"schemaVersion": 1, "marketScanApiVersion": "v2", "edgeCacheEnabled": False},
                "dataSources": {"activeProvider": "CloudflareR2Seed"},
                "marketIndex": {**_market_index(entry=1000), "generationId": "short"},
                "marketReportCsv": _market_report_csv(entry=1000),
            }
        )


def test_validate_public_smoke_payloads_rejects_wrong_runtime():
    with pytest.raises(RuntimeError, match="runtime"):
        validate_public_smoke_payloads(
            {
                "health": {"runtime": "fastapi", "status": "ok"},
                "authMe": {"authenticated": False, "user": None},
                "badLogin": {"status": 401},
            "appStatus": {"dataSourceStatus": {}, "schedulerAutoScan": {}},
            "runtimeConfig": {"schemaVersion": 1, "marketScanApiVersion": "v2", "edgeCacheEnabled": False},
            "dataSources": {"activeProvider": "CloudflareR2Seed"},
                "marketIndex": _market_index(entry=1000),
                "marketReportCsv": _market_report_csv(entry=1000),
            }
        )


def test_validate_public_smoke_payloads_rejects_a_market_report_count_mismatch():
    with pytest.raises(RuntimeError, match=r"market report CSV has \d+ 'entry' rows but market index"):
        validate_public_smoke_payloads(
            {
                "health": {"runtime": "cloudflare-python-worker", "status": "ok"},
                "authMe": {"authenticated": False, "user": None},
                "badLogin": {"status": 401},
                "appStatus": {"dataSourceStatus": {}, "schedulerAutoScan": {"action": "sleep"}},
                "runtimeConfig": {"schemaVersion": 1, "marketScanApiVersion": "v2", "edgeCacheEnabled": False},
                "dataSources": {"activeProvider": "CloudflareR2Seed"},
                "marketIndex": _market_index(entry=10, watch=990),
                # One fewer "entry" row than the index reports.
                "marketReportCsv": _market_report_csv(entry=9, watch=990),
            }
        )


def test_validate_public_smoke_payloads_rejects_a_missing_market_report_csv():
    with pytest.raises(RuntimeError, match="market report CSV export"):
        validate_public_smoke_payloads(
            {
                "health": {"runtime": "cloudflare-python-worker", "status": "ok"},
                "authMe": {"authenticated": False, "user": None},
                "badLogin": {"status": 401},
                "appStatus": {"dataSourceStatus": {}, "schedulerAutoScan": {"action": "sleep"}},
                "runtimeConfig": {"schemaVersion": 1, "marketScanApiVersion": "v2", "edgeCacheEnabled": False},
                "dataSources": {"activeProvider": "CloudflareR2Seed"},
                "marketIndex": _market_index(entry=1000),
                "marketReportCsv": "",
            }
        )


def test_validate_public_smoke_payloads_rejects_a_market_report_csv_with_a_bad_header():
    with pytest.raises(RuntimeError, match="market report CSV has unexpected header"):
        validate_public_smoke_payloads(
            {
                "health": {"runtime": "cloudflare-python-worker", "status": "ok"},
                "authMe": {"authenticated": False, "user": None},
                "badLogin": {"status": 401},
                "appStatus": {"dataSourceStatus": {}, "schedulerAutoScan": {"action": "sleep"}},
                "runtimeConfig": {"schemaVersion": 1, "marketScanApiVersion": "v2", "edgeCacheEnabled": False},
                "dataSources": {"activeProvider": "CloudflareR2Seed"},
                "marketIndex": _market_index(entry=1000),
                # Missing the "status" column that market_report always writes.
                "marketReportCsv": "category,stockCode,companyName,summary\nentry,000000,Example Co,summary\n",
            }
        )


def test_validate_public_smoke_payloads_rejects_a_market_report_csv_with_an_extra_category():
    with pytest.raises(RuntimeError, match=r"market report CSV has unexpected categories: \['results'\]"):
        validate_public_smoke_payloads(
            {
                "health": {"runtime": "cloudflare-python-worker", "status": "ok"},
                "authMe": {"authenticated": False, "user": None},
                "badLogin": {"status": 401},
                "appStatus": {"dataSourceStatus": {}, "schedulerAutoScan": {"action": "sleep"}},
                "runtimeConfig": {"schemaVersion": 1, "marketScanApiVersion": "v2", "edgeCacheEnabled": False},
                "dataSources": {"activeProvider": "CloudflareR2Seed"},
                "marketIndex": _market_index(entry=10, watch=990),
                # "results" is a holdings-only category that /api/reports/market should never emit.
                "marketReportCsv": _market_report_csv(entry=10, watch=990, results=5),
            }
        )
