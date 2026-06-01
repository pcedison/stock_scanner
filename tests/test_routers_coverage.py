"""Branch coverage for FastAPI dependency helpers and router handlers.

The mock-data-default API suite (test_api.py) leaves the official-data-source
paths, a few rate-limit/report guards, and some account/holdings handlers
uncovered. These tests drive those branches directly (helpers) and over HTTP
with the official provider's network methods stubbed out.
"""

import pytest
from fastapi.testclient import TestClient

import backend.dependencies as deps_module
import backend.main as main_module
import backend.routers.market as market_module
from backend.models.holding import Holding
from backend.models.settings import ScannerSettings
from backend.services.settings_service import save_settings

# --- dependency helpers (unit level) ----------------------------------------


class _FakeRequest:
    def __init__(self, headers=None, client_host="1.2.3.4"):
        self.headers = headers or {}
        self.client = type("_Client", (), {"host": client_host})() if client_host else None


def test_auth_source_prefers_cloudflare_ip():
    assert deps_module._auth_source(_FakeRequest({"cf-connecting-ip": " 9.9.9.9 "})) == "9.9.9.9"


def test_check_scan_rate_limit_raises_429_when_exceeded(monkeypatch):
    monkeypatch.setattr(deps_module, "_SCAN_RATE_MAX_REQUESTS", 1)
    deps_module._scan_rate_store.clear()
    deps_module._check_scan_rate_limit("src-a")  # first request allowed
    with pytest.raises(deps_module.HTTPException) as excinfo:
        deps_module._check_scan_rate_limit("src-a")
    assert excinfo.value.status_code == 429
    deps_module._scan_rate_store.clear()


def test_evict_scan_rate_sources_caps_the_store(monkeypatch):
    monkeypatch.setattr(deps_module, "_SCAN_RATE_MAX_SOURCES", 2)
    deps_module._scan_rate_store.clear()
    deps_module._scan_rate_store["old"] = [1.0]
    deps_module._scan_rate_store["older"] = [0.5]
    deps_module._evict_scan_rate_sources_for("new")  # adding "new" would overflow -> evict oldest
    assert "older" not in deps_module._scan_rate_store
    assert "old" in deps_module._scan_rate_store
    deps_module._scan_rate_store.clear()


def test_report_response_rejects_unknown_format():
    with pytest.raises(deps_module.HTTPException) as excinfo:
        deps_module._report_response({}, "xml", "title", "prefix")
    assert excinfo.value.status_code == 400


def test_scan_holdings_payload_marks_unknown_stock_as_missing():
    payload = deps_module._scan_holdings_payload(
        [Holding(stockCode="2330", name="台積電", shares=1), Holding(stockCode="0000", name="未知", shares=1)],
        ScannerSettings(use_mock_data=True),
    )
    assert [m["stockCode"] for m in payload["missing"]] == ["0000"]
    assert len(payload["results"]) == 1


def test_scan_market_after_official_refresh_forces_provider_refresh(monkeypatch):
    calls = {"refresh": 0}
    monkeypatch.setattr(deps_module.official_provider, "refresh", lambda force=False: calls.__setitem__("refresh", calls["refresh"] + 1))
    monkeypatch.setattr(deps_module.official_provider, "list_snapshots", lambda settings: [])
    result = deps_module._scan_market_payload_after_official_refresh(ScannerSettings(use_mock_data=False))
    assert calls["refresh"] == 1
    assert result["dataSource"].startswith("official")


# --- router handlers over HTTP ----------------------------------------------

client = TestClient(main_module.app)


def _use_official_mode(monkeypatch, **settings_kwargs):
    """Switch the app to official mode with the provider's network stubbed out."""
    save_settings(ScannerSettings(use_mock_data=False, **settings_kwargs))
    monkeypatch.setattr(deps_module.official_provider, "list_snapshots", lambda settings: [])
    monkeypatch.setattr(deps_module.official_provider, "list_companies", lambda: [])
    monkeypatch.setattr(deps_module.official_provider, "get_snapshot", lambda code: None)
    monkeypatch.setattr(deps_module.official_provider, "refresh", lambda force=False: None)


def test_data_sources_status_runs_network_check(monkeypatch):
    monkeypatch.setattr(
        market_module.OfficialMonthlyRevenueAdapter, "health", lambda self: {"TWSE": {"ok": True}}
    )
    response = client.get("/api/data-sources/status", params={"check_network": "true"})
    assert response.status_code == 200
    assert response.json()["networkCheck"] == {"TWSE": {"ok": True}}


