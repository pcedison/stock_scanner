import re
from datetime import UTC, datetime, timedelta
from threading import Event, Lock, Thread
from time import monotonic, sleep
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

import backend.dependencies as deps_module
import backend.main as main_module
import backend.routers.market as market_module
from backend.models.settings import ScannerSettings
from backend.services.auth import AUTH_FAILURE_LIMIT, SESSION_CLEANUP_INTERVAL_SECONDS, AuthService
from backend.services.scan_cache import ScanCacheService
from backend.services.settings_service import load_settings, save_settings

client = TestClient(main_module.app)
MOCK_SETTINGS = ScannerSettings(use_mock_data=True)


@pytest.fixture(autouse=True)
def force_mock_provider_for_api_tests(tmp_path, monkeypatch):
    # Redirect settings I/O to a temp directory outside OneDrive to prevent
    # Path.replace() from racing with OneDrive sync locks (WinError 5).
    test_settings = tmp_path / "settings.json"
    monkeypatch.setenv("SETTINGS_PATH", str(test_settings))
    save_settings(ScannerSettings(use_mock_data=True))
    yield


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


def test_cors_allowlist_can_be_configured(monkeypatch):
    monkeypatch.setenv("APP_CORS_ALLOW_ORIGINS", "https://app.example, http://127.0.0.1:8000")

    assert main_module._cors_allowed_origins() == ["https://app.example", "http://127.0.0.1:8000"]


