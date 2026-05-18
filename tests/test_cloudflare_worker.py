import asyncio
import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest

from backend.models.settings import ScannerSettings


ROOT = Path(__file__).resolve().parents[1]


def load_worker_module(monkeypatch):
    js_module = types.ModuleType("js")
    js_module.Object = types.SimpleNamespace(fromEntries=lambda value: value)

    def response_new(body, init=None):
        init = init or {}
        return types.SimpleNamespace(body=body, init=init, headers=dict(init.get("headers", {})))

    js_module.Response = types.SimpleNamespace(new=response_new)

    pyodide_module = types.ModuleType("pyodide")
    ffi_module = types.ModuleType("pyodide.ffi")
    ffi_module.to_js = lambda value, dict_converter=None: value

    monkeypatch.setitem(sys.modules, "js", js_module)
    monkeypatch.setitem(sys.modules, "pyodide", pyodide_module)
    monkeypatch.setitem(sys.modules, "pyodide.ffi", ffi_module)

    spec = importlib.util.spec_from_file_location("cloudflare_worker_under_test", ROOT / "cloudflare" / "worker.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class SingleReadRequest:
    def __init__(self, text):
        self.text_value = text
        self.text_reads = 0

    async def text(self):
        self.text_reads += 1
        if self.text_reads > 1:
            raise TypeError("Body has already been used")
        return self.text_value


class FakeStatement:
    def __init__(self, sql):
        self.sql = sql
        self.params = ()

    def bind(self, *params):
        self.params = params
        return self


class FakeD1:
    def __init__(self):
        self.prepared = []
        self.batches = []

    def prepare(self, sql):
        statement = FakeStatement(sql)
        self.prepared.append(statement)
        return statement

    async def batch(self, statements):
        self.batches.append(list(statements))
        return []


class CompleteFakeStatement:
    def __init__(self, sql):
        self.sql = sql
        self.params = ()

    def bind(self, *params):
        self.params = params
        return self

    async def run(self):
        return {"success": True, "meta": {"changed_db": True}}

    async def first(self):
        return {"id": 1, "name": "row"}

    async def all(self):
        return {"results": [{"id": 1}, {"id": 2}]}


class CompleteFakeD1:
    def __init__(self):
        self.prepared = []

    def prepare(self, sql):
        statement = CompleteFakeStatement(sql)
        self.prepared.append(statement)
        return statement


class FakeR2Object:
    def __init__(self, text):
        self._text = text

    async def text(self):
        return self._text


class FakeR2Cache:
    def __init__(self, objects=None):
        self.objects = objects or {}
        self.calls = []

    async def get(self, key):
        self.calls.append(key)
        return self.objects.get(key)


class FakeJsNull:
    def to_py(self):
        return None


JsNullWithoutToPy = type("JsNull", (), {})


def test_worker_request_json_reads_body_once(monkeypatch):
    worker = load_worker_module(monkeypatch)
    request = SingleReadRequest('{"username":"pcedison@gmail.com","password":"test-password-123"}')

    payload = asyncio.run(worker.Api(env=None).request_json(request))

    assert payload["username"] == "pcedison@gmail.com"
    assert request.text_reads == 1


def test_worker_request_json_rejects_invalid_json(monkeypatch):
    worker = load_worker_module(monkeypatch)
    request = SingleReadRequest("{bad")

    try:
        asyncio.run(worker.Api(env=None).request_json(request))
    except worker.BadRequestError as exc:
        assert str(exc) == "JSON 格式錯誤"
    else:
        raise AssertionError("BadRequestError was not raised")


def test_worker_security_headers_and_auth_pattern(monkeypatch):
    worker = load_worker_module(monkeypatch)

    response = worker.json_response({"ok": True})

    assert response.headers["strict-transport-security"].startswith("max-age=31536000")
    assert "script-src 'self'" in response.headers["content-security-policy"]
    assert "style-src 'self';" in response.headers["content-security-policy"]
    assert "unsafe-inline" not in response.headers["content-security-policy"]
    assert response.headers["x-frame-options"] == "DENY"
    assert worker.normalize_username(" Qa+audit%2026@example.com ") == "qa+audit%2026@example.com"
    try:
        worker.normalize_username("bad account@example.com")
    except ValueError as exc:
        assert "不可包含空白" in str(exc)
    else:
        raise AssertionError("ValueError was not raised")


def test_worker_cors_uses_env_allowlist_and_dev_loopback(monkeypatch):
    worker = load_worker_module(monkeypatch)
    api = worker.Api(env=None)

    localhost_request = types.SimpleNamespace(headers={"origin": "http://127.0.0.1:8000"})
    headers = api.cors_headers(localhost_request)

    assert headers["access-control-allow-origin"] == "http://127.0.0.1:8000"
    assert headers["access-control-allow-credentials"] == "true"
    assert api.cors_headers(types.SimpleNamespace(headers={"origin": "https://evil.example"}))[
        "access-control-allow-origin"
    ] == "http://localhost:8000"

    production_api = worker.Api(
        env=types.SimpleNamespace(
            APP_ENV="production",
            APP_CORS_ALLOW_ORIGINS="https://stock-scanner-beta.pages.dev,http://localhost:8787,http://bad.example",
        )
    )
    assert production_api.cors_headers(types.SimpleNamespace(headers={"origin": "https://stock-scanner-beta.pages.dev"}))[
        "access-control-allow-origin"
    ] == "https://stock-scanner-beta.pages.dev"
    assert production_api.cors_headers(types.SimpleNamespace(headers={"origin": "http://localhost:8787"}))[
        "access-control-allow-origin"
    ] == "https://stock-scanner-beta.pages.dev"
    assert production_api.cors_allowed_origins() == ("https://stock-scanner-beta.pages.dev",)


def test_worker_options_preflight_uses_cors_and_security_headers(monkeypatch):
    worker = load_worker_module(monkeypatch)
    api = worker.Api(env=None)
    request = types.SimpleNamespace(
        method="OPTIONS",
        url="https://stock-scanner-beta-api.example/api/settings",
        headers={"origin": "http://localhost:8000"},
    )

    response = asyncio.run(api.fetch(request))

    assert response.init["status"] == 204
    headers = response.headers
    assert headers["access-control-allow-origin"] == "http://localhost:8000"
    assert headers["strict-transport-security"].startswith("max-age=31536000")


def test_worker_production_csrf_guard_requires_custom_header(monkeypatch):
    worker = load_worker_module(monkeypatch)
    api = worker.Api(env=types.SimpleNamespace(APP_ENV="production", APP_CORS_ALLOW_ORIGINS="https://stock-scanner-beta.pages.dev"))
    missing_header = types.SimpleNamespace(
        method="POST",
        url="https://stock-scanner-beta-api.example/api/scan/market",
        headers={"origin": "https://stock-scanner-beta.pages.dev"},
    )
    with_header = types.SimpleNamespace(
        method="POST",
        url="https://stock-scanner-beta-api.example/api/scan/market",
        headers={"origin": "https://stock-scanner-beta.pages.dev", "x-stock-scanner-csrf": "1"},
    )

    async def fake_route(request, path, query):
        return worker.json_response({"ok": True})

    api.route = fake_route

    missing = asyncio.run(api.fetch(missing_header))
    present = asyncio.run(api.fetch(with_header))

    assert missing.init["status"] == 403
    assert json.loads(missing.body)["detail"] == "CSRF header required"
    assert present.init["status"] == 200


def test_worker_error_response_preserves_rate_limit_retry_after(monkeypatch):
    worker = load_worker_module(monkeypatch)

    response = worker.error_response("slow down", status=429, headers={"retry-after": "30"})

    assert response.init["status"] == 429
    assert response.headers["retry-after"] == "30"


def test_worker_manifest_quality_flags_undersized_seed(monkeypatch):
    worker = load_worker_module(monkeypatch)

    bad = worker.manifest_quality({"counts": {"companies": 10, "entry": 1, "watch": 2, "excluded": 3, "analysis": 9}})
    good = worker.manifest_quality({"counts": {"companies": 1000, "entry": 10, "watch": 980, "excluded": 10, "analysis": 1000}})

    assert bad["ok"] is False
    assert bad["problems"]
    assert good["ok"] is True


def test_worker_settings_payload_validation(monkeypatch):
    worker = load_worker_module(monkeypatch)

    settings = worker.settings_from_payload(
        {
            "auto_scan_full_market": False,
            "manual_scan_enabled": True,
            "exclude_financial_industry": True,
            "use_mock_data": False,
            "scan_twse": True,
            "scan_tpex": False,
            "spring_festival_guard": True,
            "revenue_growth_mode": "monthly",
        },
        strict=True,
    )
    assert settings["auto_scan_full_market"] is False
    assert settings["scan_tpex"] is False
    assert settings["revenue_growth_mode"] == "monthly"

    with pytest.raises(worker.ValidationError):
        worker.settings_from_payload({"manual_scan_enabled": "false"}, strict=True)
    with pytest.raises(worker.ValidationError):
        worker.settings_from_payload({"revenue_growth_mode": "bad-mode"}, strict=True)
    assert worker.settings_from_payload({"manual_scan_enabled": "false"})["manual_scan_enabled"] is True


def test_worker_default_settings_matches_fastapi_schema(monkeypatch):
    worker = load_worker_module(monkeypatch)
    fastapi_settings = ScannerSettings().model_dump()

    assert set(worker.DEFAULT_SETTINGS) == set(fastapi_settings)
    assert worker.DEFAULT_SETTINGS == fastapi_settings


def test_worker_app_status_reads_d1_settings(monkeypatch):
    worker = load_worker_module(monkeypatch)
    api = worker.Api(env=None)

    async def fake_r2_json(key, fallback):
        assert key == "public/data_sources_status.json"
        return {"activeProvider": "CloudflareR2Seed"}

    async def fake_get_settings():
        return {"auto_scan_full_market": False, "manual_scan_enabled": False}

    api.r2_json = fake_r2_json
    api.get_settings = fake_get_settings

    response = asyncio.run(api.route(types.SimpleNamespace(method="GET"), "/api/app-status", {}))
    payload = json.loads(response.body)

    assert payload["dataSourceStatus"]["activeProvider"] == "CloudflareR2Seed"
    assert payload["schedulerAutoScan"]["autoScanEnabled"] is False
    assert payload["schedulerAutoScan"]["manualScanEnabled"] is False
    assert set(payload["backtestStatus"]) >= {"status", "trades", "metrics"}
    assert set(payload["backtestStatus"]["metrics"]) == {"tradeCount", "winRate", "totalReturn", "maxDrawdown"}


def test_worker_health_reports_degraded_cache_quality(monkeypatch):
    worker = load_worker_module(monkeypatch)
    api = worker.Api(env=None)

    async def fake_r2_json(key, fallback):
        assert key == "public/manifest.json"
        return {"counts": {"companies": 0, "entry": 0, "watch": 0, "excluded": 0, "analysis": 0}}

    api.r2_json = fake_r2_json

    response = asyncio.run(api.route(types.SimpleNamespace(method="GET"), "/api/health", {}))
    payload = json.loads(response.body)

    assert payload["status"] == "degraded"
    assert payload["cacheQuality"]["ok"] is False


def test_worker_scheduler_auto_scan_reads_d1_settings(monkeypatch):
    worker = load_worker_module(monkeypatch)
    api = worker.Api(env=None)

    async def fake_get_settings():
        return {"auto_scan_full_market": False, "manual_scan_enabled": False}

    api.get_settings = fake_get_settings

    response = asyncio.run(api.route(types.SimpleNamespace(method="GET"), "/api/scheduler/auto-scan", {}))
    payload = json.loads(response.body)

    assert payload["autoScanEnabled"] is False
    assert payload["manualScanEnabled"] is False


def test_worker_db_helpers_normalize_d1_shapes(monkeypatch):
    worker = load_worker_module(monkeypatch)
    fake_db = CompleteFakeD1()
    api = worker.Api(env=types.SimpleNamespace(DB=fake_db))

    run_result = asyncio.run(api.db_run("UPDATE items SET value = ?", 1))
    first_result = asyncio.run(api.db_first("SELECT * FROM items WHERE id = ?", 1))
    all_result = asyncio.run(api.db_all("SELECT * FROM items"))

    assert run_result["success"] is True
    assert first_result == {"id": 1, "name": "row"}
    assert all_result == [{"id": 1}, {"id": 2}]
    assert fake_db.prepared[0].params == (1,)
    assert fake_db.prepared[1].params == (1,)


def test_worker_r2_json_caches_misses_and_parses_json(monkeypatch):
    worker = load_worker_module(monkeypatch)
    cache = FakeR2Cache({"public/ok.json": FakeR2Object('{"ok": true}')})
    api = worker.Api(env=types.SimpleNamespace(CACHE=cache))

    ok = asyncio.run(api.r2_json("public/ok.json", {"ok": False}))
    missing = asyncio.run(api.r2_json("public/missing.json", {"items": []}))
    cached_missing = asyncio.run(api.r2_json("public/missing.json", {"items": ["should-not-appear"]}))

    assert ok == {"ok": True}
    assert missing == {"items": []}
    assert cached_missing == {"items": []}
    assert cache.calls == ["public/ok.json", "public/missing.json"]


def test_worker_r2_json_treats_pyodide_js_null_as_missing(monkeypatch):
    worker = load_worker_module(monkeypatch)
    cache = FakeR2Cache({"public/missing.json": FakeJsNull(), "public/also-missing.json": JsNullWithoutToPy()})
    api = worker.Api(env=types.SimpleNamespace(CACHE=cache))

    payload = asyncio.run(api.r2_json("public/missing.json", {"ok": False}))
    payload_without_to_py = asyncio.run(api.r2_json("public/also-missing.json", {"ok": "fallback"}))

    assert payload == {"ok": False}
    assert payload_without_to_py == {"ok": "fallback"}


def test_worker_holdings_scan_prefers_holding_exit_analysis(monkeypatch):
    worker = load_worker_module(monkeypatch)
    api = worker.Api(env=None)

    async def fake_r2_json(key, fallback):
        if key == "public/holding_analysis_shards/30.json":
            return {
                "3008": {
                    "stockCode": "3008",
                    "companyName": "大立光",
                    "status": "EXIT",
                    "summary": "已觸發高優先出場條件。",
                    "reasons": [
                        {"code": "X4", "title": "季度 EPS 不可減少超過 10%", "passed": False, "severity": "EXIT", "message": "X4"},
                        {"code": "HOLDING", "title": "目前持股", "passed": True, "severity": "INFO", "message": "seed note"},
                    ],
                }
            }
        return fallback

    api.r2_json = fake_r2_json

    payload = asyncio.run(
        api.holdings_scan_payload(
            {"holdings": [{"stockCode": "3008", "name": "大立光", "shares": 1000, "averageCost": 2000}]}
        )
    )

    result = payload["results"][0]
    assert result["status"] == "EXIT"
    assert any(reason["code"] == "X4" and reason["severity"] == "EXIT" for reason in result["reasons"])
    assert sum(1 for reason in result["reasons"] if reason["code"] == "HOLDING") == 1
    assert "平均成本 2000" in next(reason["message"] for reason in result["reasons"] if reason["code"] == "HOLDING")


def test_worker_holdings_scan_marks_x_rules_missing_for_old_entry_cache(monkeypatch):
    worker = load_worker_module(monkeypatch)
    api = worker.Api(env=None)

    async def fake_r2_json(key, fallback):
        if key == "public/analysis_shards/23.json":
            return {
                "2357": {
                    "stockCode": "2357",
                    "companyName": "華碩",
                    "status": "ENTRY",
                    "summary": "進場快取",
                    "reasons": [{"code": "E1", "title": "近 5 年沒有虧損", "passed": True, "severity": "INFO", "message": "E1"}],
                }
            }
        return fallback

    api.r2_json = fake_r2_json

    payload = asyncio.run(
        api.holdings_scan_payload(
            {"holdings": [{"stockCode": "2357", "name": "華碩", "shares": 100, "averageCost": None}]}
        )
    )

    result = payload["results"][0]
    x_rules = [reason for reason in result["reasons"] if reason["code"].startswith("X")]
    assert result["status"] == "INSUFFICIENT_DATA"
    assert [reason["code"] for reason in x_rules] == ["X1", "X2", "X3", "X4", "X5"]
    assert all(reason["severity"] == "INSUFFICIENT_DATA" for reason in x_rules)


def test_worker_replace_holdings_uses_single_d1_batch_and_preserves_null_average_cost(monkeypatch):
    worker = load_worker_module(monkeypatch)
    fake_db = FakeD1()
    api = worker.Api(env=types.SimpleNamespace(DB=fake_db))

    async def fake_list_holdings(user_id):
        assert user_id == 42
        return [
            {"stockCode": "2330", "name": "台積電", "shares": 1000, "averageCost": None},
            {"stockCode": "2357", "name": "華碩", "shares": 200, "averageCost": 510.5},
        ]

    api.list_holdings = fake_list_holdings

    result = asyncio.run(
        api.replace_holdings(
            42,
            [
                {"stockCode": "2330", "name": "台積電", "shares": 1000, "averageCost": None},
                {"stockCode": "2357", "name": "華碩", "shares": 200, "averageCost": 510.5},
            ],
        )
    )

    assert len(fake_db.batches) == 1
    batch = fake_db.batches[0]
    assert len(batch) == 3
    assert batch[0].sql == "DELETE FROM holdings WHERE user_id = ?"
    assert batch[0].params == (42,)
    assert "INSERT INTO holdings" in batch[1].sql
    assert batch[1].params[:4] == (42, "2330", "台積電", 1000)
    assert batch[1].params[4] is None
    assert batch[2].params[:4] == (42, "2357", "華碩", 200)
    assert batch[2].params[4] == 510.5
    assert result[0]["averageCost"] is None


def test_worker_upsert_holding_preserves_null_average_cost(monkeypatch):
    worker = load_worker_module(monkeypatch)
    api = worker.Api(env=None)
    calls = []

    async def fake_db_run(sql, *params):
        calls.append((sql, params))

    api.db_run = fake_db_run

    asyncio.run(api.upsert_holding(7, {"stockCode": "2330", "name": "台積電", "shares": 3, "averageCost": None}))

    assert len(calls) == 1
    assert "INSERT INTO holdings" in calls[0][0]
    assert calls[0][1][:4] == (7, "2330", "台積電", 3)
    assert calls[0][1][4] is None
