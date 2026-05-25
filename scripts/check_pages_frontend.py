from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen


ROOT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_INDEX = ROOT_DIR / "frontend" / "index.html"
PAGES_HOST_SUFFIX = ".pages.dev"
ASSET_PATTERN = re.compile(r"""(?:src|href)=["']([^"']+\?v=[^"']+)["']""")
CHECK_USER_AGENT = "Mozilla/5.0 stock-scanner-release-check/1.0"


def validate_pages_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme != "https" or not (parsed.hostname or "").endswith(PAGES_HOST_SUFFIX):
        raise RuntimeError("Pages frontend check URL must be an https://*.pages.dev URL")
    return url


def expected_cache_busted_assets(index_path: Path = DEFAULT_INDEX) -> list[str]:
    html = index_path.read_text(encoding="utf-8")
    return sorted(dict.fromkeys(ASSET_PATTERN.findall(html)))


def fetch_pages_html(url: str, timeout: int) -> str:
    safe_url = validate_pages_url(url)
    request = Request(safe_url, headers={"cache-control": "no-cache", "user-agent": CHECK_USER_AGENT})
    try:
        # validate_pages_url restricts requests to the Cloudflare Pages HTTPS domain.
        with urlopen(request, timeout=timeout) as response:  # nosec B310
            if response.status != 200:
                raise RuntimeError(f"{safe_url} returned HTTP {response.status}")
            return response.read().decode("utf-8", errors="replace")
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"{safe_url} returned HTTP {exc.code}: {body[:1000]}") from exc
    except URLError as exc:
        raise RuntimeError(f"{safe_url} could not be fetched: {exc}") from exc


def missing_assets(remote_html: str, expected_assets: Iterable[str]) -> list[str]:
    return [asset for asset in expected_assets if asset not in remote_html]


def validate_pages_frontend(url: str, index_path: Path = DEFAULT_INDEX, timeout: int = 10) -> dict[str, object]:
    expected = expected_cache_busted_assets(index_path)
    html = fetch_pages_html(url, timeout)
    missing = missing_assets(html, expected)
    if missing:
        raise RuntimeError(f"Pages frontend is missing cache-busted assets: {', '.join(missing)}")
    return {"url": validate_pages_url(url), "checkedAssets": expected, "ok": True}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify the public Cloudflare Pages frontend matches frontend/index.html cache-busted assets.")
    parser.add_argument("--url", required=True)
    parser.add_argument("--index", type=Path, default=DEFAULT_INDEX)
    parser.add_argument("--timeout", type=int, default=10)
    args = parser.parse_args(argv)

    try:
        result = validate_pages_frontend(args.url, args.index, args.timeout)
    except RuntimeError as exc:
        print(f"Pages frontend check failed: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
