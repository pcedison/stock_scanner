from __future__ import annotations

import asyncio
import inspect
import json
import types
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import pytest
from fastapi.testclient import TestClient

import backend.main as main_module
from backend.models.holding import Holding
from backend.models.settings import ScannerSettings
from backend.services.auth import AUTH_FAILURE_LIMIT, AuthService, AuthUser
from tests.test_cloudflare_worker import load_worker_module


FIXTURES = json.loads((Path(__file__).parent / "fixtures" / "api_worker_contracts.json").read_text(encoding="utf-8"))


class ContractRequest:
    def __init__(self, method: str, path: str, payload: dict | None = None, headers: dict | None = None):
        self.method = method
        self.url = f"https://stock-scanner-beta-api.example{path}"
        self.headers = headers or {}
        self._text = "" if payload is None else json.dumps(payload)

    async def text(self):
        return self._text


def worker_payload(response):
    return json.loads(response.body)


def worker_status(response) -> int:
    return int(response.init.get("status", 200))


@dataclass(frozen=True)
class EndpointContractCase:
    name: str
    run: Callable

    def __str__(self) -> str:
        return self.name


def fastapi_json(response):
    return response.json()


def assert_status_and_keys_match(fastapi_response, worker_response, required_keys: set[str] | None = None):
    assert fastapi_response.status_code == worker_status(worker_response) == 200
    fastapi_payload = fastapi_json(fastapi_response)
    worker_response_payload = worker_payload(worker_response)
    if required_keys is None:
        assert set(fastapi_payload) == set(worker_response_payload)
    else:
        assert set(fastapi_payload) >= required_keys
        assert set(worker_response_payload) >= required_keys
    return fastapi_payload, worker_response_payload


def make_worker_api(worker):
    api = worker.Api(env=None)

    async def fake_r2_json(key, fallback):
        if key == "public/data_sources_status.json":
            return {
                "activeProvider": "CloudflareR2Seed",
                "mockDataAvailable": True,
                "officialDataAvailable": False,
            }
        return fallback

    async def fake_get_settings():
        return ScannerSettings(use_mock_data=True).model_dump()

    api.r2_json = fake_r2_json
    api.get_settings = fake_get_settings
    return api


def run_worker_fetch(api, method: str, path: str, payload: dict | None = None, headers: dict | None = None):
    return asyncio.run(api.fetch(ContractRequest(method, path, payload, headers)))


def contract_app_status(monkeypatch, worker):
    monkeypatch.setattr(main_module, "load_settings", lambda: ScannerSettings(use_mock_data=True))
    client = TestClient(main_module.app)
    api = make_worker_api(worker)

    fastapi_response = client.get("/api/app-status")
    worker_response = run_worker_fetch(api, "GET", "/api/app-status")
    fastapi_payload, worker_response_payload = assert_status_and_keys_match(
        fastapi_response,
        worker_response,
        set(FIXTURES["appStatusKeys"]),
    )

    assert list(fastapi_payload) == FIXTURES["appStatusKeys"]
    assert list(worker_response_payload) == FIXTURES["appStatusKeys"]
    assert list(fastapi_payload["schedulerAutoScan"]) == FIXTURES["schedulerAutoScanKeys"]
    assert list(worker_response_payload["schedulerAutoScan"]) == FIXTURES["schedulerAutoScanKeys"]
    assert set(fastapi_payload["backtestStatus"]["metrics"]) == set(worker_response_payload["backtestStatus"]["metrics"])


def contract_data_sources_status(monkeypatch, worker):
    monkeypatch.setattr(main_module, "load_settings", lambda: ScannerSettings(use_mock_data=True))
    client = TestClient(main_module.app)
    api = make_worker_api(worker)

    fastapi_response = client.get("/api/data-sources/status")
    worker_response = run_worker_fetch(api, "GET", "/api/data-sources/status")
    fastapi_payload, worker_response_payload = assert_status_and_keys_match(
        fastapi_response,
        worker_response,
        {"activeProvider"},
    )
    assert isinstance(fastapi_payload["activeProvider"], str)
    assert isinstance(worker_response_payload["activeProvider"], str)


