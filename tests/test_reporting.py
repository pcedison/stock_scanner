"""Branch coverage for markdown/CSV scan-report rendering."""

from backend.services.reporting import render_csv_report, render_markdown_report


def test_markdown_report_handles_empty_payload():
    out = render_markdown_report({}, "測試報告")
    assert "沒有可輸出的掃描結果。" in out


def test_markdown_report_renders_entries_and_missing_section():
    payload = {
        "generatedAt": "2026-06-01",
        "dataSource": "official",
        "entry": [
            {
                "stockCode": "2330",
                "companyName": "台積電",
                "status": "ENTRY",
                "summary": "ok",
                "reasons": [
                    {"code": "E1", "passed": True, "severity": "INFO", "title": "t", "message": "m"}
                ],
            }
        ],
        "missing": [{"stockCode": "9999", "name": "缺資料", "reason": "no data"}],
    }
    out = render_markdown_report(payload, "測試報告")
    assert "## 2330 台積電" in out
    assert "## 缺少資料" in out
    assert "9999 缺資料: no data" in out


def test_csv_report_includes_result_rows_and_missing():
    payload = {
        "entry": [
            {
                "stockCode": "2330",
                "companyName": "台積電",
                "status": "ENTRY",
                "summary": "ok",
                "reasons": [{"code": "E1", "passed": True, "severity": "INFO"}],
            }
        ],
        "missing": [{"stockCode": "9999", "name": "缺資料", "reason": "no data"}],
    }
    out = render_csv_report(payload)
    assert "entry,2330,台積電,ENTRY,ok" in out
    assert "missing,9999,缺資料,MISSING,no data" in out
