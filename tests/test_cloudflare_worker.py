import asyncio
import importlib
import importlib.util
import json
import re
import sys
import types
from datetime import UTC, datetime
from pathlib import Path

import pytest

from backend.models.settings import ScannerSettings
from backend.services.market_query import build_market_generation

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


class FakeArrayBuffer:
    def __init__(self, raw):
        self._raw = raw

    def to_py(self):
        return memoryview(self._raw)


class FakeR2Object:
    def __init__(self, text, *, raw=None, size=None):
        self._text = text
        self._raw = raw if raw is not None else str(text).encode("utf-8")
        self.size = len(self._raw) if size is None else size
        self.text_reads = 0
        self.array_reads = 0

    async def text(self):
        self.text_reads += 1
        return self._text

    async def arrayBuffer(self):
        self.array_reads += 1
        return FakeArrayBuffer(self._raw)


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
            SUPER_USER_USERNAME="pcedison@gmail.com",
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
    assert "idempotency-key" in headers["access-control-allow-headers"].split(",")
    assert headers["strict-transport-security"].startswith("max-age=31536000")
    assert re.fullmatch(r"[0-9a-f]{24}", headers["x-request-id"])


def test_worker_production_requires_super_user_binding(monkeypatch):
    worker = load_worker_module(monkeypatch)

    with pytest.raises(RuntimeError, match="SUPER_USER_USERNAME"):
        worker.Api(
            env=types.SimpleNamespace(
                APP_ENV="production",
                APP_CORS_ALLOW_ORIGINS="https://stock-scanner-beta.pages.dev",
            )
        )


def test_worker_on_fetch_maps_runtime_security_initialization_failure(monkeypatch, capsys):
    worker = load_worker_module(monkeypatch)
    env = types.SimpleNamespace(
        APP_ENV="production",
        APP_CORS_ALLOW_ORIGINS="https://stock-scanner-beta.pages.dev",
        DB=RoutingFakeD1(),
        CACHE=FakeR2Cache(),
    )

    response = asyncio.run(
        worker.on_fetch(
            RouteRequest(
                path="/api/health",
                headers={
                    "authorization": "Bearer hidden",
                    "origin": "https://stock-scanner-beta.pages.dev",
                },
            ),
            env,
        )
    )
    payload = json.loads(response.body)

    assert response.init["status"] == 500
    assert payload == {
        "detail": "伺服器暫時無法處理請求，請稍後再試。",
        "code": "INTERNAL_ERROR",
        "requestId": response.headers["x-request-id"],
        "retryable": False,
        "stage": "worker",
    }
    record = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert record["requestId"] == payload["requestId"]
    assert record["stage"] == "worker"
    assert response.headers["access-control-allow-origin"] == "https://stock-scanner-beta.pages.dev"
    assert response.headers["strict-transport-security"].startswith("max-age=31536000")
    serialized = json.dumps({"payload": payload, "record": record})
    assert "SUPER_USER_USERNAME" not in serialized
    assert "Bearer hidden" not in serialized


def test_worker_production_csrf_guard_requires_custom_header(monkeypatch):
    worker = load_worker_module(monkeypatch)
    api = worker.Api(
        env=types.SimpleNamespace(
            APP_ENV="production",
            APP_CORS_ALLOW_ORIGINS="https://stock-scanner-beta.pages.dev",
            SUPER_USER_USERNAME="pcedison@gmail.com",
        )
    )
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


def test_worker_error_response_adds_safe_metadata_and_request_header(monkeypatch):
    worker = load_worker_module(monkeypatch)

    response = worker.error_response(
        "dependency failed",
        status=503,
        headers={"retry-after": "30"},
        code="DEPENDENCY_UNAVAILABLE",
        request_id="request-123",
        retryable=True,
        stage="r2_read",
    )

    assert response.init["status"] == 503
    assert response.headers["retry-after"] == "30"
    assert response.headers["x-request-id"] == "request-123"
    assert json.loads(response.body) == {
        "detail": "dependency failed",
        "code": "DEPENDENCY_UNAVAILABLE",
        "requestId": "request-123",
        "retryable": True,
        "stage": "r2_read",
    }


def test_worker_manifest_quality_flags_undersized_seed(monkeypatch):
    worker = load_worker_module(monkeypatch)

    bad = worker.manifest_quality({"counts": {"companies": 10, "entry": 1, "watch": 2, "excluded": 3, "analysis": 9}})
    good = worker.manifest_quality(
        {
            "counts": {"companies": 1000, "entry": 10, "watch": 980, "excluded": 10, "analysis": 1000},
            "financialFreshness": {"status": "ok", "blocksDeployment": False},
        }
    )

    assert bad["ok"] is False
    assert bad["problems"]
    assert good["ok"] is True


def test_worker_manifest_quality_flags_blocking_financial_freshness(monkeypatch):
    worker = load_worker_module(monkeypatch)

    quality = worker.manifest_quality(
        {
            "counts": {"companies": 1000, "entry": 10, "watch": 980, "excluded": 10, "analysis": 1000},
            "financialFreshness": {
                "status": "stale",
                "blocksDeployment": True,
                "expectedFinancialPeriod": "2026Q1",
                "latestCachedFinancialPeriod": "2025Q4",
            },
        }
    )

    assert quality["ok"] is False
    assert any("financial freshness" in problem for problem in quality["problems"])


def test_worker_manifest_quality_flags_missing_financial_freshness(monkeypatch):
    worker = load_worker_module(monkeypatch)

    quality = worker.manifest_quality(
        {"counts": {"companies": 1000, "entry": 10, "watch": 980, "excluded": 10, "analysis": 1000}}
    )

    assert quality["ok"] is False
    assert any("financial freshness" in problem for problem in quality["problems"])


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2026-07-12T00:52:48", datetime(2026, 7, 12, 0, 52, 48, tzinfo=UTC)),
        ("2026-07-12T00:52:48Z", datetime(2026, 7, 12, 0, 52, 48, tzinfo=UTC)),
        ("2026-07-12T08:52:48+08:00", datetime(2026, 7, 12, 0, 52, 48, tzinfo=UTC)),
        ("not-a-timestamp", None),
    ],
)
def test_worker_parse_time_normalizes_iso_timestamps_to_utc(monkeypatch, raw, expected):
    worker = load_worker_module(monkeypatch)

    assert worker.parse_time(raw) == expected


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


def _healthy_worker_manifest(generated_at="2026-07-11T21:52:48+00:00"):
    return {
        "generatedAt": generated_at,
        "sourceLastCheckedAt": generated_at,
        "counts": {"companies": 1000, "analysis": 1000, "entry": 10, "watch": 980, "excluded": 10},
        "financialFreshness": {
            "status": "ok",
            "blocksDeployment": False,
            "expectedFinancialPeriod": "2026Q1",
            "latestCachedFinancialPeriod": "2026Q1",
        },
    }


def test_worker_health_is_fresh_before_dynamic_cache_boundary(monkeypatch):
    manifest = _healthy_worker_manifest()
    worker, api, _db = build_router_api(monkeypatch, r2={"public/manifest.json": manifest})
    pin_worker_time(monkeypatch, worker, "2026-07-12T00:52:47+00:00")

    response = asyncio.run(api.fetch(RouteRequest(path="/api/health")))
    payload = json.loads(response.body)

    assert response.init["status"] == 200
    assert payload["status"] == "ok"
    assert payload["cacheStatus"]["isStale"] is False
    assert payload["cacheStatus"]["nextRefreshAfter"].startswith("2026-07-12T00:52:48")
    assert payload["cacheStatus"]["refreshReason"] == "monthly_revenue_window"


def test_worker_health_degrades_at_dynamic_cache_boundary(monkeypatch):
    manifest = _healthy_worker_manifest()
    worker, api, _db = build_router_api(monkeypatch, r2={"public/manifest.json": manifest})
    pin_worker_time(monkeypatch, worker, "2026-07-12T00:52:48+00:00")

    response = asyncio.run(api.fetch(RouteRequest(path="/api/health")))
    payload = json.loads(response.body)

    assert response.init["status"] == 200
    assert payload["status"] == "degraded"
    assert payload["cacheStatus"]["isStale"] is True
    assert payload["cacheStatus"]["nextRefreshAfter"].startswith("2026-07-12T00:52:48")
    assert payload["cacheStatus"]["refreshReason"] == "monthly_revenue_window"


def test_worker_health_degrades_after_dynamic_cache_boundary(monkeypatch):
    manifest = _healthy_worker_manifest()
    worker, api, _db = build_router_api(monkeypatch, r2={"public/manifest.json": manifest})
    pin_worker_time(monkeypatch, worker, "2026-07-12T00:52:49+00:00")

    response = asyncio.run(api.fetch(RouteRequest(path="/api/health")))
    payload = json.loads(response.body)

    assert response.init["status"] == 200
    assert payload["status"] == "degraded"
    assert payload["cacheStatus"]["isStale"] is True


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
    asyncio.run(api.db_run("UPDATE items SET optional_value = ?", None))

    assert run_result["success"] is True
    assert first_result == {"id": 1, "name": "row"}
    assert all_result == [{"id": 1}, {"id": 2}]
    assert fake_db.prepared[0].params == (1,)
    assert fake_db.prepared[1].params == (1,)
    assert fake_db.prepared[3].params == ("",)


def test_worker_db_run_wraps_write_failure_as_nonretryable(monkeypatch):
    worker = load_worker_module(monkeypatch)

    class FailingStatement:
        def bind(self, *params):
            return self

        async def run(self):
            raise RuntimeError("Network connection lost")

    class FailingD1:
        def prepare(self, sql):
            return FailingStatement()

    api = worker.Api(env=types.SimpleNamespace(DB=FailingD1()))

    with pytest.raises(worker.DependencyFailure) as caught:
        asyncio.run(api.db_run("UPDATE items SET value = ?", 1))

    assert caught.value.stage == "d1_write"
    assert caught.value.retryable is False
    assert caught.value.error_type == "RuntimeError"


def test_worker_replace_holdings_wraps_batch_failure_as_nonretryable(monkeypatch):
    worker = load_worker_module(monkeypatch)

    class FailingBatchD1:
        def prepare(self, sql):
            return FakeStatement(sql)

        async def batch(self, statements):
            raise RuntimeError("Network connection lost")

    api = worker.Api(env=types.SimpleNamespace(DB=FailingBatchD1()))

    with pytest.raises(worker.DependencyFailure) as caught:
        asyncio.run(api.replace_holdings(7, []))

    assert caught.value.stage == "d1_write"
    assert caught.value.retryable is False


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


def test_worker_cache_status_exposes_refresh_job_owner_run(monkeypatch):
    worker = load_worker_module(monkeypatch)
    api = worker.Api(env=types.SimpleNamespace(GITHUB_REPOSITORY="pcedison/stock_scanner"))

    async def fake_r2_json(key, fallback):
        assert key == "public/manifest.json"
        return {"generatedAt": "2026-05-22T00:00:00+00:00", "counts": {"companies": 1000, "analysis": 1000}}

    async def fake_db_all(sql, *params):
        assert "owner_run_id" in sql
        return [
            {
                "id": "job-1",
                "job_type": "market_scan",
                "cache_key": "abc",
                "status": "running",
                "reason": "manual",
                "queued_at": "2026-05-22T00:00:00+00:00",
                "started_at": "2026-05-22T00:01:00+00:00",
                "finished_at": None,
                "updated_at": "2026-05-22T00:01:00+00:00",
                "error": None,
                "owner_run_id": "26276779259",
            }
        ]

    api.r2_json = fake_r2_json
    api.db_all = fake_db_all

    payload = asyncio.run(api.cache_status())

    assert payload["recentJobs"][0]["ownerRunId"] == "26276779259"
    assert payload["recentJobs"][0]["ownerRunUrl"] == "https://github.com/pcedison/stock_scanner/actions/runs/26276779259"
    assert payload["recentJobs"][0]["hasError"] is False


def test_worker_cache_status_treats_blank_refresh_job_error_as_empty(monkeypatch):
    worker = load_worker_module(monkeypatch)
    api = worker.Api(env=types.SimpleNamespace(GITHUB_REPOSITORY="pcedison/stock_scanner"))

    payload = api.refresh_job_payload(
        {
            "id": "job-1",
            "job_type": "market_scan",
            "cache_key": "abc",
            "status": "success",
            "reason": "manual",
            "queued_at": "2026-05-22T00:00:00+00:00",
            "started_at": "2026-05-22T00:01:00+00:00",
            "finished_at": "2026-05-22T00:02:00+00:00",
            "updated_at": "2026-05-22T00:02:00+00:00",
            "error": "",
            "owner_run_id": "26276779259",
        }
    )

    assert payload["hasError"] is False
    assert "error" not in payload


def test_worker_session_cookie_allows_cross_site_pages_fetch(monkeypatch):
    worker = load_worker_module(monkeypatch)

    assert "SameSite=None" in worker.session_cookie("token")
    assert "Secure" in worker.session_cookie("token")
    assert "SameSite=None" in worker.clear_session_cookie()


def test_worker_cache_status_redacts_refresh_job_error(monkeypatch):
    worker = load_worker_module(monkeypatch)
    api = worker.Api(env=types.SimpleNamespace(GITHUB_REPOSITORY="pcedison/stock_scanner"))

    async def fake_r2_json(key, fallback):
        assert key == "public/manifest.json"
        return {"generatedAt": "2026-05-22T00:00:00+00:00", "counts": {"companies": 1000, "analysis": 1000}}

    async def fake_db_all(sql, *params):
        return [
            {
                "id": "job-1",
                "job_type": "market_scan",
                "cache_key": "abc",
                "status": "failed",
                "reason": "manual",
                "queued_at": "2026-05-22T00:00:00+00:00",
                "started_at": "2026-05-22T00:01:00+00:00",
                "finished_at": "2026-05-22T00:02:00+00:00",
                "updated_at": "2026-05-22T00:02:00+00:00",
                "error": "RuntimeError: private cache path",
                "owner_run_id": "26276779259",
            }
        ]

    api.r2_json = fake_r2_json
    api.db_all = fake_db_all

    payload = asyncio.run(api.cache_status())

    job = payload["recentJobs"][0]
    assert job["hasError"] is True
    assert "error" not in job
    assert "private cache path" not in str(payload)


