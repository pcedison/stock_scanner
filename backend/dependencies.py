"""Shared runtime: service singletons, request schemas, and helper functions
used across the API routers (backend/routers/*). Kept separate from
backend.main (which only builds the FastAPI app, middleware and router
wiring) so the routers can import these without importing the app — no
import cycle.
"""

from __future__ import annotations

import os
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from threading import Event, Lock, RLock
from time import monotonic

from fastapi import HTTPException, Request, Response
from pydantic import BaseModel, Field

from backend.models.holding import Holding
from backend.models.settings import ScannerSettings
from backend.services.auth import AuthService
from backend.services.backtest import DEFAULT_BACKTEST_PATH, run_backtest
from backend.services.data_provider import MockDataProvider
from backend.services.market_scan import scan_market_payload as _scan_market_payload_impl
from backend.services.official_data_provider import OfficialDataProvider
from backend.services.official_history_backfill import OfficialHistoryBackfillService
from backend.services.reporting import render_csv_report, render_markdown_report
from backend.services.rules import RuleEngine
from backend.services.scan_cache import ScanCacheService
from backend.services.settings_service import load_settings


def _env_flag(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _cookie_secure() -> bool:
    return _env_flag("SESSION_COOKIE_SECURE")


def _runtime_environment() -> str:
    return os.getenv("APP_ENV", os.getenv("ENVIRONMENT", "development")).strip().lower() or "development"


def _is_production_environment() -> bool:
    return _runtime_environment() in {"prod", "production"}


mock_provider = MockDataProvider()
official_provider = OfficialDataProvider()
history_backfill_service = OfficialHistoryBackfillService(official_provider.history_store)
scan_cache_service = ScanCacheService()
engine = RuleEngine()
auth_db_path = os.getenv("AUTH_DB_PATH")
auth_service = AuthService(auth_db_path) if auth_db_path else AuthService()
SESSION_COOKIE_NAME = "stock_scanner_session"
SESSION_MAX_AGE_SECONDS = 60 * 60 * 24 * 30
MARKET_SCAN_RULESET_VERSION = "20260612-entry-x-rules-v1"


def _file_cache_signature(path: Path) -> dict:
    try:
        stat = path.stat()
    except FileNotFoundError:
        return {"path": str(path), "missing": True}
    return {"path": str(path), "mtimeNs": stat.st_mtime_ns, "size": stat.st_size}


def _scan_market_cache_context(settings: ScannerSettings) -> dict:
    context: dict = {
        "ruleset": MARKET_SCAN_RULESET_VERSION,
        "provider": "mock" if settings.use_mock_data else "official",
    }
    if settings.use_mock_data:
        return context
    context["data"] = {
        "monthlyRevenueHistory": _file_cache_signature(official_provider.monthly_revenue_history.path),
        "officialFundamentalsHistory": _file_cache_signature(official_provider.history_store.path),
        "fundamentalsImport": _file_cache_signature(official_provider.import_adapter.path),
    }
    return context

_backtest_cache: dict = {}
_backtest_cache_lock = RLock()
_backtest_refresh_events: dict[tuple[str, int | None, int | None], Event] = {}
_BACKTEST_CACHE_TTL_SECONDS = 3600

_SCAN_RATE_WINDOW_SECONDS = 60
_SCAN_RATE_MAX_REQUESTS = 10
_SCAN_RATE_MAX_SOURCES = 4096
_scan_rate_lock = Lock()
_scan_rate_store: dict[str, list[float]] = defaultdict(list)


def _prune_scan_rate_store(cutoff: float) -> None:
    for tracked_source, timestamps in list(_scan_rate_store.items()):
        timestamps[:] = [timestamp for timestamp in timestamps if timestamp > cutoff]
        if not timestamps:
            _scan_rate_store.pop(tracked_source, None)


def _evict_scan_rate_sources_for(source: str) -> None:
    overflow = len(_scan_rate_store) - _SCAN_RATE_MAX_SOURCES + (0 if source in _scan_rate_store else 1)
    if overflow <= 0:
        return
    oldest_sources = sorted(
        (
            (timestamps[-1] if timestamps else 0.0, tracked_source)
            for tracked_source, timestamps in _scan_rate_store.items()
            if tracked_source != source
        )
    )
    for _, tracked_source in oldest_sources[:overflow]:
        _scan_rate_store.pop(tracked_source, None)


def _check_scan_rate_limit(source: str) -> None:
    now = monotonic()
    cutoff = now - _SCAN_RATE_WINDOW_SECONDS
    with _scan_rate_lock:
        _prune_scan_rate_store(cutoff)
        _evict_scan_rate_sources_for(source)
        timestamps = _scan_rate_store[source]
        if len(timestamps) >= _SCAN_RATE_MAX_REQUESTS:
            raise HTTPException(
                status_code=429,
                detail="掃描頻率過高，請稍後再試",
                headers={"Retry-After": str(_SCAN_RATE_WINDOW_SECONDS)},
            )
        timestamps.append(now)


def _backtest_source_signature() -> tuple[str, int | None, int | None]:
    try:
        stat = DEFAULT_BACKTEST_PATH.stat()
    except FileNotFoundError:
        return (str(DEFAULT_BACKTEST_PATH), None, None)
    return (str(DEFAULT_BACKTEST_PATH), stat.st_mtime_ns, stat.st_size)


def _backtest_status_cached() -> dict:
    signature = _backtest_source_signature()
    while True:
        now = monotonic()
        with _backtest_cache_lock:
            if signature == _backtest_cache.get("signature") and now < _backtest_cache.get("expires_at", 0.0):
                return _backtest_cache["result"]
            refresh_event = _backtest_refresh_events.get(signature)
            if refresh_event is None:
                refresh_event = Event()
                _backtest_refresh_events[signature] = refresh_event
                break
        refresh_event.wait()
        signature = _backtest_source_signature()

    try:
        result = run_backtest()
        with _backtest_cache_lock:
            if _backtest_source_signature() == signature:
                _backtest_cache["result"] = result
                _backtest_cache["signature"] = signature
                _backtest_cache["expires_at"] = monotonic() + _BACKTEST_CACHE_TTL_SECONDS
        return result
    finally:
        with _backtest_cache_lock:
            event = _backtest_refresh_events.pop(signature, None)
            if event is not None:
                event.set()


class AnalyzeRequest(BaseModel):
    settings: ScannerSettings | None = None


class ScanMarketRequest(BaseModel):
    settings: ScannerSettings | None = None
    refreshMode: str = Field(default="auto", pattern="^(auto|force|cache_only)$")


class ScanHoldingsRequest(BaseModel):
    holdings: list[Holding] = Field(default_factory=list, max_length=500)
    settings: ScannerSettings | None = None


class AuthRequest(BaseModel):
    username: str = Field(..., min_length=3, max_length=80)
    password: str = Field(..., min_length=8, max_length=128)
    displayName: str | None = Field(default=None, max_length=80)


class HoldingsReplaceRequest(BaseModel):
    holdings: list[Holding] = Field(default_factory=list, max_length=500)


class HoldingUpsertRequest(BaseModel):
    holding: Holding


class ReportFormatRequest(BaseModel):
    settings: ScannerSettings | None = None


def _set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        SESSION_COOKIE_NAME,
        token,
        max_age=SESSION_MAX_AGE_SECONDS,
        httponly=True,
        samesite="lax",
        secure=_cookie_secure(),
        path="/",
    )


