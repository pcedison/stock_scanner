from __future__ import annotations

import os
from collections import defaultdict
from datetime import UTC, date, datetime
from pathlib import Path
from threading import Event, Lock, RLock
from time import monotonic
from urllib.parse import urlparse

from fastapi import Body, FastAPI, HTTPException, Query, Request, Response
from fastapi.encoders import jsonable_encoder
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from backend.adapters.official_monthly_revenue import OfficialMonthlyRevenueAdapter
from backend.models.holding import Holding
from backend.models.settings import ScannerSettings
from backend.services.auth import AuthRateLimitError, AuthService, super_user_username
from backend.services.backtest import DEFAULT_BACKTEST_PATH, run_backtest
from backend.services.calendar import load_market_calendar, update_market_calendar
from backend.services.data_provider import MockDataProvider
from backend.services.integrations import integration_status
from backend.services.market_scan import data_sources_status_payload
from backend.services.market_scan import scan_market_payload as _scan_market_payload_impl
from backend.services.official_data_provider import OfficialDataProvider
from backend.services.official_history_backfill import OfficialHistoryBackfillService
from backend.services.reporting import render_csv_report, render_markdown_report
from backend.services.rules import RuleEngine
from backend.services.scan_cache import ScanCacheService
from backend.services.scheduler import should_wake_up
from backend.services.settings_service import load_settings, save_settings

ROOT_DIR = Path(__file__).resolve().parents[1]
FRONTEND_DIR = ROOT_DIR / "frontend"
DEFAULT_CORS_ALLOW_ORIGINS = ("http://localhost", "http://localhost:8000", "http://127.0.0.1:8000")
LOCAL_CORS_HOSTS = {"localhost", "127.0.0.1", "::1"}


