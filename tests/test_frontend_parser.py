import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MOJIBAKE_CONTROL_RE = re.compile(r"[\u0080-\u009f\ue000-\uf8ff]")


@pytest.fixture(autouse=True)
def _require_node() -> None:
    # These tests exercise the real frontend JS via node. A missing node must be a
    # hard failure, not a silent skip, so CI/local runs can't quietly lose this
    # coverage.
    if shutil.which("node") is None:
        pytest.fail("Node.js is required for the frontend parser tests; install Node to run them.", pytrace=False)


def _run_node_json(script: str) -> dict:
    completed = subprocess.run(
        ["node", "-e", script],
        cwd=ROOT,
        check=True,
        text=True,
        encoding="utf-8",
        capture_output=True,
    )
    return json.loads(completed.stdout)


def test_frontend_dom_helpers_escape_empty_state_html():
    script = r"""
const { emptyStateHtml, escapeHtml, setEmptyState } = require("./frontend/dom.js");
const target = {};
Object.defineProperty(target, "innerHTML", { set(value) { this.value = value; } });
setEmptyState(target, '<img src=x onerror="alert(1)">');
console.log(JSON.stringify({
  escaped: escapeHtml('<img src=x onerror="alert(1)">'),
  empty: emptyStateHtml('<script>alert(1)</script>'),
  setEmpty: target.value,
}));
"""
    completed = subprocess.run(
        ["node", "-e", script],
        cwd=ROOT,
        check=True,
        text=True,
        encoding="utf-8",
        capture_output=True,
    )
    payload = json.loads(completed.stdout)

    assert "<img" not in payload["escaped"]
    assert "&lt;img" in payload["escaped"]
    assert "<script" not in payload["empty"].lower()
    assert "&lt;script" in payload["empty"].lower()
    assert "<img" not in payload["setEmpty"].lower()
    assert "&lt;img" in payload["setEmpty"].lower()


def test_market_render_column_includes_sr_only_stock_identity():
    script = r"""
const { createMarketRender } = require("./frontend/market_render.js");
const renderer = createMarketRender({
  getState: () => ({ marketListPages: { announced: { watch: 0 } }, expandedMarketResultIds: new Set() }),
  escapeHtml(value) {
    return String(value ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  },
  safeText(value, fallback = "") {
    const text = value == null ? "" : String(value).trim();
    return text || fallback;
  },
  safeCompanyName(result) {
    return result?.companyName || "";
  },
  displayResultStatus(result) {
    return { status: result?.status || "WATCH", summary: result?.summary || "" };
  },
  statusClass(status) {
    return String(status || "").toLowerCase();
  },
  statusLabel(status) {
    return status || "";
  },
  renderRule() {
    return "";
  },
  resultActionButtons() {
    return "";
  },
  sortRulesForDisplay(rules) {
    return Array.isArray(rules) ? rules : [];
  },
  sortMarketResultsForDisplay(_columnKey, results) {
    return Array.isArray(results) ? results : [];
  },
  marketColumnNote() {
    return "";
  },
  marketResultId(result, disclosureGroup, columnKey) {
    return `${disclosureGroup}:${columnKey}:${result.stockCode}`;
  },
  MARKET_LIST_PAGE_SIZE: 12,
  MARKET_RESULT_COLUMNS: [["watch", "Watch"]],
  MARKET_DISCLOSURE_TABS: [{ key: "announced" }],
});
const html = renderer.renderMarketColumn("announced", "watch", "Watch", [
  { stockCode: "1101", companyName: "台泥", status: "WATCH", reasons: [] },
]);
console.log(JSON.stringify({ html }));
"""
    payload = _run_node_json(script)

    assert '<span class="sr-only">1101 台泥</span>' in payload["html"]
    assert 'data-market-result-toggle="announced:watch:1101"' in payload["html"]


def test_overview_counts_follow_active_disclosure_tab():
    script = r"""
const { state, activeMarketDisclosureKey, renderOverviewStats } = require("./frontend/app.js");
const nodes = new Map();
global.document = {
  querySelector(selector) {
    if (!nodes.has(selector)) nodes.set(selector, { textContent: "" });
    return nodes.get(selector);
  },
};
const officialQ = { code: "OFFICIAL_Q", severity: "INFO", message: "2026Q1 EPS 1.23" };
const oldQ = { code: "OFFICIAL_Q", severity: "INFO", message: "2025Q4 EPS 1.23" };
const scan = {
  generatedAt: "2026-05-20T00:00:00.000Z",
  filingContext: { activeFinancialReport: { period: "2026Q1" } },
  entry: [
    { stockCode: "1001", status: "ENTRY", reasons: [officialQ] },
    { stockCode: "1002", status: "ENTRY", reasons: [oldQ] },
  ],
  watch: [
    { stockCode: "2001", status: "INSUFFICIENT_DATA", reasons: [officialQ, { code: "E3", severity: "WATCH" }] },
    { stockCode: "2002", status: "INSUFFICIENT_DATA", reasons: [{ code: "E3", severity: "INSUFFICIENT_DATA" }] },
  ],
  excluded: [
    { stockCode: "3001", status: "EXCLUDED", reasons: [officialQ] },
  ],
};
function counts() {
  return {
    entry: nodes.get("#overview-entry-count").textContent,
    watch: nodes.get("#overview-watch-count").textContent,
    excluded: nodes.get("#overview-excluded-count").textContent,
  };
}
state.activeMarketDisclosureTab = "announced";
renderOverviewStats(scan);
const announced = counts();
state.activeMarketDisclosureTab = "pending";
renderOverviewStats(scan);
const pending = counts();
console.log(JSON.stringify({ announced, pending, fallback: activeMarketDisclosureKey("bogus") }));
"""
    completed = subprocess.run(
        ["node", "-e", script],
        cwd=ROOT,
        check=True,
        text=True,
        encoding="utf-8",
        capture_output=True,
    )
    payload = json.loads(completed.stdout)

    assert payload["announced"] == {"entry": "1", "watch": "1", "excluded": "1"}
    assert payload["pending"] == {"entry": "1", "watch": "1", "excluded": "0"}
    assert payload["fallback"] == "announced"


