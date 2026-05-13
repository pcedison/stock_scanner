from fastapi.testclient import TestClient
import pytest

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


def test_data_source_status_is_explicitly_mock():
    response = client.get("/api/data-sources/status")
    payload = response.json()

    assert response.status_code == 200
    assert payload["activeProvider"] == "MockDataProvider"
    assert payload["activeProviderIsRealtime"] is False
    assert payload["activeProviderIsFullMarket"] is False


def test_scheduler_wakeup_endpoint():
    response = client.get("/api/scheduler/wakeup?today=2026-02-13")
    payload = response.json()

    assert response.status_code == 200
    assert payload["status"] == "WAKE"
    assert "SPRING_FESTIVAL_GUARD" in payload["events"]
