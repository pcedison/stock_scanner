from pathlib import Path

from scripts.check_frontend_hygiene import (
    dangerous_inner_html_assignments,
    frontend_hygiene_report,
    validate_frontend_hygiene,
)

ROOT_DIR = Path(__file__).resolve().parents[1]
STYLE_VERSION = "20260519-design-refresh"
APP_VERSION = "20260612-x1-exit-rule"
STRATEGY_CONTENT_VERSION = "20260612-x1-exit-rule"
DOM_VERSION = "20260525-dom-hardening"


def test_frontend_hygiene_current_budget_passes():
    report = frontend_hygiene_report()

    assert validate_frontend_hygiene(report, max_app_lines=2400, max_inner_html=0) == []
    assert report["innerHTMLAssignments"] == 0
    assert report["hasSplitReferenceData"] is True
    assert report["hasSplitStrategyContent"] is True
    assert report["hasSplitStorageHelper"] is True
    assert report["hasSplitRendererFactory"] is True
    assert report["appOwnsStrategyContent"] is False


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

    problems = validate_frontend_hygiene(report, max_app_lines=2400, max_inner_html=0)

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
    problems = validate_frontend_hygiene(report, max_app_lines=2400, max_inner_html=0)

    assert report["dangerousInnerHTMLAssignments"] == [{"line": 3, "statement": "target.innerHTML = payload.name;"}]
    assert any("dangerous HTML sinks" in problem for problem in problems)


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


def test_frontend_hygiene_rejects_raw_set_safe_html_sink(tmp_path):
    app_js = tmp_path / "app.js"
    dom_js = tmp_path / "dom.js"
    auth_js = tmp_path / "auth.js"
    app_js.write_text(
        """
function renderMixed(target, payload) {
  setSafeHtml(target, `<p>${escapeHtml(payload.name)} ${payload.raw}</p>`);
}
""",
        encoding="utf-8",
    )
    dom_js.write_text("const StockScannerDom = {}; function escapeHtml(value) { return value; }", encoding="utf-8")
    auth_js.write_text("const StockScannerAuth = { normalizeAuthUser() {} };", encoding="utf-8")

    report = frontend_hygiene_report(app_js, dom_js, auth_js)
    problems = validate_frontend_hygiene(report, max_app_lines=2400, max_inner_html=0)

    assert report["dangerousInnerHTMLAssignments"][0]["line"] == 3
    assert report["dangerousInnerHTMLAssignments"][0]["rawTemplateInterpolations"] == ["${payload.raw}"]
    assert any("dangerous HTML sinks" in problem for problem in problems)


def test_frontend_css_cache_buster_includes_design_refresh_styles():
    index_html = (ROOT_DIR / "frontend" / "index.html").read_text(encoding="utf-8")
    styles_css = (ROOT_DIR / "frontend" / "styles.css").read_text(encoding="utf-8")

    assert f'href="/styles.css?v={STYLE_VERSION}" as="style"' in index_html
    assert f'href="/styles.css?v={STYLE_VERSION}"' in index_html
    assert f'src="/dom.js?v={DOM_VERSION}"' in index_html
    assert 'src="/auth.js?v=20260521-auth-helpers"' in index_html
    assert 'src="/reference_data.js?v=20260522-frontend-split"' in index_html
    assert f'src="/strategy_content.js?v={STRATEGY_CONTENT_VERSION}"' in index_html
    assert 'src="/storage.js?v=20260522-frontend-split"' in index_html
    assert 'src="/renderers.js?v=20260522-frontend-split"' in index_html
    assert 'src="/api_client.js?v=20260530-cacheable-scan-get"' in index_html
    assert f'src="/app.js?v={APP_VERSION}"' in index_html
    assert ".kpi-card" in styles_css
    assert "Claude Design v2 port" in styles_css
    assert ".holding-exit-alert-banner.critical" in styles_css


def test_map_chain_sink_does_not_run_into_following_code():
    # A fully-escaped `.map(...).join("")` template sink, followed by unrelated
    # code containing a raw `${...}` (a fetch URL). The statement must terminate at
    # its own `);` rather than running away and flagging the downstream code.
    source = """
function renderList(target, items) {
  setSafeHtml(target, `
    <ul>
      ${items
        .map((item) => `<li>${escapeHtml(item.name)}</li>`)
        .join("")}
    </ul>
  `);
}

async function loadDetails(result) {
  const detail = await apiJson(`/api/analyze/${result.stockCode}`, { method: "POST" });
}
"""
    assert dangerous_inner_html_assignments(source) == []


def test_nested_template_literals_have_no_false_positive():
    source = 'setSafeHtml(target, `<div>${rows.map((r) => `<span>${escapeHtml(r.label)}</span>`).join("")}</div>`);\n'
    assert dangerous_inner_html_assignments(source) == []


def test_multiline_template_with_raw_interpolation_is_still_flagged():
    source = """
setSafeHtml(target, `
  <div>${escapeHtml(payload.title)}</div>
  <div>${payload.body}</div>
`);
"""
    findings = dangerous_inner_html_assignments(source)
    assert len(findings) == 1
    assert findings[0]["rawTemplateInterpolations"] == ["${payload.body}"]