def test_backfill_history_endpoint(monkeypatch):
    class _Result:
        def as_dict(self):
            return {"processed": 0}

    monkeypatch.setattr(deps_module.official_provider, "list_companies", lambda: [])
    monkeypatch.setattr(deps_module.history_backfill_service, "backfill", lambda companies, **kwargs: _Result())
    response = client.post("/api/data-sources/backfill-history", params={"limit": 5, "mode": "full_quarterly"})
    assert response.status_code == 200
    assert response.json() == {"processed": 0}


def test_calendar_update_endpoint(monkeypatch):
    from backend.services.calendar import MarketCalendar

    fake = MarketCalendar(year=2026, closed_dates=set(), spring_festival_dates=set(), source="test", source_url="")
    monkeypatch.setattr(market_module, "update_market_calendar", lambda year: fake)
    response = client.post("/api/calendar/2026/update")
    assert response.status_code == 200
    assert response.json()["year"] == 2026


def test_analyze_unknown_stock_returns_404():
    response = client.post("/api/analyze/0000")
    assert response.status_code == 404


def test_scan_market_official_mode_uses_cache(monkeypatch):
    _use_official_mode(monkeypatch)
    post = client.post("/api/scan/market", json={"refreshMode": "cache_only"})
    get = client.get("/api/scan/market")
    assert post.status_code == 200
    assert get.status_code == 200


def test_cache_status_endpoint():
    response = client.get("/api/cache/status")
    assert response.status_code == 200
    assert "marketScan" in response.json()


def test_scan_holdings_official_mode(monkeypatch):
    _use_official_mode(monkeypatch)
    response = client.post("/api/scan/holdings", json={"holdings": [{"stockCode": "2330", "name": "台積電", "shares": 1}]})
    assert response.status_code == 200
    assert response.json()["dataSource"].startswith("official")


def test_market_report_official_mode(monkeypatch):
    _use_official_mode(monkeypatch)
    response = client.post("/api/reports/market", params={"report_format": "markdown"})
    assert response.status_code == 200
    assert "台股市場掃描報告" in response.text


def test_scheduler_auto_scan_manual_prompt_when_auto_disabled(monkeypatch):
    save_settings(ScannerSettings(use_mock_data=True, auto_scan_full_market=False))
    response = client.get("/api/scheduler/auto-scan", params={"today": "2026-03-31"})
    assert response.status_code == 200
    assert response.json()["action"] == "manual_prompt"


def test_scheduler_auto_scan_scans_and_backfills(monkeypatch):
    _use_official_mode(monkeypatch, auto_scan_full_market=True)

    class _Result:
        def as_dict(self):
            return {"processed": 0}

    monkeypatch.setattr(deps_module.history_backfill_service, "backfill", lambda companies, **kwargs: _Result())
    response = client.get("/api/scheduler/auto-scan", params={"today": "2026-03-31", "backfill_history": "true"})
    assert response.status_code == 200
    body = response.json()
    assert body["action"] == "scanned"
    assert body["historyBackfill"] == {"processed": 0}


# --- accounts router --------------------------------------------------------


def test_register_rejects_duplicate_username():
    test_client = TestClient(main_module.app)
    from uuid import uuid4

    username = f"user_{uuid4().hex[:10]}"
    first = test_client.post("/api/auth/register", json={"username": username, "password": "test-password-123"})
    assert first.status_code == 200
    second = TestClient(main_module.app).post(
        "/api/auth/register", json={"username": username, "password": "test-password-123"}
    )
    assert second.status_code == 400  # create_user ValueError -> 400


def test_register_returns_429_when_rate_limited(monkeypatch):
    from backend.services.auth import AuthRateLimitError

    def deny(username, source):
        raise AuthRateLimitError(30)

    monkeypatch.setattr(deps_module.auth_service, "assert_auth_allowed", deny)
    response = TestClient(main_module.app).post(
        "/api/auth/register", json={"username": "whoever", "password": "test-password-123"}
    )
    assert response.status_code == 429
    assert response.headers["Retry-After"] == "30"


def test_auth_me_without_session_is_unauthenticated():
    response = TestClient(main_module.app).get("/api/auth/me")
    assert response.status_code == 200
    assert response.json() == {"authenticated": False, "user": None, "holdings": []}


def test_upsert_my_holding_via_post():
    from uuid import uuid4

    test_client = TestClient(main_module.app)
    test_client.post("/api/auth/register", json={"username": f"user_{uuid4().hex[:10]}", "password": "test-password-123"})
    response = test_client.post(
        "/api/me/holdings",
        json={"holding": {"stockCode": "2330", "name": "台積電", "shares": 5, "averageCost": 600}},
    )
    assert response.status_code == 200
    assert [h["stockCode"] for h in response.json()["holdings"]] == ["2330"]