def test_worker_market_scan_uses_precomputed_summary(monkeypatch):
    worker = load_worker_module(monkeypatch)
    cache = FakeR2Cache(
        {
            "public/manifest.json": FakeR2Object('{"generatedAt":"2026-05-19T00:00:00+00:00"}'),
            "public/market_scan_summary.json": FakeR2Object(
                json.dumps(
                    {
                        "generatedAt": "2026-05-19T00:00:00+00:00",
                        "dataSource": "cloudflare_r2_seed",
                        "entry": [{"stockCode": "1234", "companyName": "Demo", "status": "ENTRY", "summary": "ok"}],
                        "watch": [],
                        "excluded": [],
                    }
                )
            ),
            "public/market_scan_latest.json": FakeR2Object('{"entry":[{"stockCode":"9999"}]}'),
        }
    )
    api = worker.Api(env=types.SimpleNamespace(CACHE=cache))

    refresh_forces = []

    async def fake_refresh_job(manifest, force=False):
        refresh_forces.append(force)
        return {"status": "fresh", "reason": "test"}

    api.ensure_refresh_job = fake_refresh_job

    request = SingleReadRequest('{"refreshMode":"force"}')
    request.method = "POST"
    response = asyncio.run(api.route(request, "/api/scan/market", {}))
    payload = json.loads(response.body)

    assert payload["entry"][0]["stockCode"] == "1234"
    assert payload["detailMode"] == "summary"
    assert payload["cacheStatus"]["refreshStatus"] == "fresh"
    assert refresh_forces == [True]
    assert "public/market_scan_latest.json" not in cache.calls


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
    assert batch[1].params[4] == ""
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
    assert calls[0][1][4] == ""


def test_worker_empty_market_scan_shape(monkeypatch):
    worker = load_worker_module(monkeypatch)
    scan = worker.empty_market_scan()
    assert scan["entry"] == []
    assert scan["watch"] == []
    assert scan["excluded"] == []
    assert scan["universeSize"] == 0
    assert scan["dataSource"] == "cloudflare_r2_seed"


def test_worker_compact_scan_result_trims_to_summary_fields(monkeypatch):
    worker = load_worker_module(monkeypatch)
    compact = worker.compact_scan_result(
        {
            "stockCode": "2330",
            "companyName": "台積電",
            "status": "ENTRY",
            "summary": "ok",
            "extra": "dropped",
            "reasons": [
                {"code": "E1", "title": "t", "passed": True, "severity": "info", "message": "m", "evidence": "drop"},
                "not-a-dict",
            ],
        }
    )
    assert compact["stockCode"] == "2330"
    assert "extra" not in compact
    assert compact["reasons"] == [{"code": "E1", "title": "t", "passed": True, "severity": "info", "message": "m"}]
    assert compact["detailsAvailable"] is True
    assert compact["hasFullDetails"] is False


def test_worker_compact_market_scan_falls_back_for_non_dict(monkeypatch):
    worker = load_worker_module(monkeypatch)
    fallback = worker.compact_market_scan(None)
    assert fallback["entry"] == []
    assert "detailMode" not in fallback  # returns the empty scan verbatim

    compact = worker.compact_market_scan(
        {"entry": [{"stockCode": "1", "summary": "s", "reasons": []}], "watch": [], "excluded": []}
    )
    assert compact["detailMode"] == "summary"
    assert compact["entry"][0]["hasFullDetails"] is False


def test_worker_report_response_renders_markdown_and_csv(monkeypatch):
    worker = load_worker_module(monkeypatch)
    payload = {
        "generatedAt": "2026-05-19T00:00:00+00:00",
        "dataSource": "cloudflare_r2_seed",
        "entry": [{"stockCode": "2330", "companyName": "台積電", "status": "ENTRY", "summary": "好"}],
        "watch": [],
        "excluded": [],
    }

    markdown = worker.report_response(payload, "markdown", "台股市場掃描報告", "market_scan")
    assert "# 台股市場掃描報告" in markdown.body
    assert "2330 台積電：好" in markdown.body
    assert markdown.init["headers"]["content-type"] == "text/markdown; charset=utf-8"
    assert ".md" in markdown.init["headers"]["content-disposition"]

    csv_report = worker.report_response(payload, "csv", "台股市場掃描報告", "market_scan")
    assert "category,stockCode,companyName,status,summary" in csv_report.body
    assert "entry,2330,台積電,ENTRY,好" in csv_report.body
    assert csv_report.init["headers"]["content-type"] == "text/csv; charset=utf-8"


# --------------------------------------------------------------------------- #
# Stateful D1 harness — interprets the exact queries worker.py issues so every
# DB-backed route runs end-to-end through Api.fetch (auth, holdings, admin,
# settings, refresh jobs). Pairs with FakeR2Cache for the R2-seed read paths.
# --------------------------------------------------------------------------- #
class RoutingFakeStatement:
    def __init__(self, db, sql):
        self.db = db
        self.sql = " ".join(sql.split())
        self.params = ()

    def bind(self, *params):
        self.params = params
        return self

    async def run(self):
        return await self.db.run_sql(self.sql, self.params)

    async def first(self):
        return self.db.first_sql(self.sql, self.params)

    async def all(self):
        return {"results": self.db.all_sql(self.sql, self.params)}


class RoutingFakeD1:
    def __init__(self):
        self.users = []
        self.sessions = []
        self.holdings = []
        self.app_kv = {}
        self.auth_attempts = []
        self.refresh_jobs = []
        self._next_user_id = 1
        self._refresh_insert_lock = asyncio.Lock()
        self.complete_active_after_idempotency_miss = False
        self.hide_refresh_job_reads = False
        self.refresh_insert_attempts = 0
        self.prepare_calls = []

    def prepare(self, sql):
        self.prepare_calls.append(" ".join(sql.split()))
        return RoutingFakeStatement(self, sql)

    async def batch(self, statements):
        return [await statement.run() for statement in statements]

    def _user_by_name(self, username):
        return next((u for u in self.users if u["username"] == username), None)

    def _attempt(self, identifier):
        return next((a for a in self.auth_attempts if a["identifier"] == identifier), None)

    def _refresh_time(self, job):
        return str(job.get("finished_at") or job.get("updated_at") or job["queued_at"]).replace(" ", "T")

    def first_sql(self, sql, params):
        if "FROM sessions" in sql and "JOIN users" in sql:
            token, now = params
            session = next((s for s in self.sessions if s["token_hash"] == token and s["expires_at"] > now), None)
            if not session:
                return None
            user = next((u for u in self.users if u["id"] == session["user_id"]), None)
            return None if user is None else {"id": user["id"], "username": user["username"], "display_name": user["display_name"]}
        if "SELECT id FROM users WHERE username" in sql:
            user = self._user_by_name(params[0])
            return None if user is None else {"id": user["id"]}
        if "password_hash FROM users WHERE username" in sql:
            user = self._user_by_name(params[0])
            return None if user is None else dict(user)
        if "id, username, display_name FROM users WHERE username" in sql:
            user = self._user_by_name(params[0])
            return None if user is None else {"id": user["id"], "username": user["username"], "display_name": user["display_name"]}
        if "SELECT id, username FROM users WHERE id" in sql:
            user = next((u for u in self.users if u["id"] == params[0]), None)
            return None if user is None else {"id": user["id"], "username": user["username"]}
        if "FROM app_kv WHERE key" in sql:
            return None if params[0] not in self.app_kv else {"value": self.app_kv[params[0]]}
        if "SELECT locked_until FROM auth_attempts" in sql:
            attempt = self._attempt(params[0])
            return None if attempt is None else {"locked_until": attempt.get("locked_until")}
        if "failure_count, first_failed_at FROM auth_attempts" in sql:
            attempt = self._attempt(params[0])
            return None if attempt is None else {"failure_count": attempt["failure_count"], "first_failed_at": attempt["first_failed_at"]}
        if "FROM refresh_jobs" in sql and "WHERE idempotency_key = ?" in sql:
            if self.hide_refresh_job_reads:
                return None
            row = next((dict(job) for job in self.refresh_jobs if job.get("idempotency_key") == params[0]), None)
            if row is None and self.complete_active_after_idempotency_miss:
                self.complete_active_after_idempotency_miss = False
                for job in self.refresh_jobs:
                    if job["status"] in {"queued", "running"}:
                        job["status"] = "success"
            return row
        if "SELECT * FROM refresh_jobs" in sql and "WHERE id = ?" in sql:
            if self.hide_refresh_job_reads:
                return None
            job_id, job_type = params
            return next(
                (dict(job) for job in self.refresh_jobs if job["id"] == job_id and job["job_type"] == job_type),
                None,
            )
        if "FROM refresh_jobs" in sql and "cache_key = ?" in sql and "status IN ('queued', 'running')" in sql:
            if self.hide_refresh_job_reads:
                return None
            job_type, cache_key = params
            return next(
                (
                    dict(job)
                    for job in sorted(self.refresh_jobs, key=lambda row: (row["queued_at"], row["id"]), reverse=True)
                    if job["job_type"] == job_type
                    and job["cache_key"] == cache_key
                    and job["status"] in {"queued", "running"}
                ),
                None,
            )
        if "FROM refresh_jobs" in sql and "status IN ('success', 'failed')" in sql:
            if self.hide_refresh_job_reads:
                return None
            job_type, cutoff = params
            cutoff = str(cutoff).replace(" ", "T")
            return next(
                (
                    dict(job)
                    for job in sorted(
                        self.refresh_jobs,
                        key=self._refresh_time,
                        reverse=True,
                    )
                    if job["job_type"] == job_type
                    and job["status"] in {"success", "failed"}
                    and self._refresh_time(job) >= cutoff
                ),
                None,
            )
        if "FROM refresh_jobs" in sql and "status IN ('queued', 'running')" in sql:
            if self.hide_refresh_job_reads:
                return None
            job_type = params[0]
            cutoff = params[1] if len(params) > 1 else ""
            return next(
                (j for j in sorted(self.refresh_jobs, key=lambda r: r["queued_at"], reverse=True)
                 if j["job_type"] == job_type and j["status"] in {"queued", "running"} and j["queued_at"] > cutoff),
                None,
            )
        raise AssertionError(f"Unhandled first() SQL: {sql}")

    def all_sql(self, sql, params):
        if "FROM holdings WHERE user_id" in sql and "stock_code, name, shares" in sql:
            rows = sorted((h for h in self.holdings if h["user_id"] == params[0]), key=lambda h: h["stock_code"])
            return [{"stock_code": h["stock_code"], "name": h["name"], "shares": h["shares"], "average_cost": h["average_cost"]} for h in rows]
        if "FROM users" in sql and "LEFT JOIN holdings" in sql:
            now, super_user = params
            ordered = sorted(self.users, key=lambda u: 0 if u["username"] == super_user else 1)
            return [self._admin_row(user, now) for user in ordered]
        if "FROM refresh_jobs" in sql and "ORDER BY queued_at DESC" in sql:
            jobs = sorted(self.refresh_jobs, key=lambda r: r["queued_at"], reverse=True)[:10]
            return [dict(j) for j in jobs]
        raise AssertionError(f"Unhandled all() SQL: {sql}")

    def _admin_row(self, user, now):
        codes = {h["stock_code"] for h in self.holdings if h["user_id"] == user["id"]}
        sessions = {s["token_hash"] for s in self.sessions if s["user_id"] == user["id"] and s["expires_at"] > now}
        return {
            "id": user["id"],
            "username": user["username"],
            "display_name": user["display_name"],
            "created_at": user["created_at"],
            "holdings_count": len(codes),
            "active_session_count": len(sessions),
        }

    async def run_sql(self, sql, params):
        if "INSERT INTO users" in sql:
            username, display_name, password_hash, created_at = params
            self.users.append({"id": self._next_user_id, "username": username, "display_name": display_name, "password_hash": password_hash, "created_at": created_at})
            self._next_user_id += 1
        elif "INSERT INTO sessions" in sql:
            token_hash, user_id, created_at, expires_at = params
            self.sessions.append({"token_hash": token_hash, "user_id": user_id, "created_at": created_at, "expires_at": expires_at})
        elif "DELETE FROM sessions WHERE token_hash" in sql:
            self.sessions = [s for s in self.sessions if s["token_hash"] != params[0]]
        elif "INSERT INTO holdings" in sql:
            user_id, stock_code, name, shares, average_cost, created_at, updated_at = params
            existing = next((h for h in self.holdings if h["user_id"] == user_id and h["stock_code"] == stock_code), None)
            if existing:
                existing.update({"name": name, "shares": shares, "average_cost": average_cost, "updated_at": updated_at})
            else:
                self.holdings.append({"user_id": user_id, "stock_code": stock_code, "name": name, "shares": shares, "average_cost": average_cost, "created_at": created_at, "updated_at": updated_at})
        elif "DELETE FROM holdings WHERE user_id" in sql and "stock_code" in sql:
            user_id, stock_code = params
            self.holdings = [h for h in self.holdings if not (h["user_id"] == user_id and h["stock_code"] == stock_code)]
        elif "DELETE FROM holdings WHERE user_id" in sql:
            self.holdings = [h for h in self.holdings if h["user_id"] != params[0]]
        elif "DELETE FROM users WHERE id" in sql:
            self.users = [u for u in self.users if u["id"] != params[0]]
        elif "INSERT INTO app_kv" in sql:
            key, value, _updated = params
            self.app_kv[key] = value
        elif "DELETE FROM auth_attempts" in sql and "first_failed_at <=" in sql:
            stale_before = params[0]
            self.auth_attempts = [a for a in self.auth_attempts if a.get("locked_until") or a["first_failed_at"] > stale_before]
        elif "DELETE FROM auth_attempts WHERE identifier" in sql:
            self.auth_attempts = [a for a in self.auth_attempts if a["identifier"] != params[0]]
        elif "INSERT INTO auth_attempts" in sql:
            identifier, failure_count, first_failed_at, last_failed_at, locked_until = params
            row = {"identifier": identifier, "failure_count": failure_count, "first_failed_at": first_failed_at, "last_failed_at": last_failed_at, "locked_until": locked_until}
            attempt = self._attempt(identifier)
            attempt.update(row) if attempt else self.auth_attempts.append(row)
        elif "DELETE FROM refresh_jobs WHERE queued_at" in sql:
            self.refresh_jobs = [j for j in self.refresh_jobs if j["queued_at"] >= params[0]]
        elif "INSERT OR IGNORE INTO refresh_jobs" in sql:
            job_id, job_type, cache_key, idempotency_key, reason, queued_at, updated_at = params[:7]
            terminal_cutoff = str(params[-1] if len(params) > 7 else "").replace(" ", "T")
            async with self._refresh_insert_lock:
                await asyncio.sleep(0)
                self.refresh_insert_attempts += 1
                conflict = any(
                    (idempotency_key is not None and job.get("idempotency_key") == idempotency_key)
                    or (job["job_type"] == job_type and job["status"] in {"queued", "running"})
                    or (
                        job["job_type"] == job_type
                        and job["status"] in {"success", "failed"}
                        and self._refresh_time(job) >= terminal_cutoff
                    )
                    for job in self.refresh_jobs
                )
                if conflict:
                    return {"success": True, "meta": {"changes": 0}}
                self.refresh_jobs.append(
                    {
                        "id": job_id,
                        "job_type": job_type,
                        "cache_key": cache_key,
                        "idempotency_key": idempotency_key,
                        "status": "queued",
                        "reason": reason,
                        "queued_at": queued_at,
                        "updated_at": updated_at,
                    }
                )
        elif "INSERT INTO refresh_jobs" in sql:
            await asyncio.sleep(0)
            keys = ("id", "job_type", "cache_key", "status", "reason", "queued_at", "updated_at")
            self.refresh_jobs.append(dict(zip(keys, params, strict=True)))
        else:
            raise AssertionError(f"Unhandled run() SQL: {sql}")
        return {"success": True, "meta": {"changes": 1}}

    def active_job_count(self, job_type, cache_key=None):
        return sum(
            1
            for job in self.refresh_jobs
            if job["job_type"] == job_type
            and job["status"] in {"queued", "running"}
            and (cache_key is None or job["cache_key"] == cache_key)
        )


