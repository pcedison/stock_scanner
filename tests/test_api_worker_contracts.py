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
from backend.services.auth import AuthUser
from tests.test_cloudflare_worker import load_worker_module


FIXTURES = json.loads((Path(__file__).parent / "fixtures" / "api_worker_contracts.json").read_text(encoding="utf-8"))


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
