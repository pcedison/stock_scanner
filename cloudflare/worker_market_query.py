from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

PAGE_SIZE = 100
MAX_CURSOR = 2_000_000
MAX_INDEX_BYTES = 50 * 1024
MAX_PAGE_BYTES = 500 * 1024
DISCLOSURES = frozenset({"announced", "pending"})
CATEGORIES = frozenset({"entry", "watch", "excluded"})
GENERATION_PATTERN = re.compile(r"[0-9a-f]{24}")
INDEX_FIELDS = frozenset(
    {
        "schemaVersion",
        "generationId",
        "generatedAt",
        "pageSize",
        "detailMode",
        "disclosurePeriod",
        "filingContext",
        "financialFreshness",
        "cacheStatusInputs",
        "counts",
        "disclosures",
    }
)
PAGE_FIELDS = frozenset(
    {"schemaVersion", "generationId", "disclosure", "category", "cursor", "limit", "total", "nextCursor", "items"}
)
REFERENCE_FIELDS = frozenset({"key", "cursor", "count", "bytes", "sha256"})
ITEM_FIELDS = frozenset(
    {"stockCode", "companyName", "status", "summary", "reasons", "detailsAvailable", "hasFullDetails"}
)
REASON_FIELDS = frozenset({"code", "title", "passed", "severity", "message"})
REASON_CODES = frozenset({"E4", "OFFICIAL_Q", "OFFICIAL_VALUATION", "X1", "X2", "X3", "X4", "X5"})
INDEX_ROUTE = "/api/scan/market/index"
RESULTS_ROUTE = "/api/scan/market/results"
ROUTES = frozenset({INDEX_ROUTE, RESULTS_ROUTE})


@dataclass(frozen=True)
class MarketResultsQuery:
    disclosure: str
    category: str
    cursor: int
    limit: int
    generation_id: str | None


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def _first_query_value(query: Mapping[str, list[str]], field: str, *, required: bool = True) -> str | None:
    values = query.get(field)
    if values is None and not required:
        return None
    _require(isinstance(values, list) and len(values) == 1, f"invalid {field}")
    value = values[0]
    _require(isinstance(value, str) and bool(value), f"invalid {field}")
    return value


def _bounded_int(value: str | None, field: str, minimum: int, maximum: int) -> int:
    _require(isinstance(value, str) and bool(re.fullmatch(r"[0-9]+", value)), f"invalid {field}")
    _require(len(value) <= 32, f"invalid {field}")
    significant = value.lstrip("0") or "0"
    _require(len(significant) <= len(str(maximum)), f"invalid {field}")
    parsed = int(value)
    _require(minimum <= parsed <= maximum, f"invalid {field}")
    return parsed


def parse_market_results_query(query: Mapping[str, list[str]]) -> MarketResultsQuery:
    disclosure = _first_query_value(query, "disclosure")
    category = _first_query_value(query, "category")
    cursor = _bounded_int(_first_query_value(query, "cursor"), "cursor", 0, MAX_CURSOR)
    limit = _bounded_int(_first_query_value(query, "limit"), "limit", 1, PAGE_SIZE)
    generation_id = _first_query_value(query, "generationId", required=False)
    _require(disclosure in DISCLOSURES, "invalid disclosure")
    _require(category in CATEGORIES, "invalid category")
    _require(generation_id is None or bool(GENERATION_PATTERN.fullmatch(generation_id)), "invalid generationId")
    return MarketResultsQuery(disclosure, category, cursor, limit, generation_id)


def _nonnegative_int(value: Any, field: str) -> int:
    _require(type(value) is int and value >= 0, f"invalid {field}")
    return value


