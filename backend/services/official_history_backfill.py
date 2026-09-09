from __future__ import annotations

import argparse
import json
import time
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from backend.adapters.fundamentals_history import OfficialFundamentalsHistoryStore
from backend.adapters.mops_historical_fundamentals import OfficialMopsHistoricalFundamentalsAdapter
from backend.models.company import Company
from backend.services.filing_calendar import filing_context

ROOT_DIR = Path(__file__).resolve().parents[2]
DEFAULT_PROGRESS_PATH = ROOT_DIR / "data" / "official_history_backfill_progress.json"


def _latest_annual_year(fiscal_year: int, quarter: int) -> int:
    return fiscal_year if quarter == 4 else fiscal_year - 1


def _dedupe_periods(periods: Iterable[tuple[int, int]]) -> list[tuple[int, int]]:
    seen: set[tuple[int, int]] = set()
    output: list[tuple[int, int]] = []
    for year, quarter in periods:
        period = (int(year), int(quarter))
        if period in seen:
            continue
        seen.add(period)
        output.append(period)
    return output


def _strategy_backfill_periods(fiscal_year: int, quarter: int, years: int = 5) -> list[tuple[int, int]]:
    """Minimal official periods needed by the current strategy.

    MOPS statement pages include the requested period and a comparable prior
    period, so requesting Q4 every other year can fill five annual Q4 rows with
    fewer official requests. Same-quarter periods provide EPS/net-income/gross
    margin YoY for the active filing window.
    """

    latest_annual = _latest_annual_year(fiscal_year, quarter)
    annual_requests = [(year, 4) for year in range(latest_annual, latest_annual - years, -2)]
    same_quarter_requests = [(fiscal_year, quarter)]
    if quarter != 4:
        same_quarter_requests.append((fiscal_year - 1, quarter))
    return _dedupe_periods([*same_quarter_requests, *annual_requests])


def _full_quarterly_periods(fiscal_year: int, quarter: int, years: int = 5) -> list[tuple[int, int]]:
    latest_annual = _latest_annual_year(fiscal_year, quarter)
    first_year = latest_annual - years + 1
    periods: list[tuple[int, int]] = []
    for year in range(latest_annual, first_year - 1, -1):
        for period_quarter in range(4, 0, -1):
            periods.append((year, period_quarter))
    if fiscal_year > latest_annual:
        for period_quarter in range(quarter, 0, -1):
            periods.insert(0, (fiscal_year, period_quarter))
    return _dedupe_periods(periods)


def _backfill_periods(fiscal_year: int, quarter: int, years: int = 5, mode: str = "strategy") -> list[tuple[int, int]]:
    if mode == "full_quarterly":
        return _full_quarterly_periods(fiscal_year, quarter, years=years)
    return _strategy_backfill_periods(fiscal_year, quarter, years=years)


def _period_label(year: int, quarter: int) -> str:
    return f"{year}Q{quarter}"


@dataclass
class _CompanyFetch:
    incomes: list = field(default_factory=list)
    balances: list = field(default_factory=list)
    errors: list[dict[str, Any]] = field(default_factory=list)
    pending: list[dict[str, Any]] = field(default_factory=list)


class BackfillProgressStore:
    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path is not None else DEFAULT_PROGRESS_PATH

    def load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def save(self, payload: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload["updatedAt"] = datetime.now(UTC).isoformat()
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        tmp.replace(self.path)

    def reset(self) -> None:
        if self.path.exists():
            self.path.unlink()


@dataclass(frozen=True)
class OfficialHistoryBackfillResult:
    requestedCompanies: int
    backfilledCompanies: int
    skippedCompanies: int
    failedCompanies: int
    pendingCompanies: int
    incomeRows: int
    balanceRows: int
    historyStatus: dict[str, Any]
    periods: list[str]
    progress: dict[str, Any]
    completed: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "requestedCompanies": self.requestedCompanies,
            "backfilledCompanies": self.backfilledCompanies,
            "skippedCompanies": self.skippedCompanies,
            "failedCompanies": self.failedCompanies,
            "pendingCompanies": self.pendingCompanies,
            "incomeRows": self.incomeRows,
            "balanceRows": self.balanceRows,
            "historyStatus": self.historyStatus,
            "periods": self.periods,
            "progress": self.progress,
            "completed": self.completed,
        }


