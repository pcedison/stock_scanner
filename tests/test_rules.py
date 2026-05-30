from backend.models.financial import AnnualFinancial, MonthlyRevenue
from backend.models.holding import Holding
from backend.models.settings import ScannerSettings
from backend.services.data_provider import MockDataProvider
from backend.services.rules import (
    RuleEngine,
    _annual_net_income_yoy,
    _exit_rules,
    _healthy_entry_rules,
    _is_spring_month,
    _revenue_growth_for_entry,
)


def _healthy_exit_overrides(base):
    """Monthly + quarterly values that keep every X1-X5 exit rule passing."""
    return {
        "monthlyRevenue": base.monthlyRevenue.model_copy(
            update={"monthlyRevenueYoY": 60.0, "previousMonthRevenueYoY": 60.0}
        ),
        "quarterlyFinancial": base.quarterlyFinancial.model_copy(
            update={"epsYoY": 10.0, "netIncomeYoY": 10.0, "grossMarginYoY": 5.0}
        ),
    }


def test_2357_passes_entry_rules_with_reasons():
    provider = MockDataProvider()
    result = RuleEngine().evaluate_entry(provider.get_snapshot("2357"), ScannerSettings())

    assert result.status == "ENTRY"
    assert {reason.code for reason in result.reasons} >= {"E1", "E2", "E3", "E4", "E5", "E6"}
    assert all(reason.message for reason in result.reasons)
    e1 = next(reason for reason in result.reasons if reason.code == "E1")
    assert e1.evidence
    assert e1.evidence[0]["metric"] == "annual_net_income"


def test_e4_per_threshold_is_twenty_not_fifteen():
    provider = MockDataProvider()
    base_snapshot = provider.get_snapshot("2357")
    snapshot = base_snapshot.model_copy(
        update={
            "valuation": base_snapshot.valuation.model_copy(update={"per": 18.5}),
        }
    )

    result = RuleEngine().evaluate_entry(snapshot, ScannerSettings())
    e4 = next(reason for reason in result.reasons if reason.code == "E4")

    assert result.status == "ENTRY"
    assert e4.passed is True
    assert e4.title == "本益比小於 20"


def test_e4_per_threshold_excludes_twenty_and_above():
    provider = MockDataProvider()
    base_snapshot = provider.get_snapshot("2357")
    snapshot = base_snapshot.model_copy(
        update={
            "valuation": base_snapshot.valuation.model_copy(update={"per": 20.0}),
        }
    )

    result = RuleEngine().evaluate_entry(snapshot, ScannerSettings())
    e4 = next(reason for reason in result.reasons if reason.code == "E4")

    assert result.status == "WATCH"
    assert e4.passed is False
    assert e4.severity == "WATCH"


def test_financial_company_is_excluded_by_default():
    provider = MockDataProvider()
    result = RuleEngine().evaluate_entry(provider.get_snapshot("2881"), ScannerSettings())

    assert result.status == "EXCLUDED"
    assert any(reason.code == "E6" and not reason.passed for reason in result.reasons)


def test_holding_with_x4_and_x5_returns_exit():
    provider = MockDataProvider()
    holding = Holding(stockCode="3008", name="大立光", shares=1000, averageCost=2000)
    result = RuleEngine().evaluate_holding(provider.get_snapshot("3008"), holding, ScannerSettings())

    assert result.status == "EXIT"
    assert any(reason.code == "X4" and not reason.passed and reason.severity == "EXIT" for reason in result.reasons)
    assert any(reason.code == "X5" and not reason.passed for reason in result.reasons)


