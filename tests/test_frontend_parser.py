import json
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not available")
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


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not available")
def test_overview_counts_follow_active_market_disclosure_tab():
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


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not available")
def test_parse_stock_input_cases_do_not_return_undefined():
    script = r"""
const { DEFAULT_COMPANIES, SUPER_USER_USERNAME, STRATEGY_STATUS_DETAILS, state, findStrategyStatusDetail, parseStockInput, normalizeCompanies, renderAnalysisCard, renderMarketResultRow, renderMarketPagination, renderStrategyRuleCards, adminUsersErrorMessage, loadHoldingsFromStorage, normalizeHoldingRecords, apiErrorMessage, normalizeAuthUsername, normalizeAuthUser, authValidationMessage, isSuperUserIdentity, isSuperUser, groupMarketScanResults, sortMarketResultsForDisplay, e4PerValue, hasInsufficientData, hasFinancialReportForContext, hasPublishedScanData, isPartialPublishedResult, formatEvidenceValue, renderRuleEvidence, renderRule, sortRulesForDisplay, settingsPermissionMessage, holdingExitCodes, holdingSignal, renderHoldingSignal, holdingExitAlerts, renderHoldingExitAlertBanner } = require("./frontend/app.js");
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
    { code: "X1", title: "月營收年增率不可低於 30%", passed: true, severity: "INFO", message: "X1" },
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
    { code: "X1", title: "月營收年增率不可低於 30%", passed: false, severity: "WARNING", message: "X1" },
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
const pcedisonFromUsername = normalizeAuthUser({ username: " PCEDISON@GMAIL.COM ", displayName: "", isSuperUser: false });
const pcedisonMissingFlag = normalizeAuthUser({ username: "pcedison@gmail.com" });
const normalWithFlag = normalizeAuthUser({ username: "normal@example.com", isSuperUser: true });
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
console.log(JSON.stringify({ output, unknown, incompleteCompanies, incompleteParsed, incompleteHtml, partialHtml, pendingHtml, holdingPartialHtml, compactMarketRow, marketPagination, trackedAddHtml, untrackedAddHtml, holdings, repaired, repairedStorage: fakeStorage.value, normalizedHoldings, apiErrors, authValidation, adminErrors, auth: { superUserUsername: SUPER_USER_USERNAME, normalizedSuperUsername: normalizeAuthUsername(" PCEDISON@GMAIL.COM "), pcedisonFromUsername, pcedisonMissingFlag, normalWithFlag, pcedisonIdentity: isSuperUserIdentity(pcedisonMissingFlag), normalIdentity: isSuperUserIdentity(normalWithFlag), superState, normalState, normalSettingsPermission, anonymousSettingsPermission }, marketGroups, sortedEntryByPer, extractedPer, filingAwareGroups, evidenceValue, evidenceHtml, evidenceRuleHtml, orderedCodes, orderedHtml, exitCodes, exitSignal, exitSignalHtml, exitAlerts, exitAlertBanner, strategyCardsHtml, strategyDetailLabels: STRATEGY_STATUS_DETAILS.map((item) => item.label), entryDetail: findStrategyStatusDetail("entry"), addWatchDetail: findStrategyStatusDetail("addWatch"), tSeriesDetail: findStrategyStatusDetail("grossMargin"), hasInsufficient: hasInsufficientData(marketGroups.announced.watch[0]), hasFinancialForContext: hasFinancialReportForContext(filingAwareGroups.announced.watch[0], q1Context), hasPublished: hasPublishedScanData(marketGroups.announced.watch[0]), isPartialPublished: isPartialPublishedResult(partialPublishedResult) }));
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
    assert payload["auth"]["superUserUsername"] == "pcedison@gmail.com"
    assert payload["auth"]["normalizedSuperUsername"] == "pcedison@gmail.com"
    assert payload["auth"]["pcedisonFromUsername"]["isSuperUser"] is True
    assert payload["auth"]["pcedisonMissingFlag"]["isSuperUser"] is True
    assert payload["auth"]["normalWithFlag"]["isSuperUser"] is False
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
    assert "3008" in payload["exitAlertBanner"]
    assert "data-open-holding-alert-details" in payload["exitAlertBanner"]
    assert "出場 X1、X4" in payload["exitSignalHtml"]
    assert payload["strategyDetailLabels"] == ["進場 E1-E6", "加碼 A1-A7", "出場 X1-X5", "T 系列追蹤", "春節輔助營收", "金融業不套主策略", "待補資料不硬判斷"]
    assert "strategy-rule-card" in payload["strategyCardsHtml"]
    assert "營收 YoY &gt;= 50%" in payload["strategyCardsHtml"]
    assert "PER &lt; 20" in payload["strategyCardsHtml"]
    assert "存貨週轉率 &gt; 2.5" in payload["strategyCardsHtml"]
    assert [item[0] for item in payload["entryDetail"]["items"]] == ["E1", "E2", "E3", "E4", "E5", "E6"]
    assert [item[0] for item in payload["addWatchDetail"]["items"]] == ["A1", "A2", "A3", "A4", "A5", "A6", "A7"]
    assert [item[0] for item in payload["tSeriesDetail"]["items"]][:2] == ["T1/T2", "T3"]
    assert payload["hasInsufficient"] is True
    assert payload["hasFinancialForContext"] is True
    assert payload["hasPublished"] is True
    assert payload["isPartialPublished"] is True


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not available")
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
