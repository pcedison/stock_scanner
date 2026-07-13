import copy
import hashlib
import json
import re

import pytest

import backend.services.market_query as market_query
from backend.services.market_query import (
    CATEGORIES,
    DISCLOSURES,
    MAX_INDEX_BYTES,
    MAX_PAGE_BYTES,
    PAGE_SIZE,
    build_market_generation,
    canonical_json_bytes,
    query_market_generation,
)


def _reason(code: str, message: str, *, severity: str = "INFO") -> dict:
    return {
        "code": code,
        "title": code,
        "passed": severity != "INSUFFICIENT_DATA",
        "severity": severity,
        "message": message,
    }


def _result(
    stock_code: str,
    category: str,
    *,
    period: str | None = "2026Q1",
    per: float | None = None,
) -> dict:
    status = {"entry": "ENTRY", "watch": "WATCH", "excluded": "EXCLUDED"}[category]
    reasons = []
    if per is not None:
        reasons.append(_reason("E4", f"PER {per:g} 倍"))
    if period is not None:
        reasons.append(_reason("OFFICIAL_Q", f"已採用 {period} 官方財報"))
    return {
        "stockCode": stock_code,
        "companyName": f"Company {stock_code}",
        "status": status,
        "summary": f"Summary {stock_code}",
        "reasons": reasons,
        "detailsAvailable": True,
        "hasFullDetails": False,
    }


def sample_scan(
    *,
    order: str = "forward",
    watch_rows: int | None = None,
    active_period: str = "2026Q2",
    freshness_period: str = "2026Q1",
) -> dict:
    entry = [
        _result("1002", "entry", per=18.5),
        _result("1001", "entry", per=7.25),
        _result("1003", "entry", period=None, per=4.5),
    ]
    if watch_rows is None:
        watch = [
            _result("2002", "watch"),
            _result("2001", "watch", period=None),
        ]
    else:
        watch = [_result(f"{2000 + index:04d}", "watch") for index in range(watch_rows)]
    excluded = [
        _result("3002", "excluded"),
        _result("3001", "excluded", period=None),
    ]
    if order == "reverse":
        entry.reverse()
        watch.reverse()
        excluded.reverse()
        for items in (entry, watch, excluded):
            for index, result in enumerate(items):
                result["reasons"] = [dict(reversed(list(reason.items()))) for reason in reversed(result["reasons"])]
                items[index] = dict(reversed(list(result.items())))
    return {
        "generatedAt": "2026-07-13T01:02:03+00:00",
        "filingContext": {
            "activeFinancialReport": {"period": active_period},
            "freshnessFinancialReport": {"period": freshness_period},
            "monthlyRevenuePeriod": "2026-06",
        },
        "financialFreshness": {
            "status": "fresh",
            "latestCachedFinancialPeriod": freshness_period,
        },
        "universeSize": len(entry) + len(watch) + len(excluded),
        "entry": entry,
        "watch": watch,
        "excluded": excluded,
        "results": [*entry, *watch, *excluded],
        "detailMode": "summary",
    }


def _decoded_pages(generation) -> dict[str, dict]:
    return {
        key: json.loads(content.decode("utf-8"))
        for key, content in generation.files.items()
        if key != f"public/market_scan/v2/{generation.generation_id}/index.json"
    }


def _raw_pages(generation) -> dict[str, bytes]:
    index_key = f"public/market_scan/v2/{generation.generation_id}/index.json"
    return {key: content for key, content in generation.files.items() if key != index_key}


def _watch_generation(rows: int = 201):
    return build_market_generation(sample_scan(watch_rows=rows))


def _query_watch(generation, *, index=None, pages=None, cursor=0, limit=1):
    return query_market_generation(
        index or generation.index,
        pages or _raw_pages(generation),
        "announced",
        "watch",
        cursor,
        limit,
    )


def _replace_page(generation, index, pages, page_index, **updates):
    reference = index["disclosures"]["announced"]["watch"]["pages"][page_index]
    page = json.loads(pages[reference["key"]])
    page.update(updates)
    content = canonical_json_bytes(page)
    pages[reference["key"]] = content
    reference["bytes"] = len(content)
    reference["sha256"] = hashlib.sha256(content).hexdigest()


def _generation_identities(generation) -> set[tuple[str, str, str]]:
    identities = set()
    for disclosure in DISCLOSURES:
        for category in CATEGORIES:
            for reference in generation.index["disclosures"][disclosure][category]["pages"]:
                page = json.loads(generation.files[reference["key"]].decode("utf-8"))
                identities.update((str(item["stockCode"]), disclosure, category) for item in page["items"])
    return identities


def test_generation_is_canonical_and_preserves_identity():
    first = build_market_generation(sample_scan(order="forward"))
    second = build_market_generation(sample_scan(order="reverse"))

    assert first.generation_id == second.generation_id
    assert re.fullmatch(r"[0-9a-f]{24}", first.generation_id)
    assert _generation_identities(first) == {
        ("1001", "announced", "entry"),
        ("1002", "announced", "entry"),
        ("1003", "pending", "entry"),
        ("2002", "announced", "watch"),
        ("2001", "pending", "watch"),
        ("3002", "announced", "excluded"),
        ("3001", "pending", "excluded"),
    }


