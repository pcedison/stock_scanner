from urllib.error import HTTPError

import pytest

from scripts.check_pages_api_redirect import (
    CHECK_USER_AGENT,
    check_pages_api_redirect,
    validate_expected_origin,
    validate_pages_api_url,
)


class FakeResponse:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class FakeRedirectOpener:
    def __init__(self, code=307, location="https://worker.example/api/health"):
        self.code = code
        self.location = location

    def open(self, request, timeout=20):
        raise HTTPError(
            request.full_url,
            self.code,
            "redirect",
            {"location": self.location},
            None,
        )


def test_redirect_check_uses_browser_like_user_agent():
    assert CHECK_USER_AGENT.startswith("Mozilla/5.0 ")


def test_validate_pages_api_url_restricts_to_production_pages_api():
    assert validate_pages_api_url("https://stock-scanner-beta.pages.dev/api/health")
    with pytest.raises(RuntimeError, match="pages.dev"):
        validate_pages_api_url("https://stock-scanner-beta-api.pcedison.workers.dev/api/health")


def test_validate_expected_origin_requires_https_origin():
    assert validate_expected_origin("https://worker.example/") == "https://worker.example"
    with pytest.raises(RuntimeError, match="https origin"):
        validate_expected_origin("https://worker.example/api")


def test_check_pages_api_redirect_accepts_worker_origin_and_path():
    location = check_pages_api_redirect(
        "https://stock-scanner-beta.pages.dev/api/health",
        "https://worker.example",
        opener=FakeRedirectOpener(),
    )

    assert location == "https://worker.example/api/health"


def test_check_pages_api_redirect_rejects_wrong_status_or_origin():
    with pytest.raises(RuntimeError, match="HTTP 307"):
        check_pages_api_redirect(
            "https://stock-scanner-beta.pages.dev/api/health",
            "https://worker.example",
            opener=FakeRedirectOpener(code=500, location="https://worker.example/api/health"),
        )
    with pytest.raises(RuntimeError, match="origin"):
        check_pages_api_redirect(
            "https://stock-scanner-beta.pages.dev/api/health",
            "https://worker.example",
            opener=FakeRedirectOpener(location="https://other.example/api/health"),
        )
