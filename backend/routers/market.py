"""Market data and scanning: data sources, scheduler, calendar, companies,
single-stock analysis, market/holdings scans, cache status, and reports."""

from __future__ import annotations

import re
import secrets
from collections import OrderedDict
from datetime import date
from threading import RLock

from fastapi import APIRouter, Body, HTTPException, Query, Request, Response
from fastapi.encoders import jsonable_encoder

from backend import dependencies as deps
from backend.adapters.official_monthly_revenue import OfficialMonthlyRevenueAdapter
from backend.dependencies import (
    AnalyzeRequest,
    ReportFormatRequest,
    ScanHoldingsRequest,
    _active_provider,
    _auth_source,
    _check_scan_rate_limit,
    _effective_settings,
    _ensure_manual_scan_enabled,
    _paginated_items,
    _report_response,
    _scan_holdings_payload,
    _scan_market_cache_context,
    _scan_market_payload,
    _scan_market_payload_after_official_refresh,
    engine,
    history_backfill_service,
    mock_provider,
    official_provider,
    refresh_job_service,
    scan_cache_service,
)
from backend.models.settings import ScannerSettings
from backend.services.calendar import load_market_calendar, update_market_calendar
from backend.services.market_query import (
    MAX_INDEX_BYTES,
    MAX_PAGE_BYTES,
    MarketGeneration,
    build_market_generation,
    canonical_json_bytes,
    query_market_generation,
)
from backend.services.market_scan import data_sources_status_payload
from backend.services.refresh_jobs import normalize_idempotency_key
from backend.services.scan_cache import refresh_policy, scan_cache_key
from backend.services.scheduler import should_wake_up

router = APIRouter()


@router.get("/api/data-sources/status")
def data_sources_status(check_network: bool = False) -> dict:
    settings = deps.load_settings()
    payload = data_sources_status_payload(
        settings,
        official_provider,
        mock_universe_size=len(mock_provider.list_companies()),
        scan_cache_status=scan_cache_service.status(
            settings if not settings.use_mock_data else None,
            _scan_market_cache_context(settings) if not settings.use_mock_data else None,
        ),
    )
    if check_network:
        payload["networkCheck"] = OfficialMonthlyRevenueAdapter().health()
    return payload


@router.post("/api/data-sources/backfill-history")
def backfill_history(
    limit: int = 20,
    years: int = 5,
    mode: str = "strategy",
    reset_progress: bool = False,
    throttle_seconds: float = 0.15,
) -> dict:
    safe_limit = max(1, min(limit, 500))
    safe_years = max(1, min(years, 8))
    safe_mode = "full_quarterly" if mode == "full_quarterly" else "strategy"
    safe_throttle = max(0.0, min(throttle_seconds, 2.0))
    companies = official_provider.list_companies()
    result = history_backfill_service.backfill(
        companies,
        limit=safe_limit,
        years=safe_years,
        mode=safe_mode,
        reset_progress=reset_progress,
        throttle_seconds=safe_throttle,
    )
    return result.as_dict()


@router.get("/api/scheduler/wakeup")
def scheduler_wakeup(today: date | None = None) -> dict:
    return should_wake_up(today).__dict__


@router.get("/api/scheduler/auto-scan")
def scheduler_auto_scan(today: date | None = None, execute: bool = True, backfill_history: bool = False) -> dict:
    settings = deps.load_settings()
    decision = should_wake_up(today)
    should_scan = decision.status == "WAKE" and settings.auto_scan_full_market
    payload = {
        "decision": decision.__dict__,
        "autoScanEnabled": settings.auto_scan_full_market,
        "manualScanEnabled": settings.manual_scan_enabled,
        "action": "sleep",
        "scan": None,
        "historyBackfill": None,
    }
    if decision.status == "WAKE" and not settings.auto_scan_full_market:
        payload["action"] = "manual_prompt"
    elif should_scan and not execute:
        payload["action"] = "ready"
    elif should_scan:
        payload["action"] = "scanned"
        payload["scan"] = _scan_market_payload(settings)
        if backfill_history and not settings.use_mock_data:
            payload["historyBackfill"] = history_backfill_service.backfill(official_provider.list_companies(), limit=20).as_dict()
    return payload


