from __future__ import annotations

import csv
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.adapters.fundamentals_history import OfficialFundamentalsHistoryStore
from backend.services.official_data_provider import OfficialDataProvider


PROGRESS_PATH = ROOT / "data" / "official_history_backfill_progress.json"
MARKDOWN_PATH = ROOT / "docs" / "official_history_failed_companies_2026Q1.md"
CSV_PATH = ROOT / "data" / "official_history_failed_companies_2026Q1.csv"

REQUIRED_ANNUAL_YEARS = [2021, 2022, 2023, 2024, 2025]
REQUIRED_QUARTERS = ["2025Q1", "2026Q1"]
REQUIRED_WINDOW = "2021-2025 年度年報 + 2025Q1/2026Q1 季報比對"


def _has_usable_quarter(history: OfficialFundamentalsHistoryStore, code: str, period: str) -> bool:
    row = history.quarter(code, period)
    return bool(row and (row.netIncome is not None or row.eps is not None or row.revenue is not None))


def _classify(missing_annual_years: list[int], missing_quarters: list[str]) -> str:
    if missing_quarters == ["2026Q1"] and not missing_annual_years:
        return "當期尚未公告；應列 pending，公告後重試"
    if missing_annual_years and max(missing_annual_years) <= 2023 and not any(
        year >= 2024 for year in missing_annual_years
    ):
        return "初步推論：早期年度歷史不足，可能為太新、轉板或代號沿革需查證"
    if missing_annual_years and min(missing_annual_years) >= 2024:
        return "初步推論：近年年度官方無列，可能為新掛牌、特殊申報或資料格式需查證"
    if "2025Q1" in missing_quarters and not missing_annual_years:
        return "初步推論：缺去年同期季報，比較基準不足，可能為新掛牌或轉板需查證"
    if missing_annual_years or missing_quarters:
        return "初步推論：歷史期別不完整，可能為新掛牌、轉板、代號沿革或官方無列，需逐檔查證"
    return "進度檔曾回報無列，但目前快取已有必要資料；需重新跑進度清理"


def build_rows() -> tuple[list[dict[str, object]], int]:
    progress = json.loads(PROGRESS_PATH.read_text(encoding="utf-8"))
    failed = progress.get("failedCompanies", {})
    pending = progress.get("pendingCompanies", {})
    provider = OfficialDataProvider()
    companies = {company.stockCode: company for company in provider.list_companies()}
    history = OfficialFundamentalsHistoryStore()

    rows: list[dict[str, object]] = []
    for code in sorted(failed):
        company = companies.get(code)
        errors = failed.get(code, [])
        failed_periods = [
            str(error.get("period")) for error in errors if isinstance(error, dict) and error.get("period")
        ]
        missing_annual_years = [
            year for year in REQUIRED_ANNUAL_YEARS if not _has_usable_quarter(history, code, f"{year}Q4")
        ]
        missing_quarters = [
            period for period in REQUIRED_QUARTERS if not _has_usable_quarter(history, code, period)
        ]
        rows.append(
            {
                "stock_code": code,
                "company_name": company.name if company else "UNKNOWN",
                "market": company.market if company else "",
                "industry": company.industryName if company else "",
                "required_window": REQUIRED_WINDOW,
                "missing_annual_years": "、".join(map(str, missing_annual_years)) or "-",
                "missing_annual_count": len(missing_annual_years),
                "missing_quarters": "、".join(missing_quarters) or "-",
                "failed_periods": "、".join(failed_periods) or "-",
                "initial_reason": _classify(missing_annual_years, missing_quarters),
                "subagent_todo": (
                    "查官方 MOPS、公司重大訊息、上市櫃/轉板日期與代號沿革，確認缺資料原因；"
                    "若合法來源可補，回填官方歷史快取或 fundamentals_import.csv。"
                ),
            }
        )
    return rows, len(pending)


def write_csv(rows: list[dict[str, object]]) -> None:
    CSV_PATH.parent.mkdir(parents=True, exist_ok=True)
    with CSV_PATH.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def write_markdown(rows: list[dict[str, object]], pending_count: int) -> None:
    reason_counts = Counter(str(row["initial_reason"]) for row in rows)
    MARKDOWN_PATH.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# 官方歷史回補未補齊公司清單（2026Q1）",
        "",
        "產生日期：2026-05-14",
        "",
        "本清單來自 `data/official_history_backfill_progress.json` 的 `failedCompanies`，用於後續指定 subagent 或人工逐檔確認。",
        "",
        "本輪策略資料需求：2021-2025 五個完整年度年報（Q4）作為近 5 年獲利檢查，另需 2026Q1 與 2025Q1 作為當期季報 YoY 比對。",
        "",
        "注意：下表的「初步原因」是依本地官方歷史快取缺口推論，尚未逐檔查上市日期、轉板日期、合併分割或更名沿革；後續 subagent 應以公開資訊觀測站、證交所、櫃買中心與公司公告逐檔確認。",
        "",
        "## 摘要",
        "",
        f"- failed 公司數：{len(rows)}",
        f"- pending 公司數：{pending_count}（多數為 2026Q1 尚未公告，不列入本清單）",
        "- 歷史快取覆蓋：1963 家、16916 筆",
        "",
        "## 初步原因統計",
        "",
    ]
    for reason, count in reason_counts.most_common():
        lines.append(f"- {reason}：{count} 家")
    lines.extend(
        [
            "",
            "## 待查公司清單",
            "",
            "| 股票代號 | 公司名稱 | 市場 | 產業 | 缺年度 | 缺年度數 | 缺季度 | 回補失敗期別 | 初步原因 |",
            "|---|---|---|---|---|---:|---|---|---|",
        ]
    )
    for row in rows:
        values = [
            row["stock_code"],
            row["company_name"],
            row["market"],
            row["industry"],
            row["missing_annual_years"],
            row["missing_annual_count"],
            row["missing_quarters"],
            row["failed_periods"],
            row["initial_reason"],
        ]
        escaped = [str(value).replace("|", "/") for value in values]
        lines.append("| " + " | ".join(escaped) + " |")
    lines.extend(
        [
            "",
            "## 後續 subagent 工作包",
            "",
            "1. 逐檔打開公開資訊觀測站與 TWSE/TPEx 公司資料，確認上市、上櫃、興櫃轉板、合併分割、更名或代號沿革。",
            "2. 若公司上市櫃未滿 5 年，標示為「歷史不足」，不要把 E1/E2 判定為失敗。",
            "3. 若公司其實有官方年報或季報，但本 adapter 未抓到，補 adapter 規則並重跑該公司。",
            "4. 若只能透過合法授權資料取得，列入 `data/fundamentals_import.csv` 人工補洞清單。",
        ]
    )
    MARKDOWN_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    rows, pending_count = build_rows()
    write_csv(rows)
    write_markdown(rows, pending_count)
    print(
        {
            "markdown": str(MARKDOWN_PATH),
            "csv": str(CSV_PATH),
            "rows": len(rows),
            "pending": pending_count,
        }
    )


if __name__ == "__main__":
    main()
