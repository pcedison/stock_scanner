from backend.services.backtest import BacktestRow, _entry_signal, _exit_signal, run_backtest


def _row(**kwargs) -> BacktestRow:
    defaults = {
        "stockCode": "1234",
        "period": "2026-04",
        "closePrice": 100.0,
        "cumulativeRevenueYoY": None,
        "monthlyRevenueYoY": None,
        "epsYoY": None,
        "netIncomeYoY": None,
        "per": None,
        "inventoryTurnover": None,
    }
    defaults.update(kwargs)
    return BacktestRow(**defaults)


def test_entry_signal_all_criteria_met():
    assert _entry_signal(_row(cumulativeRevenueYoY=60.0, per=15.0, inventoryTurnover=3.0)) is True


def test_entry_signal_boundary_values():
    base = {"cumulativeRevenueYoY": 50.0, "per": 19.9, "inventoryTurnover": 2.6}
    assert _entry_signal(_row(**base)) is True
    assert _entry_signal(_row(**{**base, "cumulativeRevenueYoY": 49.9})) is False
    assert _entry_signal(_row(**{**base, "per": 20.0})) is False
    assert _entry_signal(_row(**{**base, "inventoryTurnover": 2.5})) is False


def test_entry_signal_none_returns_false():
    assert _entry_signal(_row()) is False


def test_exit_signal_monthly_revenue():
    assert _exit_signal(_row(monthlyRevenueYoY=29.9)) is True
    assert _exit_signal(_row(monthlyRevenueYoY=30.0)) is False


def test_exit_signal_eps_drop():
    assert _exit_signal(_row(epsYoY=-10.0)) is True
    assert _exit_signal(_row(epsYoY=-9.9)) is False


def test_exit_signal_net_income_negative():
    assert _exit_signal(_row(netIncomeYoY=-0.01)) is True
    assert _exit_signal(_row(netIncomeYoY=0.0)) is False


def test_exit_signal_none_returns_false():
    assert _exit_signal(_row()) is False


def test_run_backtest_from_csv(tmp_path):
    path = tmp_path / "backtest_history.csv"
    path.write_text(
        "\n".join(
            [
                "stock_code,period,close_price,cumulative_revenue_yoy,monthly_revenue_yoy,eps_yoy,net_income_yoy,per,inventory_turnover",
                "2357,2025-01,100,55,60,20,20,12,3",
                "2357,2025-02,120,58,28,18,18,13,3",
                "5274,2025-01,80,66,70,30,35,10,4",
                "5274,2025-02,72,67,65,-12,30,11,4",
                "9999,2025-01,50,70,75,25,25,18,4",
                "9999,2025-02,55,72,20,25,25,18,4",
            ]
        ),
        encoding="utf-8",
    )

    result = run_backtest(path)

    assert result["status"] == "OK"
    assert result["metrics"]["tradeCount"] == 3
    assert result["metrics"]["winRate"] == 2 / 3
    assert result["metrics"]["totalReturn"] > 0


def test_run_backtest_reports_missing_file(tmp_path):
    result = run_backtest(tmp_path / "missing.csv")

    assert result["status"] == "NO_DATA"
    assert result["metrics"]["tradeCount"] == 0
