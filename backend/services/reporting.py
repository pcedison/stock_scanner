from __future__ import annotations

import csv
from io import StringIO
from typing import Any, Iterable


def _value(item: Any, key: str, default: Any = "") -> Any:
    if isinstance(item, dict):
        return item.get(key, default)
    return getattr(item, key, default)


def _rows_from_payload(payload: dict[str, Any]) -> list[tuple[str, Any]]:
    rows: list[tuple[str, Any]] = []
    for group in ["entry", "watch", "excluded"]:
        for item in payload.get(group, []):
            rows.append((group, item))
    for item in payload.get("results", []):
        rows.append(("holding", item))
    return rows


def _rule_summary(rules: Iterable[Any]) -> str:
    parts = []
    for rule in rules or []:
        code = _value(rule, "code")
        passed = "PASS" if _value(rule, "passed") else "FAIL"
        severity = _value(rule, "severity")
        parts.append(f"{code}:{passed}:{severity}")
    return "; ".join(parts)


def render_markdown_report(payload: dict[str, Any], title: str) -> str:
    generated_at = payload.get("generatedAt", "")
    data_source = payload.get("dataSource", "")
    lines = [
        f"# {title}",
        "",
        f"- 產生時間：{generated_at}",
        f"- 資料來源：{data_source or 'holdings'}",
        f"- Universe：{payload.get('universeSize', len(payload.get('results', [])))}",
    ]
    if payload.get("note"):
        lines.append(f"- 說明：{payload['note']}")
    lines.append("")

    rows = _rows_from_payload(payload)
    if not rows:
        lines.append("沒有可輸出的掃描結果。")
        return "\n".join(lines) + "\n"

    for group, item in rows:
        stock_code = _value(item, "stockCode")
        company_name = _value(item, "companyName")
        status = _value(item, "status")
        summary = _value(item, "summary")
        lines.extend([f"## {stock_code} {company_name}", "", f"- 分類：{group}", f"- 狀態：{status}", f"- 摘要：{summary}", ""])
        for rule in _value(item, "reasons", []):
            mark = "通過" if _value(rule, "passed") else "未通過"
            lines.append(
                f"- `{_value(rule, 'code')}` {mark} / {_value(rule, 'severity')}：{_value(rule, 'title')} - {_value(rule, 'message')}"
            )
        lines.append("")

    missing = payload.get("missing", [])
    if missing:
        lines.extend(["## 缺少資料", ""])
        for item in missing:
            lines.append(f"- {item.get('stockCode')} {item.get('name')}: {item.get('reason')}")
        lines.append("")
    return "\n".join(lines)


def render_csv_report(payload: dict[str, Any]) -> str:
    output = StringIO()
    writer = csv.DictWriter(
        output,
        fieldnames=["group", "stockCode", "companyName", "status", "summary", "rules"],
        lineterminator="\n",
    )
    writer.writeheader()
    for group, item in _rows_from_payload(payload):
        writer.writerow(
            {
                "group": group,
                "stockCode": _value(item, "stockCode"),
                "companyName": _value(item, "companyName"),
                "status": _value(item, "status"),
                "summary": _value(item, "summary"),
                "rules": _rule_summary(_value(item, "reasons", [])),
            }
        )
    for item in payload.get("missing", []):
        writer.writerow(
            {
                "group": "missing",
                "stockCode": item.get("stockCode", ""),
                "companyName": item.get("name", ""),
                "status": "MISSING",
                "summary": item.get("reason", ""),
                "rules": "",
            }
        )
    return output.getvalue()
