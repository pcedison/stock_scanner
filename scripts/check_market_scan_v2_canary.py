from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin, urlsplit
from urllib.request import Request, urlopen

DISCLOSURES = ("announced", "pending")
CATEGORIES = ("entry", "watch", "excluded")
MAX_INDEX_BYTES = 50 * 1024
MAX_PAGE_BYTES = 500 * 1024


@dataclass(frozen=True)
class FetchResult:
    status: int
    body: bytes
    headers: dict[str, str]

    def json(self) -> dict[str, Any]:
        return json.loads(self.body.decode("utf-8"))


class PublicClient:
    def __init__(self, base_url: str, timeout_seconds: int):
        try:
            parsed = urlsplit(base_url)
        except ValueError as exc:
            raise RuntimeError("canary base URL must be a valid HTTPS URL") from exc
        if parsed.scheme.lower() != "https" or not parsed.netloc or parsed.username or parsed.password:
            raise RuntimeError("canary base URL must be a credential-free HTTPS URL")
        self.base_url = base_url.rstrip("/") + "/"
        self.origin = (parsed.scheme.lower(), parsed.netloc.lower())
        self.timeout_seconds = timeout_seconds

    def get(self, path: str, query: dict[str, Any] | None = None) -> FetchResult:
        suffix = path.lstrip("/")
        if query:
            suffix = f"{suffix}?{urlencode(query)}"
        target = urljoin(self.base_url, suffix)
        parsed_target = urlsplit(target)
        if (parsed_target.scheme.lower(), parsed_target.netloc.lower()) != self.origin:
            raise RuntimeError("canary requests must remain on the same-origin HTTPS endpoint")
        request = Request(
            target,
            headers={"Accept": "application/json", "User-Agent": "stock-scanner-v2-canary/1.0"},
            method="GET",
        )
        try:
            # The parsed target is constrained to same-origin HTTPS immediately above.
            with urlopen(request, timeout=self.timeout_seconds) as response:  # nosec B310
                return FetchResult(response.status, response.read(), {key.lower(): value for key, value in response.headers.items()})
        except HTTPError as exc:
            return FetchResult(exc.code, exc.read(), {key.lower(): value for key, value in exc.headers.items()})
        except URLError as exc:
            raise RuntimeError(f"GET {path} failed: {exc.reason}") from exc


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def _json_bytes(payload: Any) -> bytes:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _stock_code(item: dict[str, Any]) -> str:
    value = item.get("stockCode") or item.get("code")
    if not isinstance(value, str) or not value:
        raise RuntimeError("market item missing stockCode")
    return value


def legacy_identity_sets(payload: dict[str, Any]) -> dict[str, set[str]]:
    result: dict[str, set[str]] = {}
    for category in CATEGORIES:
        rows = payload.get(category) or []
        _require(isinstance(rows, list), f"legacy {category} is not a list")
        result[category] = {_stock_code(item) for item in rows}
    return result


def _validate_public_cache_headers(result: FetchResult, *, require_edge_hit: bool = False) -> None:
    cache_control = result.headers.get("cache-control", "")
    edge_control = result.headers.get("cloudflare-cdn-cache-control", "")
    _require("private" not in cache_control.lower(), "public v2 response must not be private")
    _require("no-store" not in cache_control.lower(), "public v2 response unexpectedly no-store")
    if require_edge_hit:
        _require("stale-while-revalidate" in edge_control, "missing Cloudflare edge stale-while-revalidate")


def _validate_no_store(result: FetchResult, label: str) -> None:
    _require("no-store" in result.headers.get("cache-control", "").lower(), f"{label} must be no-store")


def _validate_index(index: dict[str, Any], raw: bytes) -> None:
    _require(len(raw) < MAX_INDEX_BYTES, "market v2 index exceeds byte budget")
    _require(index.get("schemaVersion") == 2, "market index schemaVersion must be 2")
    _require(isinstance(index.get("generationId"), str) and len(index["generationId"]) == 24, "invalid generationId")
    for disclosure in DISCLOSURES:
        _require(disclosure in index.get("disclosures", {}), f"missing disclosure {disclosure}")
        for category in CATEGORIES:
            bucket = index["disclosures"][disclosure][category]
            _require(isinstance(bucket.get("pages"), list), f"missing pages for {disclosure}/{category}")


