from __future__ import annotations

import copy
import hashlib
import json

import pytest

from backend.services.market_query import build_market_generation
from cloudflare.worker_market_query import (
    merge_market_pages,
    parse_market_results_query,
    select_page_references,
    validate_market_index,
)


def _query(**overrides: list[str]):
    query = {
        "disclosure": ["announced"],
        "category": ["watch"],
        "cursor": ["96"],
        "limit": ["10"],
    }
    query.update(overrides)
    return query


def _generation(size: int = 150):
    return build_market_generation(
        {
            "generatedAt": "2026-07-13T00:00:00+00:00",
            "filingContext": {},
            "entry": [],
            "watch": [
                {
                    "stockCode": f"{index:04d}",
                    "companyName": f"Company {index}",
                    "status": "WATCH",
                    "summary": "pending",
                    "reasons": [],
                    "detailsAvailable": True,
                    "hasFullDetails": False,
                }
                for index in range(size)
            ],
            "excluded": [],
        }
    )


@pytest.mark.parametrize(
    ("query", "message"),
    [
        (_query(disclosure=["other"]), "disclosure"),
        (_query(category=["other"]), "category"),
        (_query(cursor=["-1"]), "cursor"),
        (_query(cursor=["true"]), "cursor"),
        (_query(cursor=["2000001"]), "cursor"),
        (_query(cursor=["9" * 5000]), "cursor"),
        (_query(cursor=["0" * 5000]), "cursor"),
        (_query(limit=["0"]), "limit"),
        (_query(limit=["101"]), "limit"),
        (_query(limit=["0" * 5000]), "limit"),
        (_query(limit=["1", "2"]), "limit"),
        (_query(generationId=["not-a-generation"]), "generationId"),
        (_query(generationId=["A" * 24]), "generationId"),
        ({}, "disclosure"),
    ],
)
def test_parse_market_results_query_rejects_invalid_values(query, message):
    with pytest.raises(ValueError, match=message):
        parse_market_results_query(query)


def test_parse_market_results_query_accepts_strict_valid_values():
    parsed = parse_market_results_query(_query(generationId=["a" * 24]))

    assert parsed.disclosure == "announced"
    assert parsed.category == "watch"
    assert parsed.cursor == 96
    assert parsed.limit == 10
    assert parsed.generation_id == "a" * 24

    leading_zeroes = parse_market_results_query(_query(cursor=["00000000"], limit=["001"]))
    assert (leading_zeroes.cursor, leading_zeroes.limit) == (0, 1)


def test_select_and_merge_market_pages_spans_two_physical_pages():
    generation = _generation()
    query = parse_market_results_query(_query(generationId=[generation.generation_id]))

    references = select_page_references(generation.index, query)
    pages = {reference["key"]: generation.files[reference["key"]] for reference in references}
    result = merge_market_pages(generation.index, pages, query)

    assert [reference["cursor"] for reference in references] == [0, 100]
    assert len(references) == 2
    assert [item["stockCode"] for item in result["items"]] == [f"{index:04d}" for index in range(96, 106)]
    assert result["nextCursor"] == 106


def test_merge_market_pages_rejects_page_identity_mismatch():
    generation = _generation()
    query = parse_market_results_query(_query(generationId=[generation.generation_id]))
    index = copy.deepcopy(generation.index)
    references = select_page_references(index, query)
    pages = {reference["key"]: generation.files[reference["key"]] for reference in references}
    first_key = references[0]["key"]
    first_page = json.loads(pages[first_key])
    first_page["generationId"] = "b" * 24
    pages[first_key] = json.dumps(first_page, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    references[0]["bytes"] = len(pages[first_key])
    references[0]["sha256"] = hashlib.sha256(pages[first_key]).hexdigest()

    with pytest.raises(ValueError, match="identity"):
        merge_market_pages(index, pages, query)


def test_merge_market_pages_returns_empty_window_beyond_total():
    generation = _generation(size=1)
    query = parse_market_results_query(_query(cursor=["999"], limit=["10"]))

    assert select_page_references(generation.index, query) == []
    result = merge_market_pages(generation.index, {}, query)

    assert result["cursor"] == 999
    assert result["total"] == 1
    assert result["items"] == []
    assert result["nextCursor"] is None


@pytest.mark.parametrize(
    ("size", "field", "value"),
    [
        (150, "cursor", False),
        (1, "total", True),
    ],
)
def test_merge_market_pages_rejects_boolean_numeric_identity_fields(size, field, value):
    generation = _generation(size=size)
    query = parse_market_results_query(_query(cursor=["0"], limit=["1"]))
    index = copy.deepcopy(generation.index)
    references = select_page_references(index, query)
    pages = {reference["key"]: generation.files[reference["key"]] for reference in references}
    first_key = references[0]["key"]
    first_page = json.loads(pages[first_key])
    first_page[field] = value
    pages[first_key] = json.dumps(first_page, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    references[0]["bytes"] = len(pages[first_key])
    references[0]["sha256"] = hashlib.sha256(pages[first_key]).hexdigest()

    with pytest.raises(ValueError, match="identity"):
        merge_market_pages(index, pages, query)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda index: index.pop("counts"), "fields"),
        (lambda index: index.__setitem__("detailMode", "full"), "detailMode"),
        (lambda index: index.__setitem__("cacheStatusInputs", []), "cacheStatusInputs"),
        (lambda index: index["counts"].__setitem__("universeSize", 999), "counts"),
        (lambda index: index["disclosures"].__setitem__("extra", {}), "disclosures"),
        (lambda index: index.__setitem__("generatedAt", 7), "generatedAt"),
    ],
)
def test_validate_market_index_rejects_schema_and_aggregate_mutations(mutation, message):
    index = copy.deepcopy(_generation(size=1).index)
    mutation(index)

    with pytest.raises(ValueError, match=message):
        validate_market_index(index)


