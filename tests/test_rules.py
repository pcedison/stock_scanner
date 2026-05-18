from backend.models.financial import MonthlyRevenue
from backend.models.holding import Holding
from backend.models.settings import ScannerSettings
from backend.services.data_provider import MockDataProvider
from backend.services.rules import RuleEngine


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