def contract_scheduler_wakeup(monkeypatch, worker):
    client = TestClient(main_module.app)
    api = make_worker_api(worker)

    fastapi_response = client.get("/api/scheduler/wakeup?today=2026-02-13")
    worker_response = run_worker_fetch(api, "GET", "/api/scheduler/wakeup?today=2026-02-13")
    assert fastapi_response.status_code == worker_status(worker_response) == 200
    fastapi_payload = fastapi_response.json()
    worker_response_payload = worker_payload(worker_response)
    assert isinstance(fastapi_payload["status"], str)
    assert isinstance(worker_response_payload["status"], str)


def contract_scheduler_auto_scan(monkeypatch, worker):
    monkeypatch.setattr(main_module, "load_settings", lambda: ScannerSettings(use_mock_data=True))
    client = TestClient(main_module.app)
    api = make_worker_api(worker)

    fastapi_response = client.get("/api/scheduler/auto-scan?today=2026-02-13&execute=false")
    worker_response = run_worker_fetch(api, "GET", "/api/scheduler/auto-scan?today=2026-02-13&execute=false")
    fastapi_payload, worker_response_payload = assert_status_and_keys_match(
        fastapi_response,
        worker_response,
        {"action", "autoScanEnabled", "manualScanEnabled", "scan"},
    )
    assert isinstance(fastapi_payload["autoScanEnabled"], bool)
    assert isinstance(worker_response_payload["autoScanEnabled"], bool)


def contract_backtest(monkeypatch, worker):
    client = TestClient(main_module.app)
    api = make_worker_api(worker)

    fastapi_response = client.get("/api/backtest")
    worker_response = run_worker_fetch(api, "GET", "/api/backtest")
    fastapi_payload, worker_response_payload = assert_status_and_keys_match(
        fastapi_response,
        worker_response,
        {"status", "trades", "metrics", "note"},
    )
    assert set(fastapi_payload["metrics"]) == set(worker_response_payload["metrics"])


def contract_auth_required(monkeypatch, worker):
    client = TestClient(main_module.app)
    api = make_worker_api(worker)

    fastapi_response = client.get("/api/me/holdings")
    worker_response = run_worker_fetch(api, "GET", "/api/me/holdings")

    assert fastapi_response.status_code == worker_status(worker_response) == 401
    assert "detail" in fastapi_response.json()
    assert "detail" in worker_payload(worker_response)


def contract_settings_validation(monkeypatch, worker):
    monkeypatch.setattr(main_module, "_require_super_user", lambda request: None)
    client = TestClient(main_module.app)
    invalid_settings = {**ScannerSettings().model_dump(), "manual_scan_enabled": "false"}
    api = make_worker_api(worker)

    async def fake_require_super_user(request):
        return {"id": 1, "username": "contract-settings@example.com", "display_name": None}

    api.require_super_user = fake_require_super_user

    fastapi_response = client.put("/api/settings", json=invalid_settings)
    worker_response = run_worker_fetch(api, "PUT", "/api/settings", invalid_settings)

    assert fastapi_response.status_code == worker_status(worker_response) == 422
    assert "detail" in fastapi_response.json()
    assert "detail" in worker_payload(worker_response)


def contract_reports(monkeypatch, worker):
    monkeypatch.setattr(main_module, "load_settings", lambda: ScannerSettings(use_mock_data=True))
    client = TestClient(main_module.app)
    api = make_worker_api(worker)

    async def fake_r2_json(key, fallback):
        return {"generatedAt": "2026-05-18T00:00:00+00:00", "dataSource": "contract", "entry": [], "watch": [], "excluded": []}

    api.r2_json = fake_r2_json

    fastapi_response = client.post(
        "/api/reports/market?report_format=markdown",
        json={"settings": ScannerSettings(use_mock_data=True).model_dump()},
    )
    worker_response = run_worker_fetch(api, "POST", "/api/reports/market?report_format=markdown", {})

    assert fastapi_response.status_code == worker_status(worker_response) == 200
    assert fastapi_response.headers["content-type"].startswith("text/markdown")
    assert worker_response.headers["content-type"].startswith("text/markdown")
    assert fastapi_response.text.startswith("# ")
    assert worker_response.body.startswith("# ")


