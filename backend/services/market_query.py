from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

PAGE_SIZE = 100
MAX_INDEX_BYTES = 50 * 1024
MAX_PAGE_BYTES = 500 * 1024
DISCLOSURES = ("announced", "pending")
CATEGORIES = ("entry", "watch", "excluded")

_SCHEMA_VERSION = 2
_RESULT_FIELDS = ("stockCode", "companyName", "status", "summary", "reasons", "detailsAvailable", "hasFullDetails")
_USABLE_RULE_CODES = {"E3", "E4", "E6", "OFFICIAL_Q", "OFFICIAL_VALUATION"}
_NUMBER_PATTERN = re.compile(r"-?\d+(?:\.\d+)?")


@dataclass(frozen=True)
class MarketGeneration:
    generation_id: str
    index: dict[str, Any]
    files: dict[str, bytes]


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def canonical_json_bytes(payload: Any) -> bytes:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _normalize_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        _require(not any(not isinstance(key, str) for key in value), "market generation mappings require string keys")
        return {key: _normalize_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        normalized = [_normalize_json(item) for item in value]
        return sorted(normalized, key=canonical_json_bytes)
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    raise ValueError(f"market generation contains a non-JSON value: {type(value).__name__}")


def _reasons(result: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    reasons = result.get("reasons")
    return [reason for reason in reasons if isinstance(reason, Mapping)] if isinstance(reasons, list) else []


def _expected_financial_period(scan: Mapping[str, Any]) -> str | None:
    filing_context = scan.get("filingContext")
    if not isinstance(filing_context, Mapping):
        return None
    for field in ("freshnessFinancialReport", "activeFinancialReport"):
        report = filing_context.get(field)
        period = report.get("period") if isinstance(report, Mapping) else None
        if isinstance(period, str) and period:
            return period
    return None


def _has_published_data(result: Mapping[str, Any], expected_period: str | None) -> bool:
    reasons = _reasons(result)
    if expected_period and not any(
        reason.get("code") == "OFFICIAL_Q"
        and reason.get("severity") != "INSUFFICIENT_DATA"
        and expected_period in str(reason.get("message") or "")
        for reason in reasons
    ):
        return False
    status = result.get("status")
    if status and status != "INSUFFICIENT_DATA":
        return True
    return any(
        reason.get("code") in _USABLE_RULE_CODES and reason.get("severity") != "INSUFFICIENT_DATA" for reason in reasons
    )


def _page_item(result: Mapping[str, Any]) -> dict[str, Any]:
    _require(bool(str(result.get("stockCode") or "")), "market result requires stockCode")
    return _normalize_json({field: result[field] for field in _RESULT_FIELDS if field in result})


def _e4_per(result: Mapping[str, Any]) -> float | None:
    for reason in _reasons(result):
        if reason.get("code") != "E4":
            continue
        match = _NUMBER_PATTERN.search(str(reason.get("message") or "").replace(",", "", 1))
        if match:
            return float(match.group(0))
        return None
    return None


def _result_tiebreaker(result: Mapping[str, Any]) -> tuple[str, bytes]:
    return str(result.get("stockCode") or ""), canonical_json_bytes(_page_item(result))


def _sort_results(category: str, results: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    if category == "entry":
        ordered = sorted(
            results,
            key=lambda result: (
                _e4_per(result) is None,
                _e4_per(result) if _e4_per(result) is not None else math.inf,
                *_result_tiebreaker(result),
            ),
        )
    else:
        ordered = sorted(results, key=_result_tiebreaker)
    return [_page_item(result) for result in ordered]


def _validated_results(scan: Mapping[str, Any], category: str) -> list[Mapping[str, Any]]:
    raw = scan.get(category, [])
    _require(isinstance(raw, list), f"market scan {category} must be a list")
    _require(not any(not isinstance(item, Mapping) for item in raw), f"market scan {category} items must be objects")
    return list(raw)


def build_market_generation(
    scan: Mapping[str, Any],
    *,
    page_size: int = PAGE_SIZE,
    cache_status_inputs: Mapping[str, Any] | None = None,
) -> MarketGeneration:
    _require(page_size == PAGE_SIZE, "page_size must be 100")
    _require(isinstance(scan, Mapping), "market scan must be an object")
    _require(
        cache_status_inputs is None or isinstance(cache_status_inputs, Mapping),
        "cache_status_inputs must be an object",
    )

    normalized_scan = _normalize_json(scan)
    normalized_cache_inputs = _normalize_json(cache_status_inputs or {})
    identity_envelope = {"cacheStatusInputs": normalized_cache_inputs, "scan": normalized_scan}
    generation_id = hashlib.sha256(canonical_json_bytes(identity_envelope)).hexdigest()[:24]
    expected_period = _expected_financial_period(scan)

    grouped: dict[str, dict[str, list[dict[str, Any]]]] = {
        disclosure: {category: [] for category in CATEGORIES} for disclosure in DISCLOSURES
    }
    for category in CATEGORIES:
        by_disclosure: dict[str, list[Mapping[str, Any]]] = {disclosure: [] for disclosure in DISCLOSURES}
        for result in _validated_results(scan, category):
            normalized_result = _page_item(result)
            disclosure = "announced" if _has_published_data(normalized_result, expected_period) else "pending"
            by_disclosure[disclosure].append(normalized_result)
        for disclosure in DISCLOSURES:
            grouped[disclosure][category] = _sort_results(category, by_disclosure[disclosure])

    disclosures: dict[str, Any] = {}
    files: dict[str, bytes] = {}
    category_counts = dict.fromkeys(CATEGORIES, 0)
    disclosure_counts = dict.fromkeys(DISCLOSURES, 0)
    for disclosure in DISCLOSURES:
        disclosure_index: dict[str, Any] = {"count": 0}
        for category in CATEGORIES:
            items = grouped[disclosure][category]
            total = len(items)
            references = []
            for cursor in range(0, total, page_size):
                page_items = items[cursor : cursor + page_size]
                next_cursor = cursor + len(page_items) if cursor + len(page_items) < total else None
                page = {
                    "schemaVersion": _SCHEMA_VERSION,
                    "generationId": generation_id,
                    "disclosure": disclosure,
                    "category": category,
                    "cursor": cursor,
                    "limit": page_size,
                    "total": total,
                    "nextCursor": next_cursor,
                    "items": page_items,
                }
                content = canonical_json_bytes(page)
                _require(len(content) < MAX_PAGE_BYTES, "market page must be smaller than 500 KiB")
                object_key = f"public/market_scan/v2/{generation_id}/{disclosure}/{category}/{cursor}.json"
                files[object_key] = content
                references.append(
                    {
                        "key": object_key,
                        "cursor": cursor,
                        "count": len(page_items),
                        "bytes": len(content),
                        "sha256": hashlib.sha256(content).hexdigest(),
                    }
                )
            disclosure_index[category] = {"count": total, "pages": references}
            disclosure_index["count"] += total
            category_counts[category] += total
        disclosure_counts[disclosure] = disclosure_index["count"]
        disclosures[disclosure] = disclosure_index

    filing_context = scan.get("filingContext") if isinstance(scan.get("filingContext"), Mapping) else {}
    financial_freshness = scan.get("financialFreshness") if isinstance(scan.get("financialFreshness"), Mapping) else {}
    generation_index = {
        "schemaVersion": _SCHEMA_VERSION,
        "generationId": generation_id,
        "generatedAt": scan.get("generatedAt"),
        "pageSize": page_size,
        "detailMode": "summary",
        "disclosurePeriod": expected_period,
        "filingContext": _normalize_json(filing_context),
        "financialFreshness": _normalize_json(financial_freshness),
        "cacheStatusInputs": normalized_cache_inputs,
        "counts": {
            "universeSize": sum(disclosure_counts.values()),
            "announced": disclosure_counts["announced"],
            "pending": disclosure_counts["pending"],
            "categories": category_counts,
        },
        "disclosures": disclosures,
    }
    index_content = canonical_json_bytes(generation_index)
    _require(len(index_content) < MAX_INDEX_BYTES, "market index must be smaller than 50 KiB")
    generation_index_key = f"public/market_scan/v2/{generation_id}/index.json"
    files[generation_index_key] = index_content
    return MarketGeneration(generation_id=generation_id, index=generation_index, files=files)


def _load_page(pages: Mapping[str, Any], reference: Mapping[str, Any]) -> dict[str, Any]:
    key = reference.get("key")
    if not isinstance(key, str) or key not in pages:
        raise ValueError("referenced market page is missing")
    value = pages[key]
    raw = value if isinstance(value, bytes) else canonical_json_bytes(value)
    _require(len(raw) == reference["bytes"], "market page byte count mismatch")
    _require(len(raw) < MAX_PAGE_BYTES, "market page must be smaller than 500 KiB")
    _require(hashlib.sha256(raw).hexdigest() == reference.get("sha256"), "market page SHA-256 mismatch")
    try:
        decoded = json.loads(raw)
    except (TypeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("market page is malformed") from exc
    _require(isinstance(decoded, dict), "market page must be an object")
    return decoded


def _require_nonnegative_int(value: Any, field: str) -> int:
    if type(value) is not int or value < 0:
        raise ValueError(f"market {field} must be a non-negative integer")
    return value


def _validated_references(
    references: Any,
    generation_id: str,
    disclosure: str,
    category: str,
    total: int,
) -> list[Mapping[str, Any]]:
    _require(isinstance(references, list), "market index page references must be a list")
    expected_cursor = 0
    for reference in references:
        _require(isinstance(reference, Mapping), "market page reference must be an object")
        page_cursor = _require_nonnegative_int(reference.get("cursor"), "page cursor")
        page_count = _require_nonnegative_int(reference.get("count"), "page count")
        byte_count = _require_nonnegative_int(reference.get("bytes"), "page bytes")
        _require(page_cursor == expected_cursor, "market page references must be contiguous")
        expected_count = min(PAGE_SIZE, total - page_cursor)
        _require(expected_count > 0 and page_count == expected_count, "market page reference count mismatch")
        expected_key = f"public/market_scan/v2/{generation_id}/{disclosure}/{category}/{page_cursor}.json"
        key = reference.get("key")
        _require(key == expected_key, "market page reference key mismatch")
        _require(0 < byte_count < MAX_PAGE_BYTES, "market page reference bytes are out of bounds")
        sha256 = reference.get("sha256")
        _require(isinstance(sha256, str) and bool(re.fullmatch(r"[0-9a-f]{64}", sha256)), "invalid page SHA-256")
        expected_cursor += page_count
    _require(expected_cursor == total, "market page references must be contiguous through total")
    return references


def query_market_generation(
    index: Mapping[str, Any],
    pages: Mapping[str, Any],
    disclosure: str,
    category: str,
    cursor: int,
    limit: int,
) -> dict[str, Any]:
    _require(index.get("schemaVersion") == _SCHEMA_VERSION, "invalid market index schemaVersion")
    generation_id = index.get("generationId")
    if not isinstance(generation_id, str) or not re.fullmatch(r"[0-9a-f]{24}", generation_id):
        raise ValueError("invalid ID")
    _require(disclosure in DISCLOSURES, "invalid disclosure")
    _require(category in CATEGORIES, "invalid category")
    _require(type(cursor) is int and cursor >= 0, "cursor must be non-negative")
    _require(type(limit) is int and 1 <= limit <= PAGE_SIZE, "limit must be between 1 and 100")
    try:
        bucket = index["disclosures"][disclosure][category]
        total = _require_nonnegative_int(bucket["count"], "bucket total")
        references = _validated_references(bucket["pages"], generation_id, disclosure, category, total)
    except (KeyError, TypeError) as exc:
        raise ValueError("market index disclosure bucket is malformed") from exc
    requested_end = min(total, cursor + limit)
    items: list[Any] = []
    for reference in references:
        page_cursor = reference["cursor"]
        page_count = reference["count"]
        if page_cursor >= requested_end or page_cursor + page_count <= cursor:
            continue
        page = _load_page(pages, reference)
        expected_fields = {
            "schemaVersion": _SCHEMA_VERSION,
            "generationId": generation_id,
            "disclosure": disclosure,
            "category": category,
            "cursor": page_cursor,
            "total": total,
        }
        _require(
            not any(page.get(key) != value for key, value in expected_fields.items())
            and not any(type(page.get(key)) is not int for key in ("schemaVersion", "cursor", "total")),
            "market page identity mismatch",
        )
        _require(type(page.get("limit")) is int and page.get("limit") == PAGE_SIZE, "market page limit mismatch")
        expected_next_cursor = page_cursor + page_count if page_cursor + page_count < total else None
        _require(
            page.get("nextCursor") == expected_next_cursor
            and (expected_next_cursor is None or type(page.get("nextCursor")) is int),
            "market page nextCursor mismatch",
        )
        page_items = page.get("items")
        if not isinstance(page_items, list) or len(page_items) != page_count:
            raise ValueError("market page item count mismatch")
        start = max(cursor, page_cursor) - page_cursor
        end = min(requested_end, page_cursor + page_count) - page_cursor
        items.extend(page_items[start:end])
    expected_count = max(0, requested_end - min(cursor, total))
    _require(len(items) == expected_count, "market page range is incomplete")
    next_cursor = cursor + len(items) if cursor + len(items) < total else None
    return {
        "schemaVersion": _SCHEMA_VERSION,
        "generationId": generation_id,
        "disclosure": disclosure,
        "category": category,
        "cursor": cursor,
        "limit": limit,
        "total": total,
        "nextCursor": next_cursor,
        "items": items,
    }
