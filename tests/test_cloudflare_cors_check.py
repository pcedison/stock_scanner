from email.message import Message

import pytest

from scripts.check_cloudflare_cors import (
    CORS_CHECK_USER_AGENT,
    preflight_url_from_health_url,
    validate_cors_headers,
    validate_health_url,
    validate_https_origin,
)


def _headers(values: dict[str, str]) -> Message:
    message = Message()
    for key, value in values.items():
        message[key] = value
    return message


def test_cors_check_uses_browser_like_user_agent():
    assert CORS_CHECK_USER_AGENT.startswith("Mozilla/5.0 ")


def test_validate_health_url_requires_https_api_health():
    assert (
        validate_health_url("https://stock-scanner-beta-api.pcedison.workers.dev/api/health")
        == "https://stock-scanner-beta-api.pcedison.workers.dev/api/health"
    )
    with pytest.raises(RuntimeError, match="https URL"):
        validate_health_url("http://worker.example/api/health")


def test_validate_origin_requires_https_origin_only():
    assert validate_https_origin("https://stock-scanner-beta.pages.dev/") == "https://stock-scanner-beta.pages.dev"
    with pytest.raises(RuntimeError, match="origin"):
        validate_https_origin("https://stock-scanner-beta.pages.dev/path")


def test_preflight_url_uses_scan_market_endpoint():
    assert (
        preflight_url_from_health_url("https://worker.example/api/health")
        == "https://worker.example/api/scan/market"
    )


def test_validate_cors_headers_accepts_get_and_preflight_headers():
    origin = "https://stock-scanner-beta.pages.dev"
    validate_cors_headers(
        _headers(
            {
                "Access-Control-Allow-Origin": origin,
                "Access-Control-Allow-Credentials": "true",
            }
        ),
        origin,
    )
    validate_cors_headers(
        _headers(
            {
                "Access-Control-Allow-Origin": origin,
                "Access-Control-Allow-Credentials": "true",
                "Access-Control-Allow-Methods": "GET,POST,PUT,DELETE,OPTIONS",
                "Access-Control-Allow-Headers": "content-type,x-stock-scanner-csrf",
            }
        ),
        origin,
        require_post_preflight=True,
    )


def test_validate_cors_headers_rejects_missing_preflight_headers():
    with pytest.raises(RuntimeError, match="x-stock-scanner-csrf"):
        validate_cors_headers(
            _headers(
                {
                    "Access-Control-Allow-Origin": "https://stock-scanner-beta.pages.dev",
                    "Access-Control-Allow-Credentials": "true",
                    "Access-Control-Allow-Methods": "GET,OPTIONS",
                    "Access-Control-Allow-Headers": "content-type",
                }
            ),
            "https://stock-scanner-beta.pages.dev",
            require_post_preflight=True,
        )
