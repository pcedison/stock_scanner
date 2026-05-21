from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from backend.adapters.official_fundamentals import OfficialBalanceSheetRow, OfficialIncomeStatementRow


DEFAULT_HISTORY_PATH = Path(__file__).resolve().parents[2] / "data" / "official_fundamentals_history.json"


def _period(fiscal_year: int | None, quarter: int | None) -> str | None:
    if fiscal_year is None or quarter is None:
        return None
    if quarter < 1 or quarter > 4:
        return None
    return f"{fiscal_year}Q{quarter}"


def _period_key(period: str) -> tuple[int, int]:
    try:
        year_text, quarter_text = period.upper().split("Q", 1)
        return int(year_text), int(quarter_text)
    except (ValueError, AttributeError):
        return (0, 0)


def _yoy(current: float | None, previous: float | None) -> float | None:
    if current is None or previous in (None, 0):
        return None
    return ((current - previous) / abs(previous)) * 100


def _margin_delta(current: float | None, previous: float | None) -> float | None:
    if current is None or previous is None:
        return None
    return current - previous


def _first_present(*values: Any) -> Any:
    for value in values:
        if value is not None:
            return value
    return None


def _without_seen_at(record: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in record.items() if key != "lastSeenAt"}


@dataclass(frozen=True)
class HistoricalQuarterlyFundamental:
    stockCode: str
    period: str
    fiscalYear: int
    quarter: int
    revenue: float | None = None
    costOfRevenue: float | None = None
    grossProfit: float | None = None
    grossMargin: float | None = None
    operatingIncome: float | None = None
    operatingMargin: float | None = None
    netIncome: float | None = None
    eps: float | None = None
    inventory: float | None = None

    @property
    def epsYoY(self) -> float | None:
        return None


