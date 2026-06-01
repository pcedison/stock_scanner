"""Branch coverage for the optional CSV fundamentals-import adapter.

A happy-path CSV parse is covered in test_official_provider; these tests pin
the loosely-typed cell helpers and the adapter's empty/cache/skip branches.
"""

from backend.adapters.fundamentals_import import (
    ImportedQuarterlyFundamental,
    LocalFundamentalsImportAdapter,
    _quarter_key,
    _to_bool,
    _to_month,
    _to_quarter,
    _to_year,
)

# --- cell helpers -----------------------------------------------------------


def test_to_bool_recognizes_truthy_falsy_and_unknown():
    assert _to_bool("") is None
    assert _to_bool("1") is True
    assert _to_bool("是") is True
    assert _to_bool("0") is False
    assert _to_bool("否") is False
    assert _to_bool("maybe") is None


def test_to_year_handles_blank_roc_and_ad():
    assert _to_year("") is None
    assert _to_year("2025") == 2025
    assert _to_year("113") == 2024  # ROC year rolled forward


def test_to_quarter_extracts_and_bounds():
    assert _to_quarter("Q3") == 3
    assert _to_quarter("2025Q4") == 4
    assert _to_quarter("") is None
    assert _to_quarter("abc") is None
    assert _to_quarter("Q9") is None  # out of 1..4


def test_to_month_normalizes_formats_and_rejects_garbage():
    assert _to_month("") is None
    assert _to_month("2026-04") == "2026-04"
    assert _to_month("2026/04") == "2026-04"
    assert _to_month("202604") == "2026-04"
    assert _to_month("11504") == "2026-04"  # ROC month via roc_month_to_ad
    assert _to_month("xx") is None


def test_quarter_key_and_period_property():
    full = ImportedQuarterlyFundamental(stockCode="9999", fiscalYear=2025, quarter=4)
    empty = ImportedQuarterlyFundamental(stockCode="9999")
    assert _quarter_key(full) == (2025, 4)
    assert _quarter_key(empty) == (0, 0)
    assert full.period == "2025Q4"
    assert empty.period is None


# --- adapter branches -------------------------------------------------------


def test_fetch_bundle_returns_disabled_when_file_absent(tmp_path):
    bundle = LocalFundamentalsImportAdapter(tmp_path / "missing.csv").fetch_bundle()
    assert bundle.status["enabled"] is False
    assert bundle.status["rows"] == 0
    assert bundle.monthly == {}


def test_fetch_bundle_returns_cached_result_when_unchanged(tmp_path):
    path = tmp_path / "import.csv"
    path.write_text("stock_code,eps\n9999,2.5\n", encoding="utf-8")
    adapter = LocalFundamentalsImportAdapter(path)
    first = adapter.fetch_bundle()
    second = adapter.fetch_bundle()
    assert first is second  # mtime unchanged -> cache hit


def test_fetch_bundle_skips_rows_without_stock_code(tmp_path):
    path = tmp_path / "import.csv"
    path.write_text("stock_code,eps\n,3.0\n", encoding="utf-8")
    status = LocalFundamentalsImportAdapter(path).fetch_bundle().status
    assert status["rows"] == 1
    assert status["skippedRows"] == 1


def test_fetch_bundle_drops_rows_with_no_recognized_fields(tmp_path):
    path = tmp_path / "import.csv"
    path.write_text("stock_code,unrelated\n9999,foo\n", encoding="utf-8")
    bundle = LocalFundamentalsImportAdapter(path).fetch_bundle()
    # stock_code present but every section parses to None -> nothing stored.
    assert bundle.status["rows"] == 1
    assert bundle.status["skippedRows"] == 0
    assert bundle.monthly == {} and bundle.quarterly == {}
    assert bundle.valuations == {} and bundle.annuals == {}
