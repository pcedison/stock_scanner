from urllib.error import HTTPError
from pathlib import Path

import pytest

from scripts.check_pages_api_redirect import (
    CHECK_USER_AGENT,
    check_pages_api_proxy,
    check_pages_api_redirect,
    validate_pages_api_url,
)

ROOT = Path(__file__).resolve().parents[1]


class FakeResponse:
    def __init__(self, status=200, body='{"status":"ok","runtime":"cloudflare-python-worker"}'):
        self.status = status
        self.body = body.encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return self.body

    def getcode(self):
        return self.status


class FakeProxyOpener:
    def __init__(self, response=None):
        self.response = response or FakeResponse()
        self.request = None

    def open(self, request, timeout=20):
        self.request = request
        return self.response


class FakeRedirectOpener:
    def open(self, request, timeout=20):
        raise HTTPError(
            request.full_url,
            307,
            "redirect",
            {"location": "https://worker.example/api/health"},
            None,
        )


def test_redirect_check_uses_browser_like_user_agent():
    assert CHECK_USER_AGENT.startswith("Mozilla/5.0 ")


def test_validate_pages_api_url_restricts_to_production_pages_api():
    assert validate_pages_api_url("https://stock-scanner-beta.pages.dev/api/health")
    with pytest.raises(RuntimeError, match="pages.dev"):
        validate_pages_api_url("https://stock-scanner-beta-api.pcedison.workers.dev/api/health")


def test_check_pages_api_proxy_accepts_worker_health_payload():
    opener = FakeProxyOpener()
    payload = check_pages_api_proxy(
        "https://stock-scanner-beta.pages.dev/api/health",
        opener=opener,
    )

    assert payload == {"status": "ok", "runtime": "cloudflare-python-worker"}
    assert opener.request.headers["User-agent"] == CHECK_USER_AGENT


def test_check_pages_api_redirect_legacy_name_uses_proxy_semantics():
    payload = check_pages_api_redirect(
        "https://stock-scanner-beta.pages.dev/api/health",
        "https://worker.example",
        opener=FakeProxyOpener(),
    )

    assert payload["runtime"] == "cloudflare-python-worker"


def test_check_pages_api_proxy_rejects_redirect_or_wrong_runtime():
    with pytest.raises(RuntimeError, match="redirect HTTP 307"):
        check_pages_api_proxy("https://stock-scanner-beta.pages.dev/api/health", opener=FakeRedirectOpener())
    with pytest.raises(RuntimeError, match="unexpected runtime"):
        check_pages_api_proxy(
            "https://stock-scanner-beta.pages.dev/api/health",
            opener=FakeProxyOpener(FakeResponse(body='{"status":"ok","runtime":"other"}')),
        )


def test_cloudflare_docs_describe_status_preserving_pages_proxy_and_valid_gate():
    deployment = (ROOT / "docs" / "cloudflare_deployment.md").read_text(encoding="utf-8")
    architecture = (ROOT / "docs" / "current_architecture.md").read_text(encoding="utf-8")
    combined = f"{deployment}\n{architecture}"

    assert "status-preserving proxy" in deployment
    assert "healthy `/api/health` returns HTTP 200" in deployment
    assert "other `/api/*` responses preserve the upstream Worker status" in deployment
    assert "Production browser mode normally calls the Worker directly" in architecture
    assert (
        "python scripts\\check_pages_api_redirect.py --url "
        "https://stock-scanner-beta.pages.dev/api/health"
    ) in deployment
    assert "--expected-origin" not in combined
    assert "HTTP 307" not in combined