def _env_flag(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _cookie_secure() -> bool:
    return _env_flag("SESSION_COOKIE_SECURE")


def _runtime_environment() -> str:
    return os.getenv("APP_ENV", os.getenv("ENVIRONMENT", "development")).strip().lower() or "development"


def _is_production_environment() -> bool:
    return _runtime_environment() in {"prod", "production"}


def _csv_env(name: str, default: tuple[str, ...] = ()) -> list[str]:
    raw = os.getenv(name)
    if raw is None:
        return list(default)
    return [item.strip() for item in raw.split(",") if item.strip()]


def _cors_allowed_origins() -> list[str]:
    return _csv_env("APP_CORS_ALLOW_ORIGINS", DEFAULT_CORS_ALLOW_ORIGINS)


def _origin_host(origin: str) -> str:
    return (urlparse(origin).hostname or "").lower()


def _is_local_cors_origin(origin: str) -> bool:
    return _origin_host(origin) in LOCAL_CORS_HOSTS


def _is_https_origin(origin: str) -> bool:
    return urlparse(origin).scheme.lower() == "https"


def _validate_runtime_security(cors_origins: list[str]) -> None:
    if not _is_production_environment():
        return
    if not _cookie_secure():
        raise RuntimeError("SESSION_COOKIE_SECURE must be enabled when APP_ENV=production")
    local_origins = sorted(origin for origin in cors_origins if _is_local_cors_origin(origin))
    if local_origins and not _env_flag("APP_ALLOW_LOCAL_CORS_IN_PRODUCTION"):
        raise RuntimeError(
            "APP_CORS_ALLOW_ORIGINS must not include localhost origins in production: "
            + ", ".join(local_origins)
        )
    insecure_origins = sorted(origin for origin in cors_origins if not _is_https_origin(origin))
    if insecure_origins and not _env_flag("APP_ALLOW_INSECURE_CORS_IN_PRODUCTION"):
        raise RuntimeError(
            "APP_CORS_ALLOW_ORIGINS must use https origins in production: "
            + ", ".join(insecure_origins)
        )
    if not super_user_username():
        raise RuntimeError("SUPER_USER_USERNAME must be configured when APP_ENV=production")


CORS_ALLOWED_ORIGINS = _cors_allowed_origins()
_validate_runtime_security(CORS_ALLOWED_ORIGINS)

app = FastAPI(title="台股財報事件驅動掃描器", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)

SECURITY_HEADERS = {
    "Strict-Transport-Security": "max-age=31536000; includeSubDomains; preload",
    "Content-Security-Policy": (
        "default-src 'self'; "
        "script-src 'self'; "
        "style-src 'self'; "
        "img-src 'self' data:; "
        "font-src 'self' data:; "
        "connect-src 'self'; "
        "base-uri 'self'; "
        "form-action 'self'; "
        "frame-ancestors 'none'; "
        "object-src 'none'; "
        "upgrade-insecure-requests"
    ),
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=(), usb=()",
}

CSRF_HEADER_NAME = "x-stock-scanner-csrf"
CSRF_HEADER_VALUE = "1"
UNSAFE_API_METHODS = {"POST", "PUT", "DELETE"}


def _requires_csrf_header(request: Request) -> bool:
    return (
        _is_production_environment()
        and request.method.upper() in UNSAFE_API_METHODS
        and request.url.path.startswith("/api/")
    )


def _has_valid_csrf_header(request: Request) -> bool:
    return request.headers.get(CSRF_HEADER_NAME) == CSRF_HEADER_VALUE


@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    if _requires_csrf_header(request) and not _has_valid_csrf_header(request):
        response = JSONResponse({"detail": "CSRF header required"}, status_code=403)
        for key, value in SECURITY_HEADERS.items():
            response.headers.setdefault(key, value)
        return response
    response = await call_next(request)
    for key, value in SECURITY_HEADERS.items():
        response.headers.setdefault(key, value)
    return response

mock_provider = MockDataProvider()
official_provider = OfficialDataProvider()
history_backfill_service = OfficialHistoryBackfillService(official_provider.history_store)
scan_cache_service = ScanCacheService()
engine = RuleEngine()
auth_db_path = os.getenv("AUTH_DB_PATH")
auth_service = AuthService(auth_db_path) if auth_db_path else AuthService()
SESSION_COOKIE_NAME = "stock_scanner_session"
SESSION_MAX_AGE_SECONDS = 60 * 60 * 24 * 30

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


@app.get("/api/health")
def health() -> dict:
    settings = load_settings()
    return {
        "status": "ok",
        "dataSource": "mock" if settings.use_mock_data else "official_twse_tpex",
        "time": datetime.now(UTC).isoformat(),
    }


@app.post("/api/auth/register")
def register(payload: AuthRequest, request: Request, response: Response) -> dict:
    source = _auth_source(request)
    try:
        auth_service.assert_auth_allowed(payload.username, source)
    except AuthRateLimitError as exc:
        raise HTTPException(status_code=429, detail=str(exc), headers={"Retry-After": str(exc.retry_after_seconds)}) from exc
    try:
        user = auth_service.create_user(payload.username, payload.password, payload.displayName)
    except ValueError as exc:
        auth_service.record_auth_failure(payload.username, source)
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    auth_service.clear_auth_failures(payload.username, source)
    token = auth_service.create_session(user.id)
    _set_session_cookie(response, token)
    holdings = auth_service.list_holdings(user.id)
    return {"authenticated": True, "user": user.public_dict(), "holdings": [h.model_dump() for h in holdings]}


@app.post("/api/auth/login")
def login(payload: AuthRequest, request: Request, response: Response) -> dict:
    source = _auth_source(request)
    try:
        auth_service.assert_auth_allowed(payload.username, source)
    except AuthRateLimitError as exc:
        raise HTTPException(status_code=429, detail=str(exc), headers={"Retry-After": str(exc.retry_after_seconds)}) from exc
    user = auth_service.authenticate(payload.username, payload.password)
    if user is None:
        auth_service.record_auth_failure(payload.username, source)
        raise HTTPException(status_code=401, detail="帳號或密碼錯誤")
    auth_service.clear_auth_failures(payload.username, source)
    token = auth_service.create_session(user.id)
    _set_session_cookie(response, token)
    holdings = auth_service.list_holdings(user.id)
    return {"authenticated": True, "user": user.public_dict(), "holdings": [h.model_dump() for h in holdings]}


@app.post("/api/auth/logout")
def logout(request: Request, response: Response) -> dict:
    auth_service.delete_session(request.cookies.get(SESSION_COOKIE_NAME))
    _clear_session_cookie(response)
    return {"authenticated": False}


@app.get("/api/auth/me")
def auth_me(request: Request) -> dict:
    user = _current_user(request)
    if user is None:
        return {"authenticated": False, "user": None, "holdings": []}
    holdings = auth_service.list_holdings(user.id)
    return {"authenticated": True, "user": user.public_dict(), "holdings": [h.model_dump() for h in holdings]}


@app.get("/api/admin/users")
def list_admin_users(request: Request) -> dict:
    _require_super_user(request)
    return {"superUser": super_user_username(), "users": auth_service.list_users()}


@app.delete("/api/admin/users/{user_id}")
def delete_admin_user(user_id: int, request: Request) -> dict:
    _require_super_user(request)
    try:
        deleted = auth_service.delete_user(user_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not deleted:
        raise HTTPException(status_code=404, detail="User not found")
    return {"superUser": super_user_username(), "users": auth_service.list_users()}


@app.get("/api/me/holdings")
def get_my_holdings(request: Request) -> dict:
    user = _require_user(request)
    return _holdings_payload(auth_service.list_holdings(user.id))


@app.put("/api/me/holdings")
def replace_my_holdings(payload: HoldingsReplaceRequest, request: Request) -> dict:
    user = _require_user(request)
    return _holdings_payload(auth_service.replace_holdings(user.id, payload.holdings))


@app.post("/api/me/holdings")
def upsert_my_holding(payload: HoldingUpsertRequest, request: Request) -> dict:
    user = _require_user(request)
    return _holdings_payload(auth_service.upsert_holding(user.id, payload.holding))


@app.delete("/api/me/holdings/{stock_code}")
def delete_my_holding(stock_code: str, request: Request) -> dict:
    user = _require_user(request)
    return _holdings_payload(auth_service.delete_holding(user.id, stock_code))


@app.get("/api/data-sources/status")
def data_sources_status(check_network: bool = False) -> dict:
    settings = load_settings()
    payload = data_sources_status_payload(
        settings,
        official_provider,
        mock_universe_size=len(mock_provider.list_companies()),
        scan_cache_status=scan_cache_service.status(settings if not settings.use_mock_data else None),
    )
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
def scheduler_wakeup(today: date | None = None) -> dict:
    return should_wake_up(today).__dict__


@app.get("/api/scheduler/auto-scan")
def scheduler_auto_scan(today: date | None = None, execute: bool = True, backfill_history: bool = False) -> dict:
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
def list_companies(page: int = Query(default=1, ge=1), limit: int = Query(default=100, ge=1, le=500)) -> dict:
    provider = _active_provider(load_settings())
    return _paginated_items(provider.list_companies(), page, limit)


@app.post("/api/analyze/{stock_code}")
def analyze_stock(stock_code: str, payload: AnalyzeRequest | None = Body(default=None)):
    settings = _effective_settings(payload.settings if payload else None)
    provider = _active_provider(settings)
    snapshot = provider.get_snapshot(stock_code)
    if snapshot is None:
        raise HTTPException(status_code=404, detail="找不到股票或目前資料源缺少可分析財報資料")
    return engine.evaluate_entry(snapshot, settings)


@app.post("/api/scan/market")
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


@app.get("/api/cache/status")
def cache_status() -> dict:
    settings = load_settings()
    return {
        "marketScan": scan_cache_service.status(settings if not settings.use_mock_data else None),
        "officialHistory": official_provider.history_store.status(),
    }


@app.post("/api/scan/holdings")
def scan_holdings(request: Request, payload: ScanHoldingsRequest) -> dict:
    settings = _effective_settings(payload.settings)
    _ensure_manual_scan_enabled(settings)
    if not settings.use_mock_data:
        _check_scan_rate_limit(_auth_source(request))
    return _scan_holdings_payload(payload.holdings, settings)


@app.post("/api/reports/market")
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


@app.post("/api/reports/holdings")
def holdings_report(report_format: str = "markdown", payload: ScanHoldingsRequest | None = Body(default=None)):
    settings = _effective_settings(payload.settings if payload else None)
    scan_payload = _scan_holdings_payload(payload.holdings if payload else [], settings)
    return _report_response(scan_payload, report_format, "台股持股追蹤報告", "holdings_scan")


@app.get("/api/integrations/status")
def integrations_status() -> dict:
    return integration_status()


@app.get("/api/backtest")
def backtest_status() -> dict:
    return _backtest_status_cached()


@app.get("/api/app-status")
def app_status(today: date | None = None) -> dict:
    settings = load_settings()
    official_status = official_provider.status(refresh=False) if not settings.use_mock_data else None
    data_source_payload = {
        "activeProvider": "MockDataProvider" if settings.use_mock_data else "OfficialDataProvider",
        "mockDataAvailable": True,
        "officialDataAvailable": official_status is not None,
    }
    scheduler_payload = should_wake_up(today).__dict__
    auto_scan_payload = {
        "action": "ready",
        "autoScanEnabled": settings.auto_scan_full_market,
        "manualScanEnabled": settings.manual_scan_enabled,
        "scan": None,
    }
    return {
        "dataSourceStatus": data_source_payload,
        "schedulerStatus": scheduler_payload,
        "schedulerAutoScan": auto_scan_payload,
        "integrationStatus": integration_status(),
        "backtestStatus": _backtest_status_cached(),
    }


@app.get("/api/settings")
def get_settings() -> ScannerSettings:
    return load_settings()


@app.put("/api/settings")
def put_settings(settings: ScannerSettings, request: Request) -> ScannerSettings:
    _require_super_user(request)
    return save_settings(settings)


if FRONTEND_DIR.exists():
    app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