def test_api_client_falls_back_to_worker_after_pages_proxy_5xx():
    script = r"""
const { createApiClient } = require("./frontend/api_client.js");
const calls = [];
global.fetch = async (url, options) => {
  calls.push({ url, credentials: options.credentials, csrf: options.headers.get("X-Stock-Scanner-CSRF") });
  if (String(url).startsWith("/api/")) {
    return { ok: false, status: 503, text: async () => "proxy down", headers: { get: () => "text/plain" } };
  }
  return { ok: true, status: 200, text: async () => '{"ok":true}', headers: { get: () => "application/json" } };
};
const client = createApiClient({
  csrfHeaderName: "X-Stock-Scanner-CSRF",
  csrfHeaderValue: "1",
  fallbackOrigin: "https://worker.example",
});
(async () => {
  const first = await client.request("/api/scan/market", { method: "POST", body: "{}" });
  const second = await client.request("/api/settings");
  console.log(JSON.stringify({ firstStatus: first.status, secondStatus: second.status, calls, activeOrigin: client.activeOrigin() }));
})();
"""
    completed = subprocess.run(
        ["node", "-e", script],
        cwd=ROOT,
        check=True,
        text=True,
        encoding="utf-8",
        capture_output=True,
    )
    payload = json.loads(completed.stdout)

    assert payload["firstStatus"] == 200
    assert payload["secondStatus"] == 200
    assert [item["url"] for item in payload["calls"]] == [
        "/api/scan/market",
        "https://worker.example/api/scan/market",
        "https://worker.example/api/settings",
    ]
    assert payload["calls"][1]["credentials"] == "include"
    assert payload["calls"][1]["csrf"] == "1"
    assert payload["activeOrigin"] == "https://worker.example"


def test_api_client_does_not_direct_fallback_for_account_mutations():
    script = r"""
const { createApiClient } = require("./frontend/api_client.js");
const calls = [];
global.fetch = async (url, options) => {
  calls.push({ url, method: options.method });
  return { ok: false, status: 503, text: async () => "proxy down", headers: { get: () => "text/plain" } };
};
const client = createApiClient({
  csrfHeaderName: "X-Stock-Scanner-CSRF",
  csrfHeaderValue: "1",
  fallbackOrigin: "https://worker.example",
});
(async () => {
  const response = await client.request("/api/auth/register", { method: "POST", body: "{}" });
  console.log(JSON.stringify({ status: response.status, calls, activeOrigin: client.activeOrigin() }));
})();
"""
    completed = subprocess.run(
        ["node", "-e", script],
        cwd=ROOT,
        check=True,
        text=True,
        encoding="utf-8",
        capture_output=True,
    )
    payload = json.loads(completed.stdout)

    assert payload["status"] == 503
    assert payload["calls"] == [{"url": "/api/auth/register", "method": "POST"}]
    assert payload["activeOrigin"] == ""


def test_api_client_direct_mode_uses_worker_for_account_mutations():
    script = r"""
const { createApiClient } = require("./frontend/api_client.js");
const calls = [];
global.fetch = async (url, options) => {
  calls.push({ url, method: options.method, credentials: options.credentials, csrf: options.headers.get("X-Stock-Scanner-CSRF") });
  return { ok: true, status: 200, text: async () => '{"authenticated":true}', headers: { get: () => "application/json" } };
};
const client = createApiClient({
  apiMode: "direct",
  csrfHeaderName: "X-Stock-Scanner-CSRF",
  csrfHeaderValue: "1",
  fallbackOrigin: "https://worker.example",
});
(async () => {
  const response = await client.request("/api/auth/register", { method: "POST", body: "{}" });
  console.log(JSON.stringify({ status: response.status, calls, activeOrigin: client.activeOrigin(), mode: client.apiMode() }));
})();
"""
    completed = subprocess.run(
        ["node", "-e", script],
        cwd=ROOT,
        check=True,
        text=True,
        encoding="utf-8",
        capture_output=True,
    )
    payload = json.loads(completed.stdout)

    assert payload["status"] == 200
    assert payload["calls"] == [
        {
            "url": "https://worker.example/api/auth/register",
            "method": "POST",
            "credentials": "include",
            "csrf": "1",
        }
    ]
    assert payload["activeOrigin"] == "https://worker.example"
    assert payload["mode"] == "direct"