def _validate_page(
    page: dict[str, Any],
    raw: bytes,
    *,
    index: dict[str, Any],
    disclosure: str,
    category: str,
    cursor: int,
    reference: dict[str, Any],
) -> set[str]:
    _require(len(raw) < MAX_PAGE_BYTES, "market v2 page exceeds byte budget")
    _require(hashlib.sha256(raw).hexdigest() == reference["sha256"], "market v2 page hash mismatch")
    _require(page.get("generationId") == index["generationId"], "mixed market generation")
    _require(page.get("disclosure") == disclosure and page.get("category") == category, "page identity mismatch")
    _require(page.get("cursor") == cursor, "page cursor mismatch")
    rows = page.get("items")
    if not isinstance(rows, list):
        raise RuntimeError("page items must be a list")
    _require(len(rows) == reference["count"], "page item count mismatch")
    return {_stock_code(item) for item in rows}


def fetch_v2_identity_sets(
    client: PublicClient,
    *,
    index_path: str,
    results_path: str,
    expected_generation: str | None = None,
    require_edge_hit: bool = False,
) -> tuple[dict[str, set[str]], dict[str, Any]]:
    index_result = client.get(index_path)
    _require(index_result.status == 200, f"market index returned HTTP {index_result.status}")
    _validate_public_cache_headers(index_result, require_edge_hit=require_edge_hit)
    index = index_result.json()
    _validate_index(index, index_result.body)
    if expected_generation:
        _require(index["generationId"] == expected_generation, "market generation did not match expected generation")
    identities: dict[str, set[str]] = {category: set() for category in CATEGORIES}
    page_count = 0
    for disclosure in DISCLOSURES:
        for category in CATEGORIES:
            for reference in index["disclosures"][disclosure][category]["pages"]:
                query = {
                    "disclosure": disclosure,
                    "category": category,
                    "cursor": reference["cursor"],
                    "limit": index["pageSize"],
                    "generationId": index["generationId"],
                }
                page_result = client.get(results_path, query)
                _require(page_result.status == 200, f"market page returned HTTP {page_result.status}")
                _validate_public_cache_headers(page_result, require_edge_hit=require_edge_hit)
                identities[category] |= _validate_page(
                    page_result.json(),
                    page_result.body,
                    index=index,
                    disclosure=disclosure,
                    category=category,
                    cursor=reference["cursor"],
                    reference=reference,
                )
                page_count += 1
    return identities, {"generationId": index["generationId"], "pageCount": page_count}


def run_canary(
    *,
    base_url: str,
    legacy_path: str = "/api/scan/market",
    index_path: str = "/api/scan/market/index",
    results_path: str = "/api/scan/market/results",
    expected_generation: str | None = None,
    require_edge_hit: bool = False,
    timeout_seconds: int = 20,
) -> dict[str, Any]:
    client = PublicClient(base_url, timeout_seconds)
    legacy_result = client.get(legacy_path)
    _require(legacy_result.status == 200, f"legacy market returned HTTP {legacy_result.status}")
    _validate_no_store(legacy_result, "legacy market")
    legacy_sets = legacy_identity_sets(legacy_result.json())
    v2_sets, summary = fetch_v2_identity_sets(
        client,
        index_path=index_path,
        results_path=results_path,
        expected_generation=expected_generation,
        require_edge_hit=require_edge_hit,
    )
    _require(legacy_sets == v2_sets, "v1/v2 market identity parity mismatch")
    private_result = client.get("/api/auth/me")
    _validate_no_store(private_result, "auth/me")
    return {
        **summary,
        "categories": {category: len(codes) for category, codes in v2_sets.items()},
        "status": "ok",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate deployed market scan v1/v2 parity and cache boundaries.")
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--legacy-path", default="/api/scan/market")
    parser.add_argument("--index-path", default="/api/scan/market/index")
    parser.add_argument("--results-path", default="/api/scan/market/results")
    parser.add_argument("--expected-generation")
    parser.add_argument("--require-edge-hit", action="store_true")
    parser.add_argument("--timeout-seconds", type=int, default=20)
    args = parser.parse_args(argv)
    try:
        start = time.monotonic()
        summary = run_canary(
            base_url=args.base_url,
            legacy_path=args.legacy_path,
            index_path=args.index_path,
            results_path=args.results_path,
            expected_generation=args.expected_generation,
            require_edge_hit=args.require_edge_hit,
            timeout_seconds=args.timeout_seconds,
        )
        summary["elapsedSeconds"] = round(time.monotonic() - start, 3)
    except RuntimeError as exc:
        print(f"Market v2 canary failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