def _clear_session_cookie(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE_NAME, path="/")


def _auth_source(request: Request) -> str:
    cf_ip = request.headers.get("cf-connecting-ip")
    if cf_ip:
        return cf_ip.strip()
    forwarded_for = request.headers.get("x-forwarded-for")
    if forwarded_for:
        return forwarded_for.split(",", 1)[0].strip()
    return request.client.host if request.client else "unknown"


def _current_user(request: Request):
    return auth_service.get_user_by_session(request.cookies.get(SESSION_COOKIE_NAME))


def _require_user(request: Request):
    user = _current_user(request)
    if user is None:
        raise HTTPException(status_code=401, detail="請先登入")
    return user


def _require_super_user(request: Request):
    user = _require_user(request)
    if not auth_service.is_super_user(user):
        raise HTTPException(status_code=403, detail="Only the super user can manage users")
    return user


def _holdings_payload(holdings: list[Holding]) -> dict:
    return {"holdings": [holding.model_dump() for holding in holdings]}


def _effective_settings(settings: ScannerSettings | None) -> ScannerSettings:
    return settings or load_settings()


def _active_provider(settings: ScannerSettings):
    return mock_provider if settings.use_mock_data else official_provider


def _ensure_manual_scan_enabled(settings: ScannerSettings) -> None:
    if not settings.manual_scan_enabled:
        raise HTTPException(status_code=403, detail="手動掃描已在設定中停用")


def _paginated_items(items: list, page: int, limit: int) -> dict:
    total = len(items)
    safe_page = max(1, page)
    safe_limit = max(1, min(limit, 500))
    start = (safe_page - 1) * safe_limit
    end = start + safe_limit
    return {
        "items": items[start:end],
        "page": safe_page,
        "limit": safe_limit,
        "total": total,
        "hasMore": end < total,
    }


def _scan_market_payload(settings: ScannerSettings) -> dict:
    return _scan_market_payload_impl(settings, _active_provider(settings), engine)


def _scan_market_payload_after_official_refresh(settings: ScannerSettings) -> dict:
    if not settings.use_mock_data:
        official_provider.refresh(force=True)
    return _scan_market_payload(settings)


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
        "generatedAt": datetime.now(UTC).isoformat(),
        "dataSource": "mock" if settings.use_mock_data else "official_twse_tpex",
        "results": results,
        "missing": missing,
    }


def _report_response(payload: dict, report_format: str, title: str, filename_prefix: str) -> Response:
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
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