def test_pages_dev_api_client_defaults_to_same_origin_for_account_routes():
    script = r"""
const { createApiClient } = require("./frontend/api_client.js");
global.location = { hostname: "stock-scanner-beta.pages.dev" };
const calls = [];
global.fetch = async (url, options) => {
  calls.push({ url, method: options.method, credentials: options.credentials });
  return { ok: true, status: 200, text: async () => '{"authenticated":true}', headers: { get: () => "application/json" } };
};
const client = createApiClient({
  csrfHeaderName: "X-Stock-Scanner-CSRF",
  csrfHeaderValue: "1",
  fallbackOrigin: "https://worker.example",
});
(async () => {
  await client.request("/api/auth/login", { method: "POST", body: "{}" });
  console.log(JSON.stringify({ mode: client.apiMode(), calls, activeOrigin: client.activeOrigin() }));
})().catch((error) => {
  console.error(error);
  process.exit(1);
});
"""
    completed = subprocess.run(
        ["node", "-e", script],
        cwd=ROOT,
        check=True,
        text=True,
        encoding="utf-8",
        capture_output=True,
    )
    payload = json.loads(completed.stdout)

    assert payload["mode"] == "same-origin"
    assert payload["calls"] == [{"url": "/api/auth/login", "method": "POST", "credentials": "same-origin"}]
    assert payload["activeOrigin"] == ""


