from scripts.check_code_size_budgets import collect_budget_report


def test_current_high_churn_files_stay_within_size_budgets():
    report = collect_budget_report()

    assert report["ok"] is True
    assert report["problems"] == []
    app_budget = next(item for item in report["files"] if item["path"] == "frontend/app.js")
    assert app_budget["lines"] <= app_budget["budget"]
    worker_budget = next(item for item in report["files"] if item["path"] == "cloudflare/worker.py")
    worker_support_budget = next(item for item in report["files"] if item["path"] == "cloudflare/worker_support.py")
    worker_observability_budget = next(
        item for item in report["files"] if item["path"] == "cloudflare/worker_observability.py"
    )
    worker_health_budget = next(item for item in report["files"] if item["path"] == "cloudflare/worker_health.py")
    assert worker_budget["lines"] < 1000
    # Raised from 900 when the trading-calendar clamp landed; the rule itself lives in
    # cloudflare/worker_trading_calendar.py, which carries its own budget.
    assert worker_budget["budget"] == 905
    assert worker_support_budget["lines"] <= worker_support_budget["budget"]
    assert worker_observability_budget["budget"] == 200
    assert worker_observability_budget["lines"] <= worker_observability_budget["budget"]
    assert worker_health_budget["budget"] == 120
    assert worker_health_budget["lines"] <= worker_health_budget["budget"]