def contract_admin_delete(monkeypatch, worker, tmp_path):
    admin_username = "contract-admin@example.com"
    monkeypatch.setenv("SUPER_USER_USERNAME", admin_username)
    auth_service = AuthService(tmp_path / "auth.sqlite3")
    super_user = auth_service.create_user(admin_username, "test-password-123")
    monkeypatch.setattr(main_module, "auth_service", auth_service)
    monkeypatch.setattr(main_module, "_require_super_user", lambda request: None)
    client = TestClient(main_module.app)
    api = make_worker_api(worker)
    api._super_user = admin_username

    async def fake_require_super_user(request):
        return {"id": super_user.id, "username": admin_username, "display_name": None}

    async def fake_db_first(sql, *params):
        return {"id": super_user.id, "username": admin_username}

    api.require_super_user = fake_require_super_user
    api.db_first = fake_db_first

    fastapi_response = client.delete(f"/api/admin/users/{super_user.id}")
    worker_response = run_worker_fetch(api, "DELETE", f"/api/admin/users/{super_user.id}")

    assert fastapi_response.status_code == worker_status(worker_response) == 400
    assert "detail" in fastapi_response.json()
    assert "detail" in worker_payload(worker_response)


def contract_holdings(monkeypatch, worker, tmp_path):
    auth_service = AuthService(tmp_path / "auth.sqlite3")
    monkeypatch.setattr(main_module, "auth_service", auth_service)
    client = TestClient(main_module.app)
    username = "contract-holdings@example.com"
    client.post("/api/auth/register", json={"username": username, "password": "test-password-123"})
    holdings = [{"stockCode": "2330", "name": "TSMC", "shares": 1000, "averageCost": None}]
    api = make_worker_api(worker)
    stored_holdings = []

    async def fake_require_user(request):
        return {"id": 1, "username": username, "display_name": None}

    async def fake_replace_holdings(user_id, payload):
        stored_holdings.clear()
        stored_holdings.extend(payload)
        return list(stored_holdings)

    async def fake_list_holdings(user_id):
        return list(stored_holdings)

    api.require_user = fake_require_user
    api.replace_holdings = fake_replace_holdings
    api.list_holdings = fake_list_holdings

    fastapi_put = client.put("/api/me/holdings", json={"holdings": holdings})
    fastapi_get = client.get("/api/me/holdings")
    worker_put = run_worker_fetch(api, "PUT", "/api/me/holdings", {"holdings": holdings})
    worker_get = run_worker_fetch(api, "GET", "/api/me/holdings")

    assert fastapi_put.status_code == worker_status(worker_put) == 200
    assert fastapi_get.status_code == worker_status(worker_get) == 200
    assert fastapi_get.json()["holdings"] == worker_payload(worker_get)["holdings"] == holdings


ENDPOINT_CONTRACT_MATRIX = [
    EndpointContractCase("app-status", contract_app_status),
    EndpointContractCase("data-sources/status", contract_data_sources_status),
    EndpointContractCase("scheduler/wakeup", contract_scheduler_wakeup),
    EndpointContractCase("scheduler/auto-scan", contract_scheduler_auto_scan),
    EndpointContractCase("backtest", contract_backtest),
    EndpointContractCase("auth required", contract_auth_required),
    EndpointContractCase("settings validation", contract_settings_validation),
    EndpointContractCase("reports", contract_reports),
    EndpointContractCase("admin delete", contract_admin_delete),
    EndpointContractCase("holdings", contract_holdings),
]


