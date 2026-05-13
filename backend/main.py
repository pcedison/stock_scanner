from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import Body, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from backend.adapters.official_monthly_revenue import OfficialMonthlyRevenueAdapter
from backend.models.holding import Holding
from backend.models.settings import ScannerSettings
from backend.services.data_provider import MockDataProvider
from backend.services.official_data_provider import OfficialDataProvider
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
engine = RuleEngine()


class AnalyzeRequest(BaseModel):
    settings: Optional[ScannerSettings] = None


class ScanMarketRequest(BaseModel):
    settings: Optional[ScannerSettings] = None


class ScanHoldingsRequest(BaseModel):
    holdings: list[Holding] = Field(default_factory=list)
    settings: Optional[ScannerSettings] = None


def _effective_settings(settings: Optional[ScannerSettings]) -> ScannerSettings:
    return settings or load_settings()


def _active_provider(settings: ScannerSettings):
    return mock_provider if settings.use_mock_data else official_provider


def _scan_market_payload(settings: ScannerSettings) -> dict:
    provider = _active_provider(settings)
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
        else "目前掃描官方 TWSE/TPEx 上市櫃 universe 中已公告月營收的公司；月營收為官方公開資料，但季報、年報、PER、存貨週轉率與毛利率尚未完整整合，因此多數結果會是 INSUFFICIENT_DATA。這不是逐筆行情 realtime。"
    )
    return {
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "dataSource": data_source,
        "universeSize": len(entry) + len(watch) + len(excluded),
        "note": note,
        "entry": entry,
        "watch": watch,
        "excluded": excluded,
    }


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok", "dataSource": "mock", "time": datetime.now(timezone.utc).isoformat()}


@app.get("/api/data-sources/status")
def data_sources_status(check_network: bool = False) -> dict:
    settings = load_settings()
    official_status = official_provider.status() if not settings.use_mock_data else None
    payload = {
        "activeProvider": "MockDataProvider" if settings.use_mock_data else "OfficialDataProvider",
        "activeProviderIsRealtime": False,
        "activeProviderIsFullMarket": not settings.use_mock_data,
        "activeProviderHasCompleteFundamentals": False,
        "mockUniverseSize": len(mock_provider.list_companies()),
        "officialUniverseSize": official_status["companies"] if official_status else None,
        "officialMonthlySnapshotSize": official_status["monthlySnapshots"] if official_status else None,
        "officialMonthlyRevenueAdapters": {
            "TWSE_COMPANY_PROFILE": "https://openapi.twse.com.tw/v1/opendata/t187ap03_L",
            "TPEX_COMPANY_PROFILE": "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap03_O",
            "TWSE": "https://openapi.twse.com.tw/v1/opendata/t187ap05_L",
            "TPEX": "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap05_O",
        },
        "note": "關閉 Mock Data 後會改掃官方 TWSE/TPEx universe 與最新月營收；正式完整基本面掃描仍缺季報、年報、PER、存貨週轉率與毛利率等官方資料整合。",
    }
    if check_network:
        payload["networkCheck"] = OfficialMonthlyRevenueAdapter().health()
    return payload


@app.get("/api/scheduler/wakeup")
def scheduler_wakeup(today: Optional[date] = None) -> dict:
    return should_wake_up(today).__dict__


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
    return _scan_market_payload(settings)


@app.post("/api/scan/holdings")
def scan_holdings(payload: ScanHoldingsRequest) -> dict:
    settings = _effective_settings(payload.settings)
    provider = _active_provider(settings)
    results = []
    missing = []
    for holding in payload.holdings:
        snapshot = provider.get_snapshot(holding.stockCode)
        if snapshot is None:
            missing.append({"stockCode": holding.stockCode, "name": holding.name, "reason": "找不到股票或目前資料源缺少可分析財報資料"})
            continue
        results.append(engine.evaluate_holding(snapshot, holding, settings))

    return {
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "results": results,
        "missing": missing,
    }


@app.get("/api/settings")
def get_settings() -> ScannerSettings:
    return load_settings()


@app.put("/api/settings")
def put_settings(settings: ScannerSettings) -> ScannerSettings:
    return save_settings(settings)


if FRONTEND_DIR.exists():
    app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
