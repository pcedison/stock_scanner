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
    snapshots = deps_module.mock_provider.list_snapshots(ScannerSettings(use_mock_data=True))
    by_code = {snapshot.company.stockCode: snapshot for snapshot in snapshots}
    monkeypatch.setattr(deps_module.official_provider, "list_snapshots", lambda settings: snapshots)
    monkeypatch.setattr(deps_module.official_provider, "list_companies", lambda: [item.company for item in snapshots])
    monkeypatch.setattr(deps_module.official_provider, "get_snapshot", by_code.get)
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


def test_market_v2_excludes_volatile_cache_status_and_spans_physical_pages(monkeypatch):
    served = {"count": 0}
    stable_scan = {
        "generatedAt": "2026-07-13T00:00:00+00:00",
        "filingContext": {
            "monthlyRevenuePeriod": "2026-06",
            "freshnessFinancialReport": {"period": "2026Q1"},
            "activeFinancialReport": {"period": "2026Q2"},
        },
        "financialFreshness": {"latestCachedFinancialPeriod": "2026Q1"},
        "entry": [],
        "watch": [
            {
                "stockCode": f"{index:04d}",
                "companyName": f"Company {index}",
                "status": "WATCH",
                "summary": "announced",
                "reasons": [
                    {
                        "code": "OFFICIAL_Q",
                        "title": "Financial report",
                        "passed": True,
                        "severity": "INFO",
                        "message": "2026Q1 is available",
                    }
                ],
                "internalEvidence": {"must": "not affect generation"},
            }
            for index in range(150)
        ],
        "excluded": [],
    }

    def cached_scan():
        served["count"] += 1
        return {**stable_scan, "cacheStatus": {"servedAt": f"request-{served['count']}"}}

    monkeypatch.setattr(market_module, "scan_market_cached", cached_scan)

    first = client.get("/api/scan/market/index").json()
    second = client.get("/api/scan/market/index").json()

    assert first["generationId"] == second["generationId"]
    assert first["cacheStatus"] != second["cacheStatus"]
    assert first["cacheStatusInputs"] == {
        "generatedAt": "2026-07-13T00:00:00+00:00",
        "latestRevenuePeriod": "2026-06",
        "latestFinancialPeriod": "2026Q1",
    }

    results = client.get(
        "/api/scan/market/results",
        params={
            "disclosure": "announced",
            "category": "watch",
            "cursor": 96,
            "limit": 10,
            "generationId": first["generationId"],
        },
    )
    assert results.status_code == 200
    assert [item["stockCode"] for item in results.json()["items"]] == [f"{index:04d}" for index in range(96, 106)]

    beyond = client.get(
        "/api/scan/market/results",
        params={"disclosure": "announced", "category": "watch", "cursor": 999, "limit": 10},
    )
    assert beyond.status_code == 200
    assert beyond.json()["items"] == []
    assert beyond.json()["nextCursor"] is None


def test_market_v2_maps_local_generation_build_failure_to_safe_503(monkeypatch):
    monkeypatch.setattr(market_module, "build_market_generation", lambda *args, **kwargs: (_ for _ in ()).throw(ValueError("secret")))
    safe_client = TestClient(main_module.app, raise_server_exceptions=False)

    index = safe_client.get("/api/scan/market/index")
    results = safe_client.get(
        "/api/scan/market/results",
        params={"disclosure": "announced", "category": "watch", "cursor": 0, "limit": 1},
    )

    assert index.status_code == 503
    assert results.status_code == 503
    assert index.json() == results.json() == {"detail": "market_query_unavailable"}


def test_market_v2_generation_memory_keeps_only_current_and_previous(monkeypatch):
    with market_module._market_generation_lock:
        market_module._market_generations.clear()
    calls = {"value": 0}

    def changing_scan():
        calls["value"] += 1
        return {
            "generatedAt": f"2026-07-13T00:00:0{calls['value']}+00:00",
            "filingContext": {},
            "entry": [
                {
                    "stockCode": "2330",
                    "companyName": "TSMC",
                    "status": "ENTRY",
                    "summary": "announced",
                    "reasons": [],
                }
            ],
            "watch": [],
            "excluded": [],
        }

    monkeypatch.setattr(market_module, "scan_market_cached", changing_scan)
    generation_ids = [client.get("/api/scan/market/index").json()["generationId"] for _ in range(3)]

    assert len(market_module._market_generations) == 2
    assert generation_ids[0] not in market_module._market_generations
    assert list(market_module._market_generations) == generation_ids[1:]

    query = {"disclosure": "announced", "category": "entry", "cursor": 0, "limit": 1}
    previous = client.get(
        "/api/scan/market/results",
        params={**query, "generationId": generation_ids[1]},
    )
    assert previous.status_code == 200
    assert previous.json()["generationId"] == generation_ids[1]
    assert calls["value"] == 3

    evicted = client.get(
        "/api/scan/market/results",
        params={**query, "generationId": generation_ids[0]},
    )
    assert evicted.status_code == 409
    assert calls["value"] == 4
    assert len(market_module._market_generations) == 2
    with market_module._market_generation_lock:
        market_module._market_generations.clear()


def test_market_v2_fastapi_rejects_wire_responses_over_budget(monkeypatch):
    generation = market_module.build_market_generation(
        {
            "generatedAt": "2026-07-13T00:00:00+00:00",
            "filingContext": {},
            "entry": [{"stockCode": "2330", "status": "ENTRY", "reasons": []}],
            "watch": [],
            "excluded": [],
        }
    )
    monkeypatch.setattr(
        market_module,
        "_current_market_generation",
        lambda: (generation, {"padding": "x" * (50 * 1024)}),
    )

    index = client.get("/api/scan/market/index")
    assert index.status_code == 503

    monkeypatch.setattr(
        market_module,
        "query_market_generation",
        lambda *_args: {
            "schemaVersion": 2,
            "generationId": generation.generation_id,
            "disclosure": "announced",
            "category": "entry",
            "cursor": 0,
            "limit": 1,
            "total": 1,
            "nextCursor": None,
            "items": [{"stockCode": "2330", "summary": "x" * (500 * 1024)}],
        },
    )
    results = client.get(
        "/api/scan/market/results",
        params={"disclosure": "announced", "category": "entry", "cursor": 0, "limit": 1},
    )
    assert results.status_code == 503


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