def test_merge_market_pages_rejects_non_object_items():
    generation = _generation(size=1)
    query = parse_market_results_query(_query(cursor=["0"], limit=["1"]))
    index = copy.deepcopy(generation.index)
    references = select_page_references(index, query)
    first_key = references[0]["key"]
    page = json.loads(generation.files[first_key])
    page["items"] = [42]
    raw = json.dumps(page, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    references[0]["bytes"] = len(raw)
    references[0]["sha256"] = hashlib.sha256(raw).hexdigest()

    with pytest.raises(ValueError, match="item"):
        merge_market_pages(index, {first_key: raw}, query)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda item: item.__setitem__("companyName", []),
        lambda item: item.__setitem__("status", True),
        lambda item: item.__setitem__("summary", []),
        lambda item: item.__setitem__("reasons", "not-a-list"),
        lambda item: item.__setitem__("detailsAvailable", 1),
        lambda item: item.__setitem__("hasFullDetails", 0),
        lambda item: item.__setitem__("reasons", ["not-an-object"]),
        lambda item: item.__setitem__("reasons", [{"code": "UNKNOWN"}]),
        lambda item: item.__setitem__("reasons", [{"code": "E4", "extra": True}]),
        lambda item: item.__setitem__("reasons", [{"code": "E4", "title": []}]),
        lambda item: item.__setitem__("reasons", [{"code": "E4", "passed": 1}]),
        lambda item: item.__setitem__("reasons", [{"code": "E4", "severity": False}]),
        lambda item: item.__setitem__("reasons", [{"code": "E4", "message": []}]),
    ],
)
def test_merge_market_pages_rejects_malformed_item_fields_and_reasons(mutation):
    generation = _generation(size=1)
    query = parse_market_results_query(_query(cursor=["0"], limit=["1"]))
    index = copy.deepcopy(generation.index)
    references = select_page_references(index, query)
    first_key = references[0]["key"]
    page = json.loads(generation.files[first_key])
    mutation(page["items"][0])
    raw = json.dumps(page, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    references[0]["bytes"] = len(raw)
    references[0]["sha256"] = hashlib.sha256(raw).hexdigest()

    with pytest.raises(ValueError, match="item"):
        merge_market_pages(index, {first_key: raw}, query)


def test_merge_market_pages_accepts_task1_summary_item_schema():
    generation = build_market_generation(
        {
            "generatedAt": "2026-07-13T00:00:00+00:00",
            "entry": [],
            "watch": [
                {
                    "stockCode": "2330",
                    "companyName": "TSMC",
                    "status": "WATCH",
                    "summary": "observe",
                    "reasons": [
                        {
                            "code": "E4",
                            "title": "valuation",
                            "passed": True,
                            "severity": "INFO",
                            "message": "within range",
                        }
                    ],
                    "detailsAvailable": True,
                    "hasFullDetails": False,
                }
            ],
            "excluded": [],
        }
    )
    query = parse_market_results_query(
        _query(cursor=["0"], limit=["1"], generationId=[generation.generation_id])
    )
    references = select_page_references(generation.index, query)

    result = merge_market_pages(
        generation.index,
        {reference["key"]: generation.files[reference["key"]] for reference in references},
        query,
    )

    assert result["items"][0]["reasons"][0]["code"] == "E4"
