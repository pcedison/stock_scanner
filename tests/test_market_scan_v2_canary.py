from __future__ import annotations

import hashlib
import json
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest

from scripts import check_market_scan_v2_canary as canary
from scripts.check_market_scan_v2_canary import FetchResult

GENERATION = "0123456789abcdef01234567"


def raw_json(payload):
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def page_payload(category, cursor, codes):
    return {
        "schemaVersion": 2,
        "generationId": GENERATION,
        "disclosure": "announced",
        "category": category,
        "cursor": cursor,
        "limit": 100,
        "total": len(codes),
        "nextCursor": None,
        "items": [{"stockCode": code, "detailsAvailable": False, "hasFullDetails": False} for code in codes],
    }


def index_payload(page_bodies):
    def refs(category):
        body = page_bodies[category]
        return [
            {
                "key": f"public/market_scan/v2/{GENERATION}/announced/{category}/0.json",
                "cursor": 0,
                "count": len(json.loads(body)["items"]),
                "bytes": len(body),
                "sha256": hashlib.sha256(body).hexdigest(),
            }
        ]

    empty = {"count": 0, "pages": []}
    return {
        "schemaVersion": 2,
        "generationId": GENERATION,
        "generatedAt": "2026-07-13T00:00:00+00:00",
        "pageSize": 100,
        "detailMode": "summary",
        "disclosurePeriod": "2026Q1",
        "filingContext": {},
        "financialFreshness": {},
        "cacheStatusInputs": {},
        "counts": {
            "universeSize": 3,
            "announced": 3,
            "pending": 0,
            "categories": {"entry": 1, "watch": 1, "excluded": 1},
        },
        "disclosures": {
            "announced": {
                "count": 3,
                "entry": {"count": 1, "pages": refs("entry")},
                "watch": {"count": 1, "pages": refs("watch")},
                "excluded": {"count": 1, "pages": refs("excluded")},
            },
            "pending": {"count": 0, "entry": empty, "watch": empty, "excluded": empty},
        },
    }


class FakeClient:
    def __init__(self, base_url, timeout_seconds, *, hash_mismatch=False, mixed_generation=False, no_store_private=True):
        self.hash_mismatch = hash_mismatch
        self.mixed_generation = mixed_generation
        self.no_store_private = no_store_private
        self.page_bodies = {
            "entry": raw_json(page_payload("entry", 0, ["1101"])),
            "watch": raw_json(page_payload("watch", 0, ["2330"])),
            "excluded": raw_json(page_payload("excluded", 0, ["2882"])),
        }
        if mixed_generation:
            payload = json.loads(self.page_bodies["watch"])
            payload["generationId"] = "f" * 24
            self.page_bodies["watch"] = raw_json(payload)
        self.index = index_payload(self.page_bodies)
        if hash_mismatch:
            self.index["disclosures"]["announced"]["entry"]["pages"][0]["sha256"] = "f" * 64

    def get(self, path, query=None):
        public_headers = {
            "cache-control": "public, max-age=0",
            "cloudflare-cdn-cache-control": "public, max-age=300, stale-while-revalidate=60, stale-if-error=3600",
        }
        if path == "/api/scan/market":
            return FetchResult(
                200,
                raw_json(
                    {
                        "entry": [{"stockCode": "1101"}],
                        "watch": [{"stockCode": "2330"}],
                        "excluded": [{"stockCode": "2882"}],
                    }
                ),
                {"cache-control": "no-store"},
            )
        if path == "/api/scan/market/index":
            return FetchResult(200, raw_json(self.index), public_headers)
        if path == "/api/scan/market/results":
            category = query["category"]
            body = self.page_bodies[category]
            return FetchResult(200, body, public_headers)
        if path == "/api/auth/me":
            return FetchResult(200, raw_json({"authenticated": False}), {"cache-control": "no-store" if self.no_store_private else "public"})
        raise AssertionError(f"unexpected path {path} {query}")


def test_canary_validates_v1_v2_parity_hashes_generations_and_no_store(monkeypatch):
    monkeypatch.setattr(canary, "PublicClient", FakeClient)

    summary = canary.run_canary(base_url="https://worker.example")

    assert summary["status"] == "ok"
    assert summary["generationId"] == GENERATION
    assert summary["categories"] == {"entry": 1, "watch": 1, "excluded": 1}


def test_canary_rejects_hash_mismatch(monkeypatch):
    monkeypatch.setattr(
        canary,
        "PublicClient",
        lambda base_url, timeout_seconds: FakeClient(base_url, timeout_seconds, hash_mismatch=True),
    )

    with pytest.raises(RuntimeError, match="hash mismatch"):
        canary.run_canary(base_url="https://worker.example")


def test_canary_rejects_mixed_generation(monkeypatch):
    monkeypatch.setattr(
        canary,
        "PublicClient",
        lambda base_url, timeout_seconds: FakeClient(base_url, timeout_seconds, mixed_generation=True),
    )

    with pytest.raises(RuntimeError, match="mixed market generation"):
        canary.run_canary(base_url="https://worker.example")


def test_canary_rejects_private_response_without_no_store(monkeypatch):
    monkeypatch.setattr(
        canary,
        "PublicClient",
        lambda base_url, timeout_seconds: FakeClient(base_url, timeout_seconds, no_store_private=False),
    )

    with pytest.raises(RuntimeError, match="auth/me must be no-store"):
        canary.run_canary(base_url="https://worker.example")


def test_public_client_never_sends_credentials(monkeypatch):
    captured: dict[str, Any] = {}

    class Response:
        status = 200
        headers = {"cache-control": "no-store"}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return b"{}"

    def fake_urlopen(request, timeout):
        captured["headers"] = dict(request.header_items())
        captured["url"] = request.full_url
        return Response()

    monkeypatch.setattr(canary, "urlopen", fake_urlopen)
    client = canary.PublicClient("https://worker.example", 5)
    client.get("/api/scan/market/results", {"category": "entry", "cursor": 0})

    assert "Cookie" not in captured["headers"]
    captured_url = captured["url"]
    assert isinstance(captured_url, str)
    assert parse_qs(urlparse(captured_url).query)["category"] == ["entry"]


@pytest.mark.parametrize(
    "base_url",
    ("http://worker.example", "file:///tmp/worker", "https://user:password@worker.example"),
)
def test_public_client_rejects_non_https_or_credentialed_base_urls(base_url):
    with pytest.raises(RuntimeError, match="HTTPS"):
        canary.PublicClient(base_url, 5)


def test_public_client_rejects_cross_origin_paths(monkeypatch):
    monkeypatch.setattr(canary, "urlopen", lambda *_args, **_kwargs: pytest.fail("urlopen must not run"))
    client = canary.PublicClient("https://worker.example", 5)

    with pytest.raises(RuntimeError, match="same-origin HTTPS"):
        client.get("https://attacker.example/data")
