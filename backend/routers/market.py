"""Market data and scanning: data sources, scheduler, calendar, companies,
single-stock analysis, market/holdings scans, cache status, and reports."""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Body, HTTPException, Query, Request
from fastapi.encoders import jsonable_encoder

from backend import dependencies as deps
from backend.adapters.official_monthly_revenue import OfficialMonthlyRevenueAdapter
from backend.dependencies import (
    AnalyzeRequest,
    ReportFormatRequest,
    ScanHoldingsRequest,
    ScanMarketRequest,
    _active_provider,
    _auth_source,
    _check_scan_rate_limit,
    _effective_settings,
    _ensure_manual_scan_enabled,
    _paginated_items,
    _report_response,
    _scan_holdings_payload,
    _scan_market_payload,
    _scan_market_payload_after_official_refresh,
    engine,
    history_backfill_service,
    mock_provider,
    official_provider,
    scan_cache_service,
)
from backend.services.calendar import load_market_calendar, update_market_calendar
from backend.services.market_scan import data_sources_status_payload
from backend.services.scheduler import should_wake_up

router = APIRouter()


@router.get("/api/data-sources/status")
def data_sources_status(check_network: bool = False) -> dict:
    settings = deps.load_settings()
    payload = data_sources_status_payload(
        settings,
        official_provider,
        mock_universe_size=len(mock_provider.list_companies()),
        scan_cache_status=scan_cache_service.status(settings if not settings.use_mock_data else None),
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


@router.post("/api/scan/market")
def scan_market(request: Request, payload: ScanMarketRequest | None = Body(default=None)) -> dict:
    settings = _effective_settings(payload.settings if payload else None)
    _ensure_manual_scan_enabled(settings)
    if settings.use_mock_data:
        return _scan_market_payload(settings)
    _check_scan_rate_limit(_auth_source(request))
    refresh_mode = payload.refreshMode if payload else "auto"
    return scan_cache_service.get_or_refresh(
        settings,
        build_sync=lambda: jsonable_encoder(_scan_market_payload(settings)),
        build_refresh=lambda: jsonable_encoder(_scan_market_payload_after_official_refresh(settings)),
        refresh_mode=refresh_mode,
    )


@router.get("/api/scan/market")
def scan_market_cached() -> dict:
    # Pure read counterpart to the POST handler: serves the cached market scan
    # without rate limiting or forcing a refresh, so it can be edge-cached.
    # (See docs/proposals/market-scan-cacheability.md.)
    settings = _effective_settings(None)
    if settings.use_mock_data:
        return _scan_market_payload(settings)
    return scan_cache_service.get_or_refresh(
        settings,
        build_sync=lambda: jsonable_encoder(_scan_market_payload(settings)),
        build_refresh=lambda: jsonable_encoder(_scan_market_payload_after_official_refresh(settings)),
        refresh_mode="auto",
    )


@router.get("/api/cache/status")
def cache_status() -> dict:
    settings = deps.load_settings()
    return {
        "marketScan": scan_cache_service.status(settings if not settings.use_mock_data else None),
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
        scan_payload = scan_cache_service.get_or_refresh(
            settings,
            build_sync=lambda: jsonable_encoder(_scan_market_payload(settings)),
            build_refresh=lambda: jsonable_encoder(_scan_market_payload_after_official_refresh(settings)),
            refresh_mode="auto",
        )
    return _report_response(scan_payload, report_format, "台股市場掃描報告", "market_scan")


@router.post("/api/reports/holdings")
def holdings_report(report_format: str = "markdown", payload: ScanHoldingsRequest | None = Body(default=None)):
    settings = _effective_settings(payload.settings if payload else None)
    scan_payload = _scan_holdings_payload(payload.holdings if payload else [], settings)
    return _report_response(scan_payload, report_format, "台股持股追蹤報告", "holdings_scan")
