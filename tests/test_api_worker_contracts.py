from __future__ import annotations

import asyncio
import inspect
import json
import time
import types
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import backend.dependencies as deps_module
import backend.main as main_module
import backend.routers.market as market_module
from backend.models.holding import Holding
from backend.models.settings import ScannerSettings
from backend.services.auth import AUTH_FAILURE_LIMIT, AuthService, AuthUser
from backend.services.market_query import build_market_generation
from backend.services.refresh_jobs import RefreshJobService
from backend.services.scan_cache import ScanCacheService
from tests.test_cloudflare_worker import build_router_api, load_worker_module, pin_worker_time

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
    monkeypatch.setattr(deps_module, "load_settings", lambda: ScannerSettings(use_mock_data=True))
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
    monkeypatch.setattr(deps_module, "load_settings", lambda: ScannerSettings(use_mock_data=True))
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
    monkeypatch.setattr(deps_module, "load_settings", lambda: ScannerSettings(use_mock_data=True))
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
    monkeypatch.setattr(deps_module, "_require_super_user", lambda request: None)
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
    monkeypatch.setattr(deps_module, "load_settings", lambda: ScannerSettings(use_mock_data=True))
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
    monkeypatch.setattr(deps_module, "auth_service", auth_service)
    monkeypatch.setattr(deps_module, "_require_super_user", lambda request: None)
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
    monkeypatch.setattr(deps_module, "auth_service", auth_service)
    client = TestClient(main_module.app)
    username = "contract-holdings@example.com"
    client.post("/api/auth/register", json={"username": username, "password": "test-password-123"})
    holdings = [{"stockCode": "2330", "name": "TSMC", "shares": 1000, "averageCost": None}]
    api = make_worker_api(worker)
    stored_holdings: list = []

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


def contract_runtime_config(monkeypatch, worker):
    monkeypatch.setattr(deps_module, "load_settings", lambda: ScannerSettings(use_mock_data=True))
    client = TestClient(main_module.app)
    api = make_worker_api(worker)

    fastapi_response = client.get("/api/runtime-config")
    worker_response = run_worker_fetch(api, "GET", "/api/runtime-config")
    fastapi_payload, worker_response_payload = assert_status_and_keys_match(fastapi_response, worker_response)

    assert set(fastapi_payload) == {"schemaVersion", "marketScanApiVersion", "edgeCacheEnabled"}
    assert fastapi_payload["schemaVersion"] == worker_response_payload["schemaVersion"] == 1
    # v1 is retired: both runtimes must advertise v2 so the browser never asks for the whole scan.
    assert fastapi_payload["marketScanApiVersion"] == worker_response_payload["marketScanApiVersion"] == "v2"
    assert isinstance(fastapi_payload["edgeCacheEnabled"], bool)
    assert isinstance(worker_response_payload["edgeCacheEnabled"], bool)


def _poll_fastapi_refresh_job(client, status_url: str, timeout: float = 30.0) -> dict:
    deadline = time.monotonic() + timeout
    while True:
        response = client.get(status_url)
        assert response.status_code == 200
        payload = response.json()
        if payload["status"] in {"success", "failed"}:
            return payload
        assert time.monotonic() < deadline, f"refresh job never reached a terminal state: {payload}"
        time.sleep(0.05)


def _raise_market_scan_failure(settings):
    raise RuntimeError("upstream detail that must not be exposed")


def _assert_job_status_payloads_match(client, api, db, job: dict, error: str | None) -> None:
    """Mirror a finished FastAPI job into the Worker's D1 row and compare both status reads."""
    db.refresh_jobs.append(
        {
            "id": job["jobId"],
            "job_type": "market_scan",
            "cache_key": "contract-cache-key",
            "idempotency_key": f"contract-idempotency-hash-{job['jobId']}",
            "status": job["status"],
            "reason": job["reason"],
            "queued_at": job["queuedAt"],
            "started_at": job["startedAt"],
            "finished_at": job["finishedAt"],
            "updated_at": job["updatedAt"],
            "error": error,
        }
    )
    status_url = f"/api/scan/market/refresh/{job['jobId']}"
    fastapi_status = client.get(status_url)
    worker_response = run_worker_fetch(api, "GET", status_url)

    assert fastapi_status.status_code == worker_status(worker_response) == 200
    assert fastapi_status.json() == worker_payload(worker_response) == job
    assert fastapi_status.headers["cache-control"] == worker_response.headers["cache-control"] == "no-store"
    if error is not None:
        assert error not in fastapi_status.text
        assert error not in worker_response.body