def test_production_runtime_security_requires_secure_cookie(monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.delenv("SESSION_COOKIE_SECURE", raising=False)

    with pytest.raises(RuntimeError, match="SESSION_COOKIE_SECURE"):
        main_module._validate_runtime_security(["https://stock-scanner-beta.pages.dev"])


def test_production_runtime_security_rejects_local_cors(monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("SESSION_COOKIE_SECURE", "1")
    monkeypatch.delenv("APP_ALLOW_LOCAL_CORS_IN_PRODUCTION", raising=False)

    with pytest.raises(RuntimeError, match="localhost"):
        main_module._validate_runtime_security(["http://localhost:5173", "https://stock-scanner-beta.pages.dev"])


def test_production_runtime_security_allows_explicit_https_origin(monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("SESSION_COOKIE_SECURE", "1")
    monkeypatch.setenv("SUPER_USER_USERNAME", "admin@example.com")

    main_module._validate_runtime_security(["https://stock-scanner-beta.pages.dev"])


def test_production_runtime_security_requires_super_user(monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("SESSION_COOKIE_SECURE", "1")
    monkeypatch.delenv("SUPER_USER_USERNAME", raising=False)

    with pytest.raises(RuntimeError, match="SUPER_USER_USERNAME"):
        main_module._validate_runtime_security(["https://stock-scanner-beta.pages.dev"])


def test_production_runtime_security_rejects_non_https_origin(monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("SESSION_COOKIE_SECURE", "1")

    with pytest.raises(RuntimeError, match="https origins"):
        main_module._validate_runtime_security(["http://stock-scanner-beta.pages.dev"])


def test_production_csrf_guard_requires_custom_header(monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    test_client = TestClient(main_module.app)

    payload = {"holdings": [], "settings": MOCK_SETTINGS.model_dump()}
    missing = test_client.post("/api/scan/holdings", json=payload)
    present = test_client.post(
        "/api/scan/holdings",
        headers={"x-stock-scanner-csrf": "1"},
        json=payload,
    )

    assert missing.status_code == 403
    assert missing.json()["detail"] == "CSRF header required"
    assert present.status_code == 200


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


def test_backtest_status_cache_invalidates_when_source_file_changes(tmp_path, monkeypatch):
    source = tmp_path / "backtest_history.csv"
    calls = {"count": 0}

    def fake_run_backtest():
        calls["count"] += 1
        return {"status": "OK", "trades": [], "metrics": {"tradeCount": calls["count"]}}

    monkeypatch.setattr(deps_module, "DEFAULT_BACKTEST_PATH", source)
    monkeypatch.setattr(deps_module, "run_backtest", fake_run_backtest)
    deps_module._backtest_cache.clear()

    first = deps_module._backtest_status_cached()
    second = deps_module._backtest_status_cached()
    source.write_text("changed\n", encoding="utf-8")
    third = deps_module._backtest_status_cached()

    assert first["metrics"]["tradeCount"] == 1
    assert second["metrics"]["tradeCount"] == 1
    assert third["metrics"]["tradeCount"] == 2


def test_backtest_status_cache_single_flights_concurrent_misses(tmp_path, monkeypatch):
    source = tmp_path / "backtest_history.csv"
    source.write_text("stock_code,period\n", encoding="utf-8")
    calls = {"count": 0}
    calls_lock = Lock()
    started = Event()
    release = Event()
    results = []
    errors = []

    def fake_run_backtest():
        with calls_lock:
            calls["count"] += 1
            trade_count = calls["count"]
        started.set()
        assert release.wait(timeout=5)
        return {"status": "OK", "trades": [], "metrics": {"tradeCount": trade_count}}

    def call_cached():
        try:
            results.append(deps_module._backtest_status_cached())
        except Exception as exc:  # pragma: no cover - assertion context
            errors.append(exc)

    monkeypatch.setattr(deps_module, "DEFAULT_BACKTEST_PATH", source)
    monkeypatch.setattr(deps_module, "run_backtest", fake_run_backtest)
    deps_module._backtest_cache.clear()
    deps_module._backtest_refresh_events.clear()

    first = Thread(target=call_cached)
    second = Thread(target=call_cached)
    first.start()
    assert started.wait(timeout=5)
    second.start()
    release.set()
    first.join(timeout=5)
    second.join(timeout=5)

    assert not first.is_alive()
    assert not second.is_alive()
    assert errors == []
    assert calls["count"] == 1
    assert [result["metrics"]["tradeCount"] for result in results] == [1, 1]


def test_scan_rate_limit_prunes_stale_sources_and_caps_store(monkeypatch):
    now = 1_000.0
    monkeypatch.setattr(deps_module, "monotonic", lambda: now)
    monkeypatch.setattr(deps_module, "_SCAN_RATE_MAX_REQUESTS", 100)
    monkeypatch.setattr(deps_module, "_SCAN_RATE_MAX_SOURCES", 3)
    deps_module._scan_rate_store.clear()
    deps_module._scan_rate_store.update(
        {
            "stale": [now - deps_module._SCAN_RATE_WINDOW_SECONDS - 1],
            "older": [now - 20],
            "old": [now - 10],
            "fresh": [now - 1],
        }
    )

    deps_module._check_scan_rate_limit("new")

    assert "stale" not in deps_module._scan_rate_store
    assert "older" not in deps_module._scan_rate_store
    assert "new" in deps_module._scan_rate_store
    assert len(deps_module._scan_rate_store) <= 3


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


def _all_market_v2_items(api_client, index: dict, category: str) -> list[dict]:
    items: list[dict] = []
    for disclosure in ("announced", "pending"):
        total = index["disclosures"][disclosure][category]["count"]
        for cursor in range(0, total, 100):
            page = api_client.get(
                "/api/scan/market/results",
                params={
                    "disclosure": disclosure,
                    "category": category,
                    "cursor": cursor,
                    "limit": 100,
                    "generationId": index["generationId"],
                },
            )
            assert page.status_code == 200
            items.extend(page.json()["items"])
    return items


def test_market_v2_pages_expose_entry_watch_and_excluded_results():
    index = client.get("/api/scan/market/index").json()
    grouped = {category: _all_market_v2_items(client, index, category) for category in ("entry", "watch", "excluded")}

    assert "filingContext" in index
    assert "monthlyRevenuePeriod" in index["filingContext"]
    assert any(item["stockCode"] == "2357" for item in grouped["entry"])
    assert any(item["stockCode"] == "2881" for item in grouped["excluded"])
    assert all(item["reasons"] for items in grouped.values() for item in items)
    for category, items in grouped.items():
        assert len(items) == index["counts"]["categories"][category]


def test_market_v2_index_results_and_generation_mismatch_contract():
    index_response = client.get("/api/scan/market/index")
    index = index_response.json()

    assert index_response.status_code == 200
    assert index_response.headers["cache-control"] == "no-store"
    assert index["schemaVersion"] == 2
    assert len(index["generationId"]) == 24
    assert "cacheStatus" in index
    disclosure, category = next(
        (disclosure, category)
        for disclosure in ("announced", "pending")
        for category in ("entry", "watch", "excluded")
        if index["disclosures"][disclosure][category]["count"]
    )

    results_response = client.get(
        "/api/scan/market/results",
        params={
            "disclosure": disclosure,
            "category": category,
            "cursor": 0,
            "limit": 1,
            "generationId": index["generationId"],
        },
    )
    results = results_response.json()

    assert results_response.status_code == 200
    assert results_response.headers["cache-control"] == "no-store"
    assert set(results) == {
        "schemaVersion",
        "generationId",
        "disclosure",
        "category",
        "cursor",
        "limit",
        "total",
        "nextCursor",
        "items",
    }
    assert results["generationId"] == index["generationId"]
    assert len(results["items"]) == 1

    mismatch = client.get(
        "/api/scan/market/results",
        params={
            "disclosure": disclosure,
            "category": category,
            "cursor": 0,
            "limit": 1,
            "generationId": "f" * 24 if index["generationId"] != "f" * 24 else "e" * 24,
        },
    )
    assert mismatch.status_code == 409
    assert mismatch.headers["cache-control"] == "no-store"
    assert mismatch.json()["detail"] == "generation_mismatch"


@pytest.mark.parametrize(
    "params",
    [
        {"disclosure": "other", "category": "watch", "cursor": "0", "limit": "100"},
        {"disclosure": "announced", "category": "other", "cursor": "0", "limit": "100"},
        {"disclosure": "announced", "category": "watch", "cursor": "-1", "limit": "100"},
        {"disclosure": "announced", "category": "watch", "cursor": "true", "limit": "100"},
        {"disclosure": "announced", "category": "watch", "cursor": "2000001", "limit": "100"},
        {"disclosure": "announced", "category": "watch", "cursor": "9" * 5000, "limit": "100"},
        {"disclosure": "announced", "category": "watch", "cursor": "0" * 5000, "limit": "100"},
        {"disclosure": "announced", "category": "watch", "cursor": "0", "limit": "0" * 5000},
        {"disclosure": "announced", "category": "watch", "cursor": "0", "limit": "101"},
        {
            "disclosure": "announced",
            "category": "watch",
            "cursor": "0",
            "limit": "100",
            "generationId": "A" * 24,
        },
        {},
    ],
)
def test_market_v2_results_rejects_invalid_query_values(params):
    response = client.get("/api/scan/market/results", params=params)
    assert response.status_code == 422
    assert response.headers["cache-control"] == "no-store"


def test_market_v2_results_rejects_duplicate_query_values():
    response = client.get(
        "/api/scan/market/results",
        params=[
            ("disclosure", "announced"),
            ("category", "watch"),
            ("cursor", "0"),
            ("limit", "1"),
            ("limit", "2"),
        ],
    )
    assert response.status_code == 422
    assert response.headers["cache-control"] == "no-store"


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
    monkeypatch.setattr(deps_module, "_require_super_user", lambda req: None)
    original = load_settings()
    try:
        put_response = client.put("/api/settings", json={**original.model_dump(), "manual_scan_enabled": False})
        get_response = client.get("/api/settings")
        assert put_response.status_code == 200
        assert get_response.json()["manual_scan_enabled"] is False
    finally:
        save_settings(original)


def test_settings_rejects_string_booleans(monkeypatch):
    monkeypatch.setattr(deps_module, "_require_super_user", lambda req: None)
    original = load_settings()

    response = client.put("/api/settings", json={**original.model_dump(), "manual_scan_enabled": "false"})

    assert response.status_code == 422


def test_lightweight_auth_persists_server_side_holdings():
    test_client = TestClient(main_module.app)
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
    service.close()


def test_auth_service_close_reopens_thread_local_connection(tmp_path):
    service = AuthService(tmp_path / "auth.sqlite3")
    user = service.create_user("close-test@example.com", "test-password-123")

    first_connection = service._connect()
    service.close()
    reopened_connection = service._connect()

    assert reopened_connection is not first_connection
    assert service.authenticate(user.username, "test-password-123") is not None
    service.close()


def test_failed_login_attempts_are_rate_limited(tmp_path, monkeypatch):
    auth_service = AuthService(tmp_path / "auth.sqlite3")
    monkeypatch.setattr(deps_module, "auth_service", auth_service)
    username = f"rate_{uuid4().hex[:10]}@example.com"
    auth_service.create_user(username, "test-password-123")
    test_client = TestClient(main_module.app)
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
    old_time = (datetime.now(UTC) - timedelta(days=1)).isoformat()

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

    service._last_session_cleanup_at = datetime.now(UTC) - timedelta(seconds=SESSION_CLEANUP_INTERVAL_SECONDS + 1)
    assert service.get_user_by_session(token) == user
    assert not expired_session_exists("expired-b")


def test_market_scan_is_independent_from_holding_add_and_delete():
    test_client = TestClient(main_module.app)
    username = f"user_{uuid4().hex[:10]}"
    password = "test-password-123"

    def entry_codes() -> list[str]:
        index = test_client.get("/api/scan/market/index").json()
        return [item["stockCode"] for item in _all_market_v2_items(test_client, index, "entry")]

    before_entry_codes = entry_codes()
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

    assert entry_codes() == before_entry_codes


def test_super_user_can_list_and_delete_users(tmp_path, monkeypatch):
    admin_username = "admin@example.com"
    monkeypatch.setenv("SUPER_USER_USERNAME", admin_username)
    auth_service = AuthService(tmp_path / "auth.sqlite3")
    monkeypatch.setattr(deps_module, "auth_service", auth_service)
    admin_client = TestClient(main_module.app)
    user_client = TestClient(main_module.app)
    normal_username = f"user_{uuid4().hex[:10]}@example.com"
    password = "test-password-123"

    user_response = user_client.post("/api/auth/register", json={"username": normal_username, "password": password})
    assert user_response.status_code == 200
    assert user_response.json()["user"]["isSuperUser"] is False
    normal_session_token = user_response.cookies.get(deps_module.SESSION_COOKIE_NAME)
    user_client.put(
        "/api/me/holdings",
        json={"holdings": [{"stockCode": "2330", "name": "台積電", "shares": 1000, "averageCost": 600}]},
    )
    assert user_client.get("/api/admin/users").status_code == 403

    admin_response = admin_client.post(
        "/api/auth/register",
        json={"username": f" {admin_username.upper()} ", "password": password},
    )
    assert admin_response.status_code == 200
    assert admin_response.json()["user"]["username"] == admin_username
    assert admin_response.json()["user"]["isSuperUser"] is True

    me_response = admin_client.get("/api/auth/me")
    assert me_response.status_code == 200
    assert me_response.json()["authenticated"] is True
    assert me_response.json()["user"]["username"] == admin_username
    assert me_response.json()["user"]["isSuperUser"] is True

    users_response = admin_client.get("/api/admin/users")
    assert users_response.status_code == 200
    users = users_response.json()["users"]
    normal_user = next(user for user in users if user["username"] == normal_username)
    super_user = next(user for user in users if user["username"] == admin_username)
    assert normal_user["holdingsCount"] == 1
    assert normal_user["activeSessionCount"] == 1
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
    assert auth_service.get_user_by_session(normal_session_token) is None
    with auth_service._connect() as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM sessions WHERE user_id = ?", (normal_user["id"],)).fetchone()[0]
            == 0
        )
        assert (
            connection.execute("SELECT COUNT(*) FROM holdings WHERE user_id = ?", (normal_user["id"],)).fetchone()[0]
            == 0
        )


def test_manual_scan_disabled_blocks_manual_scan_api():
    response = client.post(
        "/api/scan/holdings",
        json={
            "holdings": [],
            "settings": ScannerSettings(use_mock_data=True, manual_scan_enabled=False).model_dump(),
        },
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


def test_scheduler_monthly_revenue_window_ends_at_the_filing_deadline():
    # Revenue is due by the 10th (7/10/2026 is a Friday); nothing new is filed afterwards.
    assert "MONTHLY_REVENUE_WINDOW" in client.get("/api/scheduler/wakeup?today=2026-07-01").json()["events"]
    assert "MONTHLY_REVENUE_WINDOW" in client.get("/api/scheduler/wakeup?today=2026-07-10").json()["events"]
    assert "MONTHLY_REVENUE_WINDOW" not in client.get("/api/scheduler/wakeup?today=2026-07-13").json()["events"]


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


def _wait_for_terminal_refresh_job(status_url: str, timeout: float = 30.0) -> dict:
    deadline = monotonic() + timeout
    while True:
        response = client.get(status_url)
        assert response.status_code == 200
        payload = response.json()
        if payload["status"] in {"success", "failed"}:
            return payload
        assert monotonic() < deadline, f"refresh job never reached a terminal state: {payload}"
        sleep(0.05)


def test_market_refresh_command_queues_a_job_that_reaches_a_terminal_state():
    response = client.post("/api/scan/market/refresh", headers={"Idempotency-Key": f"api-{uuid4().hex}"})
    payload = response.json()

    assert response.status_code == 202
    assert set(payload) == {"jobId", "status", "requestId", "statusUrl"}
    assert re.fullmatch(r"[0-9a-f]{32}", payload["jobId"])
    assert re.fullmatch(r"[A-Za-z0-9._:-]{1,80}", payload["requestId"])
    assert payload["status"] == "queued"
    assert payload["statusUrl"] == f"/api/scan/market/refresh/{payload['jobId']}"
    assert response.headers["location"] == payload["statusUrl"]
    assert response.headers["cache-control"] == "no-store"

    terminal = _wait_for_terminal_refresh_job(payload["statusUrl"])

    assert terminal["status"] == "success"
    assert terminal["jobId"] == payload["jobId"]
    assert terminal["hasError"] is False
    assert terminal["reason"] in {"financial_report_window", "monthly_revenue_window", "routine_refresh"}

    index_response = client.get("/api/scan/market/index")

    assert index_response.status_code == 200
    assert len(index_response.json()["generationId"]) == 24


def test_market_refresh_command_is_idempotent_and_unknown_jobs_are_not_found():
    key = f"api-{uuid4().hex}"

    first = client.post("/api/scan/market/refresh", headers={"Idempotency-Key": key})
    # Repeat only once the first job is terminal, so the match is the key and not the
    # single-flight branch that coalesces any still-active job.
    _wait_for_terminal_refresh_job(first.json()["statusUrl"])
    repeat = client.post("/api/scan/market/refresh", headers={"Idempotency-Key": key})
    fresh_key = client.post("/api/scan/market/refresh", headers={"Idempotency-Key": f"api-{uuid4().hex}"})

    assert first.status_code == repeat.status_code == fresh_key.status_code == 202
    assert repeat.json()["jobId"] == first.json()["jobId"]
    assert fresh_key.json()["jobId"] != first.json()["jobId"]
    _wait_for_terminal_refresh_job(fresh_key.json()["statusUrl"])

    unknown = client.get(f"/api/scan/market/refresh/{'f' * 32}")
    malformed = client.get("/api/scan/market/refresh/not-a-job-id")

    assert unknown.status_code == malformed.status_code == 404
    assert unknown.json()["detail"] == "Not found"


def _stub_market_scan(company_name: str) -> dict:
    return {
        "generatedAt": "2026-07-13T00:00:00+00:00",
        "filingContext": {},
        "entry": [
            {
                "stockCode": "2330",
                "companyName": company_name,
                "status": "ENTRY",
                "summary": "announced",
                "reasons": [],
            }
        ],
        "watch": [],
        "excluded": [],
        "universeSize": 1,
    }


def test_forced_refresh_job_caches_under_the_key_of_the_data_it_refreshed(tmp_path, monkeypatch):
    # The official refresh rewrites the files the cache context fingerprints, so a job that
    # keyed its store before the rebuild would leave the next index read with a cache miss.
    cache_service = ScanCacheService(tmp_path / "market_scan_cache.json", tmp_path / "cache_refresh_state.json")
    monkeypatch.setattr(market_module, "scan_cache_service", cache_service)
    monkeypatch.setattr(deps_module, "load_settings", lambda: ScannerSettings(use_mock_data=False))
    data_version = {"value": 1}
    official_refreshes: list[int] = []
    plain_builds: list[int] = []

    def fake_cache_context(settings):
        return {"provider": "official", "dataVersion": data_version["value"]}

    def fake_refresh_and_scan(settings):
        data_version["value"] += 1
        official_refreshes.append(data_version["value"])
        return _stub_market_scan("refreshed")

    def fake_scan(settings):
        plain_builds.append(data_version["value"])
        return _stub_market_scan("not-refreshed")

    monkeypatch.setattr(market_module, "_scan_market_cache_context", fake_cache_context)
    monkeypatch.setattr(market_module, "_scan_market_payload_after_official_refresh", fake_refresh_and_scan)
    monkeypatch.setattr(market_module, "_scan_market_payload", fake_scan)

    command = client.post("/api/scan/market/refresh", headers={"Idempotency-Key": f"api-{uuid4().hex}"})

    assert command.status_code == 202
    assert _wait_for_terminal_refresh_job(command.json()["statusUrl"])["status"] == "success"
    assert official_refreshes == [2]

    index_response = client.get("/api/scan/market/index")
    index = index_response.json()
    results = client.get(
        "/api/scan/market/results",
        params={
            "disclosure": "announced",
            "category": "entry",
            "cursor": 0,
            "limit": 1,
            "generationId": index["generationId"],
        },
    )

    assert index_response.status_code == results.status_code == 200
    # The job's payload is served straight from the cache: nothing rebuilt on the read path.
    assert plain_builds == []
    assert official_refreshes == [2]
    assert index["cacheStatus"]["cacheHit"] is True
    assert results.json()["items"][0]["companyName"] == "refreshed"


def test_market_refresh_command_rejects_an_unsafe_idempotency_key():
    response = client.post("/api/scan/market/refresh", headers={"Idempotency-Key": "bad key"})

    assert response.status_code == 422
    assert "Idempotency-Key" in response.json()["detail"]


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
