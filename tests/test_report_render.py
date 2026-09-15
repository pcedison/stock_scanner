from backend.services import report_render


def _payload():
    return {
        "generatedAt": "2026-09-15T10:00:00+00:00",
        "dataSource": "OfficialDataProvider",
        "entry": [{"stockCode": "2330", "companyName": "台積電", "status": "ENTRY", "summary": "E4 PER 低"}],
        "watch": [{"stockCode": "2317", "companyName": "鴻海", "status": "WATCH", "summary": "a, b"}],
        "excluded": [],
    }


def test_csv_has_the_fixed_header_and_one_row_per_item_with_quoting():
    text = report_render.render_market_report_csv(_payload())
    lines = text.splitlines()

    assert lines[0] == "category,stockCode,companyName,status,summary"
    assert lines[1] == "entry,2330,台積電,ENTRY,E4 PER 低"
    assert lines[2] == 'watch,2317,鴻海,WATCH,"a, b"'
    assert len(lines) == 3
    # csv.writer's default dialect, the same one the Worker uses.
    assert text.endswith("\r\n")


def test_markdown_lists_only_non_empty_categories_with_counts():
    text = report_render.render_market_report_markdown(_payload(), "台股市場掃描報告")

    assert text.startswith("# 台股市場掃描報告\n\n- 產生時間：2026-09-15T10:00:00+00:00\n- 資料來源：OfficialDataProvider\n")
    assert "## 適合進場 (1)" in text
    assert "## 接近觀察 (1)" in text
    assert "排除清單" not in text
    assert "- 2330 台積電：E4 PER 低" in text


def test_markdown_defaults_data_source_when_missing():
    payload = _payload()
    payload.pop("dataSource")

    assert "- 資料來源：cloudflare_r2_seed" in report_render.render_market_report_markdown(payload, "t")