@pytest.mark.parametrize("case", ENDPOINT_CONTRACT_MATRIX, ids=str)
def test_endpoint_contract_matrix_matches_fastapi_and_worker(case, tmp_path, monkeypatch):
    worker = load_worker_module(monkeypatch)
    if len(inspect.signature(case.run).parameters) == 3:
        case.run(monkeypatch, worker, tmp_path)
    else:
        case.run(monkeypatch, worker)


def test_settings_golden_contract_matches_fastapi_and_worker(monkeypatch):
    worker = load_worker_module(monkeypatch)
    fixture = FIXTURES["settings"]

    assert ScannerSettings.model_validate(fixture["input"]).model_dump() == fixture["response"]
    assert worker.settings_from_payload(fixture["input"], strict=True) == fixture["response"]


def test_holding_golden_contract_matches_fastapi_and_worker(monkeypatch):
    worker = load_worker_module(monkeypatch)
    fixture = FIXTURES["holding"]

    assert Holding.model_validate(fixture["input"]).model_dump() == fixture["response"]
    assert worker.normalize_holding(fixture["input"]) == fixture["response"]


def test_public_user_golden_contract_matches_fastapi_and_worker(monkeypatch):
    worker = load_worker_module(monkeypatch)
    fixture = FIXTURES["publicUser"]
    backend_user = AuthUser(
        id=fixture["input"]["id"],
        username=fixture["input"]["username"],
        display_name=fixture["input"]["display_name"],
    )

    assert backend_user.public_dict() == fixture["response"]
    assert worker.public_user(fixture["input"]) == fixture["response"]


def test_admin_delete_user_behavior_matches_fastapi_and_worker(tmp_path, monkeypatch):
    worker = load_worker_module(monkeypatch)
    auth_service = AuthService(tmp_path / "auth.sqlite3")
    normal_user = auth_service.create_user("contract-delete@example.com", "test-password-123")
    auth_service.replace_holdings(
        normal_user.id,
        [Holding(stockCode="2330", name="TSMC", shares=1000, averageCost=600)],
    )
    normal_session_token = auth_service.create_session(normal_user.id)
    admin_username = "contract-admin@example.com"
    monkeypatch.setenv("SUPER_USER_USERNAME", admin_username)
    admin_user = auth_service.create_user(admin_username, "test-password-123")
    monkeypatch.setattr(main_module, "auth_service", auth_service)
    monkeypatch.setattr(main_module, "_require_super_user", lambda request: None)
    client = TestClient(main_module.app)
    api = worker.Api(env=None)
    db_run_calls = []

    async def fake_require_super_user(request):
        return {"id": admin_user.id, "username": admin_username, "display_name": None}

    async def fake_db_first(sql, *params):
        assert params == (normal_user.id,)
        return {"id": normal_user.id, "username": normal_user.username}

    async def fake_db_run(sql, *params):
        db_run_calls.append((" ".join(sql.split()), params))

    async def fake_admin_users_payload():
        return {
            "superUser": admin_username,
            "users": [
                {
                    "id": admin_user.id,
                    "username": admin_username,
                    "displayName": admin_username,
                    "createdAt": None,
                    "holdingsCount": 0,
                    "activeSessionCount": 0,
                    "isSuperUser": True,
                    "canDelete": False,
                }
            ],
        }

    api.require_super_user = fake_require_super_user
    api.db_first = fake_db_first
    api.db_run = fake_db_run
    api.admin_users_payload = fake_admin_users_payload

    fastapi_response = client.delete(f"/api/admin/users/{normal_user.id}")
    worker_response = asyncio.run(api.fetch(ContractRequest("DELETE", f"/api/admin/users/{normal_user.id}")))

    assert fastapi_response.status_code == worker_status(worker_response) == 200
    assert set(fastapi_response.json()) == set(worker_payload(worker_response)) == {"superUser", "users"}
    assert normal_user.username not in {user["username"] for user in fastapi_response.json()["users"]}
    assert normal_user.username not in {user["username"] for user in worker_payload(worker_response)["users"]}
    assert db_run_calls == [("DELETE FROM users WHERE id = ?", (normal_user.id,))]
    assert auth_service.get_user_by_session(normal_session_token) is None
    with auth_service._connect() as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM sessions WHERE user_id = ?", (normal_user.id,)).fetchone()[0]
            == 0
        )
        assert (
            connection.execute("SELECT COUNT(*) FROM holdings WHERE user_id = ?", (normal_user.id,)).fetchone()[0]
            == 0
        )