def test_parse_stock_input_cases_do_not_return_undefined():
    script = r"""
const { DEFAULT_COMPANIES, STRATEGY_STATUS_DETAILS, state, findStrategyStatusDetail, parseStockInput, normalizeCompanies, renderAnalysisCard, renderMarketResultRow, renderMarketPagination, renderStrategyRuleCards, adminUsersErrorMessage, loadHoldingsFromStorage, normalizeHoldingRecords, apiErrorMessage, normalizeAuthUsername, normalizeAuthUser, authValidationMessage, isSuperUserIdentity, isSuperUser, groupMarketScanResults, sortMarketResultsForDisplay, e4PerValue, hasInsufficientData, hasFinancialReportForContext, hasPublishedScanData, isPartialPublishedResult, formatEvidenceValue, renderRuleEvidence, renderRule, sortRulesForDisplay, settingsPermissionMessage, holdingExitCodes, holdingSignal, renderHoldingSignal, holdingExitAlerts, renderHoldingExitAlertBanner } = require("./frontend/app.js");
const cases = DEFAULT_COMPANIES.flatMap((company) => [
  [company.stockCode, company.stockCode, company.name],
  [company.name, company.stockCode, company.name],
  [`${company.stockCode} ${company.name}`, company.stockCode, company.name],
  [` ${company.stockCode}  ${company.name} `, company.stockCode, company.name],
  [`${company.name} 1000 300`, company.stockCode, company.name],
  [`${company.stockCode} ${company.name} 1000 300`, company.stockCode, company.name],
]);
const output = cases.map(([input, stockCode, name]) => {
  const parsed = parseStockInput(input, DEFAULT_COMPANIES);
  return { input, ok: parsed.ok, stockCode: parsed.stockCode, name: parsed.name, expectedCode: stockCode, expectedName: name };
});
const unknownInputs = ["9999 不存在", "不存在股票", "台灣神奇公司", "0000"];
const unknown = unknownInputs.map((input) => ({ input, ...parseStockInput(input, DEFAULT_COMPANIES) }));
const incompleteCompanies = normalizeCompanies([
  { stockCode: "9999" },
  { name: "缺代碼公司" },
  { stockCode: "1234", name: "測試公司", market: undefined, industryName: undefined },
  null,
]);
const incompleteParsed = [
  parseStockInput("9999", incompleteCompanies),
  parseStockInput("測試公司", incompleteCompanies),
];
const incompleteHtml = renderAnalysisCard({
  status: undefined,
  reasons: [{ passed: false }, null],
});
const partialPublishedResult = {
  stockCode: "1101",
  companyName: "台泥",
  status: "INSUFFICIENT_DATA",
  summary: "資料不足，不能硬給進場或排除結論。",
  reasons: [
    { code: "E1", title: "近 5 年沒有虧損", passed: false, severity: "INSUFFICIENT_DATA", message: "近 5 年年度淨利尚未自動補齊。" },
    { code: "E3", title: "今年累計營收年增率 >= 50%", passed: false, severity: "WATCH", message: "目前採用 cumulative_ytd，年增率為 -4.6%。" },
    { code: "OFFICIAL_Q", title: "最新季官方財報資料", passed: true, severity: "INFO", message: "2026Q1 EPS 0.1。" }
  ],
};
const partialHtml = renderAnalysisCard(partialPublishedResult, { disclosureGroup: "announced" });
const pendingHtml = renderAnalysisCard(partialPublishedResult, { disclosureGroup: "pending" });
const holdingPartialHtml = renderAnalysisCard(partialPublishedResult, { disclosureGroup: "holding" });
const compactMarketRow = renderMarketResultRow(partialPublishedResult, { disclosureGroup: "announced", columnKey: "watch" });
const marketPagination = renderMarketPagination("announced", "watch", 26);
state.holdings = [{ stockCode: "2357", name: "華碩", shares: 0, averageCost: null }];
const trackedAddHtml = renderAnalysisCard({ stockCode: "2357", companyName: "華碩", status: "ENTRY", summary: "ok", reasons: [] }, { allowAddAction: true });
state.holdings = [];
const untrackedAddHtml = renderAnalysisCard({ stockCode: "2357", companyName: "華碩", status: "ENTRY", summary: "ok", reasons: [] }, { allowAddAction: true });
const fakeStorage = {
  value: JSON.stringify([
    { stockCode: "2330", shares: "1000", averageCost: "600.5" },
    { name: "華碩", shares: "200", averageCost: "" },
    { stockCode: "9999", shares: "bad" }
  ]),
  getItem() { return this.value; },
  setItem(key, value) { this.value = value; },
};
const holdings = loadHoldingsFromStorage(fakeStorage, DEFAULT_COMPANIES);
fakeStorage.value = "{bad json";
const repaired = loadHoldingsFromStorage(fakeStorage, DEFAULT_COMPANIES);
const normalizedHoldings = normalizeHoldingRecords([{ stockCode: "2357", name: "x", shares: 1 }, { stockCode: "2357", shares: 2 }], DEFAULT_COMPANIES);
const marketGroups = groupMarketScanResults({
  entry: [{ stockCode: "2357", status: "ENTRY", reasons: [{ severity: "INFO" }] }],
  watch: [
    { stockCode: "1101", status: "INSUFFICIENT_DATA", reasons: [{ code: "E1", severity: "INSUFFICIENT_DATA" }, { code: "E3", severity: "WATCH" }] },
    { stockCode: "9999", status: "INSUFFICIENT_DATA", reasons: [{ code: "E3", severity: "INSUFFICIENT_DATA" }] }
  ],
  excluded: [{ stockCode: "2881", status: "EXCLUDED", reasons: [{ severity: "EXCLUDED" }] }],
});
const sortedEntryByPer = sortMarketResultsForDisplay("entry", [
  { stockCode: "3000", reasons: [{ code: "E4", message: "PER 為 18.5。" }] },
  { stockCode: "1000", reasons: [{ code: "E4", message: "PER 為 9.8。" }] },
  { stockCode: "2000", reasons: [{ code: "E4", message: "PER 為 15.2。" }] },
  { stockCode: "9999", reasons: [{ code: "E4", message: "PER 缺資料。" }] },
]).map((item) => item.stockCode);
const extractedPer = e4PerValue({ reasons: [{ code: "E4", message: "PER 為 12.34。" }] });
const q1Context = { activeFinancialReport: { period: "2026Q1" } };
const filingAwareGroups = groupMarketScanResults({
  filingContext: q1Context,
  entry: [],
  watch: [
    { stockCode: "1101", status: "INSUFFICIENT_DATA", reasons: [{ code: "E3", severity: "WATCH" }, { code: "OFFICIAL_Q", severity: "INFO", message: "2026Q1 EPS 0.1。" }] },
    { stockCode: "1102", status: "INSUFFICIENT_DATA", reasons: [{ code: "E3", severity: "WATCH" }, { code: "OFFICIAL_Q", severity: "INFO", message: "2025Q4 EPS 0.1。" }] },
    { stockCode: "1103", status: "INSUFFICIENT_DATA", reasons: [{ code: "E3", severity: "WATCH" }] }
  ],
  excluded: [],
});
const evidenceRule = {
  code: "E1",
  title: "近 5 年沒有虧損",
  passed: false,
  severity: "WATCH",
  message: "近 5 年年度淨利有 2 年虧損；詳見年度表格。",
  evidence: [
    { label: "2025", value: 11962952, unit: "thousand_twd", metric: "annual_net_income" },
    { label: "2024", value: 8969775, unit: "thousand_twd", metric: "annual_net_income" },
    { label: "2023", value: -1699593, unit: "thousand_twd", metric: "annual_net_income" }
  ],
};
const evidenceValue = formatEvidenceValue(evidenceRule.evidence[0]);
const evidenceHtml = renderRuleEvidence(evidenceRule);
const evidenceRuleHtml = renderRule(evidenceRule);
const orderedCodes = sortRulesForDisplay([
  { code: "HOLDING" },
  { code: "X2" },
  { code: "T3" },
  { code: "E1" },
  { code: "OFFICIAL_Q" },
  { code: "A1" },
  { code: "OFFICIAL_VALUATION" },
]).map((rule) => rule.code);
const orderedHtml = renderAnalysisCard({
  stockCode: "3135",
  companyName: "凌航",
  status: "HOLD",
  summary: "order test",
  reasons: [
    { code: "HOLDING", title: "目前持股", passed: true, severity: "INFO", message: "持股狀態" },
    { code: "X1", title: "當年度的累計營收年增率 >= 最新當月份營收年增率的 50%", passed: true, severity: "INFO", message: "X1" },
    { code: "T3", title: "毛利率追蹤", passed: true, severity: "INFO", message: "T3" },
    { code: "E1", title: "近 5 年沒有虧損", passed: true, severity: "INFO", message: "E1" },
    { code: "OFFICIAL_Q", title: "最新季官方財報資料", passed: true, severity: "INFO", message: "OFFICIAL_Q" },
    { code: "OFFICIAL_VALUATION", title: "官方估值資料", passed: true, severity: "INFO", message: "OFFICIAL_VALUATION" },
    { code: "A1", title: "原進場條件仍符合", passed: true, severity: "INFO", message: "A1" },
  ],
});
const exitHoldingResult = {
  stockCode: "3008",
  companyName: "大立光",
  status: "EXIT",
  summary: "已觸發高優先出場條件，建議出清或至少大幅降低部位。",
  reasons: [
    { code: "X1", title: "當年度的累計營收年增率 >= 最新當月份營收年增率的 50%", passed: false, severity: "WARNING", message: "X1" },
    { code: "X4", title: "季度 EPS 不可減少超過 10%", passed: false, severity: "EXIT", message: "X4" },
    { code: "HOLDING", title: "目前持股", passed: true, severity: "INFO", message: "目前 1000 股" },
  ],
};
const exitCodes = holdingExitCodes(exitHoldingResult);
const exitSignal = holdingSignal(exitHoldingResult);
const exitSignalHtml = renderHoldingSignal(exitHoldingResult);
state.holdings = [{ stockCode: "3008", name: "Largan", shares: 100, averageCost: 2000 }];
const exitAlerts = holdingExitAlerts({ results: [exitHoldingResult], missing: [] });
const exitAlertBanner = renderHoldingExitAlertBanner(exitAlerts);
state.holdings = [];
const pcedisonFromUsername = normalizeAuthUser({ username: " PCEDISON@GMAIL.COM ", displayName: "", isSuperUser: true });
const pcedisonMissingFlag = normalizeAuthUser({ username: "pcedison@gmail.com", isSuperUser: true });
const normalWithFlag = normalizeAuthUser({ username: "normal@example.com", isSuperUser: false });
state.auth = { authenticated: true, user: pcedisonFromUsername };
const superState = isSuperUser();
state.auth = { checked: true, authenticated: true, user: normalWithFlag };
const normalState = isSuperUser();
const normalSettingsPermission = settingsPermissionMessage();
state.auth = { checked: true, authenticated: false, user: null };
const anonymousSettingsPermission = settingsPermissionMessage();
const header = (contentType) => ({ get: () => contentType });
const apiErrors = {
  html500: apiErrorMessage({ status: 500, headers: header("text/html") }, '<!DOCTYPE html><html><head><title>Worker threw exception</title></head><body>raw</body></html>'),
  workerJson500: apiErrorMessage({ status: 500, headers: header("application/json") }, JSON.stringify({ detail: "Cloudflare Worker API error: internal detail" })),
  login401: apiErrorMessage({ status: 401, headers: header("application/json") }, JSON.stringify({ detail: "帳號或密碼錯誤" })),
};
const strategyCardsHtml = renderStrategyRuleCards();
const authValidation = {
  valid: authValidationMessage("qa@example.com", "test-password-123"),
  badEmail: authValidationMessage("qa", "test-password-123"),
  shortPassword: authValidationMessage("qa@example.com", "short"),
};
const adminErrors = {
  notFound: adminUsersErrorMessage("Not found"),
  normal: adminUsersErrorMessage("請先登入"),
};
console.log(JSON.stringify({ output, unknown, incompleteCompanies, incompleteParsed, incompleteHtml, partialHtml, pendingHtml, holdingPartialHtml, compactMarketRow, marketPagination, trackedAddHtml, untrackedAddHtml, holdings, repaired, repairedStorage: fakeStorage.value, normalizedHoldings, apiErrors, authValidation, adminErrors, auth: { normalizedSuperUsername: normalizeAuthUsername(" PCEDISON@GMAIL.COM "), pcedisonFromUsername, pcedisonMissingFlag, normalWithFlag, pcedisonIdentity: isSuperUserIdentity(pcedisonMissingFlag), normalIdentity: isSuperUserIdentity(normalWithFlag), superState, normalState, normalSettingsPermission, anonymousSettingsPermission }, marketGroups, sortedEntryByPer, extractedPer, filingAwareGroups, evidenceValue, evidenceHtml, evidenceRuleHtml, orderedCodes, orderedHtml, exitCodes, exitSignal, exitSignalHtml, exitAlerts, exitAlertBanner, strategyCardsHtml, strategyDetailLabels: STRATEGY_STATUS_DETAILS.map((item) => item.label), entryDetail: findStrategyStatusDetail("entry"), addWatchDetail: findStrategyStatusDetail("addWatch"), tSeriesDetail: findStrategyStatusDetail("grossMargin"), hasInsufficient: hasInsufficientData(marketGroups.announced.watch[0]), hasFinancialForContext: hasFinancialReportForContext(filingAwareGroups.announced.watch[0], q1Context), hasPublished: hasPublishedScanData(marketGroups.announced.watch[0]), isPartialPublished: isPartialPublishedResult(partialPublishedResult) }));
"""
    completed = subprocess.run(
        ["node", "-e", script],
        cwd=ROOT,
        check=True,
        text=True,
        encoding="utf-8",
        capture_output=True,
    )
    payload = json.loads(completed.stdout)

    for item in payload["output"]:
        assert item["ok"] is True
        assert item["stockCode"] == item["expectedCode"]
        assert item["name"] == item["expectedName"]
        assert "undefined" not in json.dumps(item, ensure_ascii=False)

    for item in payload["unknown"]:
        assert item["ok"] is False
        assert item["error"] == "找不到資料"
        generated = {key: value for key, value in item.items() if key != "input"}
        assert "undefined" not in json.dumps(generated, ensure_ascii=False)

    assert payload["incompleteParsed"][0]["ok"] is True
    assert payload["incompleteParsed"][0]["name"] == "未知公司"
    assert payload["incompleteParsed"][1]["ok"] is True
    assert payload["incompleteParsed"][1]["stockCode"] == "1234"
    assert "undefined" not in json.dumps(payload["incompleteCompanies"], ensure_ascii=False)
    assert "undefined" not in json.dumps(payload["incompleteParsed"], ensure_ascii=False)
    assert "undefined" not in payload["incompleteHtml"]
    assert "可初篩" in payload["partialHtml"]
    assert "待補" in payload["partialHtml"]
    assert "資料不足，不能硬給進場或排除結論。" not in payload["partialHtml"]
    assert "待補資料" in payload["pendingHtml"]
    assert "可追蹤" in payload["holdingPartialHtml"]
    assert "完整續抱或出場結論仍待補" in payload["holdingPartialHtml"]
    assert "1101 台泥" in payload["compactMarketRow"]
    assert "data-market-result-toggle" in payload["compactMarketRow"]
    assert "E1 待補" not in payload["compactMarketRow"]
    assert "第 1 / 5 頁" in payload["marketPagination"]
    assert "下一頁" in payload["marketPagination"]
    assert "已在持股" in payload["trackedAddHtml"]
    assert "data-add-from-result" not in payload["trackedAddHtml"]
    assert "data-add-from-result" in payload["untrackedAddHtml"]
    assert [item["stockCode"] for item in payload["holdings"]] == ["2330", "2357"]
    assert payload["holdings"][0]["shares"] == 1000
    assert payload["holdings"][1]["averageCost"] is None
    assert payload["repaired"] == []
    assert payload["repairedStorage"] == "[]"
    assert payload["normalizedHoldings"][0]["shares"] == 2
    assert payload["apiErrors"]["html500"] == "伺服器暫時無法處理請求，請稍後再試。"
    assert payload["apiErrors"]["workerJson500"] == "伺服器暫時無法處理請求，請稍後再試。"
    assert payload["apiErrors"]["login401"] == "帳號或密碼錯誤"
    assert payload["authValidation"]["valid"] == ""
    assert payload["authValidation"]["badEmail"] == "請輸入有效電子信箱。"
    assert payload["authValidation"]["shortPassword"] == "密碼至少需要 8 個字元。"
    assert "管理 API 尚未部署" in payload["adminErrors"]["notFound"]
    assert payload["adminErrors"]["normal"] == "使用者清單讀取失敗：請先登入"
    assert payload["auth"]["normalizedSuperUsername"] == "pcedison@gmail.com"
    assert payload["auth"]["pcedisonFromUsername"]["isSuperUser"] is True  # API flag trusted
    assert payload["auth"]["pcedisonMissingFlag"]["isSuperUser"] is True  # API flag trusted
    assert payload["auth"]["normalWithFlag"]["isSuperUser"] is False  # API flag trusted
    assert payload["auth"]["pcedisonIdentity"] is True
    assert payload["auth"]["normalIdentity"] is False
    assert payload["auth"]["superState"] is True
    assert payload["auth"]["normalState"] is False
    assert payload["auth"]["normalSettingsPermission"] == "需要管理員"
    assert payload["auth"]["anonymousSettingsPermission"] == "需要登入管理員"
    assert payload["marketGroups"]["announced"]["entry"][0]["stockCode"] == "2357"
    assert payload["marketGroups"]["announced"]["watch"][0]["stockCode"] == "1101"
    assert payload["marketGroups"]["announced"]["excluded"][0]["stockCode"] == "2881"
    assert payload["marketGroups"]["pending"]["watch"][0]["stockCode"] == "9999"
    assert payload["sortedEntryByPer"] == ["1000", "2000", "3000", "9999"]
    assert payload["extractedPer"] == 12.34
    assert payload["filingAwareGroups"]["announced"]["watch"][0]["stockCode"] == "1101"
    assert [item["stockCode"] for item in payload["filingAwareGroups"]["pending"]["watch"]] == ["1102", "1103"]
    assert payload["evidenceValue"] == "119.63 億"
    assert "evidence-table" in payload["evidenceHtml"]
    assert "data-evidence-width" in payload["evidenceHtml"]
    assert 'style="' not in payload["evidenceHtml"]
    assert "2025" in payload["evidenceHtml"]
    assert "虧損" in payload["evidenceRuleHtml"]
    assert payload["orderedCodes"] == ["E1", "OFFICIAL_Q", "OFFICIAL_VALUATION", "X2", "T3", "A1", "HOLDING"]
    assert payload["orderedHtml"].find("E1 通過") < payload["orderedHtml"].find("OFFICIAL_Q 通過")
    assert payload["orderedHtml"].find("OFFICIAL_VALUATION 通過") < payload["orderedHtml"].find("X1 通過")
    assert payload["orderedHtml"].find("T3 通過") < payload["orderedHtml"].find("A1 通過")
    assert payload["exitCodes"] == ["X1", "X4"]
    assert payload["exitSignal"]["status"] == "EXIT"
    assert payload["exitSignal"]["label"] == "出場 X1、X4"
    assert "holding-signal" in payload["exitSignalHtml"]
    assert payload["exitAlerts"][0]["status"] == "EXIT"
    assert payload["exitAlerts"][0]["stockCode"] == "3008"
    assert payload["exitAlerts"][0]["exitCodes"] == ["X1", "X4"]
    assert "holding-exit-alert-banner" in payload["exitAlertBanner"]
    assert "holding-exit-alert-row exit" in payload["exitAlertBanner"]
    assert "3008" in payload["exitAlertBanner"]
    assert "data-open-holding-alert-details" in payload["exitAlertBanner"]
    assert "出場 X1、X4" in payload["exitSignalHtml"]
    assert payload["strategyDetailLabels"] == [
        "進場 E1-E6",
        "加碼 A1-A7",
        "出場 X1-X5",
        "T 系列追蹤",
        "春節輔助營收",
        "金融業不套主策略",
        "待補資料不硬判斷",
    ]
    assert "strategy-rule-card" in payload["strategyCardsHtml"]
    assert "營收 YoY &gt;= 50%" in payload["strategyCardsHtml"]
    assert "PER &lt; 21.5" in payload["strategyCardsHtml"]
    assert "存貨週轉率 &gt; 2.5" in payload["strategyCardsHtml"]
    assert "本月 YoY &lt;= 200%" in payload["strategyCardsHtml"]
    assert [item[0] for item in payload["entryDetail"]["items"]] == ["E1", "E2", "E3", "E4", "E5", "E6"]
    assert [item[0] for item in payload["addWatchDetail"]["items"]] == ["A1", "A2", "A3", "A4", "A5", "A6", "A7"]
    assert [item[0] for item in payload["tSeriesDetail"]["items"]][:2] == ["T1/T2", "T3"]
    assert payload["hasInsufficient"] is True
    assert payload["hasFinancialForContext"] is True
    assert payload["hasPublished"] is True
    assert payload["isPartialPublished"] is True