def test_canonical_identity_also_produces_identical_page_bytes():
    first_scan = sample_scan()
    first_scan["entry"][0]["reasons"].insert(0, _reason("E4", "PER 1 倍"))
    second_scan = sample_scan()
    second_scan["entry"][0]["reasons"].insert(0, _reason("E4", "PER 1 倍"))
    second_scan["entry"][0]["reasons"].reverse()

    first = build_market_generation(first_scan)
    second = build_market_generation(second_scan)

    assert first.generation_id == second.generation_id
    assert first.files == second.files


@pytest.mark.parametrize(("watch_rows", "expected_counts"), [(100, [100]), (101, [100, 1])])
def test_generation_uses_freshness_period_and_splits_at_page_boundary(watch_rows, expected_counts):
    generation = build_market_generation(
        sample_scan(watch_rows=watch_rows, active_period="2026Q2", freshness_period="2026Q1"),
        page_size=100,
    )

    assert generation.index["disclosurePeriod"] == "2026Q1"
    refs = generation.index["disclosures"]["announced"]["watch"]["pages"]
    assert [ref["cursor"] for ref in refs] == list(range(0, watch_rows, 100))
    assert [ref["count"] for ref in refs] == expected_counts
    pages = [json.loads(generation.files[ref["key"]]) for ref in refs]
    assert [page["nextCursor"] for page in pages] == ([None] if watch_rows == 100 else [100, None])


def test_entry_sorting_happens_before_page_slicing():
    scan = sample_scan()
    scan["entry"] = [
        *[_result(f"{4000 + index:04d}", "entry", per=float(200 - index)) for index in range(100)],
        _result("0999", "entry", per=0.5),
    ]
    scan["universeSize"] = len(scan["entry"]) + len(scan["watch"]) + len(scan["excluded"])
    generation = build_market_generation(scan)
    refs = generation.index["disclosures"]["announced"]["entry"]["pages"]
    first_page = json.loads(generation.files[refs[0]["key"]].decode("utf-8"))
    second_page = json.loads(generation.files[refs[1]["key"]].decode("utf-8"))

    assert first_page["items"][0]["stockCode"] == "0999"
    assert second_page["items"][-1]["stockCode"] == "4000"


def test_generation_files_hold_exact_index_and_hashed_page_bytes():
    generation = build_market_generation(
        sample_scan(),
        cache_status_inputs={
            "generatedAt": "2026-07-13T01:02:03+00:00",
            "latestRevenuePeriod": "2026-06",
            "latestFinancialPeriod": "2026Q1",
        },
    )
    index_key = f"public/market_scan/v2/{generation.generation_id}/index.json"

    assert generation.files[index_key] == canonical_json_bytes(generation.index)
    assert len(generation.files[index_key]) < MAX_INDEX_BYTES
    assert generation.index["cacheStatusInputs"]["latestFinancialPeriod"] == "2026Q1"
    for disclosure in DISCLOSURES:
        for category in CATEGORIES:
            bucket = generation.index["disclosures"][disclosure][category]
            assert bucket["count"] == sum(reference["count"] for reference in bucket["pages"])
            for reference in bucket["pages"]:
                content = generation.files[reference["key"]]
                assert reference["sha256"] == hashlib.sha256(content).hexdigest()
                assert reference["bytes"] == len(content)
                assert len(content) < MAX_PAGE_BYTES


def test_cache_status_inputs_are_part_of_the_immutable_generation_identity():
    first = build_market_generation(
        sample_scan(),
        cache_status_inputs={"latestFinancialPeriod": "2026Q1", "latestRevenuePeriod": "2026-06"},
    )
    second = build_market_generation(
        sample_scan(),
        cache_status_inputs={"latestFinancialPeriod": "2026Q1", "latestRevenuePeriod": "2026-05"},
    )

    assert first.generation_id != second.generation_id
    assert canonical_json_bytes(first.index) != canonical_json_bytes(second.index)


def test_query_spans_physical_pages_without_changing_identity():
    generation = build_market_generation(sample_scan(watch_rows=102))
    response = query_market_generation(
        generation.index,
        _decoded_pages(generation),
        "announced",
        "watch",
        96,
        6,
    )

    assert response["generationId"] == generation.generation_id
    assert response["cursor"] == 96
    assert response["limit"] == 6
    assert response["total"] == 102
    assert response["nextCursor"] is None
    assert [item["stockCode"] for item in response["items"]] == [f"{2000 + index:04d}" for index in range(96, 102)]


@pytest.mark.parametrize(
    ("cursor", "limit", "message"),
    [
        (True, 1, "cursor"),
        (0, True, "limit"),
        (-1, 1, "cursor"),
    ],
)
def test_query_rejects_bool_and_negative_request_integers(cursor, limit, message):
    generation = _watch_generation()
    with pytest.raises(ValueError, match=message):
        _query_watch(generation, cursor=cursor, limit=limit)


