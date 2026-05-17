from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

import backend.main as main_module
from backend.main import app
from backend.models.settings import ScannerSettings
from backend.services.auth import AUTH_FAILURE_LIMIT, SESSION_CLEANUP_INTERVAL_SECONDS, AuthService, SUPER_USER_USERNAME
from backend.services.settings_service import load_settings, save_settings


client = TestClient(app)
MOCK_SETTINGS = ScannerSettings(use_mock_data=True)


@pytest.fixture(autouse=True)
def force_mock_provider_for_api_tests():
    original = load_settings()
    save_settings(ScannerSettings(**{**original.model_dump(), "use_mock_data": True}))
    try:
        yield
    finally:
        save_settings(original)


def test_health_endpoint():
    response = client.get("/api/health")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert response.headers["Strict-Transport-Security"].startswith("max-age=31536000")
    assert "script-src 'self'" in response.headers["Content-Security-Policy"]
    assert "style-src 'self';" in response.headers["Content-Security-Policy"]
    assert "unsafe-inline" not in response.headers["Content-Security-Policy"]
    assert response.headers["X-Frame-Options"] == "DENY"
    assert "geolocation=()" in response.headers["Permissions-Policy"]


def test_app_status_contract_shape():
    response = client.get("/api/app-status")
    payload = response.json()

    assert response.status_code == 200
    assert set(payload) == {
        "dataSourceStatus",
        "schedulerStatus",
        "schedulerAutoScan",
        "integrationStatus",
        "backtestStatus",
    }
    assert set(payload["schedulerAutoScan"]) == {"action", "autoScanEnabled", "manualScanEnabled", "scan"}
    assert set(payload["backtestStatus"]) >= {"status", "trades", "metrics"}
    assert set(payload["backtestStatus"]["metrics"]) == {"tradeCount", "winRate", "totalReturn", "maxDrawdown"}


def test_companies_endpoint_is_paginated():
    response = client.get("/api/companies?page=1&limit=2")
    payload = response.json()

    assert response.status_code == 200
    assert len(payload["items"]) == 2
    assert payload["page"] == 1
    assert payload["limit"] == 2
    assert payload["total"] >= 2
    assert payload["hasMore"] is True


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


def test_settings_put_requires_auth():
    original = client.get("/api/settings").json()
    response = client.put("/api/settings", json={**original, "manual_scan_enabled": False})
    assert response.status_code == 401


def test_settings_api_round_trip_as_super_user(monkeypatch):
    monkeypatch.setattr(main_module, "_require_super_user", lambda req: None)
    original = load_settings()
    try:
        put_response = client.put("/api/settings", json={**original.model_dump(), "manual_scan_enabled": False})
        get_response = client.get("/api/settings")
        assert put_response.status_code == 200
        assert get_response.json()["manual_scan_enabled"] is False
    finally:
        save_settings(original)


def test_settings_rejects_string_booleans(monkeypatch):
    monkeypatch.setattr(main_module, "_require_super_user", lambda req: None)
    original = load_settings()

    response = client.put("/api/settings", json={**original.model_dump(), "manual_scan_enabled": "false"})

    assert response.status_code == 422


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


def test_auth_accepts_common_email_symbols(tmp_path):
    service = AuthService(tmp_path / "auth.sqlite3")

    user = service.create_user(" Qa+audit%2026@example.com ", "test-password-123")

    assert user.username == "qa+audit%2026@example.com"
    assert service.authenticate("qa+audit%2026@example.com", "test-password-123") is not None
    with pytest.raises(ValueError):
        service.create_user("bad account@example.com", "test-password-123")


def test_failed_login_attempts_are_rate_limited(tmp_path, monkeypatch):
    auth_service = AuthService(tmp_path / "auth.sqlite3")
    monkeypatch.setattr(main_module, "auth_service", auth_service)
    username = f"rate_{uuid4().hex[:10]}@example.com"
    auth_service.create_user(username, "test-password-123")
    test_client = TestClient(app)
    headers = {"x-forwarded-for": "203.0.113.10"}

    for _ in range(AUTH_FAILURE_LIMIT):
        response = test_client.post(
            "/api/auth/login",
            headers=headers,
            json={"username": username, "password": "wrong-password"},
        )
        assert response.status_code == 401

    limited_response = test_client.post(
        "/api/auth/login",
        headers=headers,
        json={"username": username, "password": "test-password-123"},
    )
    assert limited_response.status_code == 429
    assert int(limited_response.headers["retry-after"]) > 0

    other_source_response = test_client.post(
        "/api/auth/login",
        headers={"x-forwarded-for": "203.0.113.11"},
        json={"username": username, "password": "test-password-123"},
    )
    assert other_source_response.status_code == 200