def test_frontend_renderers_escape_untrusted_html_payloads():
    script = r"""
const { state, renderAnalysisCard, renderMarketResultRow, renderRuleEvidence, renderRule } = require("./frontend/app.js");

state.holdings = [];
const hostileRule = {
  code: '<img src=x onerror="alert(1)">',
  title: '<script>alert(2)</script>',
  passed: false,
  severity: "WATCH",
  message: 'message <img src=x onerror="alert(3)"> & <b>bold</b>',
  evidence: [
    { label: '<svg onload="alert(4)">', value: 1000, unit: "shares" },
  ],
};
const hostileResult = {
  stockCode: '2357" onclick="alert(5)',
  companyName: '<img src=x onerror="alert(6)">',
  status: "WATCH",
  summary: '<script>alert(7)</script>',
  reasons: [hostileRule],
};

console.log(JSON.stringify({
  analysisCard: renderAnalysisCard(hostileResult, { allowAddAction: true }),
  marketRow: renderMarketResultRow(hostileResult, { disclosureGroup: "announced", columnKey: "watch" }),
  rule: renderRule(hostileRule),
  evidence: renderRuleEvidence(hostileRule),
}));
"""
    completed = subprocess.run(
        ["node", "-e", script],
        cwd=ROOT,
        check=True,
        text=True,
        encoding="utf-8",
        capture_output=True,
    )
    payload = json.loads(completed.stdout)

    for rendered in payload.values():
        lowered = rendered.lower()
        assert "<script" not in lowered
        assert "<img" not in lowered
        assert "<svg" not in lowered
        assert "&lt;" in rendered