@router.get("/api/calendar/{year}")
def calendar_status(year: int) -> dict:
    calendar = load_market_calendar(year)
    return {
        "year": calendar.year,
        "source": calendar.source,
        "sourceUrl": calendar.source_url,
        "closedDates": sorted(day.isoformat() for day in calendar.closed_dates),
        "springFestivalDates": sorted(day.isoformat() for day in calendar.spring_festival_dates),
    }


@router.post("/api/calendar/{year}/update")
def calendar_update(year: int) -> dict:
    calendar = update_market_calendar(year)
    return calendar_status(calendar.year)


@router.get("/api/companies/search")
def search_companies(q: str, limit: int = 20) -> dict:
    provider = _active_provider(deps.load_settings())
    companies = provider.search_companies(q, limit=max(1, min(limit, 20)))
    return {"items": companies}


@router.get("/api/companies")
def list_companies(page: int = Query(default=1, ge=1), limit: int = Query(default=100, ge=1, le=500)) -> dict:
    provider = _active_provider(deps.load_settings())
    return _paginated_items(provider.list_companies(), page, limit)


@router.post("/api/analyze/{stock_code}")
def analyze_stock(stock_code: str, payload: AnalyzeRequest | None = Body(default=None)):
    settings = _effective_settings(payload.settings if payload else None)
    provider = _active_provider(settings)
    snapshot = provider.get_snapshot(stock_code)
    if snapshot is None:
        raise HTTPException(status_code=404, detail="找不到股票或目前資料源缺少可分析財報資料")
    return engine.evaluate_entry(snapshot, settings)


def scan_market_cached() -> dict:
    # Internal cached read backing the paginated v2 routes below. The whole-scan
    # GET/POST /api/scan/market endpoints are retired, so this is no longer routed.
    settings = _effective_settings(None)
    if settings.use_mock_data:
        return _scan_market_payload(settings)
    cache_context = _scan_market_cache_context(settings)
    return scan_cache_service.get_or_refresh(
        settings,
        build_sync=lambda: jsonable_encoder(_scan_market_payload(settings)),
        build_refresh=lambda: jsonable_encoder(_scan_market_payload_after_official_refresh(settings)),
        refresh_mode="auto",
        context=cache_context,
    )


_SUMMARY_RESULT_KEYS = ("stockCode", "companyName", "status", "summary")
_SUMMARY_REASON_KEYS = ("code", "title", "passed", "severity", "message")
_SUMMARY_REASON_CODES = {"E4", "OFFICIAL_Q", "OFFICIAL_VALUATION", "X1", "X2", "X3", "X4", "X5"}
_MARKET_QUERY_HEADERS = {"Cache-Control": "no-store"}
_market_generation_lock = RLock()
_market_generations: OrderedDict[str, MarketGeneration] = OrderedDict()


def _remember_market_generation(generation: MarketGeneration) -> None:
    with _market_generation_lock:
        _market_generations[generation.generation_id] = generation
        _market_generations.move_to_end(generation.generation_id)
        while len(_market_generations) > 2:
            _market_generations.popitem(last=False)


def _remembered_market_generation(generation_id: str) -> MarketGeneration | None:
    with _market_generation_lock:
        return _market_generations.get(generation_id)


def _market_query_error(status_code: int, detail: str) -> HTTPException:
    return HTTPException(status_code=status_code, detail=detail, headers=_MARKET_QUERY_HEADERS)


def _compact_market_query_scan(scan: dict) -> dict:
    compact = {key: value for key, value in scan.items() if key != "cacheStatus"}
    for category in ("entry", "watch", "excluded", "results"):
        items = compact.get(category)
        if not isinstance(items, list):
            continue
        compact_items = []
        for item in items:
            if not isinstance(item, dict):
                continue
            result = {key: item.get(key) for key in _SUMMARY_RESULT_KEYS if key in item}
            reasons = item.get("reasons")
            if isinstance(reasons, list):
                result["reasons"] = [
                    {key: reason.get(key) for key in _SUMMARY_REASON_KEYS if key in reason}
                    for reason in reasons
                    if isinstance(reason, dict) and str(reason.get("code") or "") in _SUMMARY_REASON_CODES
                ]
            result["detailsAvailable"] = True
            result["hasFullDetails"] = False
            compact_items.append(result)
        compact[category] = compact_items
    compact["detailMode"] = "summary"
    return compact