def test_session_cleanup_is_throttled_but_deterministic(tmp_path):
    service = AuthService(tmp_path / "auth.sqlite3")
    user = service.create_user(f"cleanup_{uuid4().hex[:10]}@example.com", "test-password-123")
    token = service.create_session(user.id)
    old_time = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()

    def insert_expired_session(token_value: str) -> None:
        with service._connect() as connection:
            connection.execute(
                """
                INSERT INTO sessions (user_id, token_hash, created_at, expires_at)
                VALUES (?, ?, ?, ?)
                """,
                (user.id, service._token_hash(token_value), old_time, old_time),
            )

    def expired_session_exists(token_value: str) -> bool:
        with service._connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM sessions WHERE token_hash = ?",
                (service._token_hash(token_value),),
            ).fetchone()
        return row is not None

    insert_expired_session("expired-a")
    assert service.get_user_by_session(token) == user
    assert not expired_session_exists("expired-a")

    insert_expired_session("expired-b")
    assert service.get_user_by_session(token) == user
    assert expired_session_exists("expired-b")

    service._last_session_cleanup_at = datetime.now(timezone.utc) - timedelta(seconds=SESSION_CLEANUP_INTERVAL_SECONDS + 1)
    assert service.get_user_by_session(token) == user
    assert not expired_session_exists("expired-b")


def test_market_scan_is_independent_from_holding_add_and_delete():
    test_client = TestClient(app)
    username = f"user_{uuid4().hex[:10]}"
    password = "test-password-123"
    settings = MOCK_SETTINGS.model_dump()

    before = test_client.post("/api/scan/market", json={"settings": settings}).json()
    before_entry_codes = [item["stockCode"] for item in before["entry"]]
    assert "2357" in before_entry_codes

    register_response = test_client.post("/api/auth/register", json={"username": username, "password": password})
    assert register_response.status_code == 200

    replace_response = test_client.put(
        "/api/me/holdings",
        json={"holdings": [{"stockCode": "2357", "name": "華碩", "shares": 0, "averageCost": None}]},
    )
    assert replace_response.status_code == 200

    delete_response = test_client.delete("/api/me/holdings/2357")
    assert delete_response.status_code == 200
    assert delete_response.json()["holdings"] == []

    after = test_client.post("/api/scan/market", json={"settings": settings}).json()
    after_entry_codes = [item["stockCode"] for item in after["entry"]]
    assert after_entry_codes == before_entry_codes


def test_super_user_can_list_and_delete_users(tmp_path, monkeypatch):
    monkeypatch.setattr(main_module, "auth_service", AuthService(tmp_path / "auth.sqlite3"))
    admin_client = TestClient(app)
    user_client = TestClient(app)
    normal_username = f"user_{uuid4().hex[:10]}@example.com"
    password = "test-password-123"

    user_response = user_client.post("/api/auth/register", json={"username": normal_username, "password": password})
    assert user_response.status_code == 200
    assert user_response.json()["user"]["isSuperUser"] is False
    user_client.put(
        "/api/me/holdings",
        json={"holdings": [{"stockCode": "2330", "name": "台積電", "shares": 1000, "averageCost": 600}]},
    )
    assert user_client.get("/api/admin/users").status_code == 403

    admin_response = admin_client.post(
        "/api/auth/register",
        json={"username": " PCEDISON@GMAIL.COM ", "password": password},
    )
    assert admin_response.status_code == 200
    assert admin_response.json()["user"]["username"] == "pcedison@gmail.com"
    assert admin_response.json()["user"]["isSuperUser"] is True

    me_response = admin_client.get("/api/auth/me")
    assert me_response.status_code == 200
    assert me_response.json()["authenticated"] is True
    assert me_response.json()["user"]["username"] == "pcedison@gmail.com"
    assert me_response.json()["user"]["isSuperUser"] is True

    users_response = admin_client.get("/api/admin/users")
    assert users_response.status_code == 200
    users = users_response.json()["users"]
    normal_user = next(user for user in users if user["username"] == normal_username)
    super_user = next(user for user in users if user["username"] == "pcedison@gmail.com")
    assert normal_user["holdingsCount"] == 1
    assert normal_user["canDelete"] is True
    assert super_user["isSuperUser"] is True
    assert super_user["canDelete"] is False

    delete_super_response = admin_client.delete(f"/api/admin/users/{super_user['id']}")
    assert delete_super_response.status_code == 400

    delete_response = admin_client.delete(f"/api/admin/users/{normal_user['id']}")
    assert delete_response.status_code == 200
    remaining_usernames = {user["username"] for user in delete_response.json()["users"]}
    assert normal_username not in remaining_usernames
    assert user_client.get("/api/me/holdings").status_code == 401


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
