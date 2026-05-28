from __future__ import annotations

import argparse
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, OpenerDirector, Request, build_opener


CHECK_USER_AGENT = "Mozilla/5.0 stock-scanner-pages-api-redirect-check/1.0"


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: N802
        return None


def validate_pages_api_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname != "stock-scanner-beta.pages.dev" or not parsed.path.startswith("/api/"):
        raise RuntimeError("URL must be an https stock-scanner-beta.pages.dev /api/* URL")
    return url


def validate_expected_origin(origin: str) -> str:
    parsed = urlparse(origin)
    if parsed.scheme != "https" or not parsed.netloc or parsed.path not in {"", "/"}:
        raise RuntimeError("expected origin must be an https origin")
    return f"{parsed.scheme}://{parsed.netloc}"


def check_pages_api_redirect(url: str, expected_origin: str, opener: OpenerDirector | None = None) -> str:
    safe_url = validate_pages_api_url(url)
    safe_origin = validate_expected_origin(expected_origin)
    opener = opener or build_opener(NoRedirect)
    request = Request(safe_url, headers={"Accept": "application/json", "User-Agent": CHECK_USER_AGENT})
    try:
        opener.open(request, timeout=20)
    except HTTPError as exc:
        if exc.code != 307:
            raise RuntimeError(f"expected HTTP 307, got HTTP {exc.code}") from exc
        location = exc.headers.get("location") or ""
    except URLError as exc:
        raise RuntimeError(f"redirect check failed to connect: {exc.reason}") from exc
    else:
        raise RuntimeError("expected HTTP 307 redirect, got HTTP 200")

    parsed_location = urlparse(location)
    actual_origin = f"{parsed_location.scheme}://{parsed_location.netloc}"
    if actual_origin != safe_origin:
        raise RuntimeError(f"redirect location origin {actual_origin!r} does not match {safe_origin!r}")
    if parsed_location.path != urlparse(safe_url).path:
        raise RuntimeError("redirect location does not preserve the /api path")
    return location


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify Pages /api/* redirects to the Worker API origin.")
    parser.add_argument("--url", required=True)
    parser.add_argument("--expected-origin", required=True)
    args = parser.parse_args(argv)

    try:
        location = check_pages_api_redirect(args.url, args.expected_origin)
    except RuntimeError as exc:
        print(f"Pages API redirect check failed: {exc}", file=sys.stderr)
        return 1

    print(f"Pages API redirects to {location}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