def _latest_financial_period(scan: dict) -> str | None:
    freshness = scan.get("financialFreshness")
    cached = freshness.get("latestCachedFinancialPeriod") if isinstance(freshness, dict) else None
    if isinstance(cached, str) and cached:
        return cached
    context = scan.get("filingContext")
    if not isinstance(context, dict):
        return None
    for field in ("freshnessFinancialReport", "activeFinancialReport"):
        report = context.get(field)
        period = report.get("period") if isinstance(report, dict) else None
        if isinstance(period, str) and period:
            return period
    return None


def _current_market_generation() -> tuple[MarketGeneration, dict]:
    try:
        scan = jsonable_encoder(scan_market_cached())
        if not isinstance(scan, dict):
            raise ValueError("market scan must be an object")
        filing_context = scan.get("filingContext")
        context = filing_context if isinstance(filing_context, dict) else {}
        generation = build_market_generation(
            _compact_market_query_scan(scan),
            cache_status_inputs={
                "generatedAt": scan.get("generatedAt"),
                "latestRevenuePeriod": context.get("monthlyRevenuePeriod"),
                "latestFinancialPeriod": _latest_financial_period(scan),
            },
        )
    except (TypeError, ValueError) as exc:
        raise _market_query_error(503, "market_query_unavailable") from exc
    _remember_market_generation(generation)
    cache_status = scan.get("cacheStatus")
    return generation, dict(cache_status) if isinstance(cache_status, dict) else {}


def _market_results_params(request: Request) -> tuple[str, str, int, int, str | None]:
    def one(name: str, *, optional: bool = False) -> str | None:
        values = request.query_params.getlist(name)
        if optional and not values:
            return None
        if len(values) != 1 or not values[0]:
            raise _market_query_error(422, f"invalid {name}")
        return values[0]

    disclosure = one("disclosure")
    category = one("category")
    cursor_raw = one("cursor")
    limit_raw = one("limit")
    generation_id = one("generationId", optional=True)
    if disclosure not in {"announced", "pending"}:
        raise _market_query_error(422, "invalid disclosure")
    if category not in {"entry", "watch", "excluded"}:
        raise _market_query_error(422, "invalid category")
    if not isinstance(cursor_raw, str) or not re.fullmatch(r"[0-9]+", cursor_raw):
        raise _market_query_error(422, "invalid cursor")
    if len(cursor_raw) > 32 or len(cursor_raw.lstrip("0") or "0") > 7:
        raise _market_query_error(422, "invalid cursor")
    if not isinstance(limit_raw, str) or not re.fullmatch(r"[0-9]+", limit_raw):
        raise _market_query_error(422, "invalid limit")
    if len(limit_raw) > 32 or len(limit_raw.lstrip("0") or "0") > 3:
        raise _market_query_error(422, "invalid limit")
    cursor, limit = int(cursor_raw), int(limit_raw)
    if cursor > 2_000_000:
        raise _market_query_error(422, "invalid cursor")
    if not 1 <= limit <= 100:
        raise _market_query_error(422, "invalid limit")
    if generation_id is not None and not re.fullmatch(r"[0-9a-f]{24}", generation_id):
        raise _market_query_error(422, "invalid generationId")
    return disclosure, category, cursor, limit, generation_id


@router.get("/api/scan/market/index")
def scan_market_index(response: Response) -> dict:
    response.headers.update(_MARKET_QUERY_HEADERS)
    generation, cache_status = _current_market_generation()
    payload = {**generation.index, "cacheStatus": cache_status}
    if len(canonical_json_bytes(payload)) >= MAX_INDEX_BYTES:
        raise _market_query_error(503, "market_query_unavailable")
    return payload


@router.get("/api/scan/market/results")
def scan_market_results(request: Request, response: Response) -> dict:
    response.headers.update(_MARKET_QUERY_HEADERS)
    disclosure, category, cursor, limit, requested_generation = _market_results_params(request)
    generation = _remembered_market_generation(requested_generation) if requested_generation is not None else None
    if generation is None:
        generation, _cache_status = _current_market_generation()
    if requested_generation is not None and requested_generation != generation.generation_id:
        raise _market_query_error(409, "generation_mismatch")
    try:
        payload = query_market_generation(generation.index, generation.files, disclosure, category, cursor, limit)
    except ValueError as exc:
        raise _market_query_error(503, "market_query_unavailable") from exc
    if len(canonical_json_bytes(payload)) >= MAX_PAGE_BYTES:
        raise _market_query_error(503, "market_query_unavailable")
    return payload


# Single-flight scope for the refresh command, matching the Worker's `market_scan` job type.
# It is deliberately not the cache key: that key moves when a refresh rewrites the official files.
MARKET_REFRESH_JOB_SCOPE = "market_scan"