class OfficialFundamentalsHistoryStore:
    """Local durable history built from official TWSE/TPEx OpenAPI snapshots.

    TWSE/TPEx OpenAPI publishes the current financial-statement snapshot. This
    store makes that current feed useful as a historical pipeline by persisting
    each announced period locally, then deriving YoY and annual Q4 metrics from
    the accumulated official records.
    """

    def __init__(self, path: str | Path | None = None, max_years: int = 8) -> None:
        self.path = Path(path) if path is not None else DEFAULT_HISTORY_PATH
        self.max_years = max_years
        self._cache: dict[str, Any] | None = None
        self._cache_mtime: float | None = None

    def load(self) -> dict[str, Any]:
        if not self.path.exists():
            payload = {"schemaVersion": 1, "updatedAt": None, "quarters": {}}
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
            return {"schemaVersion": 1, "updatedAt": None, "quarters": {}}
        if not isinstance(payload, dict):
            return {"schemaVersion": 1, "updatedAt": None, "quarters": {}}
        payload.setdefault("schemaVersion", 1)
        payload.setdefault("updatedAt", None)
        payload.setdefault("quarters", {})
        if not isinstance(payload["quarters"], dict):
            payload["quarters"] = {}
        self._cache = payload
        self._cache_mtime = mtime
        return payload

    def merge_latest(
        self,
        incomes: dict[str, OfficialIncomeStatementRow],
        balances: dict[str, OfficialBalanceSheetRow],
    ) -> dict[str, Any]:
        return self.merge_rows(incomes.values(), balances.values())

    def merge_rows(
        self,
        incomes: Iterable[OfficialIncomeStatementRow],
        balances: Iterable[OfficialBalanceSheetRow],
    ) -> dict[str, Any]:
        payload = self.load()
        quarters: dict[str, dict[str, Any]] = payload["quarters"]
        changed = 0
        touched = 0

        for income in incomes:
            stock_code = income.stockCode
            period = _period(income.fiscalYear, income.quarter)
            if period is None:
                continue
            company_records = quarters.setdefault(stock_code, {})
            existing = company_records.get(period, {})
            existing_base = _without_seen_at(existing) if isinstance(existing, dict) else {}
            record = {
                **existing_base,
                "stockCode": stock_code,
                "companyName": income.companyName,
                "market": income.market,
                "period": period,
                "fiscalYear": income.fiscalYear,
                "quarter": income.quarter,
                "revenue": _first_present(income.revenue, existing_base.get("revenue")),
                "costOfRevenue": _first_present(income.costOfRevenue, existing_base.get("costOfRevenue")),
                "grossProfit": _first_present(income.grossProfit, existing_base.get("grossProfit")),
                "grossMargin": _first_present(income.grossMargin, existing_base.get("grossMargin")),
                "operatingIncome": _first_present(income.operatingIncome, existing_base.get("operatingIncome")),
                "operatingMargin": _first_present(income.operatingMargin, existing_base.get("operatingMargin")),
                "netIncome": _first_present(income.netIncome, existing_base.get("netIncome")),
                "eps": _first_present(income.eps, existing_base.get("eps")),
                "incomeSource": income.source,
            }
            if record != existing_base:
                company_records[period] = {**record, "lastSeenAt": datetime.now(timezone.utc).isoformat()}
                changed += 1
            touched += 1

        for balance in balances:
            stock_code = balance.stockCode
            period = _period(balance.fiscalYear, balance.quarter)
            if period is None:
                continue
            company_records = quarters.setdefault(stock_code, {})
            existing = company_records.get(period, {})
            existing_base = _without_seen_at(existing) if isinstance(existing, dict) else {}
            record = {
                **existing_base,
                "stockCode": stock_code,
                "companyName": _first_present(existing_base.get("companyName"), balance.companyName),
                "market": _first_present(existing_base.get("market"), balance.market),
                "period": period,
                "fiscalYear": balance.fiscalYear,
                "quarter": balance.quarter,
                "inventory": _first_present(balance.inventory, existing_base.get("inventory")),
                "balanceSource": balance.source,
            }
            if record != existing_base:
                company_records[period] = {**record, "lastSeenAt": datetime.now(timezone.utc).isoformat()}
                changed += 1
            touched += 1

        self._prune(quarters)
        if changed:
            payload["updatedAt"] = datetime.now(timezone.utc).isoformat()
            self._save(payload)

        return {
            "enabled": True,
            "path": str(self.path),
            "companies": len(quarters),
            "rows": sum(len(records) for records in quarters.values() if isinstance(records, dict)),
            "touchedRows": touched,
            "updatedRows": changed,
            "maxYears": self.max_years,
        }

    def latest_quarter(self, stock_code: str) -> HistoricalQuarterlyFundamental | None:
        records = self.load()["quarters"].get(stock_code, {})
        if not isinstance(records, dict) or not records:
            return None
        period = max(records, key=_period_key)
        return self.quarter(stock_code, period)

    def quarter(self, stock_code: str, period: str | None) -> HistoricalQuarterlyFundamental | None:
        if period is None:
            return None
        record = self.load()["quarters"].get(stock_code, {}).get(period)
        return self._to_quarter(stock_code, record) if isinstance(record, dict) else None

    def quarterly_yoy(self, stock_code: str, period: str | None) -> dict[str, float | None]:
        current = self.quarter(stock_code, period)
        if current is None:
            return {"epsYoY": None, "netIncomeYoY": None, "grossMarginYoY": None}
        previous = self.quarter(stock_code, f"{current.fiscalYear - 1}Q{current.quarter}")
        if previous is None:
            return {"epsYoY": None, "netIncomeYoY": None, "grossMarginYoY": None}
        return {
            "epsYoY": _yoy(current.eps, previous.eps),
            "netIncomeYoY": _yoy(current.netIncome, previous.netIncome),
            "grossMarginYoY": _margin_delta(current.grossMargin, previous.grossMargin),
        }

    def annual_financials(self, stock_code: str) -> list[dict[str, float | int | None]]:
        records = self.load()["quarters"].get(stock_code, {})
        if not isinstance(records, dict):
            return []
        annuals = []
        for period, record in records.items():
            if not isinstance(record, dict) or _period_key(period)[1] != 4:
                continue
            fiscal_year = record.get("fiscalYear")
            if fiscal_year is None:
                continue
            annuals.append({"year": int(fiscal_year), "netIncome": record.get("netIncome")})
        deduped = {row["year"]: row for row in sorted(annuals, key=lambda item: item["year"])}
        return list(deduped.values())

    def inventory_turnover(self, stock_code: str, period: str | None) -> float | None:
        current = self.quarter(stock_code, period)
        if current is None or current.costOfRevenue is None or current.inventory in (None, 0):
            return None
        annualized_factor = 4 / current.quarter if current.quarter else 1
        return (current.costOfRevenue * annualized_factor) / current.inventory

    def status(self) -> dict[str, Any]:
        payload = self.load()
        quarters = payload.get("quarters", {})
        if not isinstance(quarters, dict):
            quarters = {}
        return {
            "enabled": True,
            "path": str(self.path),
            "companies": len(quarters),
            "rows": sum(len(records) for records in quarters.values() if isinstance(records, dict)),
            "updatedAt": payload.get("updatedAt"),
            "maxYears": self.max_years,
        }

    def _save(self, payload: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        tmp.replace(self.path)
        self._cache = payload
        self._cache_mtime = self.path.stat().st_mtime

    def _prune(self, quarters: dict[str, dict[str, Any]]) -> None:
        for stock_code, records in list(quarters.items()):
            if not isinstance(records, dict):
                quarters.pop(stock_code, None)
                continue
            periods = sorted(records, key=_period_key)
            if not periods:
                quarters.pop(stock_code, None)
                continue
            latest_year = _period_key(periods[-1])[0]
            min_year = latest_year - self.max_years + 1
            for period in periods:
                year, _ = _period_key(period)
                if year and year < min_year:
                    records.pop(period, None)

    def _to_quarter(self, stock_code: str, record: dict[str, Any]) -> HistoricalQuarterlyFundamental | None:
        fiscal_year = record.get("fiscalYear")
        quarter = record.get("quarter")
        period = _period(fiscal_year, quarter)
        if period is None:
            return None
        return HistoricalQuarterlyFundamental(
            stockCode=stock_code,
            period=period,
            fiscalYear=int(fiscal_year),
            quarter=int(quarter),
            revenue=record.get("revenue"),
            costOfRevenue=record.get("costOfRevenue"),
            grossProfit=record.get("grossProfit"),
            grossMargin=record.get("grossMargin"),
            operatingIncome=record.get("operatingIncome"),
            operatingMargin=record.get("operatingMargin"),
            netIncome=record.get("netIncome"),
            eps=record.get("eps"),
            inventory=record.get("inventory"),
        )