def r2_seed(mapping):
    return {
        key: FakeR2Object(
            value
            if isinstance(value, str)
            else json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        )
        for key, value in mapping.items()
    }


def build_router_api(monkeypatch, *, r2=None, super_user=None, github_repo=None):
    worker = load_worker_module(monkeypatch)
    db = RoutingFakeD1()
    env_kwargs = {"DB": db, "CACHE": FakeR2Cache(r2_seed(r2 or {}))}
    if super_user is not None:
        env_kwargs["SUPER_USER_USERNAME"] = super_user
    if github_repo is not None:
        env_kwargs["GITHUB_REPOSITORY"] = github_repo
    api = worker.Api(env=types.SimpleNamespace(**env_kwargs))
    return worker, api, db


def pin_worker_time(monkeypatch, worker, iso):
    fixed = datetime.fromisoformat(iso)

    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed.astimezone(tz) if tz is not None else fixed.replace(tzinfo=None)

    monkeypatch.setattr(worker, "datetime", FixedDatetime)
    return fixed


class RouteRequest:
    def __init__(self, method="GET", path="/api/health", headers=None, body=""):
        self.method = method
        self.url = f"https://stock-scanner-beta-api.example{path}"
        self.headers = headers or {}
        self._body = body
        self.text_reads = 0

    async def text(self):
        self.text_reads += 1
        return self._body


def cookie_header(response):
    return {"cookie": response.headers["set-cookie"].split(";", 1)[0]}


def register_user(api, username, password="supersecret", display_name=None, source="203.0.113.7"):
    response = asyncio.run(api.fetch(RouteRequest(
        method="POST",
        path="/api/auth/register",
        headers={"x-forwarded-for": source},
        body=json.dumps({"username": username, "password": password, "displayName": display_name or username}),
    )))
    return response


def test_worker_on_fetch_entrypoint_serves_health(monkeypatch):
    worker, _api, _db = build_router_api(monkeypatch, r2={"public/manifest.json": {"counts": {"companies": 1000, "analysis": 1000}}})
    pin_worker_time(monkeypatch, worker, "2026-02-20T00:00:00+00:00")
    env = types.SimpleNamespace(
        DB=RoutingFakeD1(),
        CACHE=FakeR2Cache(
            r2_seed(
                {
                    "public/manifest.json": {
                        "generatedAt": "2026-02-19T23:00:00+00:00",
                        "counts": {"companies": 1000, "entry": 10, "watch": 980, "excluded": 10, "analysis": 1000},
                        "financialFreshness": {"status": "ok", "blocksDeployment": False},
                    }
                }
            )
        ),
    )

    response = asyncio.run(worker.on_fetch(RouteRequest(path="/api/health"), env))

    assert response.init["status"] == 200
    assert response.headers["x-frame-options"] == "DENY"
    assert re.fullmatch(r"[0-9a-f]{24}", response.headers["x-request-id"])
    assert json.loads(response.body)["status"] == "ok"


def test_worker_fetch_generates_a_new_request_id_per_invocation(monkeypatch):
    _worker, api, _db = build_router_api(monkeypatch)

    first = asyncio.run(api.fetch(RouteRequest(path="/api/health")))
    second = asyncio.run(api.fetch(RouteRequest(path="/api/health")))

    assert re.fullmatch(r"[0-9a-f]{24}", first.headers["x-request-id"])
    assert re.fullmatch(r"[0-9a-f]{24}", second.headers["x-request-id"])
    assert first.headers["x-request-id"] != second.headers["x-request-id"]


def test_worker_set_response_header_supports_js_headers(monkeypatch):
    worker = load_worker_module(monkeypatch)
    captured = {}

    class JsHeaders:
        def set(self, key, value):
            captured[key] = value

    worker.set_response_header(types.SimpleNamespace(headers=JsHeaders()), "x-test", "1")
    assert captured == {"x-test": "1"}


def test_worker_db_all_handles_list_and_missing_results(monkeypatch):
    worker = load_worker_module(monkeypatch)

    class Stmt:
        def __init__(self, value):
            self.value = value

        def bind(self, *params):
            return self

        async def all(self):
            return self.value

    class DB:
        def __init__(self, value):
            self.value = value

        def prepare(self, sql):
            return Stmt(self.value)

    api_list = worker.Api(env=types.SimpleNamespace(DB=DB([{"id": 1}])))
    api_none = worker.Api(env=types.SimpleNamespace(DB=DB(None)))

    assert asyncio.run(api_list.db_all("SELECT 1")) == [{"id": 1}]
    assert asyncio.run(api_none.db_all("SELECT 1")) == []


def test_worker_request_json_empty_body_and_non_object(monkeypatch):
    worker, api, _db = build_router_api(monkeypatch)

    assert asyncio.run(api.request_json(RouteRequest(body=""))) == {}

    non_object = asyncio.run(api.fetch(RouteRequest(method="POST", path="/api/scan/holdings", body="[1, 2]")))
    assert non_object.init["status"] == 400
    assert json.loads(non_object.body)["detail"] == "JSON 內容需為物件"


def test_worker_auth_and_holdings_lifecycle(monkeypatch):
    worker, api, _db = build_router_api(monkeypatch)

    register = register_user(api, "trader@example.com", display_name="Trader")
    assert register.init["status"] == 200
    body = json.loads(register.body)
    assert body["authenticated"] is True
    assert body["user"]["username"] == "trader@example.com"
    assert body["user"]["isSuperUser"] is False
    cookie = cookie_header(register)

    me = asyncio.run(api.fetch(RouteRequest(path="/api/auth/me", headers=cookie)))
    assert json.loads(me.body)["authenticated"] is True

    asyncio.run(api.fetch(RouteRequest(
        method="POST", path="/api/me/holdings", headers=cookie,
        body=json.dumps({"holding": {"stockCode": "2330", "name": "台積電", "shares": 1000, "averageCost": 600}}),
    )))
    listed = asyncio.run(api.fetch(RouteRequest(path="/api/me/holdings", headers=cookie)))
    holdings = json.loads(listed.body)["holdings"]
    assert [h["stockCode"] for h in holdings] == ["2330"]
    assert holdings[0]["averageCost"] == 600

    asyncio.run(api.fetch(RouteRequest(
        method="PUT", path="/api/me/holdings", headers=cookie,
        body=json.dumps({"holdings": [{"stockCode": "2454", "name": "聯發科", "shares": 500, "averageCost": None}]}),
    )))
    after_put = json.loads(asyncio.run(api.fetch(RouteRequest(path="/api/me/holdings", headers=cookie))).body)["holdings"]
    assert [h["stockCode"] for h in after_put] == ["2454"]
    assert after_put[0]["averageCost"] is None

    asyncio.run(api.fetch(RouteRequest(method="DELETE", path="/api/me/holdings/2454", headers=cookie)))
    assert json.loads(asyncio.run(api.fetch(RouteRequest(path="/api/me/holdings", headers=cookie))).body)["holdings"] == []

    logout = asyncio.run(api.fetch(RouteRequest(method="POST", path="/api/auth/logout", headers=cookie)))
    assert json.loads(logout.body)["authenticated"] is False
    assert json.loads(asyncio.run(api.fetch(RouteRequest(path="/api/auth/me", headers=cookie))).body)["authenticated"] is False


def test_worker_me_routes_require_authentication(monkeypatch):
    worker, api, _db = build_router_api(monkeypatch)

    response = asyncio.run(api.fetch(RouteRequest(path="/api/me/holdings")))
    assert response.init["status"] == 401
    assert json.loads(response.body)["detail"] == "請先登入"


def test_worker_login_success_and_failure_lockout(monkeypatch):
    worker, api, _db = build_router_api(monkeypatch)
    register_user(api, "locktest@example.com", password="supersecret")

    ok = asyncio.run(api.fetch(RouteRequest(
        method="POST", path="/api/auth/login",
        body=json.dumps({"username": "locktest@example.com", "password": "supersecret"}),
    )))
    assert ok.init["status"] == 200
    assert json.loads(ok.body)["authenticated"] is True

    wrong = {"username": "locktest@example.com", "password": "wrong-password"}
    statuses = []
    for _ in range(6):
        resp = asyncio.run(api.fetch(RouteRequest(method="POST", path="/api/auth/login", body=json.dumps(wrong))))
        statuses.append(resp.init["status"])
    assert 401 in statuses
    assert statuses[-1] == 429
    locked = asyncio.run(api.fetch(RouteRequest(method="POST", path="/api/auth/login", body=json.dumps(wrong))))
    assert locked.init["status"] == 429
    assert locked.headers["retry-after"].isdigit()


def test_worker_login_rejects_invalid_username(monkeypatch):
    worker, api, _db = build_router_api(monkeypatch)
    response = asyncio.run(api.fetch(RouteRequest(
        method="POST", path="/api/auth/login",
        body=json.dumps({"username": "bad name", "password": "supersecret"}),
    )))
    assert response.init["status"] == 401
    assert json.loads(response.body)["detail"] == "帳號或密碼錯誤"


def test_worker_register_validates_and_rejects_duplicates(monkeypatch):
    worker, api, _db = build_router_api(monkeypatch)

    weak = asyncio.run(api.fetch(RouteRequest(
        method="POST", path="/api/auth/register",
        body=json.dumps({"username": "dup@example.com", "password": "short"}),
    )))
    assert weak.init["status"] == 400
    assert "密碼" in json.loads(weak.body)["detail"]

    assert register_user(api, "dup@example.com").init["status"] == 200
    duplicate = register_user(api, "dup@example.com")
    assert duplicate.init["status"] == 400
    assert json.loads(duplicate.body)["detail"] == "帳號已存在"


def test_worker_admin_user_management(monkeypatch):
    worker, api, _db = build_router_api(monkeypatch, super_user="admin@example.com")
    admin = register_user(api, "admin@example.com", source="198.51.100.1")
    admin_cookie = cookie_header(admin)
    assert json.loads(admin.body)["user"]["isSuperUser"] is True
    member = register_user(api, "member@example.com", source="198.51.100.2")
    member_cookie = cookie_header(member)
    member_id = json.loads(member.body)["user"]["id"]

    listing = asyncio.run(api.fetch(RouteRequest(path="/api/admin/users", headers=admin_cookie)))
    payload = json.loads(listing.body)
    assert payload["superUser"] == "admin@example.com"
    usernames = {row["username"]: row for row in payload["users"]}
    assert usernames["admin@example.com"]["isSuperUser"] is True
    assert usernames["admin@example.com"]["canDelete"] is False
    assert usernames["member@example.com"]["canDelete"] is True

    forbidden = asyncio.run(api.fetch(RouteRequest(path="/api/admin/users", headers=member_cookie)))
    assert forbidden.init["status"] == 403

    deleted = asyncio.run(api.fetch(RouteRequest(method="DELETE", path=f"/api/admin/users/{member_id}", headers=admin_cookie)))
    assert deleted.init["status"] == 200
    assert "member@example.com" not in {row["username"] for row in json.loads(deleted.body)["users"]}

    missing = asyncio.run(api.fetch(RouteRequest(method="DELETE", path="/api/admin/users/999", headers=admin_cookie)))
    assert missing.init["status"] == 404

    admin_id = json.loads(admin.body)["user"]["id"]
    self_delete = asyncio.run(api.fetch(RouteRequest(method="DELETE", path=f"/api/admin/users/{admin_id}", headers=admin_cookie)))
    assert self_delete.init["status"] == 400
    assert "super user" in json.loads(self_delete.body)["detail"]


def test_worker_settings_get_put_roundtrip_and_validation(monkeypatch):
    worker, api, _db = build_router_api(monkeypatch, super_user="admin@example.com")
    cookie = cookie_header(register_user(api, "admin@example.com"))

    default = json.loads(asyncio.run(api.fetch(RouteRequest(path="/api/settings"))).body)
    assert default["auto_scan_full_market"] is True

    put = asyncio.run(api.fetch(RouteRequest(
        method="PUT", path="/api/settings", headers=cookie,
        body=json.dumps({**default, "auto_scan_full_market": False, "revenue_growth_mode": "monthly"}),
    )))
    assert put.init["status"] == 200
    stored = json.loads(asyncio.run(api.fetch(RouteRequest(path="/api/settings"))).body)
    assert stored["auto_scan_full_market"] is False
    assert stored["revenue_growth_mode"] == "monthly"

    invalid = asyncio.run(api.fetch(RouteRequest(
        method="PUT", path="/api/settings", headers=cookie,
        body=json.dumps({**default, "revenue_growth_mode": "nope"}),
    )))
    assert invalid.init["status"] == 422


def test_worker_settings_get_falls_back_on_corrupt_value(monkeypatch):
    worker, api, db = build_router_api(monkeypatch)
    db.app_kv["settings"] = "{not json"
    assert asyncio.run(api.get_settings()) == worker.DEFAULT_SETTINGS