def _index_identity(index: Mapping[str, Any]) -> str:
    _require(frozenset(index) == INDEX_FIELDS, "invalid index fields")
    _require(index.get("schemaVersion") == 2 and type(index.get("schemaVersion")) is int, "invalid index schema")
    _require(index.get("pageSize") == PAGE_SIZE and type(index.get("pageSize")) is int, "invalid index pageSize")
    _require(index.get("detailMode") == "summary", "invalid index detailMode")
    generated_at = index.get("generatedAt")
    _require(isinstance(generated_at, str) and bool(generated_at), "invalid index generatedAt")
    try:
        datetime.fromisoformat(generated_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("invalid index generatedAt") from exc
    _require(
        index.get("disclosurePeriod") is None or isinstance(index.get("disclosurePeriod"), str),
        "invalid index disclosurePeriod",
    )
    for field in ("filingContext", "financialFreshness", "cacheStatusInputs"):
        _require(isinstance(index.get(field), Mapping), f"invalid index {field}")
    generation_id = index.get("generationId")
    _require(isinstance(generation_id, str) and bool(GENERATION_PATTERN.fullmatch(generation_id)), "invalid index ID")
    _require(len(_canonical_bytes(index)) < MAX_INDEX_BYTES, "market index is too large")
    return generation_id


def _bucket(index: Mapping[str, Any], query: MarketResultsQuery) -> tuple[str, int, list[Mapping[str, Any]]]:
    generation_id = _index_identity(index)
    if query.generation_id is not None:
        _require(query.generation_id == generation_id, "generation mismatch")
    try:
        bucket = index["disclosures"][query.disclosure][query.category]
        _require(frozenset(bucket) == frozenset({"count", "pages"}), "invalid index bucket fields")
        total = _nonnegative_int(bucket["count"], "bucket total")
        references = bucket["pages"]
    except (KeyError, TypeError) as exc:
        raise ValueError("invalid index bucket") from exc
    _require(isinstance(references, list), "invalid page references")

    expected_cursor = 0
    for reference in references:
        _require(isinstance(reference, Mapping), "invalid page reference")
        _require(frozenset(reference) == REFERENCE_FIELDS, "invalid page reference fields")
        cursor = _nonnegative_int(reference.get("cursor"), "page cursor")
        count = _nonnegative_int(reference.get("count"), "page count")
        byte_count = _nonnegative_int(reference.get("bytes"), "page bytes")
        _require(cursor == expected_cursor, "page references are not contiguous")
        _require(count == min(PAGE_SIZE, total - cursor) and count > 0, "invalid page count")
        key = f"public/market_scan/v2/{generation_id}/{query.disclosure}/{query.category}/{cursor}.json"
        _require(reference.get("key") == key, "invalid page key")
        _require(0 < byte_count < MAX_PAGE_BYTES, "invalid page bytes")
        digest = reference.get("sha256")
        _require(isinstance(digest, str) and bool(re.fullmatch(r"[0-9a-f]{64}", digest)), "invalid page hash")
        expected_cursor += count
    _require(expected_cursor == total, "page references do not cover total")
    return generation_id, total, references


def select_page_references(index: Mapping[str, Any], query: MarketResultsQuery) -> list[Mapping[str, Any]]:
    _generation_id, total, references = _bucket(index, query)
    requested_end = min(total, query.cursor + query.limit)
    selected = [
        reference
        for reference in references
        if reference["cursor"] < requested_end and reference["cursor"] + reference["count"] > query.cursor
    ]
    _require(len(selected) <= 2, "market query requires too many physical pages")
    return selected


def _canonical_bytes(payload: Any) -> bytes:
    try:
        return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError("market query JSON is malformed") from exc


def _decode_page(value: Any, reference: Mapping[str, Any]) -> Mapping[str, Any]:
    raw = value if isinstance(value, bytes) else _canonical_bytes(value)
    _require(len(raw) == reference["bytes"] and len(raw) < MAX_PAGE_BYTES, "market page byte count mismatch")
    _require(hashlib.sha256(raw).hexdigest() == reference["sha256"], "market page hash mismatch")
    try:
        page = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("market page is malformed") from exc
    _require(isinstance(page, Mapping), "market page is malformed")
    return page


def _valid_page_reason(reason: Any) -> bool:
    return (
        isinstance(reason, Mapping)
        and frozenset(reason).issubset(REASON_FIELDS)
        and reason.get("code") in REASON_CODES
        and all(isinstance(reason[field], str) for field in ("code", "title", "severity", "message") if field in reason)
        and ("passed" not in reason or type(reason["passed"]) is bool)
    )


def _valid_page_item(item: Any) -> bool:
    return (
        isinstance(item, Mapping)
        and frozenset(item).issubset(ITEM_FIELDS)
        and {"stockCode", "detailsAvailable", "hasFullDetails"}.issubset(item)
        and isinstance(item.get("stockCode"), str)
        and bool(item.get("stockCode"))
        and all(isinstance(item[field], str) for field in ("companyName", "status", "summary") if field in item)
        and all(type(item[field]) is bool for field in ("detailsAvailable", "hasFullDetails"))
        and ("reasons" not in item or (isinstance(item["reasons"], list) and all(map(_valid_page_reason, item["reasons"]))))
    )


def merge_market_pages(
    index: Mapping[str, Any], pages: Mapping[str, Any], query: MarketResultsQuery
) -> dict[str, Any]:
    generation_id, total, _references = _bucket(index, query)
    selected = select_page_references(index, query)
    requested_end = min(total, query.cursor + query.limit)
    items: list[Any] = []
    for reference in selected:
        key = reference["key"]
        _require(key in pages, "market page is missing")
        page = _decode_page(pages[key], reference)
        _require(frozenset(page) == PAGE_FIELDS, "market page fields are invalid")
        cursor = reference["cursor"]
        expected_identity = {
            "schemaVersion": 2,
            "generationId": generation_id,
            "disclosure": query.disclosure,
            "category": query.category,
            "cursor": cursor,
            "limit": PAGE_SIZE,
            "total": total,
        }
        numeric_identity = ("schemaVersion", "cursor", "limit", "total")
        _require(
            all(type(page.get(field)) is int for field in numeric_identity)
            and all(page.get(field) == value for field, value in expected_identity.items()),
            "market page identity mismatch",
        )
        expected_next = cursor + reference["count"] if cursor + reference["count"] < total else None
        _require(
            page.get("nextCursor") == expected_next
            and (page.get("nextCursor") is None or type(page.get("nextCursor")) is int),
            "market page identity mismatch",
        )
        page_items = page.get("items")
        _require(isinstance(page_items, list) and len(page_items) == reference["count"], "market page item count mismatch")
        for item in page_items:
            _require(_valid_page_item(item), "market page item is invalid")
        start = max(query.cursor, cursor) - cursor
        end = min(requested_end, cursor + reference["count"]) - cursor
        items.extend(page_items[start:end])

    expected_count = max(0, requested_end - min(query.cursor, total))
    _require(len(items) == expected_count, "market page range is incomplete")
    next_cursor = query.cursor + len(items) if query.cursor + len(items) < total else None
    return {
        "schemaVersion": 2,
        "generationId": generation_id,
        "disclosure": query.disclosure,
        "category": query.category,
        "cursor": query.cursor,
        "limit": query.limit,
        "total": total,
        "nextCursor": next_cursor,
        "items": items,
    }


def validate_market_index(index: Any) -> Mapping[str, Any]:
    _require(isinstance(index, Mapping), "market index is unavailable")
    _index_identity(index)
    disclosures = index.get("disclosures")
    _require(isinstance(disclosures, Mapping) and frozenset(disclosures) == DISCLOSURES, "invalid index disclosures")
    counts = index.get("counts")
    _require(
        isinstance(counts, Mapping)
        and frozenset(counts) == frozenset({"universeSize", "announced", "pending", "categories"}),
        "invalid index counts",
    )
    category_counts = counts.get("categories")
    _require(
        isinstance(category_counts, Mapping) and frozenset(category_counts) == CATEGORIES,
        "invalid index counts categories",
    )
    expected_categories = dict.fromkeys(CATEGORIES, 0)
    expected_disclosures = {}
    for disclosure in DISCLOSURES:
        disclosure_index = disclosures[disclosure]
        _require(
            isinstance(disclosure_index, Mapping)
            and frozenset(disclosure_index) == frozenset({"count", *CATEGORIES}),
            "invalid index disclosure fields",
        )
        disclosure_total = 0
        for category in CATEGORIES:
            _generation_id, total, _references = _bucket(
                index, MarketResultsQuery(disclosure, category, 0, PAGE_SIZE, None)
            )
            disclosure_total += total
            expected_categories[category] += total
        _require(
            _nonnegative_int(disclosure_index.get("count"), "disclosure count") == disclosure_total,
            "invalid index disclosure aggregate",
        )
        expected_disclosures[disclosure] = disclosure_total
    universe = sum(expected_disclosures.values())
    _require(_nonnegative_int(counts.get("universeSize"), "universe count") == universe, "invalid index counts")
    for disclosure, total in expected_disclosures.items():
        _require(_nonnegative_int(counts.get(disclosure), f"{disclosure} count") == total, "invalid index counts")
    for category, total in expected_categories.items():
        _require(
            _nonnegative_int(category_counts.get(category), f"{category} count") == total,
            "invalid index counts categories",
        )
    return index


def _error(error_response, api, detail: str, status: int, code: str):
    return error_response(
        detail,
        status=status,
        code=code,
        request_id=getattr(api, "_request_id", None),
        retryable=status == 503,
        stage="market_query",
    )


def _wire_json_bytes(payload: Any) -> bytes:
    try:
        return json.dumps(payload, ensure_ascii=False).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError("market response JSON is malformed") from exc


async def _r2_json_bytes(
    api,
    key: str,
    max_bytes: int,
    dependency_call,
    dependency_failure_type,
    *,
    require_canonical: bool = True,
):
    obj = await dependency_call(lambda: api.env.CACHE.get(key), dependency_failure_type, "r2_read", True)
    if obj is None:
        return None, None
    try:
        size = getattr(obj, "size", None)
        _require(type(size) is int and 0 <= size < max_bytes, "market R2 object size is invalid")
        read_body = getattr(obj, "arrayBuffer", None)
        _require(callable(read_body), "market R2 object body is unavailable")
    except (TypeError, ValueError) as exc:
        raise ValueError("market R2 object is malformed") from exc
    buffer = await dependency_call(lambda: read_body(), dependency_failure_type, "r2_read", True)
    try:
        converted = buffer.to_py() if callable(getattr(buffer, "to_py", None)) else buffer
        raw = bytes(converted)
        _require(len(raw) == size and len(raw) < max_bytes, "market R2 object byte count mismatch")
        payload = json.loads(raw.decode("utf-8"))
        if require_canonical:
            _require(_canonical_bytes(payload) == raw, "market R2 object is not canonical")
        return payload, raw
    except (UnicodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise ValueError("market R2 object is malformed") from exc
    except Exception as exc:
        raise ValueError("market R2 object is malformed") from exc


async def _market_index_response(api, json_response, error_response, dependency_call, dependency_failure_type):
    try:
        pointer, _pointer_raw = await _r2_json_bytes(
            api, "public/market_scan_index.json", MAX_INDEX_BYTES, dependency_call, dependency_failure_type
        )
        index = validate_market_index(pointer)
        manifest, _manifest_raw = await _r2_json_bytes(
            api,
            "public/manifest.json",
            1024 * 1024,
            dependency_call,
            dependency_failure_type,
            require_canonical=False,
        )
        _require(manifest is None or isinstance(manifest, Mapping), "market manifest is malformed")
        policy = api.cache_policy()
        payload = dict(index)
        payload["cacheStatus"] = api.cache_status_from_manifest(
            manifest or {},
            {"status": "fresh", "reason": policy["reason"]},
        )
        _require(len(_wire_json_bytes(payload)) < MAX_INDEX_BYTES, "market index response exceeds wire budget")
    except (TypeError, ValueError):
        return _error(error_response, api, "Market query data is temporarily unavailable", 503, "market_query_unavailable")
    return json_response(payload, headers=api.market_v2_cache_headers())


async def _market_results_response(
    api, raw_query, json_response, error_response, dependency_call, dependency_failure_type
):
    try:
        query = parse_market_results_query(raw_query)
    except ValueError:
        return _error(error_response, api, "Invalid market query", 422, "market_query_invalid")

    key = (
        "public/market_scan_index.json"
        if query.generation_id is None
        else f"public/market_scan/v2/{query.generation_id}/index.json"
    )
    try:
        index, _index_raw = await _r2_json_bytes(
            api, key, MAX_INDEX_BYTES, dependency_call, dependency_failure_type
        )
        if query.generation_id is not None and index is None:
            return _error(error_response, api, "Requested generation is unavailable", 409, "generation_mismatch")
        validated_index = validate_market_index(index)
        if query.generation_id is not None and validated_index.get("generationId") != query.generation_id:
            return _error(error_response, api, "Requested generation is unavailable", 409, "generation_mismatch")
        references = select_page_references(validated_index, query)
    except ValueError:
        return _error(error_response, api, "Market query data is temporarily unavailable", 503, "market_query_unavailable")

    pages = {}
    for reference in references:
        try:
            _page, raw = await _r2_json_bytes(
                api, reference["key"], MAX_PAGE_BYTES, dependency_call, dependency_failure_type
            )
        except ValueError:
            return _error(error_response, api, "Market query data is temporarily unavailable", 503, "market_query_unavailable")
        if raw is None:
            return _error(error_response, api, "Market query data is temporarily unavailable", 503, "market_query_unavailable")
        pages[reference["key"]] = raw
    try:
        payload = merge_market_pages(validated_index, pages, query)
        _require(len(_wire_json_bytes(payload)) < MAX_PAGE_BYTES, "market results response exceeds wire budget")
    except ValueError:
        return _error(error_response, api, "Market query data is temporarily unavailable", 503, "market_query_unavailable")
    return json_response(payload, headers=api.market_v2_cache_headers())


async def route(api, path: str, query, json_response, error_response, dependency_call, dependency_failure_type):
    if path == INDEX_ROUTE:
        return await _market_index_response(api, json_response, error_response, dependency_call, dependency_failure_type)
    return await _market_results_response(
        api, query, json_response, error_response, dependency_call, dependency_failure_type
    )
