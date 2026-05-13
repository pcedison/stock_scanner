import json
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not available")
def test_parse_stock_input_cases_do_not_return_undefined():
    script = r"""
const { DEFAULT_COMPANIES, parseStockInput, normalizeCompanies, renderAnalysisCard } = require("./frontend/app.js");
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
console.log(JSON.stringify({ output, unknown, incompleteCompanies, incompleteParsed, incompleteHtml }));
"""
    completed = subprocess.run(["node", "-e", script], cwd=ROOT, check=True, text=True, capture_output=True)
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