def test_worker_companies_list_and_search(monkeypatch):
    companies = {"items": [
        {"stockCode": "2330", "name": "台積電", "industryName": "半導體"},
        {"stockCode": "2454", "name": "聯發科", "industryName": "半導體"},
        {"stockCode": "2603", "name": "長榮", "industryName": "航運"},
    ]}
    worker, api, _db = build_router_api(monkeypatch, r2={"public/companies.json": companies})

    page = json.loads(asyncio.run(api.fetch(RouteRequest(path="/api/companies?page=1&limit=2"))).body)
    assert [c["stockCode"] for c in page["items"]] == ["2330", "2454"]
    assert page["total"] == 3
    assert page["hasMore"] is True

    bad = asyncio.run(api.fetch(RouteRequest(path="/api/companies?page=abc")))
    assert bad.init["status"] == 400

    search = json.loads(asyncio.run(api.fetch(RouteRequest(path="/api/companies/search?q=航運"))).body)
    assert [c["stockCode"] for c in search["items"]] == ["2603"]
    empty_q = json.loads(asyncio.run(api.fetch(RouteRequest(path="/api/companies/search?q="))).body)
    assert len(empty_q["items"]) == 3


def test_worker_scan_market_get_and_post_queue_refresh(monkeypatch):
    manifest = {"generatedAt": "2026-02-19T00:00:00+00:00", "counts": {"companies": 1000, "analysis": 1000}}
    scan = {"entry": [{"stockCode": "2330", "status": "ENTRY", "summary": "好"}], "watch": [], "excluded": []}
    worker, api, db = build_router_api(monkeypatch, r2={"public/manifest.json": manifest, "public/market_scan_summary.json": scan})
    pin_worker_time(monkeypatch, worker, "2026-02-20T00:00:00+00:00")

    get = json.loads(asyncio.run(api.fetch(RouteRequest(path="/api/scan/market"))).body)
    assert get["detailMode"] == "summary"
    assert get["cacheStatus"]["isStale"] is True

    post = json.loads(asyncio.run(api.fetch(RouteRequest(method="POST", path="/api/scan/market", body=""))).body)
    assert post["cacheStatus"]["refreshStatus"] == "queued"
    assert len(db.refresh_jobs) == 1


def _worker_market_generation(prefix: str, size: int = 150):
    return build_market_generation(
        {
            "generatedAt": f"2026-07-13T00:00:0{prefix}+00:00",
            "filingContext": {},
            "entry": [],
            "watch": [
                {
                    "stockCode": f"{prefix}{index:03d}",
                    "companyName": f"Company {prefix}-{index}",
                    "status": "WATCH",
                    "summary": "announced",
                    "reasons": [],
                    "detailsAvailable": True,
                    "hasFullDetails": False,
                }
                for index in range(size)
            ],
            "excluded": [],
        }
    )


def _worker_generation_r2(generation):
    return {
        key: value.decode("utf-8")
        for key, value in generation.files.items()
    }


def test_worker_market_v2_index_uses_pointer_and_manifest_without_legacy_summary(monkeypatch):
    generation = _worker_market_generation("1")
    manifest = {"generatedAt": "2026-07-13T00:00:01+00:00", "counts": {"analysis": 150, "companies": 150}}
    worker, api, _db = build_router_api(
        monkeypatch,
        r2={"public/market_scan_index.json": generation.index, "public/manifest.json": manifest},
    )
    pin_worker_time(monkeypatch, worker, "2026-07-13T00:05:00+00:00")

    response = asyncio.run(api.fetch(RouteRequest(path="/api/scan/market/index")))
    payload = json.loads(response.body)

    assert response.init["status"] == 200
    assert payload["generationId"] == generation.generation_id
    assert payload["schemaVersion"] == 2
    assert "cacheStatus" in payload
    assert response.headers["cache-control"] == "no-store"
    assert api.env.CACHE.calls == ["public/market_scan_index.json", "public/manifest.json"]
    assert "public/market_scan_summary.json" not in api.env.CACHE.calls


def test_worker_market_v2_results_reads_pinned_generation_across_pointer_switch(monkeypatch):
    historical = _worker_market_generation("1")
    current = _worker_market_generation("2")
    r2 = {
        "public/market_scan_index.json": current.index,
        **_worker_generation_r2(historical),
        **_worker_generation_r2(current),
    }
    _worker, api, _db = build_router_api(monkeypatch, r2=r2)
    path = (
        "/api/scan/market/results?disclosure=announced&category=watch&cursor=96&limit=10"
        f"&generationId={historical.generation_id}"
    )

    response = asyncio.run(api.fetch(RouteRequest(path=path)))
    payload = json.loads(response.body)

    assert response.init["status"] == 200
    assert payload["generationId"] == historical.generation_id
    assert response.headers["cache-control"] == "no-store"
    assert [item["stockCode"] for item in payload["items"]] == [f"1{index:03d}" for index in range(96, 106)]
    assert api.env.CACHE.calls[0] == f"public/market_scan/v2/{historical.generation_id}/index.json"
    assert "public/market_scan_index.json" not in api.env.CACHE.calls
    assert "public/market_scan_summary.json" not in api.env.CACHE.calls
    assert len(api.env.CACHE.calls) == 3


def test_worker_market_v2_maps_invalid_query_missing_generation_and_page(monkeypatch):
    generation = _worker_market_generation("1")
    _worker, invalid_api, _db = build_router_api(monkeypatch, r2={"public/market_scan_index.json": generation.index})
    invalid = asyncio.run(
        invalid_api.fetch(
            RouteRequest(path="/api/scan/market/results?disclosure=other&category=watch&cursor=0&limit=100")
        )
    )
    assert invalid.init["status"] == 422
    blank_generation = asyncio.run(
        invalid_api.fetch(
            RouteRequest(
                path="/api/scan/market/results?disclosure=announced&category=watch&cursor=0&limit=1&generationId="
            )
        )
    )
    duplicate_blank_generation = asyncio.run(
        invalid_api.fetch(
            RouteRequest(
                path=(
                    "/api/scan/market/results?disclosure=announced&category=watch&cursor=0&limit=1"
                    f"&generationId={generation.generation_id}&generationId="
                )
            )
        )
    )
    assert blank_generation.init["status"] == duplicate_blank_generation.init["status"] == 422

    for cursor, limit in (("0" * 5000, "1"), ("0", "0" * 5000)):
        oversized_zeroes = asyncio.run(
            invalid_api.fetch(
                RouteRequest(
                    path=(
                        "/api/scan/market/results?disclosure=announced&category=watch"
                        f"&cursor={cursor}&limit={limit}"
                    )
                )
            )
        )
        assert oversized_zeroes.init["status"] == 422

    _worker, missing_generation_api, _db = build_router_api(monkeypatch)
    mismatch = asyncio.run(
        missing_generation_api.fetch(
            RouteRequest(
                path=(
                    "/api/scan/market/results?disclosure=announced&category=watch&cursor=0&limit=100"
                    f"&generationId={generation.generation_id}"
                )
            )
        )
    )
    assert mismatch.init["status"] == 409
    assert json.loads(mismatch.body)["code"] == "generation_mismatch"

    other_generation = _worker_market_generation("2")
    wrong_index_key = f"public/market_scan/v2/{generation.generation_id}/index.json"
    _worker, wrong_index_api, _db = build_router_api(
        monkeypatch,
        r2={wrong_index_key: other_generation.index},
    )
    wrong_index = asyncio.run(
        wrong_index_api.fetch(
            RouteRequest(
                path=(
                    "/api/scan/market/results?disclosure=announced&category=watch&cursor=0&limit=1"
                    f"&generationId={generation.generation_id}"
                )
            )
        )
    )
    assert wrong_index.init["status"] == 409
    assert json.loads(wrong_index.body)["code"] == "generation_mismatch"

    broken_r2 = {"public/market_scan_index.json": generation.index}
    _worker, missing_page_api, _db = build_router_api(monkeypatch, r2=broken_r2)
    unavailable = asyncio.run(
        missing_page_api.fetch(
            RouteRequest(path="/api/scan/market/results?disclosure=announced&category=watch&cursor=0&limit=1")
        )
    )
    assert unavailable.init["status"] == 503
    body = json.loads(unavailable.body)
    assert body["code"] == "market_query_unavailable"
    assert body["requestId"] == unavailable.headers["x-request-id"]

    _worker, missing_pointer_api, _db = build_router_api(monkeypatch)
    missing_pointer = asyncio.run(missing_pointer_api.fetch(RouteRequest(path="/api/scan/market/index")))
    assert missing_pointer.init["status"] == 503
    missing_pointer_body = json.loads(missing_pointer.body)
    assert missing_pointer_body["code"] == "market_query_unavailable"
    assert missing_pointer_body["requestId"] == missing_pointer.headers["x-request-id"]

    _worker, malformed_pointer_api, _db = build_router_api(
        monkeypatch,
        r2={"public/market_scan_index.json": {"schemaVersion": True}},
    )
    malformed_pointer = asyncio.run(malformed_pointer_api.fetch(RouteRequest(path="/api/scan/market/index")))
    malformed_pointer_body = json.loads(malformed_pointer.body)
    assert malformed_pointer.init["status"] == 503
    assert malformed_pointer_body["code"] == "market_query_unavailable"
    assert malformed_pointer_body["requestId"] == malformed_pointer.headers["x-request-id"]

    first_page_key = next(key for key in generation.files if key.endswith("/0.json"))
    malformed_r2 = {
        "public/market_scan_index.json": generation.index,
        first_page_key: "{not-json",
    }
    _worker, malformed_page_api, _db = build_router_api(monkeypatch, r2=malformed_r2)
    malformed = asyncio.run(
        malformed_page_api.fetch(
            RouteRequest(path="/api/scan/market/results?disclosure=announced&category=watch&cursor=0&limit=1")
        )
    )
    assert malformed.init["status"] == 503
    assert json.loads(malformed.body)["code"] == "market_query_unavailable"


def test_worker_market_v2_rejects_oversized_noncanonical_raw_page(monkeypatch):
    generation = _worker_market_generation("1", size=1)
    page_key = next(key for key in generation.files if key.endswith("/0.json"))
    padded_page = " " * (600 * 1024) + generation.files[page_key].decode("utf-8")
    _worker, api, _db = build_router_api(
        monkeypatch,
        r2={"public/market_scan_index.json": generation.index, page_key: padded_page},
    )

    response = asyncio.run(
        api.fetch(
            RouteRequest(path="/api/scan/market/results?disclosure=announced&category=watch&cursor=0&limit=1")
        )
    )

    assert response.init["status"] == 503
    assert json.loads(response.body)["code"] == "market_query_unavailable"
    assert api.env.CACHE.objects[page_key].text_reads == 0
    assert api.env.CACHE.objects[page_key].array_reads == 0


def test_worker_market_v2_reads_exact_r2_bytes_via_array_buffer(monkeypatch):
    generation = _worker_market_generation("1", size=1)
    _worker, api, _db = build_router_api(
        monkeypatch,
        r2={"public/market_scan_index.json": generation.index, "public/manifest.json": {}},
    )

    response = asyncio.run(api.fetch(RouteRequest(path="/api/scan/market/index")))

    assert response.init["status"] == 200
    for key in ("public/market_scan_index.json", "public/manifest.json"):
        assert api.env.CACHE.objects[key].array_reads == 1
        assert api.env.CACHE.objects[key].text_reads == 0


@pytest.mark.parametrize("invalid_size", [True, 1.0, -1, None])
def test_worker_market_v2_rejects_invalid_r2_size(monkeypatch, invalid_size):
    generation = _worker_market_generation("1", size=1)
    _worker, api, _db = build_router_api(monkeypatch, r2={"public/market_scan_index.json": generation.index})
    obj = api.env.CACHE.objects["public/market_scan_index.json"]
    obj.size = invalid_size

    response = asyncio.run(api.fetch(RouteRequest(path="/api/scan/market/index")))

    assert response.init["status"] == 503
    assert json.loads(response.body)["code"] == "market_query_unavailable"


@pytest.mark.parametrize("mutation", ["invalid_utf8", "size_mismatch", "missing_array_buffer"])
def test_worker_market_v2_rejects_inexact_or_unreadable_r2_body(monkeypatch, mutation):
    generation = _worker_market_generation("1", size=1)
    _worker, api, _db = build_router_api(monkeypatch, r2={"public/market_scan_index.json": generation.index})
    key = "public/market_scan_index.json"
    original = api.env.CACHE.objects[key]
    if mutation == "invalid_utf8":
        api.env.CACHE.objects[key] = FakeR2Object(original._text, raw=b"\xff", size=1)
    elif mutation == "size_mismatch":
        api.env.CACHE.objects[key] = FakeR2Object(original._text, raw=original._raw, size=len(original._raw) + 1)
    else:

        class TextOnlyR2Body:
            size = original.size

            async def text(self):
                return original._text

        api.env.CACHE.objects[key] = TextOnlyR2Body()

    response = asyncio.run(api.fetch(RouteRequest(path="/api/scan/market/index")))

    assert response.init["status"] == 503
    assert json.loads(response.body)["code"] == "market_query_unavailable"


def test_worker_market_v2_array_buffer_failure_is_structured_dependency_error(monkeypatch, capsys):
    generation = _worker_market_generation("1", size=1)
    _worker, api, _db = build_router_api(monkeypatch, r2={"public/market_scan_index.json": generation.index})
    obj = api.env.CACHE.objects["public/market_scan_index.json"]

    async def fail_array_buffer():
        raise RuntimeError("Network connection lost. private-r2-sentinel")

    obj.arrayBuffer = fail_array_buffer
    response = asyncio.run(api.fetch(RouteRequest(path="/api/scan/market/index")))
    payload = json.loads(response.body)

    assert response.init["status"] == 503
    assert payload["code"] == "DEPENDENCY_UNAVAILABLE"
    assert payload["retryable"] is True
    assert payload["stage"] == "r2_read"
    assert payload["requestId"] == response.headers["x-request-id"]
    record = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert record["errorCode"] == "NETWORK_LOST"
    assert "private-r2-sentinel" not in json.dumps({"payload": payload, "record": record})