def test_holding_exit_rules_still_run_when_entry_data_is_incomplete():
    provider = MockDataProvider()
    base_snapshot = provider.get_snapshot("3008")
    snapshot = base_snapshot.model_copy(update={"annualFinancials": []})
    holding = Holding(stockCode="3008", name="大立光", shares=1000, averageCost=2000)

    result = RuleEngine().evaluate_holding(snapshot, holding, ScannerSettings())

    assert result.status == "EXIT"
    assert any(reason.code == "E1" and reason.severity == "INSUFFICIENT_DATA" for reason in result.reasons)
    assert {reason.code for reason in result.reasons} >= {"X1", "X2", "X3", "X4", "X5"}
    assert any(reason.code == "X4" and not reason.passed and reason.severity == "EXIT" for reason in result.reasons)


def test_holding_add_watch_requires_a1_to_a7():
    provider = MockDataProvider()
    holding = Holding(stockCode="2357", name="華碩", shares=1000, averageCost=300)
    result = RuleEngine().evaluate_holding(provider.get_snapshot("2357"), holding, ScannerSettings())

    assert result.status == "ADD_WATCH"
    assert {reason.code for reason in result.reasons} >= {"A1", "A2", "A3", "A4", "A5", "A6", "A7", "T3"}
    assert all(reason.passed for reason in result.reasons if reason.code.startswith("A"))
    ordered_codes = [reason.code for reason in result.reasons]
    assert ordered_codes.index("E1") < ordered_codes.index("X1")
    if "OFFICIAL_Q" in ordered_codes:
        assert ordered_codes.index("OFFICIAL_Q") < ordered_codes.index("X1")
    assert ordered_codes.index("X5") < ordered_codes.index("T3") < ordered_codes.index("A1")
    assert ordered_codes.index("A7") < ordered_codes.index("HOLDING")


def test_financial_company_included_still_requires_dedicated_strategy():
    provider = MockDataProvider()
    result = RuleEngine().evaluate_entry(
        provider.get_snapshot("2881"),
        ScannerSettings(exclude_financial_industry=False),
    )

    assert result.status == "ENTRY"
    assert {reason.code for reason in result.reasons} >= {"FIN0", "FIN1", "FIN2", "FIN3", "FIN4", "FIN5", "FIN6"}
    assert not any(reason.code == "E5" for reason in result.reasons)


def test_spring_festival_guard_marks_watch_not_exit_for_revenue_drop_only():
    provider = MockDataProvider()
    snapshot = provider.get_snapshot("2357")
    spring_snapshot = snapshot.model_copy(
        update={
            "monthlyRevenue": MonthlyRevenue(
                month="2026-02",
                monthlyRevenueYoY=12.0,
                previousMonthRevenueYoY=46.0,
                cumulativeRevenueYoY=56.2,
                isSpringFestivalMonth=True,
            )
        }
    )
    holding = Holding(stockCode="2357", name="華碩", shares=1000, averageCost=300)
    result = RuleEngine().evaluate_holding(spring_snapshot, holding, ScannerSettings(spring_festival_guard=True))

    assert result.status == "WARNING"
    assert any(reason.code == "SPRING_FESTIVAL_WATCH" for reason in result.reasons)
    assert not any(reason.code in {"X4", "X5"} and not reason.passed for reason in result.reasons)


def _af(year: int, net_income: float | None) -> AnnualFinancial:
    return AnnualFinancial(year=year, netIncome=net_income)


def test_annual_net_income_yoy_edge_cases():
    assert _annual_net_income_yoy([]) is None
    assert _annual_net_income_yoy([_af(2025, 100.0)]) is None  # need at least two years
    assert _annual_net_income_yoy([_af(2024, None), _af(2025, 100.0)]) is None  # previous missing
    assert _annual_net_income_yoy([_af(2024, 100.0), _af(2025, None)]) is None  # latest missing
    assert _annual_net_income_yoy([_af(2024, 0.0), _af(2025, 100.0)]) is None  # prior zero -> undefined
    assert _annual_net_income_yoy([_af(2024, 100.0), _af(2025, 150.0)]) == 50.0


def test_is_spring_month_false_when_guard_disabled():
    snapshot = MockDataProvider().get_snapshot("2357")
    assert _is_spring_month(snapshot, ScannerSettings(spring_festival_guard=False)) is False