@pytest.mark.parametrize("bad_total", [True, "201", -1])
def test_query_rejects_non_integer_or_negative_bucket_total(bad_total):
    generation = _watch_generation()
    index = copy.deepcopy(generation.index)
    index["disclosures"]["announced"]["watch"]["count"] = bad_total

    with pytest.raises(ValueError, match="total"):
        _query_watch(generation, index=index)


@pytest.mark.parametrize(
    ("field", "bad_value"),
    [
        ("cursor", True),
        ("cursor", -1),
        ("count", True),
        ("count", -1),
        ("bytes", True),
        ("bytes", -1),
        ("bytes", MAX_PAGE_BYTES),
    ],
)
def test_query_rejects_invalid_reference_integers(field, bad_value):
    generation = _watch_generation()
    index = copy.deepcopy(generation.index)
    index["disclosures"]["announced"]["watch"]["pages"][0][field] = bad_value

    with pytest.raises(ValueError, match=field):
        _query_watch(generation, index=index)


@pytest.mark.parametrize("second_cursor", [99, 101])
def test_query_rejects_overlapping_or_gapped_page_references(second_cursor):
    generation = _watch_generation()
    index = copy.deepcopy(generation.index)
    index["disclosures"]["announced"]["watch"]["pages"][1]["cursor"] = second_cursor

    with pytest.raises(ValueError, match="contiguous"):
        _query_watch(generation, index=index)


def test_query_rejects_reversed_page_references():
    generation = _watch_generation()
    index = copy.deepcopy(generation.index)
    index["disclosures"]["announced"]["watch"]["pages"].reverse()

    with pytest.raises(ValueError, match="contiguous"):
        _query_watch(generation, index=index)


def test_query_rejects_duplicate_page_references():
    generation = _watch_generation()
    index = copy.deepcopy(generation.index)
    references = index["disclosures"]["announced"]["watch"]["pages"]
    references.insert(1, copy.deepcopy(references[0]))

    with pytest.raises(ValueError, match="contiguous"):
        _query_watch(generation, index=index)


def test_query_rejects_wrong_generation_page_key():
    generation = _watch_generation()
    index = copy.deepcopy(generation.index)
    reference = index["disclosures"]["announced"]["watch"]["pages"][0]
    wrong_key = reference["key"].replace("/announced/watch/", "/pending/watch/")
    pages = _raw_pages(generation)
    pages[wrong_key] = pages[reference["key"]]
    reference["key"] = wrong_key

    with pytest.raises(ValueError, match="key"):
        _query_watch(generation, index=index, pages=pages)


def test_query_rejects_wrong_reference_byte_count():
    generation = _watch_generation()
    index = copy.deepcopy(generation.index)
    index["disclosures"]["announced"]["watch"]["pages"][0]["bytes"] += 1

    with pytest.raises(ValueError, match="byte count"):
        _query_watch(generation, index=index)


def test_query_rejects_page_content_with_wrong_sha256():
    generation = _watch_generation()
    pages = _raw_pages(generation)
    reference = generation.index["disclosures"]["announced"]["watch"]["pages"][0]
    content = pages[reference["key"]]
    mutated = content.replace(b"Company", b"Companx", 1)
    assert len(mutated) == len(content)
    pages[reference["key"]] = mutated

    with pytest.raises(ValueError, match="SHA-256"):
        _query_watch(generation, pages=pages)


@pytest.mark.parametrize(
    ("updates", "message"),
    [
        ({"limit": 99}, "page limit"),
        ({"nextCursor": 99}, "nextCursor"),
        ({"items": []}, "item count"),
    ],
)
def test_query_rejects_mutated_page_contract(updates, message):
    generation = _watch_generation()
    index = copy.deepcopy(generation.index)
    pages = _raw_pages(generation)
    _replace_page(generation, index, pages, 0, **updates)

    with pytest.raises(ValueError, match=message):
        _query_watch(generation, index=index, pages=pages)


def test_builder_rejects_nonstandard_page_size_and_budget_boundary(monkeypatch):
    with pytest.raises(ValueError, match="page_size must be 100"):
        build_market_generation(sample_scan(), page_size=99)

    generation = build_market_generation(sample_scan())
    index_size = len(canonical_json_bytes(generation.index))
    monkeypatch.setattr(market_query, "MAX_INDEX_BYTES", index_size)
    with pytest.raises(ValueError, match="index.*50 KiB"):
        build_market_generation(sample_scan())

    monkeypatch.setattr(market_query, "MAX_INDEX_BYTES", MAX_INDEX_BYTES)
    page_size = max(
        reference["bytes"]
        for disclosure in DISCLOSURES
        for category in CATEGORIES
        for reference in generation.index["disclosures"][disclosure][category]["pages"]
    )
    monkeypatch.setattr(market_query, "MAX_PAGE_BYTES", page_size)
    with pytest.raises(ValueError, match="page.*500 KiB"):
        build_market_generation(sample_scan())


def test_public_constants_lock_the_v2_contract():
    assert PAGE_SIZE == 100
    assert MAX_INDEX_BYTES == 50 * 1024
    assert MAX_PAGE_BYTES == 500 * 1024
    assert DISCLOSURES == ("announced", "pending")
    assert CATEGORIES == ("entry", "watch", "excluded")
