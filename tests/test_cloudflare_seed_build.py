from scripts.build_cloudflare_seed import compact_market_scan_payload


def test_compact_market_scan_payload_strips_heavy_evidence():
    payload = {
        "generatedAt": "2026-05-19T00:00:00+00:00",
        "entry": [
            {
                "stockCode": "1234",
                "companyName": "Demo",
                "status": "ENTRY",
                "summary": "summary",
                "largeField": "drop me",
                "reasons": [
                    {
                        "code": "E1",
                        "title": "Rule",
                        "passed": True,
                        "severity": "INFO",
                        "message": "ok",
                        "evidence": [{"label": "heavy"}],
                    },
                    {
                        "code": "E4",
                        "title": "PER",
                        "passed": True,
                        "severity": "INFO",
                        "message": "PER 為 12.3。",
                        "evidence": [{"label": "heavy"}],
                    }
                ],
            }
        ],
        "watch": [],
        "excluded": [],
    }

    compact = compact_market_scan_payload(payload)

    assert compact["detailMode"] == "summary"
    assert compact["entry"][0]["stockCode"] == "1234"
    assert compact["entry"][0]["detailsAvailable"] is True
    assert "largeField" not in compact["entry"][0]
    assert [reason["code"] for reason in compact["entry"][0]["reasons"]] == ["E4"]
    assert "evidence" not in compact["entry"][0]["reasons"][0]
