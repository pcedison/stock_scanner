from __future__ import annotations

import csv
import json
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.adapters.fundamentals_history import OfficialFundamentalsHistoryStore
from backend.services.official_data_provider import OfficialDataProvider

PROGRESS_PATH = ROOT / "data" / "official_history_backfill_progress.json"


def _current_quarter(now: datetime | None = None) -> tuple[int, int]:
    d = now or datetime.now()
    return d.year, (d.month - 1) // 3 + 1


def _quarter_label(year: int, q: int) -> str:
    return f"{year}Q{q}"


def _prev_quarter(year: int, q: int) -> tuple[int, int]:
    return (year - 1, 4) if q == 1 else (year, q - 1)


def _report_paths(label: str) -> tuple[Path, Path]:
    return (
        ROOT / "docs" / f"official_history_failed_companies_{label}.md",
        ROOT / "data" / f"official_history_failed_companies_{label}.csv",
    )


def _build_context(now: datetime | None = None) -> dict:
    year, q = _current_quarter(now)
    prev_year, prev_q = _prev_quarter(year, q)
    current_label = _quarter_label(year, q)
    prev_label = _quarter_label(prev_year, prev_q)
    annual_start = year - 5
    annual_years = list(range(annual_start, year))
    annual_range = f"{annual_start}-{year - 1}"
    return {
        "label": current_label,
        "required_annual_years": annual_years,
        "required_quarters": [prev_label, current_label],
        "required_window": f"{annual_range} 年度年報 + {prev_label}/{current_label} 季報比對",
        "markdown_path": _report_paths(current_label)[0],
        "csv_path": _report_paths(current_label)[1],
    }


def _has_usable_quarter(history: OfficialFundamentalsHistoryStore, code: str, period: str) -> bool:
    row = history.quarter(code, period)
    return bool(row and (row.netIncome is not None or row.eps is not None or row.revenue is not None))


def _classify(missing_annual_years: list[int], missing_quarters: list[str], current_quarter: str) -> str:
    if missing_quarters == [current_quarter] and not missing_annual_years:
        return "當期尚未公告；應列 pending，公告後重試"
    cutoff_recent = datetime.now().year - 2
    if missing_annual_years and max(missing_annual_years) <= cutoff_recent and not any(
        year > cutoff_recent for year in missing_annual_years
    ):
        return "初步推論：早期年度歷史不足，可能為太新、轉板或代號沿革需查證"
    if missing_annual_years and min(missing_annual_years) >= cutoff_recent + 1:
        return "初步推論：近年年度官方無列，可能為新掛牌、特殊申報或資料格式需查證"
    if len(missing_quarters) == 1 and missing_quarters[0] != current_quarter and not missing_annual_years:
        return "初步推論：缺去年同期季報，比較基準不足，可能為新掛牌或轉板需查證"
    if missing_annual_years or missing_quarters:
        return "初步推論：歷史期別不完整，可能為新掛牌、轉板、代號沿革或官方無列，需逐檔查證"
    return "進度檔曾回報無列，但目前快取已有必要資料；需重新跑進度清理"


def build_rows(ctx: dict | None = None) -> tuple[list[dict[str, object]], int]:
    if ctx is None:
        ctx = _build_context()
    progress = json.loads(PROGRESS_PATH.read_text(encoding="utf-8"))
    failed = progress.get("failedCompanies", {})
    pending = progress.get("pendingCompanies", {})
    provider = OfficialDataProvider()
    companies = {company.stockCode: company for company in provider.list_companies()}
    history = OfficialFundamentalsHistoryStore()
    required_annual_years: list[int] = ctx["required_annual_years"]
    required_quarters: list[str] = ctx["required_quarters"]
    current_quarter: str = ctx["label"]

    rows: list[dict[str, object]] = []
    for code in sorted(failed):
        company = companies.get(code)
        errors = failed.get(code, [])
        failed_periods = [
            str(error.get("period")) for error in errors if isinstance(error, dict) and error.get("period")
        ]
        missing_annual_years = [
            year for year in required_annual_years if not _has_usable_quarter(history, code, f"{year}Q4")
        ]
        missing_quarters = [
            period for period in required_quarters if not _has_usable_quarter(history, code, period)
        ]
        rows.append(
            {
                "stock_code": code,
                "company_name": company.name if company else "UNKNOWN",
                "market": company.market if company else "",
                "industry": company.industryName if company else "",
                "required_window": ctx["required_window"],
                "missing_annual_years": "、".join(map(str, missing_annual_years)) or "-",
                "missing_annual_count": len(missing_annual_years),
                "missing_quarters": "、".join(missing_quarters) or "-",
                "failed_periods": "、".join(failed_periods) or "-",
                "initial_reason": _classify(missing_annual_years, missing_quarters, current_quarter),
                "subagent_todo": (
                    "查官方 MOPS、公司重大訊息、上市櫃/轉板日期與代號沿革，確認缺資料原因；"
                    "若合法來源可補，回填官方歷史快取或 fundamentals_import.csv。"
                ),
            }
        )
    return rows, len(pending)


def _unavailable_count() -> int:
    try:
        progress = json.loads(PROGRESS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return 0
    return len(progress.get("unavailableCompanies", {}) or {})


def write_csv(rows: list[dict[str, object]], csv_path: Path) -> None:
    if not rows:
        return
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def write_markdown(rows: list[dict[str, object]], pending_count: int, markdown_path: Path, label: str) -> None:
    reason_counts = Counter(str(row["initial_reason"]) for row in rows)
    markdown_path.parent.mkdir(parents=True, exist_ok=True)
    generated_date = datetime.now().strftime("%Y-%m-%d")
    lines = [
        f"# 官方歷史回補未補齊公司清單（{label}）",
        "",
        f"產生日期：{generated_date}",
        "",
        "本清單來自 `data/official_history_backfill_progress.json` 的 `failedCompanies`，用於後續指定 subagent 或人工逐檔確認。",
        "",
        f"本輪策略資料需求：{rows[0]['required_window'] if rows else '—'}。",
        "",
        "注意：下表的「初步原因」是依本地官方歷史快取缺口推論，尚未逐檔查上市日期、轉板日期、合併分割或更名沿革；後續 subagent 應以公開資訊觀測站、證交所、櫃買中心與公司公告逐檔確認。",
        "",
        "## 摘要",
        "",
        f"- failed 公司數：{len(rows)}（請求失敗，排程先重試 3 次，之後每週一次）",
        f"- pending 公司數：{pending_count}（多數為當期尚未公告，不列入本清單）",
        f"- 官方無此期別資料公司數：{_unavailable_count()}（MOPS 明確回覆無資料，例如晚於該年度才上市或成立，不再重試，不列入本清單）",
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
    markdown_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    ctx = _build_context()
    rows, pending_count = build_rows(ctx)
    write_csv(rows, ctx["csv_path"])
    write_markdown(rows, pending_count, ctx["markdown_path"], ctx["label"])
    print(
        {
            "markdown": str(ctx["markdown_path"]),
            "csv": str(ctx["csv_path"]),
            "label": ctx["label"],
            "rows": len(rows),
            "pending": pending_count,
        }
    )


if __name__ == "__main__":
    main()
