from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import stat
import sys
import zipfile
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))
_ROOT_DIR = _SCRIPTS_DIR.parent
if str(_ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(_ROOT_DIR))
from hydrate_cloudflare_seed_inputs import monthly_history_coverage  # noqa: E402
from seed_utils import find_seed_zip  # noqa: E402

from backend.services.market_query import build_market_generation, canonical_json_bytes  # noqa: E402

DEFAULT_ZIP = find_seed_zip(Path("data"))


def default_failed_companies_csv(data_dir: Path = Path("data")) -> Path:
    """The newest quarterly follow-up CSV; labels are ``YYYYQn`` so lexical order is chronological."""
    candidates = sorted(data_dir.glob("official_history_failed_companies_*.csv"))
    return candidates[-1] if candidates else data_dir / "official_history_failed_companies_latest.csv"


DEFAULT_FAILED_COMPANIES_CSV = default_failed_companies_csv()
REQUIRED_ENTRIES = {
    "official_fundamentals_history.json",
    "official_history_backfill_progress.json",
    "monthly_revenue_history.json",
    "cloudflare_seed/manifest.json",
    "cloudflare_seed/companies.json",
    "cloudflare_seed/data_sources_status.json",
    "cloudflare_seed/market_scan_latest.json",
    "cloudflare_seed/market_scan_index.json",
    "cloudflare_seed/analysis_by_code.json",
    "cloudflare_seed/holding_analysis_by_code.json",
}
MIN_HISTORY_COMPANIES = 1000
MIN_HISTORY_ROWS = 5000
MIN_SEED_COMPANIES = 1000
MIN_SEED_TWSE_COMPANIES = 1000
MIN_SEED_TPEX_COMPANIES = 700
MIN_SEED_ANALYSIS = 1000
MIN_CONSECUTIVE_MONTHLY_COMPANIES = 1000
MARKET_SCAN_CATEGORIES = ("entry", "watch", "excluded")
MARKET_SCAN_DISCLOSURES = ("announced", "pending")
MARKET_PAGE_SIZE = 100
MAX_MARKET_INDEX_BYTES = 50 * 1024
MAX_MARKET_PAGE_BYTES = 500 * 1024
MAX_ZIP_MEMBER_BYTES = 64 * 1024 * 1024
MAX_ZIP_UNCOMPRESSED_BYTES = 256 * 1024 * 1024
MARKET_GENERATION_ID_RE = re.compile(r"[0-9a-f]{24}")
MARKET_PAGE_SHA_RE = re.compile(r"[0-9a-f]{64}")
USABLE_MARKET_REASON_CODES = {"E3", "E4", "E6", "OFFICIAL_Q", "OFFICIAL_VALUATION"}
MARKET_DISCLOSURE_FIELDS = {"count", *MARKET_SCAN_CATEGORIES}
MARKET_CATEGORY_FIELDS = {"count", "pages"}
MARKET_REFERENCE_FIELDS = {"key", "cursor", "count", "bytes", "sha256"}
SUMMARY_RESULT_KEYS = ("stockCode", "companyName", "status", "summary")
SUMMARY_REASON_KEYS = ("code", "title", "passed", "severity", "message")
SUMMARY_REASON_CODES = {"E4", "OFFICIAL_Q", "OFFICIAL_VALUATION", "X1", "X2", "X3", "X4", "X5"}
LOCAL_USER_PATH_RE = re.compile(r"(?:[A-Za-z]:[\\/]+Users[\\/]+|/(?:Users|home)/)", re.IGNORECASE)


