from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from backend.adapters.official_monthly_revenue import OfficialMonthlyRevenueRow


DEFAULT_HISTORY_PATH = Path(__file__).resolve().parents[2] / "data" / "monthly_revenue_history.json"
MAX_MONTHS_PER_COMPANY = 14


def _month_key(month: str) -> tuple[int, int]:
    try:
        year, m = month.split("-", 1)
        return int(year), int(m)
    except (ValueError, AttributeError):
        return (0, 0)


def _prev_month(month: str) -> str:
    try:
        year, m = month.split("-", 1)
        y, mo = int(year), int(m)
        return f"{y - 1}-12" if mo == 1 else f"{y}-{mo - 1:02d}"
    except (ValueError, AttributeError):
        return ""


class MonthlyRevenueHistoryStore:
    """Persist monthly revenue YoY metrics for up to MAX_MONTHS_PER_COMPANY months per stock.

    This store enables computation of previousMonthRevenueYoY, trailingThreeMonthAverageYoY,
    and janFebCombinedRevenueYoY — fields the official APIs do not directly provide.
    The seed build workflow extracts this file from the committed seed zip before running,
    and packages it back into the new zip afterwards, so history accumulates across builds.
    """

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path is not None else DEFAULT_HISTORY_PATH
        self._cache: dict[str, Any] | None = None
        self._cache_mtime: float | None = None

    def load(self) -> dict[str, Any]:
        if not self.path.exists():
            payload: dict[str, Any] = {"schemaVersion": 1, "updatedAt": None, "months": {}}
            self._cache = payload
            self._cache_mtime = None
            return payload
        mtime = self.path.stat().st_mtime
        if self._cache is not None and self._cache_mtime == mtime:
            return self._cache
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except (OSError, json.JSONDecodeError):
            return {"schemaVersion": 1, "updatedAt": None, "months": {}}
        if not isinstance(payload, dict):
            return {"schemaVersion": 1, "updatedAt": None, "months": {}}
        payload.setdefault("schemaVersion", 1)
        payload.setdefault("updatedAt", None)
        payload.setdefault("months", {})
        if not isinstance(payload["months"], dict):
            payload["months"] = {}
        self._cache = payload
        self._cache_mtime = mtime
        return payload

    def merge_rows(self, rows: list[OfficialMonthlyRevenueRow]) -> dict[str, Any]:
        payload = self.load()
        months_data: dict[str, dict[str, Any]] = payload["months"]
        changed = 0

        for row in rows:
            if not row.stockCode or not row.dataMonth:
                continue
            company_months = months_data.setdefault(row.stockCode, {})
            record: dict[str, Any] = {
                "monthlyRevenue": row.monthlyRevenue,
                "monthlyRevenueYoY": row.monthlyRevenueYoY,
                "cumulativeRevenue": row.cumulativeRevenue,
                "cumulativeRevenueYoY": row.cumulativeRevenueYoY,
            }
            if company_months.get(row.dataMonth) != record:
                company_months[row.dataMonth] = record
                changed += 1

        for stock_code in list(months_data.keys()):
            company = months_data[stock_code]
            if not isinstance(company, dict):
                del months_data[stock_code]
                continue
            sorted_months = sorted(company.keys(), key=_month_key, reverse=True)
            for old_month in sorted_months[MAX_MONTHS_PER_COMPANY:]:
                del company[old_month]
            if not company:
                del months_data[stock_code]

        if changed:
            payload["updatedAt"] = datetime.now(timezone.utc).isoformat()
            self._save(payload)

        return {
            "enabled": True,
            "path": str(self.path),
            "companies": len(months_data),
            "rows": sum(len(m) for m in months_data.values() if isinstance(m, dict)),
            "updatedRows": changed,
        }

    def previous_month_yoy(self, stock_code: str, current_month: str) -> float | None:
        prev = _prev_month(current_month)
        if not prev:
            return None
        record = self.load()["months"].get(stock_code, {}).get(prev)
        return record.get("monthlyRevenueYoY") if isinstance(record, dict) else None

    def trailing_three_month_avg_yoy(self, stock_code: str, current_month: str) -> float | None:
        months_data = self.load()["months"].get(stock_code)
        if not isinstance(months_data, dict):
            return None
        months_to_check = [current_month]
        m = current_month
        for _ in range(2):
            m = _prev_month(m)
            if m:
                months_to_check.append(m)
        yoys = [
            float(record["monthlyRevenueYoY"])
            for month in months_to_check
            if isinstance(record := months_data.get(month), dict) and record.get("monthlyRevenueYoY") is not None
        ]
        return sum(yoys) / len(yoys) if len(yoys) >= 2 else None

    def jan_feb_combined_yoy(self, stock_code: str, current_year: int) -> float | None:
        months_data = self.load()["months"].get(stock_code)
        if not isinstance(months_data, dict):
            return None

        def _rev(month: str) -> float | None:
            record = months_data.get(month)
            return float(record["monthlyRevenue"]) if isinstance(record, dict) and record.get("monthlyRevenue") is not None else None

        jan_this, feb_this = _rev(f"{current_year}-01"), _rev(f"{current_year}-02")
        jan_prev, feb_prev = _rev(f"{current_year - 1}-01"), _rev(f"{current_year - 1}-02")
        if any(v is None for v in (jan_this, feb_this, jan_prev, feb_prev)):
            return None
        total_prev = jan_prev + feb_prev  # type: ignore[operator]
        return None if total_prev == 0 else ((jan_this + feb_this - total_prev) / abs(total_prev)) * 100  # type: ignore[operator]

    def status(self) -> dict[str, Any]:
        payload = self.load()
        months_data = payload.get("months", {})
        return {
            "enabled": True,
            "path": str(self.path),
            "companies": len(months_data) if isinstance(months_data, dict) else 0,
            "rows": sum(len(m) for m in months_data.values() if isinstance(m, dict)) if isinstance(months_data, dict) else 0,
            "updatedAt": payload.get("updatedAt"),
        }

    def _save(self, payload: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
        self._cache = payload
        self._cache_mtime = self.path.stat().st_mtime
