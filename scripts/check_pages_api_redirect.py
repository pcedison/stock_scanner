from __future__ import annotations

import argparse
import json
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, OpenerDirector, Request, build_opener

CHECK_USER_AGENT = "Mozilla/5.0 stock-scanner-pages-api-proxy-check/1.0"


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: N802
        return None


def validate_pages_api_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname != "stock-scanner-beta.pages.dev" or not parsed.path.startswith("/api/"):
        raise RuntimeError("URL must be an https stock-scanner-beta.pages.dev /api/* URL")
    return url


def check_pages_api_proxy(url: str, opener: OpenerDirector | None = None) -> dict:
    safe_url = validate_pages_api_url(url)
    opener = opener or build_opener(NoRedirect)
    request = Request(safe_url, headers={"Accept": "application/json", "User-Agent": CHECK_USER_AGENT})
    try:
        with opener.open(request, timeout=20) as response:
            status = int(getattr(response, "status", None) or response.getcode())
            body = response.read().decode("utf-8")
    except HTTPError as exc:
        if 300 <= exc.code < 400:
            location = exc.headers.get("location") or ""
            raise RuntimeError(f"expected Pages API proxy HTTP 200, got redirect HTTP {exc.code} to {location}") from exc
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Pages API proxy returned HTTP {exc.code}: {body[:500]}") from exc
    except URLError as exc:
        raise RuntimeError(f"Pages API proxy check failed to connect: {exc.reason}") from exc

    if status != 200:
        raise RuntimeError(f"expected Pages API proxy HTTP 200, got HTTP {status}")
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise RuntimeError("Pages API proxy did not return valid JSON") from exc
    if payload.get("runtime") != "cloudflare-python-worker":
        raise RuntimeError(f"Pages API proxy returned unexpected runtime: {payload.get('runtime')!r}")
    if payload.get("status") not in {"ok", "degraded"}:
        raise RuntimeError(f"Pages API proxy returned unexpected status: {payload.get('status')!r}")
    return payload


def check_pages_api_redirect(
    url: str,
    expected_origin: str | None = None,
    opener: OpenerDirector | None = None,
) -> dict:
    _ = expected_origin
    return check_pages_api_proxy(url, opener=opener)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify Pages /api/* proxies Worker API responses without redirects.")
    parser.add_argument("--url", required=True)
    args = parser.parse_args(argv)

    try:
        payload = check_pages_api_proxy(args.url)
    except RuntimeError as exc:
        print(f"Pages API proxy check failed: {exc}", file=sys.stderr)
        return 1

    print(json.dumps({"status": payload.get("status"), "runtime": payload.get("runtime")}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