def _rebuild_market_scan(settings: ScannerSettings) -> None:
    """The forced counterpart of `scan_market_cached()`: rebuild instead of serving the cache."""
    if settings.use_mock_data:
        # Mock scans are never cached (see `scan_market_cached()`), so the job only rebuilds the
        # payload: it proves the scan still runs and leaves the served data unchanged.
        _scan_market_payload(settings)
        return
    payload = jsonable_encoder(_scan_market_payload_after_official_refresh(settings))
    # The official refresh rewrites the files `_scan_market_cache_context` fingerprints, so the
    # cache key is only known after the rebuild: computing it first would store the fresh payload
    # under the pre-refresh key and leave the next read with a miss.
    context = _scan_market_cache_context(settings)
    scan_cache_service.store(scan_cache_key(settings, context), settings, payload, refresh_policy(), context)


@router.post("/api/scan/market/refresh", status_code=202)
def refresh_market_scan(request: Request, response: Response) -> dict:
    # The Worker queues a D1 job for a GitHub Actions rebuild; locally the rebuild runs on a
    # background thread, so the client polls the same status route until the job is terminal.
    settings = _effective_settings(None)
    if not settings.use_mock_data:
        # `manual_scan_enabled` is deliberately not enforced here: the Worker's refresh command
        # does not enforce it either, and the contract tests hold both runtimes to the same rules.
        _check_scan_rate_limit(_auth_source(request))
    try:
        client_key = normalize_idempotency_key(request.headers.get("idempotency-key"))
    except ValueError as exc:
        raise _market_query_error(422, str(exc)) from None
    job = refresh_job_service.start(
        lambda: _rebuild_market_scan(settings),
        scope=MARKET_REFRESH_JOB_SCOPE,
        reason=refresh_policy()["reason"],
        idempotency_key=client_key,
    )
    status_url = f"/api/scan/market/refresh/{job['jobId']}"
    response.headers.update(_MARKET_QUERY_HEADERS)
    response.headers["Location"] = status_url
    return {
        "jobId": job["jobId"],
        "status": job["status"],
        "requestId": secrets.token_hex(12),
        "statusUrl": status_url,
    }


@router.get("/api/scan/market/refresh/{job_id}")
def market_refresh_job_status(job_id: str, response: Response) -> dict:
    job = refresh_job_service.get(job_id)
    if job is None:
        raise _market_query_error(404, "Not found")
    response.headers.update(_MARKET_QUERY_HEADERS)
    return job


@router.get("/api/cache/status")
def cache_status() -> dict:
    settings = deps.load_settings()
    return {
        "marketScan": scan_cache_service.status(
            settings if not settings.use_mock_data else None,
            _scan_market_cache_context(settings) if not settings.use_mock_data else None,
        ),
        "officialHistory": official_provider.history_store.status(),
    }


@router.post("/api/scan/holdings")
def scan_holdings(request: Request, payload: ScanHoldingsRequest) -> dict:
    settings = _effective_settings(payload.settings)
    _ensure_manual_scan_enabled(settings)
    if not settings.use_mock_data:
        _check_scan_rate_limit(_auth_source(request))
    return _scan_holdings_payload(payload.holdings, settings)


@router.post("/api/reports/market")
def market_report(report_format: str = "markdown", payload: ReportFormatRequest | None = Body(default=None)):
    settings = _effective_settings(payload.settings if payload else None)
    if settings.use_mock_data:
        scan_payload = _scan_market_payload(settings)
    else:
        cache_context = _scan_market_cache_context(settings)
        scan_payload = scan_cache_service.get_or_refresh(
            settings,
            build_sync=lambda: jsonable_encoder(_scan_market_payload(settings)),
            build_refresh=lambda: jsonable_encoder(_scan_market_payload_after_official_refresh(settings)),
            refresh_mode="auto",
            context=cache_context,
        )
    return _report_response(scan_payload, report_format, "台股市場掃描報告", "market_scan")


@router.post("/api/reports/holdings")
def holdings_report(report_format: str = "markdown", payload: ScanHoldingsRequest | None = Body(default=None)):
    settings = _effective_settings(payload.settings if payload else None)
    scan_payload = _scan_holdings_payload(payload.holdings if payload else [], settings)
    return _report_response(scan_payload, report_format, "台股持股追蹤報告", "holdings_scan")