def test_worker_market_v2_rejects_wire_responses_over_budget(monkeypatch):
    generation = _worker_market_generation("1", size=1)
    r2 = {"public/market_scan_index.json": generation.index, **_worker_generation_r2(generation)}
    worker, index_api, _db = build_router_api(monkeypatch, r2=r2)
    index_api.cache_status_from_manifest = lambda *_args: {"padding": "x" * (50 * 1024)}

    index_response = asyncio.run(index_api.fetch(RouteRequest(path="/api/scan/market/index")))
    assert index_response.init["status"] == 503

    _worker, results_api, _db = build_router_api(monkeypatch, r2=r2)
    monkeypatch.setattr(
        worker.worker_market_query,
        "merge_market_pages",
        lambda *_args: {
            "schemaVersion": 2,
            "generationId": generation.generation_id,
            "disclosure": "announced",
            "category": "watch",
            "cursor": 0,
            "limit": 1,
            "total": 1,
            "nextCursor": None,
            "items": [{"stockCode": "1000", "summary": "x" * (500 * 1024)}],
        },
    )
    results_response = asyncio.run(
        results_api.fetch(
            RouteRequest(path="/api/scan/market/results?disclosure=announced&category=watch&cursor=0&limit=1")
        )
    )
    assert results_response.init["status"] == 503


def test_worker_market_v2_malformed_manifest_is_safe_503(monkeypatch):
    generation = _worker_market_generation("1", size=1)
    _worker, api, _db = build_router_api(
        monkeypatch,
        r2={
            "public/market_scan_index.json": generation.index,
            "public/manifest.json": {"counts": {"companies": "invalid"}},
        },
    )

    response = asyncio.run(api.fetch(RouteRequest(path="/api/scan/market/index")))
    assert response.init["status"] == 503
    assert json.loads(response.body)["code"] == "market_query_unavailable"


@pytest.mark.parametrize(
    ("stage", "retryable"),
    (("d1_read", True), ("d1_write", False)),
)
def test_worker_scan_market_post_serves_last_good_when_refresh_queue_fails(
    monkeypatch, capsys, stage, retryable
):
    manifest = {
        "generatedAt": "2026-02-19T00:00:00+00:00",
        "counts": {"companies": 1000, "analysis": 1000},
    }
    scan = {
        "entry": [
            {
                "stockCode": "2330",
                "status": "ENTRY",
                "summary": "last good",
                "internalOnly": "must be compacted",
            }
        ],
        "watch": [{"stockCode": "2454", "status": "WATCH", "summary": "observe"}],
        "excluded": [{"stockCode": "2603", "status": "EXCLUDED", "summary": "skip"}],
    }
    worker, api, _db = build_router_api(
        monkeypatch,
        r2={"public/manifest.json": manifest, "public/market_scan_summary.json": scan},
    )
    expected_get = json.loads(asyncio.run(api.fetch(RouteRequest(path="/api/scan/market"))).body)

    async def fail_refresh(_manifest, force=False):
        assert force is True
        failure = worker.DependencyFailure(stage, retryable, RuntimeError("secret sentinel"))
        failure.error_code = "NETWORK_LOST"
        raise failure

    api.ensure_refresh_job = fail_refresh
    response = asyncio.run(
        api.fetch(
            RouteRequest(
                method="POST",
                path="/api/scan/market",
                body='{"refreshMode":"force"}',
            )
        )
    )
    payload = json.loads(response.body)

    assert response.init["status"] == 200
    for category in ("entry", "watch", "excluded"):
        assert payload[category] == expected_get[category]
    assert "internalOnly" not in payload["entry"][0]
    assert payload["cacheStatus"]["refreshStatus"] == "unavailable"
    assert payload["cacheStatus"]["retryable"] is retryable
    assert payload["cacheStatus"]["requestId"] == response.headers["x-request-id"]
    assert re.fullmatch(r"[0-9a-f]{24}", response.headers["x-request-id"])

    record = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert record["event"] == "worker_dependency_degraded"
    assert record["method"] == "POST"
    assert record["path"] == "/api/scan/market"
    assert record["stage"] == "refresh_queue"
    assert record["status"] == response.init["status"] == 200
    assert record["errorType"] == "RuntimeError"
    assert record["errorCode"] == "NETWORK_LOST"
    assert record["requestId"] == response.headers["x-request-id"]
    assert record["durationMs"] >= 0
    assert "secret sentinel" not in json.dumps(record)


@pytest.mark.parametrize(
    "scan",
    (
        None,
        {"entry": [], "watch": [], "excluded": "not-a-list"},
    ),
    ids=("missing", "malformed"),
)
def test_worker_scan_market_post_requires_verified_last_good_for_fail_open(monkeypatch, capsys, scan):
    manifest = {
        "generatedAt": "2026-02-19T00:00:00+00:00",
        "counts": {"companies": 1000, "analysis": 1000},
    }
    r2 = {"public/manifest.json": manifest}
    if scan is not None:
        r2["public/market_scan_summary.json"] = scan
    worker, api, _db = build_router_api(monkeypatch, r2=r2)

    async def fail_refresh(_manifest, force=False):
        failure = worker.DependencyFailure("d1_read", True, RuntimeError("missing scan sentinel"))
        failure.error_code = "NETWORK_LOST"
        raise failure

    api.ensure_refresh_job = fail_refresh
    response = asyncio.run(
        api.fetch(RouteRequest(method="POST", path="/api/scan/market", body='{"refreshMode":"force"}'))
    )
    payload = json.loads(response.body)

    assert response.init["status"] == 503
    assert payload["code"] == "DEPENDENCY_UNAVAILABLE"
    assert payload["stage"] == "d1_read"
    assert "cacheStatus" not in payload
    record = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert record["event"] == "worker_request_failed"
    assert record["status"] == 503
    assert "missing scan sentinel" not in json.dumps(record)


def test_worker_scan_market_post_rejects_non_d1_refresh_failure(monkeypatch, capsys):
    manifest = {
        "generatedAt": "2026-02-19T00:00:00+00:00",
        "counts": {"companies": 1000, "analysis": 1000},
    }
    scan = {"entry": [], "watch": [], "excluded": []}
    worker, api, _db = build_router_api(
        monkeypatch,
        r2={"public/manifest.json": manifest, "public/market_scan_summary.json": scan},
    )

    async def fail_refresh(_manifest, force=False):
        failure = worker.DependencyFailure("r2_read", True, RuntimeError("wrong boundary sentinel"))
        failure.error_code = "RESET"
        raise failure

    api.ensure_refresh_job = fail_refresh
    response = asyncio.run(
        api.fetch(RouteRequest(method="POST", path="/api/scan/market", body='{"refreshMode":"force"}'))
    )
    payload = json.loads(response.body)

    assert response.init["status"] == 503
    assert payload["stage"] == "r2_read"
    assert "cacheStatus" not in payload
    record = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert record["event"] == "worker_request_failed"
    assert record["stage"] == "r2_read"
    assert "wrong boundary sentinel" not in json.dumps(record)


def test_worker_scan_market_post_rejects_unexpected_refresh_failure(monkeypatch, capsys):
    manifest = {
        "generatedAt": "2026-02-19T00:00:00+00:00",
        "counts": {"companies": 1000, "analysis": 1000},
    }
    scan = {"entry": [], "watch": [], "excluded": []}
    _worker, api, _db = build_router_api(
        monkeypatch,
        r2={"public/manifest.json": manifest, "public/market_scan_summary.json": scan},
    )

    async def fail_refresh(_manifest, force=False):
        raise RuntimeError("unexpected refresh sentinel")

    api.ensure_refresh_job = fail_refresh
    response = asyncio.run(
        api.fetch(RouteRequest(method="POST", path="/api/scan/market", body='{"refreshMode":"force"}'))
    )
    payload = json.loads(response.body)

    assert response.init["status"] == 500
    assert payload["code"] == "INTERNAL_ERROR"
    assert payload["stage"] == "route"
    assert "cacheStatus" not in payload
    record = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert record["event"] == "worker_request_failed"
    assert record["status"] == 500
    assert "unexpected refresh sentinel" not in json.dumps(record)


@pytest.mark.parametrize(
    ("method", "path", "failure_method", "stage", "retryable", "expected_status"),
    (
        ("POST", "/api/cache/refresh", "ensure_refresh_job", "d1_write", False, 500),
        ("GET", "/api/auth/me", "current_user", "d1_read", True, 503),
        ("GET", "/api/settings", "get_settings", "d1_read", True, 503),
        ("GET", "/api/me/holdings", "require_user", "d1_read", True, 503),
        ("POST", "/api/reports/market", "market_report", "d1_read", True, 503),
        ("GET", "/api/admin/users", "require_super_user", "d1_read", True, 503),
    ),
)
def test_worker_non_market_routes_do_not_fail_open_for_dependency_failure(
    monkeypatch,
    capsys,
    method,
    path,
    failure_method,
    stage,
    retryable,
    expected_status,
):
    worker, api, _db = build_router_api(monkeypatch, r2={"public/manifest.json": {}})

    async def fail(*_args, **_kwargs):
        failure = worker.DependencyFailure(stage, retryable, RuntimeError("sibling route sentinel"))
        failure.error_code = "NETWORK_LOST"
        raise failure

    setattr(api, failure_method, fail)
    response = asyncio.run(api.fetch(RouteRequest(method=method, path=path, body="")))
    payload = json.loads(response.body)

    assert response.init["status"] == expected_status
    assert payload["stage"] == stage
    assert "cacheStatus" not in payload
    record = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert record["event"] == "worker_request_failed"
    assert record["stage"] == stage
    assert record["status"] == expected_status
    assert "sibling route sentinel" not in json.dumps(record)


def test_worker_scan_market_get_serves_empty_when_seed_missing(monkeypatch):
    worker, api, _db = build_router_api(monkeypatch, r2={"public/manifest.json": {}})
    pin_worker_time(monkeypatch, worker, "2026-02-20T00:00:00+00:00")
    payload = json.loads(asyncio.run(api.fetch(RouteRequest(path="/api/scan/market"))).body)
    assert payload["entry"] == []
    assert payload["dataSource"] == "cloudflare_r2_seed"


def test_worker_analyze_and_holdings_scan(monkeypatch):
    analysis = {"public/analysis_shards/23.json": {"2330": {"stockCode": "2330", "status": "ENTRY", "summary": "好", "reasons": []}}}
    holding_shard = {"public/holding_analysis_shards/24.json": {"2454": {"stockCode": "2454", "status": "WATCH", "reasons": [{"code": "X1", "passed": True}]}}}
    worker, api, _db = build_router_api(monkeypatch, r2={**analysis, **holding_shard})

    found = asyncio.run(api.fetch(RouteRequest(method="POST", path="/api/analyze/2330", body="")))
    assert json.loads(found.body)["stockCode"] == "2330"
    not_found = asyncio.run(api.fetch(RouteRequest(method="POST", path="/api/analyze/9999", body="")))
    assert not_found.init["status"] == 404
    invalid = asyncio.run(api.fetch(RouteRequest(method="POST", path="/api/analyze/12", body="")))
    assert invalid.init["status"] == 404

    scan = asyncio.run(api.fetch(RouteRequest(
        method="POST", path="/api/scan/holdings",
        body=json.dumps({"holdings": [
            {"stockCode": "2454", "name": "聯發科", "shares": 100},
            {"stockCode": "2330", "name": "台積電", "shares": 100},
            {"stockCode": "1111", "name": "缺漏", "shares": 100},
        ]}),
    )))
    payload = json.loads(scan.body)
    assert {r["stockCode"] for r in payload["results"]} == {"2330", "2454"}
    assert [m["stockCode"] for m in payload["missing"]] == ["1111"]


def test_worker_reports_market_and_holdings(monkeypatch):
    scan = {"entry": [{"stockCode": "2330", "companyName": "台積電", "status": "ENTRY", "summary": "好"}], "watch": [], "excluded": []}
    worker, api, _db = build_router_api(monkeypatch, r2={"public/market_scan_summary.json": scan, "public/analysis_shards/23.json": {"2330": {"stockCode": "2330", "status": "ENTRY", "reasons": []}}})

    market = asyncio.run(api.fetch(RouteRequest(method="POST", path="/api/reports/market?report_format=markdown", body="")))
    assert "台股市場掃描報告" in market.body

    holdings = asyncio.run(api.fetch(RouteRequest(
        method="POST", path="/api/reports/holdings?report_format=csv",
        body=json.dumps({"holdings": [{"stockCode": "2330", "name": "台積電", "shares": 100}]}),
    )))
    assert "category,stockCode,companyName,status,summary" in holdings.body


def test_worker_cache_status_and_refresh(monkeypatch):
    manifest = {"generatedAt": "2026-02-19T00:00:00+00:00", "counts": {"companies": 1000, "analysis": 1000}}
    worker, api, db = build_router_api(monkeypatch, r2={"public/manifest.json": manifest}, github_repo="pcedison/stock_scanner")
    pin_worker_time(monkeypatch, worker, "2026-02-20T00:00:00+00:00")

    refresh = json.loads(asyncio.run(api.fetch(RouteRequest(method="POST", path="/api/cache/refresh", body=""))).body)
    assert refresh["status"] == "queued"

    status = json.loads(asyncio.run(api.fetch(RouteRequest(path="/api/cache/status"))).body)
    assert status["recentJobs"][0]["status"] == "queued"
    assert status["recentJobs"][0]["ownerRunUrl"] is None


def test_worker_ensure_refresh_job_fresh_and_existing(monkeypatch):
    manifest = {"generatedAt": "2026-02-20T00:00:00+00:00", "counts": {"companies": 1000, "analysis": 1000}}
    worker, api, db = build_router_api(monkeypatch, r2={"public/manifest.json": manifest})
    pin_worker_time(monkeypatch, worker, "2026-02-20T01:00:00+00:00")

    fresh = asyncio.run(api.ensure_refresh_job(manifest, force=False))
    assert fresh["status"] == "fresh"

    db.refresh_jobs.append({"id": "existing", "job_type": "market_scan", "cache_key": worker.cache_key_from_manifest(manifest), "status": "running", "reason": "manual", "queued_at": "2026-02-20T00:59:00+00:00", "updated_at": "2026-02-20T00:59:00+00:00"})
    existing = asyncio.run(api.ensure_refresh_job(manifest, force=True))
    assert existing["status"] == "running"
    assert existing["jobId"] == "existing"
    assert existing["ownerRunUrl"] is None


@pytest.mark.parametrize("invalid", ["", " leading", "trailing ", "slash/key", "測試", "x" * 81, 7])
def test_refresh_job_idempotent_key_rejects_invalid_values(invalid):
    refresh_jobs = importlib.import_module("cloudflare.worker_refresh_jobs")

    with pytest.raises(ValueError, match="Idempotency-Key"):
        refresh_jobs.normalize_idempotency_key(invalid)