def test_task5_ops_copy_is_localized_without_mojibake():
    index_text = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    app_text = (ROOT / "frontend" / "app.js").read_text(encoding="utf-8")
    ops_text = (ROOT / "frontend" / "ops_view_renderers.js").read_text(encoding="utf-8")
    script = r"""
const { createOpsViewRenderers } = require("./frontend/ops_view_renderers.js");

function escapeHtml(value) {
  return String(value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

const renderers = createOpsViewRenderers({
  escapeHtml,
  safeCompanyName(holding = {}) {
    return String(holding.name || holding.companyName || "未知公司");
  },
  renderHoldingSignal() {
    return '<span class="holding-signal">持股訊號</span>';
  },
  displayResultStatus(result = {}) {
    return { status: result.status || "持有", summary: result.summary || "摘要" };
  },
});

console.log(JSON.stringify({
  holdingHtml: renderers.renderHoldingCard({
    holding: { stockCode: "", name: "", shares: 1000, averageCost: 623.5 },
    isEditing: true,
    analysis: null,
    missing: { stockCode: "2330" },
  }),
  dataHtml: renderers.renderDataConsole({
    status: {
      activeProvider: "TWSE",
      activeProviderIsRealtime: true,
      activeProviderIsFullMarket: false,
      activeProviderHasCompleteFundamentals: true,
      mockUniverseSize: 8,
      officialUniverseSize: 1200,
      officialMonthlySnapshotSize: 950,
      officialHistoryRows: 4321,
      officialIncomeStatementSize: 1200,
      officialBalanceSheetSize: 1200,
      officialValuationSize: 1180,
      fundamentalsImportRows: 3600,
      financialFreshness: {
        status: "ok",
        isFresh: true,
        blocksDeployment: false,
        expectedFinancialPeriod: "2026Q1",
        latestCachedFinancialPeriod: "2026Q1",
        expectedPeriodCoverage: 1188,
        historyUpdatedAt: "2026-06-22T01:00:00+00:00",
        message: "財報快取已覆蓋 2026Q1，目前有 1188 檔公司資料。",
      },
      officialHistoricalFundamentals: { note: "官方基本面資料已匯入。" },
    },
    integrationStatus: {
      notifications: [{ configured: true }, { configured: false }],
      broker: { configured: true },
      aiSummary: { configured: false },
    },
    backtestStatus: {
      metrics: { tradeCount: 42 },
      note: "回測交易摘要已同步。",
    },
  }),
  schedulerHtml: renderers.renderSchedulerStatus({
    schedulerStatus: {
      status: "執行中",
      events: ["daily-scan", "refresh"],
      nextTradingDay: "2026-06-23",
    },
    autoAction: "自動掃描",
  }),
}));
"""
    payload = _run_node_json(script)
    rendered_text = "\n".join(payload.values())

    assert "<strong>權限</strong>" in index_text
    assert "設定對所有人可見，但只有管理員與超級使用者可以修改並儲存。" in index_text
    assert "Access" not in index_text
    assert "Settings stay visible for everyone" not in index_text

    for expected in [
        "尚未儲存持股",
        "資料來源狀態暫時無法讀取",
        "排程狀態暫時無法讀取",
        "未知代碼",
        "未知公司",
    ]:
        assert expected in app_text

    for forbidden in [
        "No saved holdings yet",
        "Data source status unavailable",
        "Status unavailable",
        '"Unknown"',
        "'Unknown'",
    ]:
        assert forbidden not in app_text

    for expected in [
        "持股股數",
        "平均成本",
        "減碼股數",
        "取消",
        "刪除",
        "儲存",
        "減碼",
        "是",
        "否",
        "已設定",
        "未設定",
        "資料源",
        "即時資料",
        "股票池",
        "基本面",
        "官方股票池",
        "月營收快照",
        "歷史列數",
        "損益表",
        "資產負債表",
        "估值資料",
        "匯入列數",
        "財報新鮮度",
        "應覆蓋期別",
        "快取最新期別",
        "當期覆蓋數",
        "阻擋部署",
        "2026Q1",
        "1188",
        "整合與通知",
        "券商",
        "AI 摘要",
        "交易筆數",
        "排程狀態",
        "事件",
        "下個交易日",
        "自動掃描",
        "未知代碼",
        "未知公司",
    ]:
        assert expected in rendered_text

    for forbidden in [
        "Yes",
        "No",
        "Configured",
        "Missing",
        "Provider",
        "Realtime",
        "Universe",
        "Fundamentals",
        "Official Universe",
        "Monthly Snapshot",
        "History Rows",
        "Income Statement",
        "Balance Sheet",
        "Valuation",
        "Import Rows",
        "Integrations",
        "Broker",
        "Trades",
        "configured",
    ]:
        assert forbidden not in rendered_text

    for text in [ops_text, rendered_text]:
        assert not MOJIBAKE_CONTROL_RE.search(text), repr(MOJIBAKE_CONTROL_RE.search(text).group(0))
