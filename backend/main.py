from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from backend.dependencies import _cookie_secure, _env_flag, _is_production_environment
from backend.routers import accounts, market, system
from backend.services.auth import super_user_username
from cloudflare.contract import (
    CSRF_HEADER_NAME,
    CSRF_HEADER_VALUE,
    UNSAFE_API_METHODS,
    is_https_origin,
    is_local_cors_origin,
)

ROOT_DIR = Path(__file__).resolve().parents[1]
FRONTEND_DIR = ROOT_DIR / "frontend"
DEFAULT_CORS_ALLOW_ORIGINS = ("http://localhost", "http://localhost:8000", "http://127.0.0.1:8000")


def _csv_env(name: str, default: tuple[str, ...] = ()) -> list[str]:
    raw = os.getenv(name)
    if raw is None:
        return list(default)
    return [item.strip() for item in raw.split(",") if item.strip()]


def _cors_allowed_origins() -> list[str]:
    return _csv_env("APP_CORS_ALLOW_ORIGINS", DEFAULT_CORS_ALLOW_ORIGINS)


def _validate_runtime_security(cors_origins: list[str]) -> None:
    if not _is_production_environment():
        return
    if not _cookie_secure():
        raise RuntimeError("SESSION_COOKIE_SECURE must be enabled when APP_ENV=production")
    local_origins = sorted(origin for origin in cors_origins if is_local_cors_origin(origin))
    if local_origins and not _env_flag("APP_ALLOW_LOCAL_CORS_IN_PRODUCTION"):
        raise RuntimeError(
            "APP_CORS_ALLOW_ORIGINS must not include localhost origins in production: " + ", ".join(local_origins)
        )
    insecure_origins = sorted(origin for origin in cors_origins if not is_https_origin(origin))
    if insecure_origins and not _env_flag("APP_ALLOW_INSECURE_CORS_IN_PRODUCTION"):
        raise RuntimeError(
            "APP_CORS_ALLOW_ORIGINS must use https origins in production: " + ", ".join(insecure_origins)
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


app.include_router(accounts.router)
app.include_router(market.router)
app.include_router(system.router)

if FRONTEND_DIR.exists():
    app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