def test_refresh_job_idempotent_key_is_hashed_and_fallback_uses_bounded_bucket():
    refresh_jobs = importlib.import_module("cloudflare.worker_refresh_jobs")
    first = datetime.fromisoformat("2026-07-13T12:01:00+00:00")
    same_bucket = datetime.fromisoformat("2026-07-13T12:09:59+00:00")
    next_bucket = datetime.fromisoformat("2026-07-13T12:10:00+00:00")

    assert refresh_jobs.normalize_idempotency_key(None) is None
    assert refresh_jobs.normalize_idempotency_key("Client_key:1.2-3") == "Client_key:1.2-3"
    assert refresh_jobs.derive_refresh_idempotency_key("market_scan", "cache-a", "client-1", first) == (
        refresh_jobs.derive_refresh_idempotency_key("market_scan", "cache-a", "client-1", next_bucket)
    )
    assert refresh_jobs.derive_refresh_idempotency_key("market_scan", "cache-a", "client-1", first) != (
        refresh_jobs.derive_refresh_idempotency_key("market_scan", "cache-a", "client-2", first)
    )
    assert refresh_jobs.derive_refresh_idempotency_key("market_scan", "cache-a", "client-1", first) != (
        refresh_jobs.derive_refresh_idempotency_key("market_scan", "cache-b", "client-1", first)
    )
    assert refresh_jobs.derive_refresh_idempotency_key("market_scan", "cache-a", "client-1", first) != (
        refresh_jobs.derive_refresh_idempotency_key("other_job", "cache-a", "client-1", first)
    )
    fallback = refresh_jobs.derive_refresh_idempotency_key("market_scan", "cache-a", None, first)
    assert fallback == refresh_jobs.derive_refresh_idempotency_key("market_scan", "cache-a", None, same_bucket)
    assert fallback != refresh_jobs.derive_refresh_idempotency_key("market_scan", "cache-a", None, next_bucket)
    assert re.fullmatch(r"[0-9a-f]{64}", fallback)


def test_refresh_job_invalid_client_key_is_rejected_even_when_cache_is_fresh(monkeypatch):
    refresh_jobs = importlib.import_module("cloudflare.worker_refresh_jobs")
    manifest = {"generatedAt": "2026-07-13T12:00:00+00:00", "counts": {"companies": 1000, "analysis": 1000}}
    worker, api, db = build_router_api(monkeypatch, r2={"public/manifest.json": manifest})
    pin_worker_time(monkeypatch, worker, "2026-07-13T12:01:00+00:00")
    now = datetime.fromisoformat("2026-07-13T12:01:00+00:00")

    with pytest.raises(ValueError, match="Idempotency-Key"):
        asyncio.run(refresh_jobs.enqueue_or_reuse_refresh_job(api, manifest, False, "invalid key", now))

    assert db.refresh_jobs == []


def test_refresh_job_same_client_key_reuses_d1_read_back_id(monkeypatch):
    refresh_jobs = importlib.import_module("cloudflare.worker_refresh_jobs")
    manifest = {"generatedAt": "2026-02-19T00:00:00+00:00", "counts": {"companies": 1000, "analysis": 1000}}
    _worker, api, db = build_router_api(monkeypatch, r2={"public/manifest.json": manifest})
    now = datetime.fromisoformat("2026-07-13T12:01:00+00:00")

    async def enqueue_twice():
        first = await refresh_jobs.enqueue_or_reuse_refresh_job(api, manifest, True, "client-same", now)
        second = await refresh_jobs.enqueue_or_reuse_refresh_job(api, manifest, True, "client-same", now)
        return first, second

    first, second = asyncio.run(enqueue_twice())

    assert first["jobId"] == second["jobId"] == db.refresh_jobs[0]["id"]
    assert len(db.refresh_jobs) == 1
    assert "client-same" not in json.dumps(db.refresh_jobs)
    assert re.fullmatch(r"[0-9a-f]{64}", db.refresh_jobs[0]["idempotency_key"])


def test_refresh_job_different_client_keys_share_active_cache_job(monkeypatch):
    refresh_jobs = importlib.import_module("cloudflare.worker_refresh_jobs")
    manifest = {"generatedAt": "2026-02-19T00:00:00+00:00", "counts": {"companies": 1000, "analysis": 1000}}
    _worker, api, db = build_router_api(monkeypatch, r2={"public/manifest.json": manifest})
    now = datetime.fromisoformat("2026-07-13T12:01:00+00:00")

    async def enqueue_both():
        return await asyncio.gather(
            refresh_jobs.enqueue_or_reuse_refresh_job(api, manifest, True, "client-a", now),
            refresh_jobs.enqueue_or_reuse_refresh_job(api, manifest, True, "client-b", now),
        )

    first, second = asyncio.run(enqueue_both())

    assert first["jobId"] == second["jobId"]
    assert db.active_job_count("market_scan") == 1


def test_refresh_job_after_success_blocks_new_request_inside_cooldown(monkeypatch):
    refresh_jobs = importlib.import_module("cloudflare.worker_refresh_jobs")
    manifest = {"generatedAt": "2026-02-19T00:00:00+00:00", "counts": {"companies": 1000, "analysis": 1000}}
    _worker, api, db = build_router_api(monkeypatch, r2={"public/manifest.json": manifest})
    now = datetime.fromisoformat("2026-07-13T12:01:00+00:00")

    asyncio.run(refresh_jobs.enqueue_or_reuse_refresh_job(api, manifest, True, "client-first", now))
    db.refresh_jobs[0]["status"] = "success"

    with pytest.raises(refresh_jobs.RefreshCooldownError, match="cooling down"):
        asyncio.run(refresh_jobs.enqueue_or_reuse_refresh_job(api, manifest, True, "client-next", now))

    assert len(db.refresh_jobs) == 1
    assert db.active_job_count("market_scan") == 0


def test_refresh_job_terminal_transition_blocks_immediate_second_insert(monkeypatch):
    refresh_jobs = importlib.import_module("cloudflare.worker_refresh_jobs")
    manifest = {"generatedAt": "2026-02-19T00:00:00+00:00", "counts": {"companies": 1000, "analysis": 1000}}
    _worker, api, db = build_router_api(monkeypatch, r2={"public/manifest.json": manifest})
    now = datetime.fromisoformat("2026-07-13T12:01:00+00:00")

    first = asyncio.run(refresh_jobs.enqueue_or_reuse_refresh_job(api, manifest, True, "client-first", now))
    db.complete_active_after_idempotency_miss = True

    with pytest.raises(refresh_jobs.RefreshCooldownError, match="cooling down"):
        asyncio.run(refresh_jobs.enqueue_or_reuse_refresh_job(api, manifest, True, "client-racing", now))

    assert first["jobId"] == db.refresh_jobs[0]["id"]
    assert len(db.refresh_jobs) == 1
    assert db.active_job_count("market_scan") == 0