def _load_json_from_zip(archive: zipfile.ZipFile, name: str) -> Any:
    try:
        info = archive.getinfo(name)
        if not (0 < info.file_size <= MAX_ZIP_MEMBER_BYTES):
            raise ValueError(f"Seed entry exceeds the uncompressed size limit: {name}")
        with archive.open(info) as handle:
            raw = handle.read(MAX_ZIP_MEMBER_BYTES + 1)
        if len(raw) != info.file_size or len(raw) > MAX_ZIP_MEMBER_BYTES:
            raise ValueError(f"Seed entry exceeds the uncompressed size limit: {name}")
        return json.loads(raw.decode("utf-8"))
    except KeyError as exc:
        raise ValueError(f"Missing required seed entry: {name}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"Seed entry is not valid JSON: {name}") from exc


def _reject_local_user_paths(payload: Any, entry_name: str, location: str = "$") -> None:
    if isinstance(payload, str):
        if LOCAL_USER_PATH_RE.search(payload):
            raise ValueError(f"Public seed entry {entry_name} contains a local user path at {location}")
        return
    if isinstance(payload, dict):
        for key, value in payload.items():
            if isinstance(key, str) and LOCAL_USER_PATH_RE.search(key):
                raise ValueError(f"Public seed entry {entry_name} contains a local user path in an object key")
            _reject_local_user_paths(value, entry_name, f"{location}.{key}")
        return
    if isinstance(payload, list):
        for index, value in enumerate(payload):
            _reject_local_user_paths(value, entry_name, f"{location}[{index}]")


def _manifest_count(payload: dict[str, Any], key: str, label: str) -> int:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{label}.{key} must be a non-negative integer")
    return value


def _nonnegative_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{label} must be a non-negative integer")
    return value


def _require_exact_market_fields(payload: dict[str, Any], expected: set[str], label: str) -> None:
    if set(payload) != expected:
        raise ValueError(f"unexpected market index fields in {label}")


def _strict_market_index_counts(pointer: dict[str, Any]) -> tuple[dict[str, int], dict[str, int]]:
    counts = pointer.get("counts")
    if not isinstance(counts, dict):
        raise ValueError("market index counts must be an object")
    _require_exact_market_fields(
        counts,
        {"universeSize", "announced", "pending", "categories"},
        "counts",
    )
    strict_counts = {
        key: _nonnegative_int(counts.get(key), f"market index counts.{key}")
        for key in ("universeSize", "announced", "pending")
    }
    raw_categories = counts.get("categories")
    if not isinstance(raw_categories, dict):
        raise ValueError("market index counts.categories must be an object")
    _require_exact_market_fields(raw_categories, set(MARKET_SCAN_CATEGORIES), "counts.categories")
    strict_categories = {
        category: _nonnegative_int(raw_categories.get(category), f"market index counts.categories.{category}")
        for category in MARKET_SCAN_CATEGORIES
    }
    return strict_counts, strict_categories


def _raw_zip_member(archive: zipfile.ZipFile, name: str, *, maximum_bytes: int, label: str) -> bytes:
    try:
        info = archive.getinfo(name)
    except KeyError as exc:
        raise ValueError(f"Missing required seed entry: {name}") from exc
    if not (0 < info.file_size < maximum_bytes):
        raise ValueError(f"{label} size must be smaller than {maximum_bytes} bytes")
    return archive.read(info)


def _validate_zip_member_info(info: zipfile.ZipInfo) -> None:
    name = info.orig_filename
    parts = name.split("/")
    if (
        not name
        or "\\" in name
        or name.startswith("/")
        or re.match(r"^[A-Za-z]:", name)
        or any(part in {"", ".", ".."} for part in parts)
    ):
        raise ValueError(f"unsafe ZIP member name: {name}")
    unix_mode = info.external_attr >> 16
    if info.create_system == 3 and stat.S_ISLNK(unix_mode):
        raise ValueError(f"Seed zip contains a symbolic link member: {name}")


def _validate_archive_budget(member_infos: list[zipfile.ZipInfo]) -> None:
    if any(info.file_size < 0 or info.file_size > MAX_ZIP_MEMBER_BYTES for info in member_infos):
        raise ValueError("Seed zip member exceeds the uncompressed size limit")
    if sum(info.file_size for info in member_infos) > MAX_ZIP_UNCOMPRESSED_BYTES:
        raise ValueError("Seed zip total uncompressed size exceeds the allowed budget")


def _is_allowed_seed_member(name: str) -> bool:
    static_names = {
        *REQUIRED_ENTRIES,
        "cloudflare_seed/market_scan_summary.json",
    }
    if name in static_names or name.startswith("cloudflare_seed/market_scan/v2/"):
        return True
    return bool(
        re.fullmatch(r"cloudflare_seed/(?:analysis_shards|holding_analysis_shards)/[^/]+\.json", name)
    )


def _expected_cache_status_inputs(market_scan: dict[str, Any]) -> dict[str, Any]:
    filing_context = market_scan.get("filingContext")
    if not isinstance(filing_context, dict):
        filing_context = {}
    financial_freshness = market_scan.get("financialFreshness")
    latest_financial_period = None
    if isinstance(financial_freshness, dict):
        cached = financial_freshness.get("latestCachedFinancialPeriod")
        if isinstance(cached, str) and cached:
            latest_financial_period = cached
    if latest_financial_period is None:
        for field in ("freshnessFinancialReport", "activeFinancialReport"):
            report = filing_context.get(field)
            period = report.get("period") if isinstance(report, dict) else None
            if isinstance(period, str) and period:
                latest_financial_period = period
                break
    return {
        "generatedAt": market_scan.get("generatedAt"),
        "latestRevenuePeriod": filing_context.get("monthlyRevenuePeriod"),
        "latestFinancialPeriod": latest_financial_period,
    }


def _market_expected_period(market_scan: dict[str, Any]) -> str | None:
    filing_context = market_scan.get("filingContext")
    if not isinstance(filing_context, dict):
        return None
    for field in ("freshnessFinancialReport", "activeFinancialReport"):
        report = filing_context.get(field)
        period = report.get("period") if isinstance(report, dict) else None
        if isinstance(period, str) and period:
            return period
    return None


def _market_disclosure(item: dict[str, Any], expected_period: str | None) -> str:
    raw_reasons = item.get("reasons")
    reasons = [reason for reason in raw_reasons if isinstance(reason, dict)] if isinstance(raw_reasons, list) else []
    if expected_period and not any(
        reason.get("code") == "OFFICIAL_Q"
        and reason.get("severity") != "INSUFFICIENT_DATA"
        and expected_period in str(reason.get("message") or "")
        for reason in reasons
    ):
        return "pending"
    status = item.get("status")
    if status and status != "INSUFFICIENT_DATA":
        return "announced"
    if any(
        reason.get("code") in USABLE_MARKET_REASON_CODES and reason.get("severity") != "INSUFFICIENT_DATA"
        for reason in reasons
    ):
        return "announced"
    return "pending"


def _legacy_market_identities(market_scan: dict[str, Any]) -> Counter[tuple[str, str, str]]:
    expected_period = _market_expected_period(market_scan)
    identities: list[tuple[str, str, str]] = []
    for category in MARKET_SCAN_CATEGORIES:
        rows = market_scan.get(category)
        if not isinstance(rows, list):
            raise ValueError(f"cloudflare_seed/market_scan_latest.json {category} must be a JSON list")
        for index, item in enumerate(rows):
            if not isinstance(item, dict):
                raise ValueError(f"legacy market {category} item {index} must be an object")
            stock_code = item.get("stockCode")
            if not isinstance(stock_code, str) or not stock_code:
                raise ValueError(f"legacy market {category} item {index} requires stockCode")
            identities.append((stock_code, _market_disclosure(item, expected_period), category))
    counts = Counter(identities)
    if any(count != 1 for count in counts.values()):
        raise ValueError("legacy scan contains a duplicate market identity")
    return counts


def _compact_market_scan_for_generation(market_scan: dict[str, Any]) -> dict[str, Any]:
    compact = dict(market_scan)
    for category in (*MARKET_SCAN_CATEGORIES, "results"):
        items = compact.get(category)
        if not isinstance(items, list):
            continue
        compact_items = []
        for index, item in enumerate(items):
            if not isinstance(item, dict):
                raise ValueError(f"legacy market {category} item {index} must be an object")
            result = {key: item.get(key) for key in SUMMARY_RESULT_KEYS if key in item}
            reasons = item.get("reasons")
            if isinstance(reasons, list):
                result["reasons"] = [
                    {key: reason.get(key) for key in SUMMARY_REASON_KEYS if key in reason}
                    for reason in reasons
                    if isinstance(reason, dict) and str(reason.get("code") or "") in SUMMARY_REASON_CODES
                ]
            result["detailsAvailable"] = True
            result["hasFullDetails"] = False
            compact_items.append(result)
        compact[category] = compact_items
    compact["detailMode"] = "summary"
    return compact


def _validate_market_v2(
    archive: zipfile.ZipFile,
    names: set[str],
    manifest: dict[str, Any],
    market_scan: dict[str, Any],
) -> dict[str, Any]:
    pointer_name = "cloudflare_seed/market_scan_index.json"
    pointer_raw = _raw_zip_member(
        archive,
        pointer_name,
        maximum_bytes=MAX_MARKET_INDEX_BYTES,
        label="market index",
    )
    if not (0 < len(pointer_raw) < MAX_MARKET_INDEX_BYTES):
        raise ValueError("market index size must be smaller than 50 KiB")
    try:
        pointer = json.loads(pointer_raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("cloudflare_seed/market_scan_index.json is not valid UTF-8 JSON") from exc
    if not isinstance(pointer, dict):
        raise ValueError("cloudflare_seed/market_scan_index.json must be a JSON object")
    if pointer.get("schemaVersion") != 2:
        raise ValueError("market index schemaVersion must be 2")
    generation_id = pointer.get("generationId")
    if not isinstance(generation_id, str) or MARKET_GENERATION_ID_RE.fullmatch(generation_id) is None:
        raise ValueError("invalid market generation ID")
    if pointer.get("pageSize") != MARKET_PAGE_SIZE:
        raise ValueError("market index pageSize must be 100")
    strict_counts, strict_categories = _strict_market_index_counts(pointer)
    cache_status_inputs = pointer.get("cacheStatusInputs")
    if not isinstance(cache_status_inputs, dict):
        raise ValueError("market index cacheStatusInputs must be an object")
    expected_cache_status_inputs = _expected_cache_status_inputs(market_scan)
    if cache_status_inputs != expected_cache_status_inputs:
        raise ValueError("market index cacheStatusInputs do not match the legacy scan")
    content_generation = build_market_generation(
        _compact_market_scan_for_generation(market_scan),
        cache_status_inputs=expected_cache_status_inputs,
    )
    if content_generation.generation_id != generation_id:
        raise ValueError("market generationId does not match the content-derived generation ID")

    immutable_index_name = f"cloudflare_seed/market_scan/v2/{generation_id}/index.json"
    immutable_raw = _raw_zip_member(
        archive,
        immutable_index_name,
        maximum_bytes=MAX_MARKET_INDEX_BYTES,
        label="market index",
    )
    if pointer_raw != immutable_raw:
        raise ValueError("market pointer bytes do not match immutable generation index")

    disclosures = pointer.get("disclosures")
    if not isinstance(disclosures, dict):
        raise ValueError("market index disclosures must be an object")
    _require_exact_market_fields(disclosures, set(MARKET_SCAN_DISCLOSURES), "disclosures")
    expected_v2_entries = {immutable_index_name}
    seen_keys: set[str] = set()
    referenced_pages: list[tuple[dict[str, Any], str, str, int, str]] = []
    disclosure_counts = dict.fromkeys(MARKET_SCAN_DISCLOSURES, 0)
    category_counts = dict.fromkeys(MARKET_SCAN_CATEGORIES, 0)

    for disclosure in MARKET_SCAN_DISCLOSURES:
        disclosure_bucket = disclosures.get(disclosure)
        if not isinstance(disclosure_bucket, dict):
            raise ValueError(f"market index {disclosure} disclosure must be an object")
        _require_exact_market_fields(disclosure_bucket, MARKET_DISCLOSURE_FIELDS, disclosure)
        disclosure_total = 0
        for category in MARKET_SCAN_CATEGORIES:
            bucket = disclosure_bucket.get(category)
            if not isinstance(bucket, dict):
                raise ValueError(f"market index {disclosure}/{category} bucket must be an object")
            _require_exact_market_fields(bucket, MARKET_CATEGORY_FIELDS, f"{disclosure}/{category}")
            total = _nonnegative_int(bucket.get("count"), f"market {disclosure}/{category} count")
            references = bucket.get("pages")
            if not isinstance(references, list):
                raise ValueError(f"market {disclosure}/{category} pages must be a list")
            expected_cursor = 0
            for reference in references:
                if not isinstance(reference, dict):
                    raise ValueError("market page reference must be an object")
                _require_exact_market_fields(
                    reference,
                    MARKET_REFERENCE_FIELDS,
                    f"{disclosure}/{category} reference",
                )
                cursor = _nonnegative_int(reference.get("cursor"), "market page cursor")
                count = _nonnegative_int(reference.get("count"), "market page count")
                byte_count = _nonnegative_int(reference.get("bytes"), "market page bytes")
                if cursor != expected_cursor:
                    raise ValueError("market page references must be contiguous")
                key = reference.get("key")
                expected_key = f"public/market_scan/v2/{generation_id}/{disclosure}/{category}/{cursor}.json"
                if (
                    not isinstance(key, str)
                    or "\\" in key
                    or ".." in key.split("/")
                    or key.startswith("/")
                    or key != expected_key
                ):
                    raise ValueError(f"unsafe market page path: {key}")
                if key in seen_keys:
                    raise ValueError(f"market page reference occurs more than once: {key}")
                expected_count = min(MARKET_PAGE_SIZE, total - cursor)
                if expected_count <= 0 or count != expected_count:
                    raise ValueError("market page reference count does not match cursor continuity")
                if not (0 < byte_count < MAX_MARKET_PAGE_BYTES):
                    raise ValueError("market page size must be smaller than 500 KiB")
                sha256 = reference.get("sha256")
                if not isinstance(sha256, str) or MARKET_PAGE_SHA_RE.fullmatch(sha256) is None:
                    raise ValueError("market page SHA-256 is invalid")
                seen_keys.add(key)
                zip_name = f"cloudflare_seed/{key.removeprefix('public/')}"
                expected_v2_entries.add(zip_name)
                referenced_pages.append((reference, disclosure, category, total, zip_name))
                expected_cursor += count
            if expected_cursor != total:
                raise ValueError("market page references must be contiguous through bucket total")
            disclosure_total += total
            category_counts[category] += total
        if _nonnegative_int(disclosure_bucket.get("count"), f"market {disclosure} count") != disclosure_total:
            raise ValueError("market index aggregate counts do not match disclosure buckets")
        disclosure_counts[disclosure] = disclosure_total

    actual_v2_entries = {name for name in names if name.startswith("cloudflare_seed/market_scan/v2/")}
    if actual_v2_entries != expected_v2_entries:
        raise ValueError("market v2 generation files do not match pointer references")

    v2_identities: list[tuple[str, str, str]] = []
    page_sizes: list[int] = []
    page_bytes_by_key: dict[str, bytes] = {}
    for reference, disclosure, category, total, zip_name in referenced_pages:
        raw = _raw_zip_member(
            archive,
            zip_name,
            maximum_bytes=MAX_MARKET_PAGE_BYTES,
            label="market page",
        )
        page_sizes.append(len(raw))
        if len(raw) != reference["bytes"]:
            raise ValueError("market page size does not match referenced raw bytes")
        if len(raw) >= MAX_MARKET_PAGE_BYTES:
            raise ValueError("market page size must be smaller than 500 KiB")
        if hashlib.sha256(raw).hexdigest() != reference["sha256"]:
            raise ValueError("market page SHA-256 does not match raw ZIP bytes")
        object_key = str(reference["key"])
        page_bytes_by_key[object_key] = raw
        try:
            page = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"market page is not valid UTF-8 JSON: {zip_name}") from exc
        if not isinstance(page, dict):
            raise ValueError("market page must be a JSON object")
        cursor = reference["cursor"]
        identity_fields = {
            "schemaVersion": 2,
            "generationId": generation_id,
            "disclosure": disclosure,
            "category": category,
            "cursor": cursor,
            "limit": MARKET_PAGE_SIZE,
            "total": total,
        }
        numeric_fields = ("schemaVersion", "cursor", "limit", "total")
        if any(type(page.get(field)) is not int for field in numeric_fields) or any(
            page.get(field) != value for field, value in identity_fields.items()
        ):
            raise ValueError("market page identity fields do not match the index")
        next_cursor = cursor + reference["count"] if cursor + reference["count"] < total else None
        actual_next_cursor = page.get("nextCursor")
        if actual_next_cursor != next_cursor or (actual_next_cursor is not None and type(actual_next_cursor) is not int):
            raise ValueError("market page nextCursor does not match cursor continuity")
        items = page.get("items")
        if not isinstance(items, list) or len(items) != reference["count"]:
            raise ValueError("market page item count does not match its reference")
        for item_index, item in enumerate(items):
            stock_code = item.get("stockCode") if isinstance(item, dict) else None
            if not isinstance(stock_code, str) or not stock_code:
                raise ValueError(f"market page item {item_index} requires stockCode")
            v2_identities.append((stock_code, disclosure, category))

    v2_counter = Counter(v2_identities)
    if any(count != 1 for count in v2_counter.values()):
        raise ValueError("market v2 pages contain a duplicate market identity")
    if v2_counter != _legacy_market_identities(market_scan):
        raise ValueError("market v1/v2 identity parity mismatch")

    expected_counts = {
        "universeSize": sum(disclosure_counts.values()),
        "announced": disclosure_counts["announced"],
        "pending": disclosure_counts["pending"],
    }
    if strict_counts != expected_counts:
        raise ValueError("market index aggregate counts do not match page buckets")
    if strict_categories != category_counts:
        raise ValueError("market index aggregate counts do not match category buckets")
    if any(content_generation.files.get(key) != raw for key, raw in page_bytes_by_key.items()):
        raise ValueError("market page bytes do not match the canonical generation bytes")
    if pointer_raw != canonical_json_bytes(content_generation.index):
        raise ValueError("market index bytes do not match the canonical generation bytes")

    actual_metrics = {
        "marketApiSchemaVersion": 2,
        "marketGenerationId": generation_id,
        "marketIndexBytes": len(pointer_raw),
        "marketPageCount": len(referenced_pages),
        "marketMaxPageBytes": max(page_sizes, default=0),
    }
    for field, actual in actual_metrics.items():
        if type(manifest.get(field)) is not type(actual) or manifest.get(field) != actual:
            raise ValueError(f"manifest {field} {manifest.get(field)!r} does not match actual {actual!r}")
    return actual_metrics


def validate_seed_zip(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise ValueError(f"Seed zip does not exist: {path}")

    with zipfile.ZipFile(path) as archive:
        member_infos = archive.infolist()
        for info in member_infos:
            _validate_zip_member_info(info)
        _validate_archive_budget(member_infos)
        member_counts = Counter(info.filename for info in member_infos)
        duplicate_members = sorted(name for name, count in member_counts.items() if count != 1)
        if duplicate_members:
            raise ValueError(f"Seed zip contains a duplicate ZIP member: {', '.join(duplicate_members)}")
        names = set(member_counts)
        unexpected_members = sorted(name for name in names if not _is_allowed_seed_member(name))
        if unexpected_members:
            raise ValueError(f"Seed zip contains an unexpected ZIP member: {', '.join(unexpected_members)}")
        missing = sorted(REQUIRED_ENTRIES - names)
        if missing:
            raise ValueError(f"Seed zip is missing required entries: {', '.join(missing)}")

        history = _load_json_from_zip(archive, "official_fundamentals_history.json")
        monthly_history = _load_json_from_zip(archive, "monthly_revenue_history.json")
        _load_json_from_zip(archive, "official_history_backfill_progress.json")
        manifest = _load_json_from_zip(archive, "cloudflare_seed/manifest.json")
        market_scan = _load_json_from_zip(archive, "cloudflare_seed/market_scan_latest.json")
        companies_payload = _load_json_from_zip(archive, "cloudflare_seed/companies.json")
        if not isinstance(manifest, dict):
            raise ValueError("cloudflare_seed/manifest.json must be a JSON object")
        if not isinstance(market_scan, dict):
            raise ValueError("cloudflare_seed/market_scan_latest.json must be a JSON object")
        preflight_category_counts: dict[str, int] = {}
        for category in MARKET_SCAN_CATEGORIES:
            rows = market_scan.get(category)
            if not isinstance(rows, list):
                raise ValueError(f"cloudflare_seed/market_scan_latest.json {category} must be a JSON list")
            preflight_category_counts[category] = len(rows)
        preflight_universe = sum(preflight_category_counts.values())
        if preflight_universe <= 0:
            raise ValueError("Cloudflare market scan has no rows")
        reported_universe = market_scan.get("universeSize")
        if reported_universe is not None and reported_universe != preflight_universe:
            raise ValueError(
                f"Cloudflare market scan universeSize {reported_universe} "
                f"does not match actual total {preflight_universe}"
            )
        v2_summary = _validate_market_v2(archive, names, manifest, market_scan)
        loaded_public_payloads = {
            "cloudflare_seed/manifest.json": manifest,
            "cloudflare_seed/market_scan_latest.json": market_scan,
            "cloudflare_seed/companies.json": companies_payload,
        }
        public_json_entries = sorted(
            name for name in names if name.startswith("cloudflare_seed/") and name.endswith(".json")
        )
        for name in public_json_entries:
            payload = loaded_public_payloads.get(name)
            if payload is None:
                payload = _load_json_from_zip(archive, name)
            _reject_local_user_paths(payload, name)
        analysis_shards = sorted(
            name for name in names if name.startswith("cloudflare_seed/analysis_shards/") and name.endswith(".json")
        )
        holding_analysis_shards = sorted(
            name
            for name in names
            if name.startswith("cloudflare_seed/holding_analysis_shards/") and name.endswith(".json")
        )
        if not analysis_shards:
            raise ValueError("Seed zip is missing cloudflare_seed/analysis_shards/*.json entries")
        if not holding_analysis_shards:
            raise ValueError("Seed zip is missing cloudflare_seed/holding_analysis_shards/*.json entries")

    if not isinstance(history, dict):
        raise ValueError("official_fundamentals_history.json must be a JSON object")
    quarters = history.get("quarters")
    if not isinstance(quarters, dict):
        raise ValueError("official_fundamentals_history.json must contain a quarters object")

    company_count = 0
    row_count = 0
    latest_period = None
    for stock_code, records in quarters.items():
        if not isinstance(stock_code, str) or not isinstance(records, dict) or not records:
            continue
        company_count += 1
        row_count += len(records)
        latest_for_company = max(records)
        latest_period = max(latest_period or latest_for_company, latest_for_company)

    if company_count < MIN_HISTORY_COMPANIES:
        raise ValueError(f"Seed history has only {company_count} companies; expected at least {MIN_HISTORY_COMPANIES}")
    if row_count < MIN_HISTORY_ROWS:
        raise ValueError(f"Seed history has only {row_count} quarterly rows; expected at least {MIN_HISTORY_ROWS}")

    if not isinstance(monthly_history, dict) or not isinstance(monthly_history.get("months"), dict):
        raise ValueError("monthly_revenue_history.json must contain a months object")
    monthly_coverage = monthly_history_coverage(monthly_history)
    if monthly_coverage["consecutiveCompanies"] < MIN_CONSECUTIVE_MONTHLY_COMPANIES:
        raise ValueError(
            "Seed has insufficient consecutive monthly revenue history: "
            f"{monthly_coverage['consecutiveCompanies']} companies cover "
            f"{monthly_coverage['previousMonth']} -> {monthly_coverage['latestMonth']}; "
            f"expected at least {MIN_CONSECUTIVE_MONTHLY_COMPANIES}"
        )

    if not isinstance(companies_payload, dict) or not isinstance(companies_payload.get("items"), list):
        raise ValueError("cloudflare_seed/companies.json must contain an items list")
    company_items = companies_payload["items"]
    companies_by_market = {"TWSE": 0, "TPEX": 0}
    for index, company in enumerate(company_items):
        if not isinstance(company, dict):
            raise ValueError(f"cloudflare_seed/companies.json items[{index}] must be a JSON object")
        market = company.get("market")
        if market in companies_by_market:
            companies_by_market[market] += 1
    actual_category_counts: dict[str, int] = {}
    for category in MARKET_SCAN_CATEGORIES:
        rows = market_scan.get(category)
        if not isinstance(rows, list):
            raise ValueError(f"cloudflare_seed/market_scan_latest.json {category} must be a JSON list")
        actual_category_counts[category] = len(rows)
    actual_universe = sum(actual_category_counts.values())
    if actual_universe <= 0:
        raise ValueError("Cloudflare market scan has no rows")
    reported_universe = market_scan.get("universeSize")
    if reported_universe is not None:
        if isinstance(reported_universe, bool) or not isinstance(reported_universe, int):
            raise ValueError("Cloudflare market scan universeSize must be an integer")
        if reported_universe != actual_universe:
            raise ValueError(
                f"Cloudflare market scan universeSize {reported_universe} does not match actual total {actual_universe}"
            )

    counts = manifest.get("counts")
    if not isinstance(counts, dict):
        raise ValueError("cloudflare_seed/manifest.json must contain a counts object")
    seed_companies = _manifest_count(counts, "companies", "manifest counts")
    if seed_companies != len(company_items):
        raise ValueError(
            f"Cloudflare manifest counts.companies {seed_companies} "
            f"does not match companies.json items {len(company_items)}"
        )
    manifest_companies_by_market = manifest.get("companiesByMarket")
    if manifest_companies_by_market is not None:
        if not isinstance(manifest_companies_by_market, dict):
            raise ValueError("cloudflare_seed/manifest.json companiesByMarket must be a JSON object")
        for market, actual_count in companies_by_market.items():
            manifest_count = _manifest_count(
                manifest_companies_by_market,
                market,
                "manifest companiesByMarket",
            )
            if manifest_count != actual_count:
                raise ValueError(
                    "Cloudflare manifest companiesByMarket does not match companies.json: "
                    f"{market} {manifest_count} != {actual_count}"
                )
    seed_analysis = _manifest_count(counts, "analysis", "manifest counts")
    seed_holding_analysis = _manifest_count(counts, "holdingAnalysis", "manifest counts")
    manifest_category_counts = {
        category: _manifest_count(counts, category, "manifest counts") for category in MARKET_SCAN_CATEGORIES
    }
    for category, actual_count in actual_category_counts.items():
        manifest_count = manifest_category_counts[category]
        if manifest_count != actual_count:
            raise ValueError(
                f"Cloudflare manifest counts.{category} {manifest_count} does not match market scan {actual_count}"
            )
    seed_universe = sum(manifest_category_counts.values())

    optional_category_counts = manifest.get("categoryCounts")
    if optional_category_counts is not None:
        if not isinstance(optional_category_counts, dict):
            raise ValueError("cloudflare_seed/manifest.json categoryCounts must be a JSON object")
        for category, actual_count in actual_category_counts.items():
            manifest_count = _manifest_count(optional_category_counts, category, "manifest categoryCounts")
            if manifest_count != actual_count:
                raise ValueError(
                    f"Cloudflare manifest categoryCounts.{category} {manifest_count} "
                    f"does not match market scan {actual_count}"
                )

    manifest_universe = manifest.get("universeSize")
    if manifest_universe is not None:
        if isinstance(manifest_universe, bool) or not isinstance(manifest_universe, int):
            raise ValueError("Cloudflare manifest universeSize must be an integer")
        if manifest_universe != actual_universe:
            raise ValueError(
                f"Cloudflare manifest universeSize {manifest_universe} does not match market scan {actual_universe}"
            )
    if seed_companies < MIN_SEED_COMPANIES:
        raise ValueError(f"Cloudflare seed has only {seed_companies} companies; expected at least {MIN_SEED_COMPANIES}")
    for market, minimum in (
        ("TWSE", MIN_SEED_TWSE_COMPANIES),
        ("TPEX", MIN_SEED_TPEX_COMPANIES),
    ):
        if companies_by_market[market] < minimum:
            raise ValueError(
                f"Cloudflare seed {market} company coverage {companies_by_market[market]} "
                f"is below required minimum {minimum}"
            )
    if seed_analysis < MIN_SEED_ANALYSIS:
        raise ValueError(
            f"Cloudflare seed has only {seed_analysis} analysis rows; expected at least {MIN_SEED_ANALYSIS}"
        )
    if seed_holding_analysis < MIN_SEED_ANALYSIS:
        raise ValueError(
            f"Cloudflare seed has only {seed_holding_analysis} holding analysis rows; expected at least {MIN_SEED_ANALYSIS}"
        )
    if seed_universe < MIN_SEED_ANALYSIS:
        raise ValueError(
            f"Cloudflare seed universe has only {seed_universe} rows; expected at least {MIN_SEED_ANALYSIS}"
        )

    return {
        "zip": str(path),
        "companies": company_count,
        "quarterlyRows": row_count,
        "latestPeriod": latest_period,
        "latestRevenueHistoryMonth": monthly_coverage["latestMonth"],
        "previousRevenueHistoryMonth": monthly_coverage["previousMonth"],
        "consecutiveRevenueHistoryCompanies": monthly_coverage["consecutiveCompanies"],
        "seedCompanies": seed_companies,
        "seedCompaniesByMarket": companies_by_market,
        "seedAnalysis": seed_analysis,
        "seedHoldingAnalysis": seed_holding_analysis,
        "seedUniverse": seed_universe,
        "seedShards": len(analysis_shards),
        "seedHoldingShards": len(holding_analysis_shards),
        "generatedAt": manifest.get("generatedAt"),
        "sourceLastCheckedAt": manifest.get("sourceLastCheckedAt"),
        "latestRevenuePeriod": manifest.get("latestRevenuePeriod"),
        "latestFinancialPeriod": manifest.get("latestFinancialPeriod"),
        **v2_summary,
        "requiredEntries": sorted(REQUIRED_ENTRIES),
    }


def validate_seed_freshness(summary: dict[str, Any], max_age_days: int | None, now: datetime | None = None) -> None:
    if max_age_days is None:
        return
    generated_at = summary.get("generatedAt")
    if not generated_at:
        raise ValueError("Seed manifest is missing generatedAt; cannot enforce freshness")
    try:
        generated = datetime.fromisoformat(str(generated_at).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"Seed manifest generatedAt is not a valid ISO datetime: {generated_at}") from exc
    if generated.tzinfo is None:
        generated = generated.replace(tzinfo=UTC)
    current = now or datetime.now(UTC)
    age_days = (current - generated).total_seconds() / 86400
    if age_days > max_age_days:
        raise ValueError(f"Seed manifest is {age_days:.1f} days old; maximum allowed is {max_age_days}")


def failed_company_summary(path: Path = DEFAULT_FAILED_COMPANIES_CSV) -> dict[str, Any]:
    if not path.exists():
        return {"path": str(path), "failedCompanies": 0, "manualStatus": {}, "reasons": {}}

    reasons: Counter[str] = Counter()
    manual_status: Counter[str] = Counter()
    with path.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            reasons[row.get("initial_reason") or "unspecified"] += 1
            manual_status[row.get("manual_status") or row.get("subagent_status") or "todo"] += 1

    return {
        "path": str(path),
        "failedCompanies": sum(reasons.values()),
        "manualStatus": dict(sorted(manual_status.items())),
        "reasons": dict(reasons.most_common()),
    }


def render_seed_summary(summary: dict[str, Any], failed_summary: dict[str, Any] | None = None) -> str:
    failed_summary = failed_summary or failed_company_summary()
    lines = [
        "# Cloudflare seed quality",
        "",
        f"- zip: `{summary['zip']}`",
        f"- official history companies: {summary['companies']}",
        f"- official history quarterly rows: {summary['quarterlyRows']}",
        f"- latest official period: {summary['latestPeriod']}",
        f"- latest monthly revenue history: {summary.get('latestRevenueHistoryMonth') or 'unknown'}",
        f"- consecutive monthly revenue coverage: {summary.get('consecutiveRevenueHistoryCompanies') or 0}",
        f"- manifest generated at: {summary.get('generatedAt') or 'unknown'}",
        f"- latest revenue period: {summary.get('latestRevenuePeriod') or 'unknown'}",
        f"- latest financial period: {summary.get('latestFinancialPeriod') or 'unknown'}",
        f"- market generation: {summary.get('marketGenerationId') or 'unknown'}",
        f"- market pages: {summary.get('marketPageCount') or 0}",
        f"- maximum market page bytes: {summary.get('marketMaxPageBytes') or 0}",
        f"- seed companies: {summary['seedCompanies']}",
        f"- seed analysis rows: {summary['seedAnalysis']}",
        f"- seed holding analysis rows: {summary['seedHoldingAnalysis']}",
        f"- seed universe rows: {summary['seedUniverse']}",
        f"- seed shards: {summary['seedShards']}",
        f"- seed holding shards: {summary['seedHoldingShards']}",
        f"- failed companies report: `{Path(str(failed_summary.get('path') or 'unknown')).as_posix()}`",
        f"- failed companies needing review: {failed_summary['failedCompanies']}",
        "",
        "## Manual follow-up status",
        "",
    ]
    manual_status = failed_summary.get("manualStatus") or {}
    if manual_status:
        lines.extend(f"- {status}: {count}" for status, count in manual_status.items())
    else:
        lines.append("- none")

    reasons = failed_summary.get("reasons") or {}
    if reasons:
        lines.extend(["", "## Missing-data reasons", ""])
        lines.extend(f"- {reason}: {count}" for reason, count in reasons.items())
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate the committed Cloudflare seed cache inputs.")
    parser.add_argument("--zip", type=Path, default=DEFAULT_ZIP, help="Path to the committed seed zip")
    parser.add_argument("--summary-md", type=Path, help="Optional Markdown quality summary output path")
    parser.add_argument("--summary-json", type=Path, help="Optional JSON quality summary output path")
    parser.add_argument("--max-age-days", type=int, help="Fail if cloudflare_seed/manifest.json generatedAt is older")
    parser.add_argument(
        "--failed-companies-csv",
        type=Path,
        default=DEFAULT_FAILED_COMPANIES_CSV,
        help="CSV used to summarize missing official history follow-up status",
    )
    args = parser.parse_args(argv)

    try:
        summary = validate_seed_zip(args.zip)
        validate_seed_freshness(summary, args.max_age_days)
    except ValueError as exc:
        print(f"Seed validation failed: {exc}", file=sys.stderr)
        return 1

    failed_summary = failed_company_summary(args.failed_companies_csv)
    if args.summary_md:
        args.summary_md.parent.mkdir(parents=True, exist_ok=True)
        args.summary_md.write_text(
            render_seed_summary(summary, failed_summary),
            encoding="utf-8",
        )
    if args.summary_json:
        args.summary_json.parent.mkdir(parents=True, exist_ok=True)
        args.summary_json.write_text(
            json.dumps(
                {"seed": summary, "failedCompanies": failed_summary}, ensure_ascii=False, indent=2, sort_keys=True
            ),
            encoding="utf-8",
        )

    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
