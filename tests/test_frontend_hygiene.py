from pathlib import Path

from scripts.check_frontend_hygiene import frontend_hygiene_report, validate_frontend_hygiene


ROOT_DIR = Path(__file__).resolve().parents[1]
STYLE_VERSION = "20260519-design-refresh"
APP_VERSION = "20260522-cache-aware-overview"


def test_frontend_hygiene_current_budget_passes():
    report = frontend_hygiene_report()

    assert validate_frontend_hygiene(report, max_app_lines=2668, max_inner_html=19) == []


def test_frontend_hygiene_rejects_inner_html_growth():
    report = {
        "appLines": 100,
        "innerHTMLAssignments": 20,
        "dangerousInnerHTMLAssignments": [],
        "insertAdjacentHTMLCalls": 0,
        "hasSharedEscapeHelper": True,
        "hasSharedAuthHelper": True,
        "emptyStateUsesSharedHelper": True,
    }

    problems = validate_frontend_hygiene(report, max_app_lines=2668, max_inner_html=19)

    assert any("innerHTML" in problem for problem in problems)


def test_frontend_hygiene_rejects_dangerous_inner_html_assignment(tmp_path):
    app_js = tmp_path / "app.js"
    dom_js = tmp_path / "dom.js"
    auth_js = tmp_path / "auth.js"
    app_js.write_text(
        """
function renderUnsafe(target, payload) {
  target.innerHTML = payload.name;
}
function renderSafe(target, payload) {
  target.innerHTML = `<p>${escapeHtml(payload.name)}</p>`;
}
""",
        encoding="utf-8",
    )
    dom_js.write_text("const StockScannerDom = {}; function escapeHtml(value) { return value; }", encoding="utf-8")
    auth_js.write_text("const StockScannerAuth = { normalizeAuthUser() {} };", encoding="utf-8")

    report = frontend_hygiene_report(app_js, dom_js, auth_js)
    problems = validate_frontend_hygiene(report, max_app_lines=2668, max_inner_html=19)

    assert report["dangerousInnerHTMLAssignments"] == [{"line": 3, "statement": "target.innerHTML = payload.name;"}]
    assert any("dangerous innerHTML assignments" in problem for problem in problems)


def test_frontend_hygiene_rejects_mixed_escaped_and_raw_template_assignment(tmp_path):
    app_js = tmp_path / "app.js"
    dom_js = tmp_path / "dom.js"
    auth_js = tmp_path / "auth.js"
    app_js.write_text(
        """
function renderMixed(target, payload) {
  target.innerHTML = `<p>${escapeHtml(payload.name)} ${payload.raw}</p>`;
}
""",
        encoding="utf-8",
    )
    dom_js.write_text("const StockScannerDom = {}; function escapeHtml(value) { return value; }", encoding="utf-8")
    auth_js.write_text("const StockScannerAuth = { normalizeAuthUser() {} };", encoding="utf-8")

    report = frontend_hygiene_report(app_js, dom_js, auth_js)

    assert report["dangerousInnerHTMLAssignments"][0]["line"] == 3
    assert report["dangerousInnerHTMLAssignments"][0]["rawTemplateInterpolations"] == ["${payload.raw}"]


def test_frontend_css_cache_buster_includes_design_refresh_styles():
    index_html = (ROOT_DIR / "frontend" / "index.html").read_text(encoding="utf-8")
    styles_css = (ROOT_DIR / "frontend" / "styles.css").read_text(encoding="utf-8")

    assert f'href="/styles.css?v={STYLE_VERSION}" as="style"' in index_html
    assert f'href="/styles.css?v={STYLE_VERSION}"' in index_html
    assert 'src="/auth.js?v=20260521-auth-helpers"' in index_html
    assert f'src="/app.js?v={APP_VERSION}"' in index_html
    assert ".kpi-card" in styles_css
    assert "Claude Design v2 port" in styles_css
    assert ".holding-exit-alert-banner.critical" in styles_css