def test_refresh_job_terminal_race_after_atomic_insert_returns_cooldown(monkeypatch):
    refresh_jobs = importlib.import_module("cloudflare.worker_refresh_jobs")
    manifest = {"generatedAt": "2026-02-19T00:00:00+00:00", "counts": {"companies": 1000, "analysis": 1000}}
    _worker, api, db = build_router_api(monkeypatch, r2={"public/manifest.json": manifest})
    now = datetime.fromisoformat("2026-07-13T12:01:00+00:00")

    async def racing_insert(_sql, *_params):
        db.refresh_jobs.append(
            {
                "id": "race-terminal",
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
        return {"meta": {"changes": 0}}

    api.db_run = racing_insert

    with pytest.raises(refresh_jobs.RefreshCooldownError, match="cooling down"):
        asyncio.run(refresh_jobs.enqueue_or_reuse_refresh_job(api, manifest, True, "racing-client", now))

    assert len(db.refresh_jobs) == 1


def test_refresh_job_missing_read_back_raises_invariant_without_returning_candidate(monkeypatch):
    refresh_jobs = importlib.import_module("cloudflare.worker_refresh_jobs")
    manifest = {"generatedAt": "2026-02-19T00:00:00+00:00", "counts": {"companies": 1000, "analysis": 1000}}
    _worker, api, db = build_router_api(monkeypatch, r2={"public/manifest.json": manifest})
    db.hide_refresh_job_reads = True
    now = datetime.fromisoformat("2026-07-13T12:01:00+00:00")

    with pytest.raises(refresh_jobs.RefreshJobReadBackError, match="read-back invariant"):
        asyncio.run(refresh_jobs.enqueue_or_reuse_refresh_job(api, manifest, True, "client-hidden", now))

    assert db.refresh_insert_attempts == 2
    assert len(db.refresh_jobs) == 1


def test_refresh_job_read_back_invariant_serves_verified_last_good(monkeypatch):
    manifest = {"generatedAt": "2026-02-19T00:00:00+00:00", "counts": {"companies": 1000, "analysis": 1000}}
    scan = {
        "entry": [{"stockCode": "2330", "status": "ENTRY", "summary": "last good"}],
        "watch": [],
        "excluded": [],
    }
    worker, api, db = build_router_api(
        monkeypatch,
        r2={"public/manifest.json": manifest, "public/market_scan_summary.json": scan},
    )
    pin_worker_time(monkeypatch, worker, "2026-07-13T12:01:00+00:00")
    db.hide_refresh_job_reads = True

    response = asyncio.run(
        api.fetch(RouteRequest(method="POST", path="/api/scan/market", body='{"refreshMode":"force"}'))
    )
    payload = json.loads(response.body)

    assert response.init["status"] == 200
    assert payload["entry"][0]["stockCode"] == "2330"
    assert payload["cacheStatus"]["refreshStatus"] == "unavailable"
    assert payload["cacheStatus"]["retryable"] is True


def test_refresh_job_read_back_invariant_without_last_good_is_safe_503(monkeypatch):
    manifest = {"generatedAt": "2026-02-19T00:00:00+00:00", "counts": {"companies": 1000, "analysis": 1000}}
    worker, api, db = build_router_api(monkeypatch, r2={"public/manifest.json": manifest})
    pin_worker_time(monkeypatch, worker, "2026-07-13T12:01:00+00:00")
    db.hide_refresh_job_reads = True

    response = asyncio.run(
        api.fetch(RouteRequest(method="POST", path="/api/scan/market", body='{"refreshMode":"force"}'))
    )
    payload = json.loads(response.body)

    assert response.init["status"] == 503
    assert payload["code"] == "DEPENDENCY_UNAVAILABLE"
    assert payload["stage"] == "d1_read"
    assert payload["retryable"] is True
    assert "cacheStatus" not in payload


def test_ensure_refresh_job_read_back_failure_drops_cause_and_client_key(monkeypatch):
    manifest = {"generatedAt": "2026-02-19T00:00:00+00:00", "counts": {"companies": 1000, "analysis": 1000}}
    worker, api, db = build_router_api(monkeypatch, r2={"public/manifest.json": manifest})
    pin_worker_time(monkeypatch, worker, "2026-07-13T12:01:00+00:00")
    db.hide_refresh_job_reads = True

    with pytest.raises(worker.DependencyFailure) as caught:
        asyncio.run(api.ensure_refresh_job(manifest, force=True, client_key="private-client-key"))

    failure = caught.value
    assert failure.__cause__ is None
    assert failure.__context__ is None
    assert failure.error_code == "REFRESH_JOB_READ_BACK_INVARIANT"
    assert "private-client-key" not in str(failure)


def test_concurrent_ensure_refresh_job_keeps_one_active_row(monkeypatch):
    manifest = {"generatedAt": "2026-02-19T00:00:00+00:00", "counts": {"companies": 1000, "analysis": 1000}}
    worker, api, db = build_router_api(monkeypatch, r2={"public/manifest.json": manifest})
    pin_worker_time(monkeypatch, worker, "2026-07-13T12:01:00+00:00")

    async def enqueue_all():
        return await asyncio.gather(
            *[
                api.ensure_refresh_job(manifest, force=True, client_key=f"client-{index}")
                for index in range(10)
            ]
        )

    jobs = asyncio.run(enqueue_all())

    assert len({job["jobId"] for job in jobs}) == 1
    assert db.active_job_count("market_scan") == 1


def test_refresh_command_returns_202_without_market_payload_and_reuses_same_key(monkeypatch):
    manifest = {"generatedAt": "2026-02-19T00:00:00+00:00", "counts": {"companies": 1000, "analysis": 1000}}
    worker, api, db = build_router_api(monkeypatch, r2={"public/manifest.json": manifest})
    pin_worker_time(monkeypatch, worker, "2026-07-13T12:01:00+00:00")
    def request():
        return RouteRequest(
            method="POST",
            path="/api/scan/market/refresh",
            headers={"idempotency-key": "refresh-command-1"},
        )

    first = asyncio.run(api.fetch(request()))
    second = asyncio.run(api.fetch(request()))
    first_payload = json.loads(first.body)
    second_payload = json.loads(second.body)

    assert first.init["status"] == second.init["status"] == 202
    assert set(first_payload) == {"jobId", "status", "requestId", "statusUrl"}
    assert not {"entry", "watch", "excluded"} & set(first_payload)
    assert re.fullmatch(r"[0-9a-f]{32}", first_payload["jobId"])
    assert first_payload["jobId"] == second_payload["jobId"]
    assert first.headers["Location"] == first_payload["statusUrl"]
    assert first_payload["statusUrl"] == f'/api/scan/market/refresh/{first_payload["jobId"]}'
    assert first_payload["requestId"] == first.headers["x-request-id"]
    assert first.headers["cache-control"] == "no-store"
    assert len(db.refresh_jobs) == 1


def test_refresh_command_requires_csrf_in_production_and_rejects_invalid_key(monkeypatch):
    manifest = {"generatedAt": "2026-02-19T00:00:00+00:00", "counts": {"companies": 1000, "analysis": 1000}}
    worker = load_worker_module(monkeypatch)
    db = RoutingFakeD1()
    api = worker.Api(
        env=types.SimpleNamespace(
            APP_ENV="production",
            APP_CORS_ALLOW_ORIGINS="https://stock-scanner-beta.pages.dev",
            SUPER_USER_USERNAME="admin@example.com",
            DB=db,
            CACHE=FakeR2Cache(r2_seed({"public/manifest.json": manifest})),
        )
    )
    pin_worker_time(monkeypatch, worker, "2026-07-13T12:01:00+00:00")

    missing = asyncio.run(
        api.fetch(RouteRequest(method="POST", path="/api/scan/market/refresh", headers={"idempotency-key": "safe"}))
    )
    invalid = asyncio.run(
        api.fetch(
            RouteRequest(
                method="POST",
                path="/api/scan/market/refresh",
                headers={"x-stock-scanner-csrf": "1", "idempotency-key": "invalid key"},
            )
        )
    )

    assert missing.init["status"] == 403
    assert json.loads(missing.body)["detail"] == "CSRF header required"
    assert invalid.init["status"] == 422
    assert "Idempotency-Key" in json.loads(invalid.body)["detail"]
    assert db.refresh_jobs == []


def test_refresh_command_rejects_fresh_manifest_without_writing_d1(monkeypatch):
    manifest = {"generatedAt": "2026-07-13T12:00:00+00:00", "counts": {"companies": 1000, "analysis": 1000}}
    worker, api, db = build_router_api(monkeypatch, r2={"public/manifest.json": manifest})
    pin_worker_time(monkeypatch, worker, "2026-07-13T12:05:00+00:00")

    response = asyncio.run(
        api.fetch(
            RouteRequest(
                method="POST",
                path="/api/scan/market/refresh",
                headers={"idempotency-key": "fresh-public-command"},
            )
        )
    )
    payload = json.loads(response.body)

    assert response.init["status"] == 429
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["retry-after"].isdigit()
    assert 1 <= int(response.headers["retry-after"]) <= 10800
    assert payload["detail"] == "Market refresh is cooling down; try again later."
    assert db.refresh_jobs == []


def test_refresh_command_terminal_job_cooldown_survives_new_cache_generation(monkeypatch):
    manifest = {
        "generatedAt": "2026-07-12T00:00:00+00:00",
        "counts": {"companies": 1001, "analysis": 1001},
    }
    worker, api, db = build_router_api(monkeypatch, r2={"public/manifest.json": manifest})
    pin_worker_time(monkeypatch, worker, "2026-07-13T12:05:00+00:00")
    db.refresh_jobs.append(
        {
            "id": "recent-success",
            "job_type": "market_scan",
            "cache_key": "previous-generation",
            "idempotency_key": "previous-key",
            "status": "success",
            "reason": "routine_refresh",
            "queued_at": "2026-07-13 11:30:00",
            "finished_at": "2026-07-13 11:40:00",
            "updated_at": "2026-07-13 11:40:00",
        }
    )

    response = asyncio.run(
        api.fetch(
            RouteRequest(
                method="POST",
                path="/api/scan/market/refresh",
                headers={"idempotency-key": "new-cache-generation"},
            )
        )
    )
    payload = json.loads(response.body)

    assert response.init["status"] == 429
    assert payload["detail"] == "Market refresh is cooling down; try again later."
    assert len(db.refresh_jobs) == 1
    assert db.refresh_jobs[0]["id"] == "recent-success"


def test_refresh_command_different_cache_generation_reuses_global_active_job(monkeypatch):
    manifest = {"generatedAt": "2026-07-12T00:00:00+00:00", "counts": {"companies": 1002, "analysis": 1002}}
    worker, api, db = build_router_api(monkeypatch, r2={"public/manifest.json": manifest})
    pin_worker_time(monkeypatch, worker, "2026-07-13T12:05:00+00:00")
    db.refresh_jobs.append(
        {
            "id": "a" * 32,
            "job_type": "market_scan",
            "cache_key": "previous-generation",
            "idempotency_key": "previous-idempotency",
            "status": "queued",
            "reason": "routine_refresh",
            "queued_at": "2026-07-13T12:00:00+00:00",
            "updated_at": "2026-07-13T12:00:00+00:00",
        }
    )

    response = asyncio.run(
        api.fetch(
            RouteRequest(
                method="POST",
                path="/api/scan/market/refresh",
                headers={"idempotency-key": "new-generation-client"},
            )
        )
    )
    payload = json.loads(response.body)

    assert response.init["status"] == 202
    assert payload["jobId"] == "a" * 32
    assert len(db.refresh_jobs) == 1


def test_refresh_command_idempotent_enqueue_failure_is_safe_503_and_legacy_keeps_lkg(monkeypatch):
    manifest = {"generatedAt": "2026-02-19T00:00:00+00:00", "counts": {"companies": 1000, "analysis": 1000}}
    scan = {"entry": [{"stockCode": "2330", "summary": "last good"}], "watch": [], "excluded": []}
    worker, api, _db = build_router_api(
        monkeypatch,
        r2={"public/manifest.json": manifest, "public/market_scan_summary.json": scan},
    )
    pin_worker_time(monkeypatch, worker, "2026-07-13T12:01:00+00:00")

    async def fail_idempotent_insert(_sql, *_params):
        failure = worker.DependencyFailure(
            "d1_write",
            False,
            RuntimeError("Authorization: Bearer secret raw SQL /Users/private"),
        )
        failure.error_code = "NETWORK_LOST"
        raise failure

    api.db_run = fail_idempotent_insert
    command = asyncio.run(
        api.fetch(
            RouteRequest(
                method="POST",
                path="/api/scan/market/refresh",
                headers={"idempotency-key": "safe-command-key"},
            )
        )
    )
    command_payload = json.loads(command.body)
    legacy = asyncio.run(api.fetch(RouteRequest(method="POST", path="/api/scan/market", body='{"refreshMode":"force"}')))
    legacy_payload = json.loads(legacy.body)

    assert command.init["status"] == 503
    assert command_payload["code"] == "DEPENDENCY_UNAVAILABLE"
    assert command_payload["retryable"] is True
    assert command_payload["stage"] == "d1_write"
    assert command_payload["requestId"] == command.headers["x-request-id"]
    assert "Bearer secret" not in command.body
    assert "/Users/private" not in command.body
    assert legacy.init["status"] == 200
    assert legacy_payload["entry"][0]["stockCode"] == "2330"
    assert legacy_payload["cacheStatus"]["refreshStatus"] == "unavailable"
    assert legacy_payload["cacheStatus"]["retryable"] is True


@pytest.mark.parametrize(
    ("message", "expected_status", "expected_retryable"),
    [
        ("network connection lost private", 503, True),
        ("connection reset private", 503, True),
        ("transient issue on remote node private", 503, True),
        ("database overloaded private", 500, False),
        ("query timeout private", 500, False),
        ("syntax or permission private", 500, False),
    ],
)
def test_refresh_command_only_classifies_known_transient_idempotent_writes_retryable(
    monkeypatch, message, expected_status, expected_retryable
):
    worker, api, _db = build_router_api(
        monkeypatch,
        r2={"public/manifest.json": {"generatedAt": "2026-02-19T00:00:00+00:00"}},
    )

    class FailingStatement:
        def bind(self, *_params):
            return self

        async def first(self):
            return None

        async def run(self):
            raise RuntimeError(message)

    api.env.DB = types.SimpleNamespace(prepare=lambda _sql: FailingStatement())
    response = asyncio.run(
        api.fetch(
            RouteRequest(
                method="POST",
                path="/api/scan/market/refresh",
                headers={"idempotency-key": "classified-write-key"},
            )
        )
    )
    payload = json.loads(response.body)

    assert response.init["status"] == expected_status
    assert payload["retryable"] is expected_retryable
    assert payload["stage"] == "d1_write"
    assert "private" not in response.body


def test_refresh_status_is_camel_case_no_store_and_redacts_internal_error(monkeypatch):
    worker, api, db = build_router_api(monkeypatch, github_repo="pcedison/stock_scanner")
    job_id = "a" * 32
    db.refresh_jobs.append(
        {
            "id": job_id,
            "job_type": "market_scan",
            "cache_key": "private-cache-key",
            "idempotency_key": "private-idempotency-hash",
            "status": "failed",
            "reason": "routine_refresh",
            "queued_at": "2026-07-13T12:01:00+00:00",
            "started_at": "2026-07-13 12:02:00",
            "finished_at": "2026-07-13 12:03:00",
            "updated_at": "2026-07-13 12:03:00",
            "owner_run_id": "12345",
            "error": "Authorization: Bearer secret raw SQL /Users/private",
            "dispatch_status": "failed",
            "dispatch_error_code": "GITHUB_HTTP_403",
        }
    )

    response = asyncio.run(api.fetch(RouteRequest(path=f"/api/scan/market/refresh/{job_id}")))
    payload = json.loads(response.body)
    serialized = json.dumps(payload)

    assert response.init["status"] == 200
    assert response.headers["cache-control"] == "no-store"
    assert payload == {
        "jobId": job_id,
        "status": "failed",
        "reason": "routine_refresh",
        "queuedAt": "2026-07-13T12:01:00+00:00",
        "startedAt": "2026-07-13 12:02:00",
        "finishedAt": "2026-07-13 12:03:00",
        "updatedAt": "2026-07-13 12:03:00",
        "ownerRunId": "12345",
        "ownerRunUrl": "https://github.com/pcedison/stock_scanner/actions/runs/12345",
        "hasError": True,
        "dispatchStatus": "failed",
        "dispatchErrorCode": "GITHUB_HTTP_403",
    }
    assert not {"error", "idempotency_key", "cache_key", "job_type", "queued_at"} & set(payload)
    assert "Bearer secret" not in serialized
    assert "/Users/private" not in serialized


def test_refresh_status_404_rejects_invalid_hex_before_query_and_handles_pre_dispatch_rows(monkeypatch):
    _worker, api, db = build_router_api(monkeypatch)
    job_id = "b" * 32
    db.refresh_jobs.append(
        {
            "id": job_id,
            "job_type": "market_scan",
            "cache_key": "cache",
            "status": "queued",
            "reason": "Authorization: Bearer secret reason",
            "queued_at": "2026-07-13T12:01:00+00:00",
            "updated_at": "2026-07-13T12:01:00+00:00",
            "owner_run_id": "../private",
            "error": None,
        }
    )

    before = len(db.prepare_calls)
    invalid = asyncio.run(api.fetch(RouteRequest(path=f"/api/scan/market/refresh/{'A' * 32}")))
    assert invalid.init["status"] == 404
    assert invalid.headers["cache-control"] == "no-store"
    assert len(db.prepare_calls) == before

    missing = asyncio.run(api.fetch(RouteRequest(path=f"/api/scan/market/refresh/{'c' * 32}")))
    assert missing.init["status"] == 404
    existing = asyncio.run(api.fetch(RouteRequest(path=f"/api/scan/market/refresh/{job_id}")))
    existing_payload = json.loads(existing.body)
    assert existing_payload["status"] == "queued"
    assert not {"reason", "ownerRunId", "ownerRunUrl", "dispatchStatus"} & set(existing_payload)
    assert "Bearer secret" not in existing.body


def test_refresh_status_unknown_main_status_is_safe_and_unknown_dispatch_fields_are_omitted(monkeypatch):
    _worker, api, db = build_router_api(monkeypatch)
    malformed_id = "d" * 32
    safe_id = "e" * 32
    base = {
        "job_type": "market_scan",
        "cache_key": "private-cache",
        "reason": "routine_refresh",
        "queued_at": "2026-07-13T12:01:00+00:00",
        "updated_at": "2026-07-13T12:01:00+00:00",
        "error": None,
    }
    db.refresh_jobs.extend(
        [
            {**base, "id": malformed_id, "status": "done-secret-value"},
            {
                **base,
                "id": safe_id,
                "status": "queued",
                "dispatch_status": "evil-secret-dispatch",
                "dispatch_error_code": "raw/error/secret",
            },
        ]
    )

    malformed = asyncio.run(api.fetch(RouteRequest(path=f"/api/scan/market/refresh/{malformed_id}")))
    safe = asyncio.run(api.fetch(RouteRequest(path=f"/api/scan/market/refresh/{safe_id}")))
    safe_payload = json.loads(safe.body)

    assert malformed.init["status"] == 500
    assert json.loads(malformed.body)["code"] == "INTERNAL_ERROR"
    assert "done-secret-value" not in malformed.body
    assert safe.init["status"] == 200
    assert not {"dispatchStatus", "dispatchErrorCode"} & set(safe_payload)
    assert "secret" not in safe.body


def test_refresh_command_coalesced_loser_key_is_cooldown_limited_after_terminal(monkeypatch):
    manifest = {"generatedAt": "2026-02-19T00:00:00+00:00", "counts": {"companies": 1000, "analysis": 1000}}
    worker, api, db = build_router_api(monkeypatch, r2={"public/manifest.json": manifest})
    pin_worker_time(monkeypatch, worker, "2026-07-13T12:01:00+00:00")

    def command(key):
        response = asyncio.run(
            api.fetch(
                RouteRequest(method="POST", path="/api/scan/market/refresh", headers={"idempotency-key": key})
            )
        )
        return json.loads(response.body)

    winner = command("winner-key")
    loser = command("loser-key")
    assert loser["jobId"] == winner["jobId"]
    db.refresh_jobs[0]["status"] = "success"
    retried_loser = command("loser-key")

    assert retried_loser["detail"] == "Market refresh is cooling down; try again later."
    assert len(db.refresh_jobs) == 1


def test_legacy_market_post_keeps_payload_and_adds_refresh_successor_headers(monkeypatch):
    manifest = {"generatedAt": "2026-02-19T00:00:00+00:00", "counts": {"companies": 1000, "analysis": 1000}}
    scan = {"entry": [], "watch": [{"stockCode": "2330"}], "excluded": []}
    worker, api, _db = build_router_api(
        monkeypatch,
        r2={"public/manifest.json": manifest, "public/market_scan_summary.json": scan},
    )
    pin_worker_time(monkeypatch, worker, "2026-07-13T12:01:00+00:00")

    response = asyncio.run(api.fetch(RouteRequest(method="POST", path="/api/scan/market", body="{}")))
    payload = json.loads(response.body)

    assert response.init["status"] == 200
    assert payload["watch"][0]["stockCode"] == "2330"
    assert response.headers["Deprecation"] == "true"
    assert response.headers["Link"] == '</api/scan/market/refresh>; rel="successor-version"'


def test_refresh_job_request_path_has_no_cleanup_delete_and_worker_delegates():
    worker_source = (ROOT / "cloudflare" / "worker.py").read_text(encoding="utf-8")
    helper_source = (ROOT / "cloudflare" / "worker_refresh_jobs.py").read_text(encoding="utf-8")

    assert "DELETE FROM refresh_jobs" not in worker_source
    assert "DELETE FROM refresh_jobs" not in helper_source
    assert "worker_refresh_jobs.enqueue_or_reuse_refresh_job" in worker_source


def test_worker_github_actions_run_url_requires_valid_repo(monkeypatch):
    worker = load_worker_module(monkeypatch)
    no_repo = worker.Api(env=types.SimpleNamespace())
    assert no_repo.github_actions_run_url("123") is None
    assert no_repo.github_actions_run_url(None) is None
    bad_repo = worker.Api(env=types.SimpleNamespace(GITHUB_REPOSITORY="not-a-slug"))
    assert bad_repo.github_actions_run_url("123") is None


def test_worker_misc_readonly_routes(monkeypatch):
    worker, api, _db = build_router_api(monkeypatch, r2={
        "public/data_sources_status.json": {"activeProvider": "CloudflareR2Seed"},
        "official/official_history_backfill_progress.json": {"done": 5},
    })

    data_sources = json.loads(asyncio.run(api.fetch(RouteRequest(path="/api/data-sources/status"))).body)
    assert data_sources["activeProvider"] == "CloudflareR2Seed"

    backfill = json.loads(asyncio.run(api.fetch(RouteRequest(method="POST", path="/api/data-sources/backfill-history", body=""))).body)
    assert backfill["progress"]["done"] == 5

    calendar = json.loads(asyncio.run(api.fetch(RouteRequest(path="/api/calendar/2026"))).body)
    assert calendar["year"] == 2026

    wakeup = json.loads(asyncio.run(api.fetch(RouteRequest(path="/api/scheduler/wakeup"))).body)
    assert wakeup["status"] == "SLEEP"

    integrations = json.loads(asyncio.run(api.fetch(RouteRequest(path="/api/integrations/status"))).body)
    assert integrations["line"] is False

    backtest = json.loads(asyncio.run(api.fetch(RouteRequest(path="/api/backtest"))).body)
    assert "metrics" in backtest

    not_found = asyncio.run(api.fetch(RouteRequest(path="/api/does-not-exist")))
    assert not_found.init["status"] == 404


def test_worker_auth_source_prefers_forwarded_headers(monkeypatch):
    worker, api, _db = build_router_api(monkeypatch)
    assert api.auth_source(RouteRequest(headers={"x-forwarded-for": "1.1.1.1, 2.2.2.2"})) == "1.1.1.1"
    assert api.auth_source(RouteRequest(headers={"cf-connecting-ip": "3.3.3.3"})) == "3.3.3.3"
    assert api.auth_source(RouteRequest(headers={"x-real-ip": "4.4.4.4"})) == "4.4.4.4"
    assert api.auth_source(RouteRequest(headers={})) == "unknown"


def test_worker_r2_json_falls_back_on_invalid_json(monkeypatch):
    worker, api, _db = build_router_api(monkeypatch, r2={"public/manifest.json": "not-json{"})
    result = asyncio.run(api.r2_json("public/manifest.json", {"fallback": True}))
    assert result == {"fallback": True}


def test_worker_r2_body_io_failure_returns_retryable_503(monkeypatch, capsys):
    _worker, api, _db = build_router_api(monkeypatch)

    class FailingR2Body:
        async def text(self):
            raise RuntimeError("raw-r2-body-sentinel")

    api.env.CACHE.objects["public/manifest.json"] = FailingR2Body()
    response = asyncio.run(api.fetch(RouteRequest(path="/api/health")))
    payload = json.loads(response.body)

    assert response.init["status"] == 503
    assert payload["code"] == "DEPENDENCY_UNAVAILABLE"
    assert payload["retryable"] is True
    assert payload["stage"] == "r2_read"
    record = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert "raw-r2-body-sentinel" not in json.dumps({"payload": payload, "record": record})


def test_worker_r2_object_conversion_failure_returns_retryable_503(monkeypatch, capsys):
    worker, api, _db = build_router_api(monkeypatch)
    raw_object = object()
    api.env.CACHE.objects["public/manifest.json"] = raw_object
    original_js_to_py = worker.js_to_py

    def fail_conversion(value):
        if value is raw_object:
            raise RuntimeError("raw-r2-conversion-sentinel")
        return original_js_to_py(value)

    monkeypatch.setattr(worker, "js_to_py", fail_conversion)
    response = asyncio.run(api.fetch(RouteRequest(path="/api/health")))
    payload = json.loads(response.body)

    assert response.init["status"] == 503
    assert payload["code"] == "DEPENDENCY_UNAVAILABLE"
    assert payload["retryable"] is True
    assert payload["stage"] == "r2_read"
    record = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert "raw-r2-conversion-sentinel" not in json.dumps({"payload": payload, "record": record})


def test_worker_fetch_maps_unexpected_error_to_500(monkeypatch, capsys):
    worker, api, _db = build_router_api(monkeypatch)

    async def boom(_key, _fallback):
        raise RuntimeError("secret-value must never be returned")

    api.r2_json = boom
    response = asyncio.run(
        api.fetch(RouteRequest(path="/api/health", headers={"authorization": "Bearer hidden"}))
    )
    payload = json.loads(response.body)

    assert response.init["status"] == 500
    assert payload["detail"] == "伺服器暫時無法處理請求，請稍後再試。"
    assert payload["code"] == "INTERNAL_ERROR"
    assert payload["retryable"] is False
    assert payload["stage"] == "route"
    assert payload["requestId"]
    assert response.headers["x-request-id"] == payload["requestId"]

    record = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert record["event"] == "worker_request_failed"
    assert record["requestId"] == payload["requestId"]
    assert record["method"] == "GET"
    assert record["path"] == "/api/health"
    assert record["errorType"] == "RuntimeError"
    assert record["durationMs"] >= 0
    serialized = json.dumps(record)
    assert "secret-value" not in serialized
    assert "Bearer hidden" not in serialized


def test_worker_r2_read_failure_returns_retryable_503(monkeypatch, capsys):
    _worker, api, _db = build_router_api(monkeypatch)

    async def fail_get(_key):
        raise RuntimeError("R2 temporarily unavailable")

    api.env.CACHE.get = fail_get
    response = asyncio.run(api.fetch(RouteRequest(path="/api/health")))
    payload = json.loads(response.body)

    assert response.init["status"] == 503
    assert payload["code"] == "DEPENDENCY_UNAVAILABLE"
    assert payload["retryable"] is True
    assert payload["stage"] == "r2_read"
    record = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert record["errorCode"] == "UNCLASSIFIED"
    assert "R2 temporarily unavailable" not in json.dumps(record)


@pytest.mark.parametrize(
    ("message", "error_code"),
    [
        ("Network connection lost.", "NETWORK_LOST"),
        ("D1 DB reset because its code was updated.", "RESET"),
        ("Cannot resolve D1 DB due to transient issue on remote node.", "TRANSIENT_REMOTE_NODE"),
    ],
)
def test_worker_d1_retryable_read_failure_returns_503(monkeypatch, capsys, message, error_code):
    _worker, api, _db = build_router_api(monkeypatch)

    class FailingStatement:
        def bind(self, *params):
            return self

        async def first(self):
            raise RuntimeError(message)

    api.env.DB.prepare = lambda _sql: FailingStatement()
    response = asyncio.run(api.fetch(RouteRequest(path="/api/settings")))
    payload = json.loads(response.body)

    assert response.init["status"] == 503
    assert payload["code"] == "DEPENDENCY_UNAVAILABLE"
    assert payload["retryable"] is True
    assert payload["stage"] == "d1_read"
    record = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert record["errorCode"] == error_code
    assert message not in json.dumps(record)


@pytest.mark.parametrize(
    ("message", "error_code"),
    [
        ("D1 DB is overloaded. Too many requests queued.", "OVERLOADED"),
        ("D1 DB storage operation exceeded timeout which caused object to be reset.", "TIMEOUT"),
    ],
)
def test_worker_d1_capacity_read_failure_is_not_retryable(monkeypatch, capsys, message, error_code):
    _worker, api, _db = build_router_api(monkeypatch)

    class FailingStatement:
        def bind(self, *params):
            return self

        async def first(self):
            raise RuntimeError(message)

    api.env.DB.prepare = lambda _sql: FailingStatement()
    response = asyncio.run(api.fetch(RouteRequest(path="/api/settings")))
    payload = json.loads(response.body)

    assert response.init["status"] == 500
    assert payload["code"] == "DEPENDENCY_FAILURE"
    assert payload["retryable"] is False
    assert payload["stage"] == "d1_read"
    record = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert record["errorCode"] == error_code
    assert message not in json.dumps(record)


@pytest.mark.parametrize("boundary", ["r2_read", "d1_read"])
def test_worker_dependency_failure_drops_raw_exception_references(monkeypatch, capsys, boundary):
    worker, api, _db = build_router_api(monkeypatch)
    sentinel = f"raw-{boundary}-sentinel"
    raw_message = f"Network connection lost. {sentinel}"

    if boundary == "r2_read":
        async def fail_get(_key):
            raise RuntimeError(raw_message)

        api.env.CACHE.get = fail_get
        def direct_call():
            return api.r2_json("public/manifest.json", {})

        request = RouteRequest(path="/api/health")
    else:
        class FailingStatement:
            def bind(self, *params):
                return self

            async def first(self):
                raise RuntimeError(raw_message)

        api.env.DB.prepare = lambda _sql: FailingStatement()
        def direct_call():
            return api.db_first("SELECT 1")

        request = RouteRequest(path="/api/settings")

    with pytest.raises(worker.DependencyFailure) as caught:
        asyncio.run(direct_call())

    failure = caught.value
    assert failure.__context__ is None
    assert failure.__cause__ is None
    traceback = failure.__traceback__
    while traceback is not None:
        if Path(traceback.tb_frame.f_code.co_filename).name in {"worker.py", "worker_observability.py"}:
            exception_locals = [
                value for value in traceback.tb_frame.f_locals.values() if isinstance(value, BaseException)
            ]
            assert all(sentinel not in str(value) for value in exception_locals)
        traceback = traceback.tb_next

    response = asyncio.run(api.fetch(request))
    record = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert sentinel not in response.body
    assert sentinel not in json.dumps(record)


def test_worker_d1_unclassified_read_failure_is_not_retryable(monkeypatch):
    _worker, api, _db = build_router_api(monkeypatch)

    class FailingStatement:
        def bind(self, *params):
            return self

        async def first(self):
            raise RuntimeError("syntax or permission error")

    api.env.DB.prepare = lambda _sql: FailingStatement()
    response = asyncio.run(api.fetch(RouteRequest(path="/api/settings")))
    payload = json.loads(response.body)

    assert response.init["status"] == 500
    assert payload["code"] == "DEPENDENCY_FAILURE"
    assert payload["retryable"] is False
    assert payload["stage"] == "d1_read"


def test_worker_d1_write_failure_is_not_marked_safe_to_retry(monkeypatch):
    worker, api, _db = build_router_api(monkeypatch, r2={"public/manifest.json": {}})

    async def fail_refresh(_manifest, force=False):
        raise worker.DependencyFailure("d1_write", False, RuntimeError("Network connection lost"))

    api.ensure_refresh_job = fail_refresh
    response = asyncio.run(api.fetch(RouteRequest(method="POST", path="/api/cache/refresh", body="")))
    payload = json.loads(response.body)

    assert response.init["status"] == 500
    assert payload["code"] == "DEPENDENCY_FAILURE"
    assert payload["retryable"] is False
    assert payload["stage"] == "d1_write"


def test_worker_cache_policy_windows(monkeypatch):
    worker = load_worker_module(monkeypatch)
    api = worker.Api(env=types.SimpleNamespace())

    pin_worker_time(monkeypatch, worker, "2026-03-31T04:00:00+00:00")
    assert api.cache_policy()["reason"] == "financial_report_window"

    pin_worker_time(monkeypatch, worker, "2026-02-10T04:00:00+00:00")
    assert api.cache_policy()["reason"] == "monthly_revenue_window"

    pin_worker_time(monkeypatch, worker, "2026-02-20T04:00:00+00:00")
    assert api.cache_policy()["reason"] == "routine_refresh"


def test_worker_subrouter_not_found_fallbacks(monkeypatch):
    worker, api, _db = build_router_api(monkeypatch)
    cases = [
        ("GET", "/api/auth/register"),
        ("PATCH", "/api/me/holdings"),
        ("GET", "/api/admin/unknown"),
        ("GET", "/api/scan/unknown"),
        ("GET", "/api/reports/market"),
        ("GET", "/api/cache/unknown"),
        ("POST", "/api/scheduler/wakeup"),
        ("POST", "/api/data-sources/status"),
        ("POST", "/api/calendar/2026"),
        ("POST", "/api/companies"),
    ]
    for method, path in cases:
        response = asyncio.run(api.fetch(RouteRequest(method=method, path=path, body="")))
        assert response.init["status"] == 404, f"{method} {path}"


def test_worker_scan_market_post_without_seed_queues_refresh(monkeypatch):
    manifest = {"generatedAt": "2026-02-19T00:00:00+00:00", "counts": {"companies": 1000, "analysis": 1000}}
    worker, api, db = build_router_api(monkeypatch, r2={"public/manifest.json": manifest})
    pin_worker_time(monkeypatch, worker, "2026-02-20T00:00:00+00:00")
    payload = json.loads(asyncio.run(api.fetch(RouteRequest(method="POST", path="/api/scan/market", body=""))).body)
    assert payload["entry"] == []
    assert payload["cacheStatus"]["refreshStatus"] == "queued"
    assert len(db.refresh_jobs) == 1


def test_worker_holding_analysis_rejects_invalid_code(monkeypatch):
    worker, api, _db = build_router_api(monkeypatch)
    assert asyncio.run(api.holding_analysis_for_stock("12")) is None


def test_worker_require_auth_attempt_allowed_clears_expired_lock(monkeypatch):
    worker, api, db = build_router_api(monkeypatch)
    pin_worker_time(monkeypatch, worker, "2026-02-20T00:00:00+00:00")
    request = RouteRequest(headers={})
    identifier = api.auth_attempt_identifier("u@example.com", request)
    db.auth_attempts.append({
        "identifier": identifier,
        "failure_count": 5,
        "first_failed_at": "2026-02-19T00:00:00+00:00",
        "last_failed_at": "2026-02-19T00:00:00+00:00",
        "locked_until": "2026-02-19T23:00:00+00:00",
    })
    asyncio.run(api.require_auth_attempt_allowed("u@example.com", request))
    assert db.auth_attempts == []
