from __future__ import annotations

import argparse
import sys
from email.message import Message
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

CORS_CHECK_USER_AGENT = "Mozilla/5.0 stock-scanner-cors-check/1.0"


def validate_https_origin(origin: str) -> str:
    parsed = urlparse(origin)
    if parsed.scheme != "https" or not parsed.netloc or parsed.path not in {"", "/"}:
        raise RuntimeError("origin must be an https origin, for example https://stock-scanner-beta.pages.dev")
    return f"{parsed.scheme}://{parsed.netloc}"


def validate_health_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.netloc or not parsed.path.endswith("/api/health"):
        raise RuntimeError("health URL must be an https URL ending in /api/health")
    return url


def preflight_url_from_health_url(url: str) -> str:
    parsed = urlparse(validate_health_url(url))
    path = parsed.path[: -len("/api/health")] + "/api/scan/market/refresh"
    return parsed._replace(path=path, query="", fragment="").geturl()


def _split_header_values(value: str | None) -> set[str]:
    return {item.strip().lower() for item in str(value or "").split(",") if item.strip()}


def validate_cors_headers(
    headers: Message,
    origin: str,
    *,
    require_post_preflight: bool = False,
) -> None:
    problems: list[str] = []
    if headers.get("access-control-allow-origin") != origin:
        problems.append("access-control-allow-origin does not echo the Pages origin")
    if str(headers.get("access-control-allow-credentials") or "").lower() != "true":
        problems.append("access-control-allow-credentials is not true")

    if require_post_preflight:
        methods = _split_header_values(headers.get("access-control-allow-methods"))
        allowed_headers = _split_header_values(headers.get("access-control-allow-headers"))
        if "post" not in methods or "options" not in methods:
            problems.append("preflight methods do not allow POST and OPTIONS")
        for header in ("content-type", "x-stock-scanner-csrf", "idempotency-key"):
            if header not in allowed_headers:
                problems.append(f"preflight headers do not allow {header}")

    if problems:
        raise RuntimeError("; ".join(problems))


def _request_headers(origin: str) -> dict[str, str]:
    return {
        "Accept": "application/json",
        "Origin": origin,
        "User-Agent": CORS_CHECK_USER_AGENT,
    }


def check_worker_cors(health_url: str, origin: str, timeout: int = 20) -> None:
    safe_url = validate_health_url(health_url)
    safe_origin = validate_https_origin(origin)
    try:
        with urlopen(Request(safe_url, headers=_request_headers(safe_origin)), timeout=timeout) as response:  # nosec B310
            validate_cors_headers(response.headers, safe_origin)

        preflight = Request(
            preflight_url_from_health_url(safe_url),
            headers={
                **_request_headers(safe_origin),
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "content-type,x-stock-scanner-csrf,idempotency-key",
            },
            method="OPTIONS",
        )
        with urlopen(preflight, timeout=timeout) as response:  # nosec B310
            validate_cors_headers(response.headers, safe_origin, require_post_preflight=True)
    except HTTPError as exc:
        raise RuntimeError(f"CORS check returned HTTP {exc.code}") from exc
    except URLError as exc:
        raise RuntimeError(f"CORS check failed to connect: {exc.reason}") from exc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate deployed Worker CORS for the Pages frontend.")
    parser.add_argument("--health-url", required=True, help="Full deployed Worker /api/health URL")
    parser.add_argument("--origin", required=True, help="Expected Pages origin")
    parser.add_argument("--timeout", type=int, default=20)
    args = parser.parse_args(argv)

    try:
        check_worker_cors(args.health_url, args.origin, timeout=args.timeout)
    except RuntimeError as exc:
        print(f"Cloudflare CORS check failed: {exc}", file=sys.stderr)
        return 1

    print(f"Cloudflare Worker CORS accepted {validate_https_origin(args.origin)}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
