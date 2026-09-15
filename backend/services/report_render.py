"""Market report renderers in the Cloudflare Worker's export format.

The seed build renders ``reports/market_scan.csv`` and ``reports/market_scan.md`` once from
the compacted market scan so the Worker can stream them instead of JSON-decoding the
multi-megabyte scan summary at request time (the pattern that exhausted the Python Worker
isolate on 2026-09-08).

The Worker cannot import this module; ``cloudflare/worker_support.report_response`` keeps an
equivalent copy for the holdings report and ``tests/test_cloudflare_worker.py`` pins the two
outputs together byte for byte. Note that the local FastAPI report
(``backend/services/reporting.py``) uses a different, richer layout; this module is the
production export format.
"""

from __future__ import annotations

import csv
import io
from datetime import UTC, datetime

# `or []` tolerates an explicit null category; the Worker copy assumes lists, and
# compact_market_scan_payload never emits null, so the rendered bytes are identical.
CSV_HEADER = ["category", "stockCode", "companyName", "status", "summary"]
CATEGORIES = ("entry", "watch", "excluded", "results")
LABELS = (("entry", "適合進場"), ("watch", "接近觀察"), ("excluded", "排除清單"), ("results", "持股"))


def render_market_report_csv(payload: dict) -> str:
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(CSV_HEADER)
    for category in CATEGORIES:
        for item in payload.get(category, []) or []:
            writer.writerow(
                [category, item.get("stockCode"), item.get("companyName"), item.get("status"), item.get("summary")]
            )
    return output.getvalue()


def render_market_report_markdown(payload: dict, title: str) -> str:
    generated = payload.get("generatedAt", datetime.now(UTC).isoformat())
    lines = [
        f"# {title}",
        "",
        f"- 產生時間：{generated}",
        f"- 資料來源：{payload.get('dataSource', 'cloudflare_r2_seed')}",
        "",
    ]
    for category, label in LABELS:
        items = payload.get(category, []) or []
        if not items:
            continue
        lines.append(f"## {label} ({len(items)})")
        for item in items:
            lines.append(f"- {item.get('stockCode')} {item.get('companyName')}：{item.get('summary')}")
        lines.append("")
    return "\n".join(lines)
