from __future__ import annotations

import asyncio
import json
import types
from pathlib import Path

from fastapi.testclient import TestClient

import backend.main as main_module
from backend.main import app
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


def test_app_status_golden_shape_matches_fastapi_and_worker(monkeypatch):
    worker = load_worker_module(monkeypatch)
    monkeypatch.setattr(main_module, "load_settings", lambda: ScannerSettings(use_mock_data=True))
    client = TestClient(app)

    async def fake_r2_json(key, fallback):
        return {"activeProvider": "CloudflareR2Seed"} if key == "public/data_sources_status.json" else fallback

    async def fake_get_settings():
        return ScannerSettings(use_mock_data=False).model_dump()

    api = worker.Api(env=None)
    api.r2_json = fake_r2_json
    api.get_settings = fake_get_settings

    fastapi_payload = client.get("/api/app-status").json()
    worker_payload = json.loads(asyncio.run(api.route(types.SimpleNamespace(method="GET"), "/api/app-status", {})).body)

    assert list(fastapi_payload) == FIXTURES["appStatusKeys"]
    assert list(worker_payload) == FIXTURES["appStatusKeys"]
    assert list(fastapi_payload["schedulerAutoScan"]) == FIXTURES["schedulerAutoScanKeys"]
    assert list(worker_payload["schedulerAutoScan"]) == FIXTURES["schedulerAutoScanKeys"]
    assert set(fastapi_payload["backtestStatus"]["metrics"]) == set(worker_payload["backtestStatus"]["metrics"])


def test_auth_required_behavior_matches_fastapi_and_worker(monkeypatch):
    worker = load_worker_module(monkeypatch)
    client = TestClient(app)
    api = worker.Api(env=None)

    fastapi_response = client.get("/api/me/holdings")
    worker_response = asyncio.run(api.fetch(ContractRequest("GET", "/api/me/holdings")))

    assert fastapi_response.status_code == worker_status(worker_response) == 401
    assert "detail" in fastapi_response.json()
    assert "detail" in worker_payload(worker_response)


def test_settings_validation_behavior_matches_fastapi_and_worker(monkeypatch):
    worker = load_worker_module(monkeypatch)
    monkeypatch.setattr(main_module, "_require_super_user", lambda request: None)
    client = TestClient(app)
    invalid_settings = {**ScannerSettings().model_dump(), "manual_scan_enabled": "false"}
    api = worker.Api(env=None)

    async def fake_require_super_user(request):
        return {"id": 1, "username": "pcedison@gmail.com", "display_name": None}

    api.require_super_user = fake_require_super_user

    fastapi_response = client.put("/api/settings", json=invalid_settings)
    worker_response = asyncio.run(api.fetch(ContractRequest("PUT", "/api/settings", invalid_settings)))

    assert fastapi_response.status_code == worker_status(worker_response) == 422
    assert "detail" in fastapi_response.json()
    assert "detail" in worker_payload(worker_response)


def test_admin_delete_super_user_behavior_matches_fastapi_and_worker(tmp_path, monkeypatch):
    worker = load_worker_module(monkeypatch)
    auth_service = AuthService(tmp_path / "auth.sqlite3")
    super_user = auth_service.create_user("pcedison@gmail.com", "test-password-123")
    monkeypatch.setattr(main_module, "auth_service", auth_service)
    monkeypatch.setattr(main_module, "_require_super_user", lambda request: None)
    client = TestClient(app)
    api = worker.Api(env=None)

    async def fake_require_super_user(request):
        return {"id": super_user.id, "username": "pcedison@gmail.com", "display_name": None}

    async def fake_db_first(sql, *params):
        return {"id": super_user.id, "username": "pcedison@gmail.com"}

    api.require_super_user = fake_require_super_user
    api.db_first = fake_db_first

    fastapi_response = client.delete(f"/api/admin/users/{super_user.id}")
    worker_response = asyncio.run(api.fetch(ContractRequest("DELETE", f"/api/admin/users/{super_user.id}")))

    assert fastapi_response.status_code == worker_status(worker_response) == 400
    assert "detail" in fastapi_response.json()
    assert "detail" in worker_payload(worker_response)


def test_holdings_roundtrip_behavior_matches_fastapi_and_worker(tmp_path, monkeypatch):
    worker = load_worker_module(monkeypatch)
    auth_service = AuthService(tmp_path / "auth.sqlite3")
    monkeypatch.setattr(main_module, "auth_service", auth_service)
    client = TestClient(app)
    username = "contract-holdings@example.com"
    client.post("/api/auth/register", json={"username": username, "password": "test-password-123"})
    holdings = [{"stockCode": "2330", "name": "TSMC", "shares": 1000, "averageCost": None}]
    api = worker.Api(env=None)
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
    worker_put = asyncio.run(api.fetch(ContractRequest("PUT", "/api/me/holdings", {"holdings": holdings})))
    worker_get = asyncio.run(api.fetch(ContractRequest("GET", "/api/me/holdings")))

    assert fastapi_put.status_code == worker_status(worker_put) == 200
    assert fastapi_get.status_code == worker_status(worker_get) == 200
    assert fastapi_get.json()["holdings"] == worker_payload(worker_get)["holdings"] == holdings


def test_report_behavior_matches_fastapi_and_worker(monkeypatch):
    worker = load_worker_module(monkeypatch)
    monkeypatch.setattr(main_module, "load_settings", lambda: ScannerSettings(use_mock_data=True))
    client = TestClient(app)
    api = worker.Api(env=None)

    async def fake_r2_json(key, fallback):
        return {"generatedAt": "2026-05-18T00:00:00+00:00", "dataSource": "contract", "entry": [], "watch": [], "excluded": []}

    api.r2_json = fake_r2_json

    fastapi_response = client.post("/api/reports/market?report_format=markdown", json={"settings": ScannerSettings(use_mock_data=True).model_dump()})
    worker_response = asyncio.run(api.fetch(ContractRequest("POST", "/api/reports/market?report_format=markdown", {})))

    assert fastapi_response.status_code == worker_status(worker_response) == 200
    assert fastapi_response.headers["content-type"].startswith("text/markdown")
    assert worker_response.headers["content-type"].startswith("text/markdown")
    assert fastapi_response.text.startswith("# ")
    assert worker_response.body.startswith("# ")


def test_rate_limit_behavior_matches_fastapi_and_worker(tmp_path, monkeypatch):
    worker = load_worker_module(monkeypatch)
    auth_service = AuthService(tmp_path / "auth.sqlite3")
    monkeypatch.setattr(main_module, "auth_service", auth_service)
    username = "contract-rate@example.com"
    password = "test-password-123"
    auth_service.create_user(username, password)
    client = TestClient(app)
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