def contract_market_refresh_command(monkeypatch, worker):
    # The Worker queues the rebuild in D1 for a GitHub Actions run; FastAPI runs it on a
    # background thread. Both must answer the command and the job read the same way, because
    # frontend/market_refresh.js validates the exact key set of both payloads.
    monkeypatch.setattr(deps_module, "load_settings", lambda: ScannerSettings(use_mock_data=True))
    client = TestClient(main_module.app)
    worker_module, api, db = build_router_api(
        monkeypatch, r2={"public/manifest.json": {"generatedAt": "2026-07-13T00:00:00+00:00"}}
    )
    pin_worker_time(monkeypatch, worker_module, "2026-07-13T12:01:00+00:00")
    idempotency_key = "contract-refresh-matrix"

    fastapi_command = client.post("/api/scan/market/refresh", headers={"idempotency-key": idempotency_key})
    worker_command = run_worker_fetch(
        api, "POST", "/api/scan/market/refresh", headers={"idempotency-key": idempotency_key}
    )
    fastapi_command_payload = fastapi_command.json()
    worker_command_payload = worker_payload(worker_command)

    assert fastapi_command.status_code == worker_status(worker_command) == 202
    assert set(fastapi_command_payload) == set(worker_command_payload) == {"jobId", "status", "requestId", "statusUrl"}
    assert fastapi_command_payload["status"] == worker_command_payload["status"] == "queued"
    assert fastapi_command.headers["location"] == fastapi_command_payload["statusUrl"]
    assert worker_command.headers["Location"] == worker_command_payload["statusUrl"]
    assert fastapi_command.headers["cache-control"] == worker_command.headers["cache-control"] == "no-store"

    # Read the FastAPI job to a terminal state, then mirror it into the Worker's D1 row so the
    # two status payloads are compared on the same job, field for field.
    succeeded = _poll_fastapi_refresh_job(client, fastapi_command_payload["statusUrl"])

    assert succeeded["status"] == "success"
    assert succeeded["hasError"] is False
    _assert_job_status_payloads_match(client, api, db, succeeded, error=None)

    # A failed rebuild must render `hasError` the same way on both sides.
    monkeypatch.setattr(market_module, "_scan_market_payload", _raise_market_scan_failure)
    failed_command = client.post("/api/scan/market/refresh", headers={"idempotency-key": "contract-refresh-failed"})
    failed = _poll_fastapi_refresh_job(client, failed_command.json()["statusUrl"])

    assert failed["status"] == "failed"
    assert failed["hasError"] is True
    _assert_job_status_payloads_match(client, api, db, failed, error="upstream detail that must not be exposed")

    unknown_status_url = f"/api/scan/market/refresh/{'f' * 32}"
    fastapi_unknown = client.get(unknown_status_url)
    worker_unknown = run_worker_fetch(api, "GET", unknown_status_url)

    assert fastapi_unknown.status_code == worker_status(worker_unknown) == 404
    assert fastapi_unknown.json()["detail"] == worker_payload(worker_unknown)["detail"] == "Not found"

    fastapi_invalid = client.post("/api/scan/market/refresh", headers={"idempotency-key": "not a safe key"})
    worker_invalid = run_worker_fetch(
        api, "POST", "/api/scan/market/refresh", headers={"idempotency-key": "not a safe key"}
    )

    assert fastapi_invalid.status_code == worker_status(worker_invalid) == 422
    assert "detail" in fastapi_invalid.json()
    assert "detail" in worker_payload(worker_invalid)


def contract_market_refresh_csrf(monkeypatch, worker):
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

    fastapi_response = client.post("/api/scan/market/refresh", headers={"idempotency-key": "contract-refresh-csrf"})
    worker_response = run_worker_fetch(
        api, "POST", "/api/scan/market/refresh", headers={"idempotency-key": "contract-refresh-csrf"}
    )

    assert fastapi_response.status_code == worker_status(worker_response) == 403
    assert fastapi_response.json()["detail"] == worker_payload(worker_response)["detail"] == "CSRF header required"


