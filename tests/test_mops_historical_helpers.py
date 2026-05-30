"""Unit tests for the MOPS historical-fundamentals period/title parsing helpers.

These pure helpers decide which fiscal year/quarter a fetched statement is
attributed to (parsed from Chinese MOPS titles/date ranges). A mistake here
silently files data under the wrong quarter, so they are worth covering even
though the network fetch layer is left untested.
"""

from backend.adapters.mops_historical_fundamentals import (
    _ad_to_roc_year,
    _label_value,
    _market_type,
    _normalize_label,
    _parse_balance_period,
    _parse_income_period,
    _parse_mops_balance_title,
    _parse_mops_income_title,
)


def test_market_type_and_roc_year():
    assert _market_type("TPEX") == "otc"
    assert _market_type("tpex") == "otc"
    assert _market_type("TWSE") == "sii"
    assert _market_type("anything") == "sii"
    assert _ad_to_roc_year(2024) == "113"


def test_normalize_label_strips_fullwidth_and_whitespace():
    assert _normalize_label("營業　收入") == "營業收入"
    assert _normalize_label("  本期  淨利  ") == "本期 淨利"
    assert _normalize_label(None) == ""


def test_parse_income_period_annual_and_range():
    assert _parse_income_period("113年度") == (2024, 4)
    assert _parse_income_period("113年01月01日至113年09月30日") == (2024, 3)
    assert _parse_income_period("113年01月01日至113年06月30日") == (2024, 2)
    assert _parse_income_period("no period here") is None


def test_parse_balance_period_from_date():
    assert _parse_balance_period("113年09月30日") == (2024, 3)
    assert _parse_balance_period("113年03月31日") == (2024, 1)
    assert _parse_balance_period("nothing") is None


def test_parse_mops_income_title_variants():
    assert _parse_mops_income_title("113年第1季") == (2024, 1)
    assert _parse_mops_income_title("113年第4季") == (2024, 4)
    assert _parse_mops_income_title("113年度") == (2024, 4)
    # Q1 detected from a Jan-Mar date range
    assert _parse_mops_income_title("113年01月01日至113年03月31日") == (2024, 1)
    assert _parse_mops_income_title("無法解析") is None


def test_parse_mops_balance_title_variants():
    assert _parse_mops_balance_title("113年03月31日") == (2024, 1)
    assert _parse_mops_balance_title("113年12月31日") == (2024, 4)
    assert _parse_mops_balance_title("無法解析") is None


def test_label_value_matches_normalized_label_and_column():
    rows = [
        ["營業收入", "1,000", "900"],
        ["營業　成本", "600", "550"],
        ["每股盈餘", "-", "n/a"],
    ]
    assert _label_value(rows, ("營業收入",), 1) == 1000.0
    assert _label_value(rows, ("營業收入",), 2) == 900.0
    # full-width space in the row label still matches the plain label
    assert _label_value(rows, ("營業成本",), 1) == 600.0
    # falls through label aliases until a numeric value is found
    assert _label_value(rows, ("缺漏", "營業收入"), 1) == 1000.0
    # non-numeric / missing -> None
    assert _label_value(rows, ("每股盈餘",), 1) is None
    assert _label_value(rows, ("不存在",), 1) is None
