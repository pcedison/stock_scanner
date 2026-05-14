from backend.services.backtest import run_backtest


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
            ]
        ),
        encoding="utf-8",
    )

    result = run_backtest(path)

    assert result["status"] == "OK"
    assert result["metrics"]["tradeCount"] == 2
    assert result["metrics"]["winRate"] == 0.5
    assert result["metrics"]["totalReturn"] > 0


def test_run_backtest_reports_missing_file(tmp_path):
    result = run_backtest(tmp_path / "missing.csv")

    assert result["status"] == "NO_DATA"
    assert result["metrics"]["tradeCount"] == 0
