import asyncio
import importlib.util
import sys
import types
from pathlib import Path

import pytest


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
    assert response.headers["x-frame-options"] == "DENY"
    assert worker.normalize_username(" Qa+audit%2026@example.com ") == "qa+audit%2026@example.com"
    try:
        worker.normalize_username("bad account@example.com")
    except ValueError as exc:
        assert "不可包含空白" in str(exc)
    else:
        raise AssertionError("ValueError was not raised")


def test_worker_cors_allows_pages_and_local_loopback(monkeypatch):
    worker = load_worker_module(monkeypatch)
    api = worker.Api(env=None)

    localhost_request = types.SimpleNamespace(headers={"origin": "http://127.0.0.1:8000"})
    headers = api.cors_headers(localhost_request)

    assert headers["access-control-allow-origin"] == "http://127.0.0.1:8000"
    assert headers["access-control-allow-credentials"] == "true"
    assert api.cors_headers(types.SimpleNamespace(headers={"origin": "https://evil.example"}))[
        "access-control-allow-origin"
    ] == "https://stock-scanner-beta.pages.dev"


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

    with pytest.raises(worker.BadRequestError):
        worker.settings_from_payload({"manual_scan_enabled": "false"}, strict=True)
    with pytest.raises(worker.BadRequestError):
        worker.settings_from_payload({"revenue_growth_mode": "bad-mode"}, strict=True)
    assert worker.settings_from_payload({"manual_scan_enabled": "false"})["manual_scan_enabled"] is True
