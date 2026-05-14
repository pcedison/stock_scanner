from fastapi.testclient import TestClient
import pytest
from uuid import uuid4

from backend.main import app
from backend.models.settings import ScannerSettings


client = TestClient(app)
MOCK_SETTINGS = ScannerSettings(use_mock_data=True)


@pytest.fixture(autouse=True)
def force_mock_provider_for_api_tests():
    original = client.get("/api/settings").json()
    client.put("/api/settings", json={**original, "use_mock_data": True})
    try:
        yield
    finally:
        client.put("/api/settings", json=original)


def test_health_endpoint():
    response = client.get("/api/health")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_company_search_supports_code_and_name():
    by_code = client.get("/api/companies/search?q=2357")
    by_name = client.get("/api/companies/search?q=華碩")

    assert by_code.status_code == 200
    assert by_code.json()["items"][0]["name"] == "華碩"
    assert by_name.json()["items"][0]["stockCode"] == "2357"


def test_analyze_api_returns_reasons():
    response = client.post("/api/analyze/2357", json={"settings": MOCK_SETTINGS.model_dump()})
    payload = response.json()

    assert response.status_code == 200
    assert payload["status"] == "ENTRY"
    assert payload["reasons"]


def test_scan_market_returns_entry_watch_and_excluded_lists():
    response = client.post("/api/scan/market", json={"settings": MOCK_SETTINGS.model_dump()})
    payload = response.json()

    assert response.status_code == 200
    assert any(item["stockCode"] == "2357" for item in payload["entry"])
    assert any(item["stockCode"] == "2881" for item in payload["excluded"])
    assert "filingContext" in payload
    assert "monthlyRevenuePeriod" in payload["filingContext"]
    assert all(item["reasons"] for group in ["entry", "watch", "excluded"] for item in payload[group])


def test_scan_holdings_returns_exit_for_exit_mock_stock():
    response = client.post(
        "/api/scan/holdings",
        json={
            "holdings": [{"stockCode": "3008", "name": "大立光", "shares": 1000, "averageCost": 2000}],
            "settings": MOCK_SETTINGS.model_dump(),
        },
    )
    payload = response.json()

    assert response.status_code == 200
    assert payload["results"][0]["status"] == "EXIT"
    assert payload["results"][0]["reasons"]


def test_settings_api_round_trip():
    original = client.get("/api/settings").json()
    updated = {**original, "manual_scan_enabled": False}
    try:
        put_response = client.put("/api/settings", json=updated)
        get_response = client.get("/api/settings")

        assert put_response.status_code == 200
        assert get_response.json()["manual_scan_enabled"] is False
    finally:
        client.put("/api/settings", json=original)


def test_lightweight_auth_persists_server_side_holdings():
    test_client = TestClient(app)
    username = f"user_{uuid4().hex[:10]}"
    password = "test-password-123"

    register_response = test_client.post("/api/auth/register", json={"username": username, "password": password})
    assert register_response.status_code == 200
    assert register_response.json()["authenticated"] is True

    replace_response = test_client.put(
        "/api/me/holdings",
        json={"holdings": [{"stockCode": "2330", "name": "台積電", "shares": 1000, "averageCost": 600}]},
    )
    assert replace_response.status_code == 200
    assert replace_response.json()["holdings"][0]["stockCode"] == "2330"

    logout_response = test_client.post("/api/auth/logout", json={})
    assert logout_response.status_code == 200
    assert test_client.get("/api/me/holdings").status_code == 401

    login_response = test_client.post("/api/auth/login", json={"username": username, "password": password})
    assert login_response.status_code == 200
    holdings_response = test_client.get("/api/me/holdings")
    assert holdings_response.status_code == 200
    assert holdings_response.json()["holdings"] == [
        {"stockCode": "2330", "name": "台積電", "shares": 1000, "averageCost": 600.0}
    ]


def test_manual_scan_disabled_blocks_manual_scan_api():
    response = client.post(
        "/api/scan/market",
        json={"settings": ScannerSettings(use_mock_data=True, manual_scan_enabled=False).model_dump()},
    )

    assert response.status_code == 403


def test_data_source_status_is_explicitly_mock():
    response = client.get("/api/data-sources/status")
    payload = response.json()

    assert response.status_code == 200
    assert payload["activeProvider"] == "MockDataProvider"
    assert payload["activeProviderIsRealtime"] is False
    assert payload["activeProviderIsFullMarket"] is False
    assert payload["thirdPartyDataPlatforms"]["MacroMicro"]["enabled"] is False


def test_scheduler_wakeup_endpoint():
    response = client.get("/api/scheduler/wakeup?today=2026-02-13")
    payload = response.json()

    assert response.status_code == 200
    assert payload["status"] == "WAKE"
    assert "SPRING_FESTIVAL_GUARD" in payload["events"]


def test_scheduler_auto_scan_runs_when_enabled():
    response = client.get("/api/scheduler/auto-scan?today=2026-02-13")
    payload = response.json()

    assert response.status_code == 200
    assert payload["action"] == "scanned"
    assert payload["scan"]["entry"]


def test_scheduler_auto_scan_can_report_without_executing_scan():
    response = client.get("/api/scheduler/auto-scan?today=2026-02-13&execute=false")
    payload = response.json()

    assert response.status_code == 200
    assert payload["action"] == "ready"
    assert payload["scan"] is None


def test_report_endpoints_return_markdown_and_csv():
    md_response = client.post("/api/reports/market?report_format=markdown", json={"settings": MOCK_SETTINGS.model_dump()})
    csv_response = client.post(
        "/api/reports/holdings?report_format=csv",
        json={
            "holdings": [{"stockCode": "3008", "name": "大立光", "shares": 1000, "averageCost": 2000}],
            "settings": MOCK_SETTINGS.model_dump(),
        },
    )

    assert md_response.status_code == 200
    assert "# 台股市場掃描報告" in md_response.text
    assert csv_response.status_code == 200
    assert "stockCode,companyName,status" in csv_response.text


def test_calendar_and_integrations_status_endpoints():
    calendar_response = client.get("/api/calendar/2026")
    integrations_response = client.get("/api/integrations/status")
    backtest_response = client.get("/api/backtest")

    assert calendar_response.status_code == 200
    assert "2026-02-20" in calendar_response.json()["closedDates"]
    assert integrations_response.status_code == 200
    assert "notifications" in integrations_response.json()
    assert backtest_response.status_code == 200
    assert "metrics" in backtest_response.json()