def test_revenue_growth_for_entry_respects_mode():
    base = MockDataProvider().get_snapshot("2357")
    snapshot = base.model_copy(
        update={
            "monthlyRevenue": base.monthlyRevenue.model_copy(
                update={
                    "monthlyRevenueYoY": 11.0,
                    "trailingThreeMonthAverageYoY": 22.0,
                    "cumulativeRevenueYoY": 33.0,
                }
            )
        }
    )
    assert _revenue_growth_for_entry(snapshot, ScannerSettings(revenue_growth_mode="monthly")) == 11.0
    assert _revenue_growth_for_entry(snapshot, ScannerSettings(revenue_growth_mode="trailing_3m_avg")) == 22.0
    assert _revenue_growth_for_entry(snapshot, ScannerSettings(revenue_growth_mode="cumulative_ytd")) == 33.0


def test_entry_and_exit_helpers_sort_annuals_when_not_provided():
    # Called without the sorted_annuals arg, the helpers sort internally; the
    # public engine always passes it, so this exercises the default-arg branch.
    snapshot = MockDataProvider().get_snapshot("2357")
    entry = _healthy_entry_rules(snapshot, ScannerSettings())
    exit_rules = _exit_rules(snapshot, ScannerSettings())
    assert any(rule.code == "E1" for rule in entry)
    assert any(rule.code == "X1" for rule in exit_rules)


def test_spring_support_growth_annotates_and_rescues_x1():
    base = MockDataProvider().get_snapshot("2357")
    spring = base.model_copy(
        update={
            "monthlyRevenue": MonthlyRevenue(
                month="2026-02",
                monthlyRevenueYoY=12.0,  # below 30 -> X1 would trigger
                previousMonthRevenueYoY=20.0,
                cumulativeRevenueYoY=40.0,
                janFebCombinedRevenueYoY=40.0,  # spring support present and >= 30
                isSpringFestivalMonth=True,
            )
        }
    )
    x1 = next(rule for rule in _exit_rules(spring, ScannerSettings(spring_festival_guard=True)) if rule.code == "X1")
    assert "輔助營收年增率為 40.0%" in x1.message
    assert x1.passed is True  # spring guard + adequate support rescues X1


def test_holding_financial_company_holds_for_manual_review():
    base = MockDataProvider().get_snapshot("2881")  # financial -> E6 excludes it
    snapshot = base.model_copy(update=_healthy_exit_overrides(base))
    holding = Holding(stockCode="2881", name="富邦金", shares=1000, averageCost=50)

    result = RuleEngine().evaluate_holding(snapshot, holding, ScannerSettings())

    assert result.status == "HOLD"
    assert "排除產業" in result.summary


def test_holding_holds_when_exit_clean_but_entry_data_incomplete():
    base = MockDataProvider().get_snapshot("2357")
    snapshot = base.model_copy(update={"annualFinancials": [], **_healthy_exit_overrides(base)})
    holding = Holding(stockCode="2357", name="華碩", shares=1000, averageCost=300)

    result = RuleEngine().evaluate_holding(snapshot, holding, ScannerSettings())

    assert result.status == "HOLD"
    assert "資料缺口" in result.summary


def test_holding_holds_when_exit_clean_but_add_watch_not_fully_met():
    base = MockDataProvider().get_snapshot("2357")  # passes entry + normally ADD_WATCH
    # eps_yoy == 0 fails A3 (> 0) while X3 (>= 0) and X4 (> -10) still pass.
    snapshot = base.model_copy(
        update={"quarterlyFinancial": base.quarterlyFinancial.model_copy(update={"epsYoY": 0.0})}
    )
    holding = Holding(stockCode="2357", name="華碩", shares=1000, averageCost=300)

    result = RuleEngine().evaluate_holding(snapshot, holding, ScannerSettings())

    assert result.status == "HOLD"
    assert "持續追蹤" in result.summary