def test_admin_delete_missing_user_behavior_matches_fastapi_and_worker(tmp_path, monkeypatch):
    worker = load_worker_module(monkeypatch)
    admin_username = "contract-admin@example.com"
    monkeypatch.setenv("SUPER_USER_USERNAME", admin_username)
    auth_service = AuthService(tmp_path / "auth.sqlite3")
    admin_user = auth_service.create_user(admin_username, "test-password-123")
    monkeypatch.setattr(main_module, "auth_service", auth_service)
    monkeypatch.setattr(main_module, "_require_super_user", lambda request: None)
    client = TestClient(main_module.app)
    api = worker.Api(env=None)

    async def fake_require_super_user(request):
        return {"id": admin_user.id, "username": admin_username, "display_name": None}

    async def fake_db_first(sql, *params):
        return None

    api.require_super_user = fake_require_super_user
    api.db_first = fake_db_first

    fastapi_response = client.delete("/api/admin/users/999999")
    worker_response = asyncio.run(api.fetch(ContractRequest("DELETE", "/api/admin/users/999999")))

    assert fastapi_response.status_code == worker_status(worker_response) == 404
    assert fastapi_response.json()["detail"] == worker_payload(worker_response)["detail"] == "User not found"


def test_rate_limit_behavior_matches_fastapi_and_worker(tmp_path, monkeypatch):
    worker = load_worker_module(monkeypatch)
    auth_service = AuthService(tmp_path / "auth.sqlite3")
    monkeypatch.setattr(main_module, "auth_service", auth_service)
    username = "contract-rate@example.com"
    password = "test-password-123"
    auth_service.create_user(username, password)
    client = TestClient(main_module.app)
    headers = {"x-forwarded-for": "203.0.113.20"}
    api = worker.Api(env=None)

    async def fake_require_auth_attempt_allowed(username, request):
        raise worker.RateLimitError(30)

    api.require_auth_attempt_allowed = fake_require_auth_attempt_allowed

    for _ in range(AUTH_FAILURE_LIMIT):
        response = client.post("/api/auth/login", headers=headers, json={"username": username, "password": "bad-password"})
        assert response.status_code == 401

    fastapi_response = client.post("/api/auth/login", headers=headers, json={"username": username, "password": password})
    worker_response = asyncio.run(
        api.fetch(ContractRequest("POST", "/api/auth/login", {"username": username, "password": password}))
    )

    assert fastapi_response.status_code == worker_status(worker_response) == 429
    assert int(fastapi_response.headers["retry-after"]) > 0
    assert int(worker_response.headers["retry-after"]) == 30


def test_production_csrf_behavior_matches_fastapi_and_worker(monkeypatch):
    worker = load_worker_module(monkeypatch)
    monkeypatch.setenv("APP_ENV", "production")
    client = TestClient(main_module.app)
    api = worker.Api(
        env=types.SimpleNamespace(
            APP_ENV="production",
            APP_CORS_ALLOW_ORIGINS="https://stock-scanner-beta.pages.dev",
            SUPER_USER_USERNAME="contract-admin@example.com",
        )
    )

    async def fake_route(request, path, query):
        return worker.json_response({"ok": True})

    api.route = fake_route

    fastapi_response = client.post("/api/scan/market", json={"settings": ScannerSettings(use_mock_data=True).model_dump()})
    worker_response = asyncio.run(api.fetch(ContractRequest("POST", "/api/scan/market", {"settings": {}})))

    assert fastapi_response.status_code == worker_status(worker_response) == 403
    assert fastapi_response.json()["detail"] == worker_payload(worker_response)["detail"] == "CSRF header required"
