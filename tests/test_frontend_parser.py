import json
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not available")
def test_parse_stock_input_cases_do_not_return_undefined():
    script = r"""
const { DEFAULT_COMPANIES, STRATEGY_STATUS_DETAILS, findStrategyStatusDetail, parseStockInput, normalizeCompanies, renderAnalysisCard, renderMarketResultRow, renderMarketPagination, loadHoldingsFromStorage, normalizeHoldingRecords, groupMarketScanResults, hasInsufficientData, hasFinancialReportForContext, hasPublishedScanData, isPartialPublishedResult } = require("./frontend/app.js");
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
console.log(JSON.stringify({ output, unknown, incompleteCompanies, incompleteParsed, incompleteHtml, partialHtml, pendingHtml, holdingPartialHtml, compactMarketRow, marketPagination, holdings, repaired, repairedStorage: fakeStorage.value, normalizedHoldings, marketGroups, filingAwareGroups, strategyDetailLabels: STRATEGY_STATUS_DETAILS.map((item) => item.label), entryDetail: findStrategyStatusDetail("entry"), hasInsufficient: hasInsufficientData(marketGroups.announced.watch[0]), hasFinancialForContext: hasFinancialReportForContext(filingAwareGroups.announced.watch[0], q1Context), hasPublished: hasPublishedScanData(marketGroups.announced.watch[0]), isPartialPublished: isPartialPublishedResult(partialPublishedResult) }));
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
    assert [item["stockCode"] for item in payload["holdings"]] == ["2330", "2357"]
    assert payload["holdings"][0]["shares"] == 1000
    assert payload["holdings"][1]["averageCost"] is None
    assert payload["repaired"] == []
    assert payload["repairedStorage"] == "[]"
    assert payload["normalizedHoldings"][0]["shares"] == 2
    assert payload["marketGroups"]["announced"]["entry"][0]["stockCode"] == "2357"
    assert payload["marketGroups"]["announced"]["watch"][0]["stockCode"] == "1101"
    assert payload["marketGroups"]["announced"]["excluded"][0]["stockCode"] == "2881"
    assert payload["marketGroups"]["pending"]["watch"][0]["stockCode"] == "9999"
    assert payload["filingAwareGroups"]["announced"]["watch"][0]["stockCode"] == "1101"
    assert [item["stockCode"] for item in payload["filingAwareGroups"]["pending"]["watch"]] == ["1102", "1103"]
    assert payload["strategyDetailLabels"] == ["進場 E1-E6", "出場 X1-X5", "毛利率追蹤", "春節輔助營收", "金融業不套主策略", "待補資料不硬判斷"]
    assert [item[0] for item in payload["entryDetail"]["items"]] == ["E1", "E2", "E3", "E4", "E5", "E6"]
    assert payload["hasInsufficient"] is True
    assert payload["hasFinancialForContext"] is True
    assert payload["hasPublished"] is True
    assert payload["isPartialPublished"] is True
