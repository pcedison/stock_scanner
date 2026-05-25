from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from backend.services.filing_calendar import filing_context

if TYPE_CHECKING:
    from backend.models.settings import ScannerSettings
    from backend.services.official_data_provider import OfficialDataProvider
    from backend.services.rules import RuleEngine

_MOCK_NOTE = (
    "MVP 目前只掃描 data/sample_companies.json 與 data/sample_fundamentals.json 的示範樣本；"
    "不是 realtime，也不是真實全台上市櫃全市場資料。"
)
_OFFICIAL_NOTE = (
    "目前掃描官方 TWSE/TPEx 上市櫃 universe，並依目前申報窗口區分當期財報已公告與尚未公告；"
    "月營收、最新季損益、資產負債表、EPS、PER/PBR/殖利率會納入初篩，"
    "最新季資料會自動累積為官方歷史快取。這不是逐筆行情 realtime。"
)

_OFFICIAL_MONTHLY_REVENUE_ADAPTERS = {
    "TWSE_COMPANY_PROFILE": "https://openapi.twse.com.tw/v1/opendata/t187ap03_L",
    "TPEX_COMPANY_PROFILE": "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap03_O",
    "TWSE": "https://openapi.twse.com.tw/v1/opendata/t187ap05_L",
    "TPEX": "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap05_O",
}
_OFFICIAL_FUNDAMENTALS_ADAPTERS = {
    "TWSE_INCOME": "https://openapi.twse.com.tw/v1/opendata/t187ap06_L_*",
    "TWSE_BALANCE": "https://openapi.twse.com.tw/v1/opendata/t187ap07_L_*",
    "TPEX_INCOME": "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap06_O_*",
    "TPEX_BALANCE": "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap07_O_*",
    "MOPS_HISTORICAL_INCOME": "https://mops.twse.com.tw/mops/api/t164sb04",
    "MOPS_HISTORICAL_BALANCE": "https://mops.twse.com.tw/mops/api/t164sb03",
    "TWSE_VALUATION": "https://www.twse.com.tw/rwd/zh/afterTrading/BWIBBU_d",
    "TPEX_VALUATION": "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_peratio_analysis",
}
_THIRD_PARTY_PLATFORMS = {
    "MacroMicro": {
        "enabled": False,
        "configured": False,
        "requires": ["licensed API plan", "API key", "data redistribution/storage terms"],
        "note": "MacroMicro API/data downloads are subscription or enterprise-licensed services. Do not scrape without explicit written authorization.",
    }
}
_PUBLIC_STATUS_ERROR_KEYS = frozenset({"error", "exception", "traceback", "lastError"})


def _public_status(value: Any) -> Any:
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        has_error = False
        for key, item in value.items():
            if str(key) in _PUBLIC_STATUS_ERROR_KEYS:
                has_error = has_error or bool(item)
                continue
            redacted[str(key)] = _public_status(item)
        if has_error:
            redacted["hasError"] = True
        return redacted
    if isinstance(value, list):
        return [_public_status(item) for item in value]
    return value


def scan_market_payload(
    settings: ScannerSettings,
    provider: OfficialDataProvider,
    engine: RuleEngine,
) -> dict:
    context = filing_context()
    entry = []
    watch = []
    excluded = []
    for snapshot in provider.list_snapshots(settings):
        result = engine.evaluate_entry(snapshot, settings)
        if result.status == "ENTRY":
            entry.append(result)
        elif result.status == "EXCLUDED":
            excluded.append(result)
        else:
            watch.append(result)

    data_source = "mock" if settings.use_mock_data else "official_twse_tpex_monthly_revenue"
    note = _MOCK_NOTE if settings.use_mock_data else _OFFICIAL_NOTE
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


def data_sources_status_payload(
    settings: ScannerSettings,
    official_provider: OfficialDataProvider,
    mock_universe_size: int = 0,
    scan_cache_status: dict | None = None,
) -> dict:
    official_status = official_provider.status(refresh=False) if not settings.use_mock_data else None
    src = official_status.get("sourceStatus", {}) if official_status else {}
    public_src = _public_status(src)
    fundamentals_import = public_src.get("fundamentalsImport", {}) if isinstance(public_src, dict) else {}
    official_history = public_src.get("officialFundamentalsHistory", {}) if isinstance(public_src, dict) else {}
    return {
        "activeProvider": "MockDataProvider" if settings.use_mock_data else "OfficialDataProvider",
        "activeProviderIsRealtime": False,
        "activeProviderIsFullMarket": not settings.use_mock_data,
        "activeProviderHasCompleteFundamentals": False,
        "mockUniverseSize": mock_universe_size,
        "officialUniverseSize": official_status["companies"] if official_status else None,
        "officialMonthlySnapshotSize": official_status["monthlySnapshots"] if official_status else None,
        "officialIncomeStatementSize": public_src.get("incomeRows") if isinstance(public_src, dict) else None,
        "officialBalanceSheetSize": public_src.get("balanceRows") if isinstance(public_src, dict) else None,
        "officialValuationSize": public_src.get("valuationRows") if isinstance(public_src, dict) else None,
        "fundamentalsImportRows": fundamentals_import.get("rows"),
        "fundamentalsImportPath": fundamentals_import.get("path"),
        "officialHistoryRows": official_history.get("rows"),
        "officialHistoryPath": official_history.get("path"),
        "officialHistoricalFundamentals": {
            "status": "official_cache_enabled",
            "cache": official_history,
            "note": (
                "TWSE/TPEx OpenAPI latest statement rows are persisted locally. "
                "Historical gaps can be backfilled from the official MOPS JSON APIs via "
                "POST /api/data-sources/backfill-history, or by data/fundamentals_import.csv "
                "when a licensed/manual source is preferred."
            ),
        },
        "marketScanCache": scan_cache_status,
        "officialMonthlyRevenueAdapters": _OFFICIAL_MONTHLY_REVENUE_ADAPTERS,
        "officialFundamentalsAdapters": _OFFICIAL_FUNDAMENTALS_ADAPTERS,
        "thirdPartyDataPlatforms": _THIRD_PARTY_PLATFORMS,
        "note": (
            "關閉 Mock Data 後會改掃官方 TWSE/TPEx universe、最新月營收、最新季損益、"
            "資產負債表與官方估值；MOPS 官方歷史 API 可續跑回補 5 年歷史快取。"
            "當期尚未公告會標示 pending，不視為可掃描結論。"
        ),
    }
