from scripts.check_frontend_hygiene import frontend_hygiene_report, validate_frontend_hygiene


def test_frontend_hygiene_current_budget_passes():
    report = frontend_hygiene_report()

    assert validate_frontend_hygiene(report, max_app_lines=2450, max_inner_html=19) == []


def test_frontend_hygiene_rejects_inner_html_growth():
    report = {
        "appLines": 100,
        "innerHTMLAssignments": 20,
        "insertAdjacentHTMLCalls": 0,
        "hasSharedEscapeHelper": True,
        "emptyStateUsesSharedHelper": True,
    }

    problems = validate_frontend_hygiene(report, max_app_lines=2450, max_inner_html=19)

    assert any("innerHTML" in problem for problem in problems)