class OfficialHistoryBackfillService:
    """Backfill official historical fundamentals from MOPS APIs with resumable progress."""

    _PROGRESS_SAVE_INTERVAL = 10

    def __init__(
        self,
        history_store: OfficialFundamentalsHistoryStore | None = None,
        adapter: OfficialMopsHistoricalFundamentalsAdapter | None = None,
        progress_store: BackfillProgressStore | None = None,
    ) -> None:
        self.history_store = history_store or OfficialFundamentalsHistoryStore()
        self.adapter = adapter or OfficialMopsHistoricalFundamentalsAdapter()
        self.progress_store = progress_store or BackfillProgressStore()

    def backfill(
        self,
        companies: Iterable[Company],
        limit: int = 20,
        *,
        years: int = 5,
        mode: str = "strategy",
        resume: bool = True,
        reset_progress: bool = False,
        throttle_seconds: float = 0.15,
        retry_failed: bool = False,
        max_seconds: float | None = None,
        workers: int = 1,
    ) -> OfficialHistoryBackfillResult:
        context = filing_context()
        active = context.get("activeFinancialReport") or {}
        fiscal_year = active.get("fiscalYear")
        quarter = active.get("quarter")
        if not fiscal_year or not quarter:
            month = context["monthlyRevenuePeriod"]
            fiscal_year = int(month[:4])
            quarter = ((int(month[-2:]) - 1) // 3) + 1
        fiscal_year = int(fiscal_year)
        quarter = int(quarter)
        years = max(1, int(years))
        mode = "full_quarterly" if mode == "full_quarterly" else "strategy"
        periods = _backfill_periods(fiscal_year, quarter, years=years, mode=mode)
        period_labels = [_period_label(year, period_quarter) for year, period_quarter in periods]
        company_list = list(companies)
        run_key = f"{fiscal_year}Q{quarter}:{years}:{mode}"

        if reset_progress:
            self.progress_store.reset()
        progress = self.progress_store.load() if resume else {}
        if progress.get("runKey") != run_key:
            progress = {
                "schemaVersion": 1,
                "runKey": run_key,
                "targetPeriod": f"{fiscal_year}Q{quarter}",
                "years": years,
                "mode": mode,
                "periods": period_labels,
                "totalCompanies": len(company_list),
                "completedCompanies": [],
                "failedCompanies": {},
                "pendingCompanies": {},
                "lastCompany": None,
                "startedAt": datetime.now(UTC).isoformat(),
            }

        requested = 0
        skipped = 0
        failed = 0
        pending = 0
        income_rows = 0
        balance_rows = 0
        _saves_since_last = 0
        completed_codes = set(progress.get("completedCompanies", []))
        failed_companies = dict(progress.get("failedCompanies", {}))
        pending_companies = dict(progress.get("pendingCompanies", {}))
        target_period_label = f"{fiscal_year}Q{quarter}"
        companies_by_code = {company.stockCode: company for company in company_list}
        for code, errors in list(failed_companies.items()):
            if (
                isinstance(errors, list)
                and errors
                and all(
                    isinstance(error, dict)
                    and error.get("period") == target_period_label
                    and error.get("error") == "no official rows returned"
                    for error in errors
                )
            ):
                pending_companies.setdefault(code, errors)
                completed_codes.add(code)
                failed_companies.pop(code, None)
                continue
            company = companies_by_code.get(code)
            if company is not None and not self._needs_backfill(company, fiscal_year, quarter, years=years):
                completed_codes.add(code)
                failed_companies.pop(code, None)
        previously_failed_codes = set(failed_companies)
        previously_pending_codes = set(pending_companies)

        deadline = time.monotonic() + max_seconds if max_seconds is not None and max_seconds > 0 else None
        worker_count = max(1, int(workers))

        def eligible_periods(company: Company) -> list[tuple[int, int]] | None:
            nonlocal skipped
            if company.stockCode in completed_codes:
                skipped += 1
                return None
            if resume and company.stockCode in previously_pending_codes:
                skipped += 1
                return None
            if resume and not retry_failed and company.stockCode in previously_failed_codes:
                skipped += 1
                return None
            if not self._needs_backfill(company, fiscal_year, quarter, years=years):
                completed_codes.add(company.stockCode)
                skipped += 1
                return None
            company_periods = [
                (year, period_quarter)
                for year, period_quarter in periods
                if self._period_needs_backfill(company, year, period_quarter)
            ]
            if not company_periods:
                completed_codes.add(company.stockCode)
                skipped += 1
                return None
            return company_periods

        def fetch_company(company: Company, company_periods: list[tuple[int, int]]) -> _CompanyFetch:
            fetched = _CompanyFetch()
            for year, period_quarter in company_periods:
                try:
                    bundle = self.adapter.fetch_company_period(
                        company.stockCode,
                        company.name,
                        company.market,
                        year,
                        period_quarter,
                    )
                except Exception as exc:  # pragma: no cover - network-dependent safety net
                    fetched.errors.append({"period": _period_label(year, period_quarter), "error": str(exc)})
                    continue
                fetched.incomes.extend(bundle.incomes)
                fetched.balances.extend(bundle.balances)
                if not bundle.incomes and not bundle.balances:
                    missing = {"period": _period_label(year, period_quarter), "error": "no official rows returned"}
                    if year == fiscal_year and period_quarter == quarter:
                        fetched.pending.append(missing)
                    else:
                        fetched.errors.append(missing)
                if throttle_seconds > 0:
                    time.sleep(throttle_seconds)
            return fetched

        company_iter = iter(company_list)
        exhausted = False
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            while not exhausted and requested < limit:
                if deadline is not None and time.monotonic() >= deadline:
                    # Time budget exhausted (e.g. slow MOPS responses inside a CI job): stop
                    # gracefully so progress persists and the next run resumes from here.
                    break
                batch: list[tuple[Company, list[tuple[int, int]]]] = []
                while len(batch) < worker_count and requested + len(batch) < limit:
                    company = next(company_iter, None)
                    if company is None:
                        exhausted = True
                        break
                    company_periods = eligible_periods(company)
                    if company_periods is not None:
                        batch.append((company, company_periods))
                if not batch:
                    continue
                requested += len(batch)
                futures = [executor.submit(fetch_company, company, company_periods) for company, company_periods in batch]
                for (company, _company_periods), future in zip(batch, futures, strict=True):
                    fetched = future.result()
                    income_rows += len(fetched.incomes)
                    balance_rows += len(fetched.balances)
                    if fetched.incomes or fetched.balances:
                        self.history_store.merge_rows(fetched.incomes, fetched.balances)
                    if fetched.errors:
                        failed += 1
                        failed_companies[company.stockCode] = fetched.errors
                    else:
                        failed_companies.pop(company.stockCode, None)
                        if fetched.pending:
                            pending += 1
                            pending_companies[company.stockCode] = fetched.pending
                        else:
                            pending_companies.pop(company.stockCode, None)
                        completed_codes.add(company.stockCode)
                    progress["completedCompanies"] = sorted(completed_codes)
                    progress["failedCompanies"] = failed_companies
                    progress["pendingCompanies"] = pending_companies
                    progress["lastCompany"] = company.stockCode
                    progress["lastStats"] = {
                        "requestedCompanies": requested,
                        "skippedCompanies": skipped,
                        "pendingCompanies": pending,
                        "incomeRows": income_rows,
                        "balanceRows": balance_rows,
                    }
                    _saves_since_last += 1
                    if _saves_since_last >= self._PROGRESS_SAVE_INTERVAL:
                        self.progress_store.save(progress)
                        _saves_since_last = 0

        progress["completedCompanies"] = sorted(completed_codes)
        progress["failedCompanies"] = failed_companies
        progress["pendingCompanies"] = pending_companies
        progress["totalCompanies"] = len(company_list)
        progress["completedCount"] = len(completed_codes)
        progress["remainingCount"] = max(0, len(company_list) - len(completed_codes))
        progress["failedCount"] = len(failed_companies)
        progress["pendingCount"] = len(pending_companies)
        progress["isComplete"] = len(completed_codes) >= len(company_list)
        self.progress_store.save(progress)

        return OfficialHistoryBackfillResult(
            requestedCompanies=requested,
            backfilledCompanies=max(0, requested - failed),
            skippedCompanies=skipped,
            failedCompanies=failed,
            pendingCompanies=pending,
            incomeRows=income_rows,
            balanceRows=balance_rows,
            historyStatus=self.history_store.status(),
            periods=period_labels,
            progress={
                "path": str(self.progress_store.path),
                "runKey": run_key,
                "totalCompanies": len(company_list),
                "completedCompanies": len(completed_codes),
                "remainingCompanies": max(0, len(company_list) - len(completed_codes)),
                "failedCompanies": len(failed_companies),
                "pendingCompanies": len(pending_companies),
                "mode": mode,
                "years": years,
            },
            completed=len(completed_codes) >= len(company_list),
        )

    def _period_needs_backfill(self, company: Company, fiscal_year: int, quarter: int) -> bool:
        row = self.history_store.quarter(company.stockCode, _period_label(fiscal_year, quarter))
        if row is None:
            return True
        if row.netIncome is None or row.eps is None:
            return True
        return bool(not company.isFinancial and (row.inventory is None or row.costOfRevenue is None))

    def _needs_backfill(self, company: Company, fiscal_year: int, quarter: int, years: int = 5) -> bool:
        previous_same_quarter = self.history_store.quarter(company.stockCode, f"{fiscal_year - 1}Q{quarter}")
        annuals = self.history_store.annual_financials(company.stockCode)
        annual_count = len([row for row in annuals if row.get("netIncome") is not None])
        return previous_same_quarter is None or annual_count < years


def _main() -> None:
    from backend.services.official_data_provider import OfficialDataProvider

    parser = argparse.ArgumentParser(description="Backfill official MOPS historical fundamentals with resumable progress.")
    parser.add_argument("--limit", type=int, default=20, help="Maximum companies to process in this run.")
    parser.add_argument("--years", type=int, default=5, help="Historical years required by the strategy.")
    parser.add_argument("--mode", choices=["strategy", "full_quarterly"], default="strategy")
    parser.add_argument("--throttle", type=float, default=0.15, help="Seconds to sleep between official MOPS requests.")
    parser.add_argument("--reset-progress", action="store_true", help="Start a fresh progress file for the current run key.")
    parser.add_argument(
        "--retry-failed",
        action="store_true",
        help="Re-request companies recorded as failed in a previous run instead of skipping them.",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=1,
        help="Companies fetched concurrently (keep small to stay polite to MOPS).",
    )
    parser.add_argument(
        "--max-seconds",
        type=float,
        default=None,
        help="Stop requesting new companies after this many seconds; progress is saved for the next run.",
    )
    args = parser.parse_args()

    provider = OfficialDataProvider()
    companies = provider.list_companies()
    service = OfficialHistoryBackfillService(provider.history_store)
    result = service.backfill(
        companies,
        limit=max(1, args.limit),
        years=args.years,
        mode=args.mode,
        throttle_seconds=max(0.0, args.throttle),
        reset_progress=args.reset_progress,
        retry_failed=args.retry_failed,
        max_seconds=args.max_seconds,
        workers=args.workers,
    )
    print(json.dumps(result.as_dict(), ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":  # pragma: no cover - module CLI entry point
    _main()