ENDPOINT_CONTRACT_MATRIX = [
    EndpointContractCase("app-status", contract_app_status),
    EndpointContractCase("data-sources/status", contract_data_sources_status),
    EndpointContractCase("runtime-config", contract_runtime_config),
    EndpointContractCase("scheduler/wakeup", contract_scheduler_wakeup),
    EndpointContractCase("scheduler/auto-scan", contract_scheduler_auto_scan),
    EndpointContractCase("backtest", contract_backtest),
    EndpointContractCase("auth required", contract_auth_required),
    EndpointContractCase("settings validation", contract_settings_validation),
    EndpointContractCase("reports", contract_reports),
    EndpointContractCase("admin delete", contract_admin_delete),
    EndpointContractCase("holdings", contract_holdings),
    EndpointContractCase("scan/market/refresh", contract_market_refresh_command),
    EndpointContractCase("scan/market/refresh csrf", contract_market_refresh_csrf),
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
    monkeypatch.setattr(deps_module, "auth_service", auth_service)
    monkeypatch.setattr(deps_module, "_require_super_user", lambda request: None)
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
    monkeypatch.setattr(deps_module, "auth_service", auth_service)
    monkeypatch.setattr(deps_module, "_require_super_user", lambda request: None)
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
    monkeypatch.setattr(deps_module, "auth_service", auth_service)
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

    fastapi_response = client.post(
        "/api/scan/holdings",
        json={"holdings": [], "settings": ScannerSettings(use_mock_data=True).model_dump()},
    )
    worker_response = asyncio.run(api.fetch(ContractRequest("POST", "/api/scan/holdings", {"holdings": []})))

    assert fastapi_response.status_code == worker_status(worker_response) == 403
    assert fastapi_response.json()["detail"] == worker_payload(worker_response)["detail"] == "CSRF header required"


def test_market_v2_success_validation_and_generation_status_contracts_match(monkeypatch):
    scan = {
        "generatedAt": "2026-07-13T00:00:00+00:00",
        "filingContext": {},
        "entry": [
            {
                "stockCode": "2330",
                "companyName": "TSMC",
                "status": "ENTRY",
                "summary": "announced",
                "reasons": [],
                "detailsAvailable": True,
                "hasFullDetails": False,
            }
        ],
        "watch": [],
        "excluded": [],
        "cacheStatus": {"servedAt": "stable"},
    }
    monkeypatch.setattr(market_module, "scan_market_cached", lambda: scan)
    fastapi = TestClient(main_module.app)
    generation = build_market_generation({key: value for key, value in scan.items() if key != "cacheStatus"})
    r2 = {
        "public/market_scan_index.json": generation.index,
        "public/manifest.json": {"generatedAt": scan["generatedAt"]},
        **{key: json.loads(value) for key, value in generation.files.items()},
    }

    worker, api, _db = build_router_api(monkeypatch, r2=r2)
    query = "?disclosure=announced&category=entry&cursor=0&limit=1"

    fast_index = fastapi.get("/api/scan/market/index")
    worker_index = run_worker_fetch(api, "GET", "/api/scan/market/index")
    assert fast_index.status_code == worker_status(worker_index) == 200
    assert fast_index.headers["cache-control"] == worker_index.headers["cache-control"] == "no-store"
    assert set(fast_index.json()) >= {"schemaVersion", "generationId", "disclosures", "counts", "cacheStatus"}
    assert set(worker_payload(worker_index)) >= {"schemaVersion", "generationId", "disclosures", "counts", "cacheStatus"}

    fast_results = fastapi.get(f"/api/scan/market/results{query}")
    worker_results = run_worker_fetch(api, "GET", f"/api/scan/market/results{query}")
    required = {
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
    assert fast_results.status_code == worker_status(worker_results) == 200
    assert fast_results.headers["cache-control"] == worker_results.headers["cache-control"] == "no-store"
    assert set(fast_results.json()) == set(worker_payload(worker_results)) == required

    invalid_query = "?disclosure=other&category=entry&cursor=0&limit=1"
    fast_invalid = fastapi.get(f"/api/scan/market/results{invalid_query}")
    worker_invalid = run_worker_fetch(api, "GET", f"/api/scan/market/results{invalid_query}")
    assert fast_invalid.status_code == worker_status(worker_invalid) == 422
    assert fast_invalid.headers["cache-control"] == worker_invalid.headers["cache-control"] == "no-store"

    mismatch_query = f"{query}&generationId={'f' * 24}"
    fast_mismatch = fastapi.get(f"/api/scan/market/results{mismatch_query}")
    worker_mismatch = run_worker_fetch(api, "GET", f"/api/scan/market/results{mismatch_query}")
    assert fast_mismatch.status_code == worker_status(worker_mismatch) == 409
    assert fast_mismatch.headers["cache-control"] == worker_mismatch.headers["cache-control"] == "no-store"


def test_refresh_command_worker_contract_is_additive_and_payload_bounded(monkeypatch):
    worker, api, _db = build_router_api(
        monkeypatch,
        r2={"public/manifest.json": {"generatedAt": "2026-07-13T00:00:00+00:00"}},
    )
    pin_worker_time(monkeypatch, worker, "2026-07-13T12:01:00+00:00")

    response = run_worker_fetch(
        api,
        "POST",
        "/api/scan/market/refresh",
        headers={"idempotency-key": "contract-refresh-1"},
    )
    payload = worker_payload(response)

    assert worker_status(response) == 202
    assert set(payload) == {"jobId", "status", "requestId", "statusUrl"}
    assert response.headers["Location"] == payload["statusUrl"]
    assert len(json.dumps(payload).encode("utf-8")) < 1024
    assert not {"entry", "watch", "excluded", "cacheStatus"} & set(payload)


def test_market_refresh_cooldown_429_matches_fastapi_and_worker(tmp_path, monkeypatch):
    # FastAPI: run one real-data refresh to a genuine success on an isolated job service/cache
    # (so no other test's leftover job leaks into this cooldown window), then a forced repeat
    # with a different key must 429 for the rest of the local cooldown. Also clear the unrelated
    # scan rate limiter (10 req/60s, keyed by client host - shared process-wide, never scoped
    # per test), so its own older 429 cannot mask the cooldown 429 under test.
    deps_module._scan_rate_store.clear()
    monkeypatch.setattr(market_module, "refresh_job_service", RefreshJobService())
    monkeypatch.setattr(
        market_module,
        "scan_cache_service",
        ScanCacheService(tmp_path / "market_scan_cache.json", tmp_path / "cache_refresh_state.json"),
    )
    monkeypatch.setattr(deps_module, "load_settings", lambda: ScannerSettings(use_mock_data=False))
    monkeypatch.setattr(market_module, "_scan_market_cache_context", lambda settings: {"provider": "official"})
    monkeypatch.setattr(
        market_module,
        "_scan_market_payload_after_official_refresh",
        lambda settings: {
            "generatedAt": "2026-07-13T00:00:00+00:00",
            "filingContext": {},
            "entry": [
                {
                    "stockCode": "2330",
                    "companyName": "contract-cooldown",
                    "status": "ENTRY",
                    "summary": "announced",
                    "reasons": [],
                }
            ],
            "watch": [],
            "excluded": [],
            "universeSize": 1,
        },
    )
    client = TestClient(main_module.app)

    fastapi_first = client.post("/api/scan/market/refresh", headers={"idempotency-key": "cooldown-parity-first"})
    assert fastapi_first.status_code == 202
    assert _poll_fastapi_refresh_job(client, fastapi_first.json()["statusUrl"])["status"] == "success"

    fastapi_blocked = client.post("/api/scan/market/refresh", headers={"idempotency-key": "cooldown-parity-next"})

    # Worker: seed a D1 row for a job that finished moments ago, matching the terminal-job
    # cooldown the Worker itself enforces (cloudflare/worker_refresh_jobs.py:226-230).
    worker, api, db = build_router_api(
        monkeypatch, r2={"public/manifest.json": {"generatedAt": "2026-07-13T00:00:00+00:00"}}
    )
    pin_worker_time(monkeypatch, worker, "2026-07-13T12:01:00+00:00")
    db.refresh_jobs.append(
        {
            "id": "f" * 32,
            "job_type": "market_scan",
            "cache_key": "previous-generation",
            "idempotency_key": "previous-key",
            "status": "success",
            "reason": "routine_refresh",
            "queued_at": "2026-07-13T12:00:00+00:00",
            "finished_at": "2026-07-13T12:00:30+00:00",
            "updated_at": "2026-07-13T12:00:30+00:00",
        }
    )

    worker_blocked = run_worker_fetch(
        api, "POST", "/api/scan/market/refresh", headers={"idempotency-key": "cooldown-parity-worker"}
    )

    assert fastapi_blocked.status_code == worker_status(worker_blocked) == 429
    assert int(fastapi_blocked.headers["retry-after"]) >= 1
    assert int(worker_blocked.headers["retry-after"]) >= 1
    assert fastapi_blocked.headers["cache-control"] == "no-store"
    assert "detail" in fastapi_blocked.json()
    assert "detail" in worker_payload(worker_blocked)


def _raise_market_refresh_failure(settings):
    raise RuntimeError("official refresh failed")


def test_market_refresh_cooldown_429_after_failed_job_matches_fastapi_and_worker(tmp_path, monkeypatch):
    # Same rule as the Worker's terminal-job cooldown (READ_RECENT_TERMINAL_SQL matches
    # success or failed): a failed job starts the cooldown too, not just a success. Also clear
    # the unrelated scan rate limiter (10 req/60s, keyed by client host - shared process-wide,
    # never scoped per test), so its own older 429 cannot mask the cooldown 429 under test.
    deps_module._scan_rate_store.clear()
    monkeypatch.setattr(market_module, "refresh_job_service", RefreshJobService())
    monkeypatch.setattr(
        market_module,
        "scan_cache_service",
        ScanCacheService(tmp_path / "market_scan_cache.json", tmp_path / "cache_refresh_state.json"),
    )
    monkeypatch.setattr(deps_module, "load_settings", lambda: ScannerSettings(use_mock_data=False))
    monkeypatch.setattr(market_module, "_scan_market_cache_context", lambda settings: {"provider": "official"})
    monkeypatch.setattr(market_module, "_scan_market_payload_after_official_refresh", _raise_market_refresh_failure)
    client = TestClient(main_module.app)

    fastapi_first = client.post("/api/scan/market/refresh", headers={"idempotency-key": "cooldown-parity-failed-first"})
    assert fastapi_first.status_code == 202
    assert _poll_fastapi_refresh_job(client, fastapi_first.json()["statusUrl"])["status"] == "failed"

    fastapi_blocked = client.post("/api/scan/market/refresh", headers={"idempotency-key": "cooldown-parity-failed-next"})

    # Worker: seed a D1 row for a `failed` job that finished moments ago - the same terminal
    # statuses READ_RECENT_TERMINAL_SQL matches (cloudflare/worker_refresh_jobs.py:38,59).
    worker, api, db = build_router_api(
        monkeypatch, r2={"public/manifest.json": {"generatedAt": "2026-07-13T00:00:00+00:00"}}
    )
    pin_worker_time(monkeypatch, worker, "2026-07-13T12:01:00+00:00")
    db.refresh_jobs.append(
        {
            "id": "e" * 32,
            "job_type": "market_scan",
            "cache_key": "previous-generation",
            "idempotency_key": "previous-key-failed",
            "status": "failed",
            "reason": "routine_refresh",
            "queued_at": "2026-07-13T12:00:00+00:00",
            "finished_at": "2026-07-13T12:00:30+00:00",
            "updated_at": "2026-07-13T12:00:30+00:00",
        }
    )

    worker_blocked = run_worker_fetch(
        api, "POST", "/api/scan/market/refresh", headers={"idempotency-key": "cooldown-parity-worker-failed"}
    )

    assert fastapi_blocked.status_code == worker_status(worker_blocked) == 429
    assert int(fastapi_blocked.headers["retry-after"]) >= 1
    assert int(worker_blocked.headers["retry-after"]) >= 1
    assert fastapi_blocked.headers["cache-control"] == "no-store"
    assert "detail" in fastapi_blocked.json()
    assert "detail" in worker_payload(worker_blocked)
