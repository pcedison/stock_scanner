from scripts.check_code_size_budgets import collect_budget_report


def test_current_high_churn_files_stay_within_size_budgets():
    report = collect_budget_report()

    assert report["ok"] is True
    assert report["problems"] == []
    app_budget = next(item for item in report["files"] if item["path"] == "frontend/app.js")
    assert app_budget["lines"] <= app_budget["budget"]
