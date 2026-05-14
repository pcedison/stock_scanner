from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import Body, FastAPI, HTTPException, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from backend.adapters.official_monthly_revenue import OfficialMonthlyRevenueAdapter
from backend.models.holding import Holding
from backend.models.settings import ScannerSettings
from backend.services.backtest import run_backtest
from backend.services.calendar import load_market_calendar, update_market_calendar
from backend.services.data_provider import MockDataProvider
from backend.services.filing_calendar import filing_context
from backend.services.integrations import integration_status
from backend.services.official_history_backfill import OfficialHistoryBackfillService
from backend.services.official_data_provider import OfficialDataProvider
from backend.services.reporting import render_csv_report, render_markdown_report
from backend.services.rules import RuleEngine
from backend.services.scheduler import should_wake_up
from backend.services.settings_service import load_settings, save_settings


ROOT_DIR = Path(__file__).resolve().parents[1]
FRONTEND_DIR = ROOT_DIR / "frontend"

app = FastAPI(title="台股財報事件驅動掃描器", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost", "http://localhost:8000", "http://127.0.0.1:8000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

mock_provider = MockDataProvider()
official_provider = OfficialDataProvider()
history_backfill_service = OfficialHistoryBackfillService(official_provider.history_store)
engine = RuleEngine()


class AnalyzeRequest(BaseModel):
    settings: Optional[ScannerSettings] = None


class ScanMarketRequest(BaseModel):
    settings: Optional[ScannerSettings] = None


class ScanHoldingsRequest(BaseModel):
    holdings: list[Holding] = Field(default_factory=list)
    settings: Optional[ScannerSettings] = None


class ReportFormatRequest(BaseModel):
    settings: Optional[ScannerSettings] = None


def _effective_settings(settings: Optional[ScannerSettings]) -> ScannerSettings:
    return settings or load_settings()


def _active_provider(settings: ScannerSettings):
    return mock_provider if settings.use_mock_data else official_provider


def _ensure_manual_scan_enabled(settings: ScannerSettings) -> None:
    if not settings.manual_scan_enabled:
        raise HTTPException(status_code=403, detail="手動掃描已在設定中停用")


def _scan_market_payload(settings: ScannerSettings) -> dict:
    provider = _active_provider(settings)
    context = filing_context()
    entry = []
    watch = []
    excluded = []
    for snapshot in provider.iter_snapshots(settings):
        result = engine.evaluate_entry(snapshot, settings)
        if result.status == "ENTRY":
            entry.append(result)
        elif result.status == "EXCLUDED":
            excluded.append(result)
        else:
            watch.append(result)

    data_source = "mock" if settings.use_mock_data else "official_twse_tpex_monthly_revenue"
    note = (
        "MVP 目前只掃描 data/sample_companies.json 與 data/sample_fundamentals.json 的示範樣本；不是 realtime，也不是真實全台上市櫃全市場資料。"
        if settings.use_mock_data
        else "目前掃描官方 TWSE/TPEx 上市櫃 universe，並依目前申報窗口區分當期財報已公告與尚未公告；月營收、最新季損益、資產負債表、EPS、PER/PBR/殖利率會納入初篩，最新季資料會自動累積為官方歷史快取。這不是逐筆行情 realtime。"
    )
    return {
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "dataSource": data_source,
        "filingContext": context,
        "universeSize": len(entry) + len(watch) + len(excluded),
        "note": note,
        "entry": entry,
        "watch": watch,
        "excluded": excluded,
    }


def _scan_holdings_payload(holdings: list[Holding], settings: ScannerSettings) -> dict:
    provider = _active_provider(settings)
    results = []
    missing = []
    for holding in holdings:
        snapshot = provider.get_snapshot(holding.stockCode)
        if snapshot is None:
            missing.append({"stockCode": holding.stockCode, "name": holding.name, "reason": "找不到股票或目前資料源缺少可分析財報資料"})
            continue
        results.append(engine.evaluate_holding(snapshot, holding, settings))

    return {
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "dataSource": "mock" if settings.use_mock_data else "official_twse_tpex",
        "results": results,
        "missing": missing,
    }


def _report_response(payload: dict, report_format: str, title: str, filename_prefix: str) -> Response:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    if report_format == "csv":
        return Response(
            content=render_csv_report(payload),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{filename_prefix}_{timestamp}.csv"'},
        )
    if report_format in {"markdown", "md"}:
        return Response(
            content=render_markdown_report(payload, title),
            media_type="text/markdown; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{filename_prefix}_{timestamp}.md"'},
        )
    raise HTTPException(status_code=400, detail="report_format must be markdown or csv")


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok", "dataSource": "mock", "time": datetime.now(timezone.utc).isoformat()}


@app.get("/api/data-sources/status")
def data_sources_status(check_network: bool = False) -> dict:
    settings = load_settings()
    official_status = official_provider.status(refresh=False) if not settings.use_mock_data else None
    payload = {
        "activeProvider": "MockDataProvider" if settings.use_mock_data else "OfficialDataProvider",
        "activeProviderIsRealtime": False,
        "activeProviderIsFullMarket": not settings.use_mock_data,
        "activeProviderHasCompleteFundamentals": False,
        "mockUniverseSize": len(mock_provider.list_companies()),
        "officialUniverseSize": official_status["companies"] if official_status else None,
        "officialMonthlySnapshotSize": official_status["monthlySnapshots"] if official_status else None,
        "officialIncomeStatementSize": official_status["sourceStatus"].get("incomeRows") if official_status else None,
        "officialBalanceSheetSize": official_status["sourceStatus"].get("balanceRows") if official_status else None,
        "officialValuationSize": official_status["sourceStatus"].get("valuationRows") if official_status else None,
        "fundamentalsImportRows": official_status["sourceStatus"].get("fundamentalsImport", {}).get("rows") if official_status else None,
        "fundamentalsImportPath": official_status["sourceStatus"].get("fundamentalsImport", {}).get("path") if official_status else None,
        "officialHistoryRows": official_status["sourceStatus"].get("officialFundamentalsHistory", {}).get("rows") if official_status else None,
        "officialHistoryPath": official_status["sourceStatus"].get("officialFundamentalsHistory", {}).get("path") if official_status else None,
        "officialHistoricalFundamentals": {
            "status": "official_cache_enabled",
            "cache": official_status["sourceStatus"].get("officialFundamentalsHistory") if official_status else None,
            "note": "TWSE/TPEx OpenAPI latest statement rows are persisted locally. Historical gaps can be backfilled from the official MOPS JSON APIs via POST /api/data-sources/backfill-history, or by data/fundamentals_import.csv when a licensed/manual source is preferred.",
        },
        "officialMonthlyRevenueAdapters": {
            "TWSE_COMPANY_PROFILE": "https://openapi.twse.com.tw/v1/opendata/t187ap03_L",
            "TPEX_COMPANY_PROFILE": "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap03_O",
            "TWSE": "https://openapi.twse.com.tw/v1/opendata/t187ap05_L",
            "TPEX": "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap05_O",
        },
        "officialFundamentalsAdapters": {
            "TWSE_INCOME": "https://openapi.twse.com.tw/v1/opendata/t187ap06_L_*",
            "TWSE_BALANCE": "https://openapi.twse.com.tw/v1/opendata/t187ap07_L_*",
            "TPEX_INCOME": "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap06_O_*",
            "TPEX_BALANCE": "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap07_O_*",
            "MOPS_HISTORICAL_INCOME": "https://mops.twse.com.tw/mops/api/t164sb04",
            "MOPS_HISTORICAL_BALANCE": "https://mops.twse.com.tw/mops/api/t164sb03",
            "TWSE_VALUATION": "https://www.twse.com.tw/rwd/zh/afterTrading/BWIBBU_d",
            "TPEX_VALUATION": "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_peratio_analysis",
        },
        "thirdPartyDataPlatforms": {
            "MacroMicro": {
                "enabled": False,
                "configured": False,
                "requires": ["licensed API plan", "API key", "data redistribution/storage terms"],
                "note": "MacroMicro API/data downloads are subscription or enterprise-licensed services. Do not scrape without explicit written authorization.",
            }
        },
        "note": "關閉 Mock Data 後會改掃官方 TWSE/TPEx universe、最新月營收、最新季損益、資產負債表與官方估值；MOPS 官方歷史 API 可續跑回補 5 年歷史快取。當期尚未公告會標示 pending，不視為可掃描結論。",
    }
    if check_network:
        payload["networkCheck"] = OfficialMonthlyRevenueAdapter().health()
    return payload


@app.post("/api/data-sources/backfill-history")
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


@app.get("/api/scheduler/wakeup")
def scheduler_wakeup(today: Optional[date] = None) -> dict:
    return should_wake_up(today).__dict__


@app.get("/api/scheduler/auto-scan")
def scheduler_auto_scan(today: Optional[date] = None, execute: bool = True, backfill_history: bool = False) -> dict:
    settings = load_settings()
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


@app.get("/api/calendar/{year}")
def calendar_status(year: int) -> dict:
    calendar = load_market_calendar(year)
    return {
        "year": calendar.year,
        "source": calendar.source,
        "sourceUrl": calendar.source_url,
        "closedDates": sorted(day.isoformat() for day in calendar.closed_dates),
        "springFestivalDates": sorted(day.isoformat() for day in calendar.spring_festival_dates),
    }


@app.post("/api/calendar/{year}/update")
def calendar_update(year: int) -> dict:
    calendar = update_market_calendar(year)
    return calendar_status(calendar.year)


@app.get("/api/companies/search")
def search_companies(q: str, limit: int = 20) -> dict:
    provider = _active_provider(load_settings())
    companies = provider.search_companies(q, limit=max(1, min(limit, 20)))
    return {"items": companies}


@app.get("/api/companies")
def list_companies() -> dict:
    provider = _active_provider(load_settings())
    return {"items": provider.list_companies()}


@app.post("/api/analyze/{stock_code}")
def analyze_stock(stock_code: str, payload: Optional[AnalyzeRequest] = Body(default=None)):
    settings = _effective_settings(payload.settings if payload else None)
    provider = _active_provider(settings)
    snapshot = provider.get_snapshot(stock_code)
    if snapshot is None:
        raise HTTPException(status_code=404, detail="找不到股票或目前資料源缺少可分析財報資料")
    return engine.evaluate_entry(snapshot, settings)


@app.post("/api/scan/market")
def scan_market(payload: Optional[ScanMarketRequest] = Body(default=None)) -> dict:
    settings = _effective_settings(payload.settings if payload else None)
    _ensure_manual_scan_enabled(settings)
    return _scan_market_payload(settings)


@app.post("/api/scan/holdings")
def scan_holdings(payload: ScanHoldingsRequest) -> dict:
    settings = _effective_settings(payload.settings)
    _ensure_manual_scan_enabled(settings)
    return _scan_holdings_payload(payload.holdings, settings)


@app.post("/api/reports/market")
def market_report(report_format: str = "markdown", payload: Optional[ReportFormatRequest] = Body(default=None)):
    settings = _effective_settings(payload.settings if payload else None)
    scan_payload = _scan_market_payload(settings)
    return _report_response(scan_payload, report_format, "台股市場掃描報告", "market_scan")


@app.post("/api/reports/holdings")
def holdings_report(report_format: str = "markdown", payload: Optional[ScanHoldingsRequest] = Body(default=None)):
    settings = _effective_settings(payload.settings if payload else None)
    scan_payload = _scan_holdings_payload(payload.holdings if payload else [], settings)
    return _report_response(scan_payload, report_format, "台股持股追蹤報告", "holdings_scan")


@app.get("/api/integrations/status")
def integrations_status() -> dict:
    return integration_status()


@app.get("/api/backtest")
def backtest_status() -> dict:
    return run_backtest()


@app.get("/api/settings")
def get_settings() -> ScannerSettings:
    return load_settings()


@app.put("/api/settings")
def put_settings(settings: ScannerSettings) -> ScannerSettings:
    return save_settings(settings)


if FRONTEND_DIR.exists():
    app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
