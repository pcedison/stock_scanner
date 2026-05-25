from __future__ import annotations

import asyncio
import csv
import hashlib
import hmac
import io
import json
import re
import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

from js import Object, Response
from pyodide.ffi import to_js


SESSION_COOKIE_NAME = "stock_scanner_session"
SESSION_MAX_AGE_SECONDS = 60 * 60 * 24 * 30
PASSWORD_ALGORITHM = "pbkdf2_sha256"
PASSWORD_ITERATIONS = 210_000
USERNAME_PATTERN = re.compile(r"^[^\s<>\"'`;]{3,80}$")
TAIPEI_TZ = timezone(timedelta(hours=8))
_LOCALHOST_ORIGIN_RE = re.compile(r"^http://(localhost|127\.0\.0\.1):\d{1,5}$")
LOCAL_CORS_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
DEFAULT_DEVELOPMENT_CORS_ALLOW_ORIGINS = ("http://localhost:8000", "http://127.0.0.1:8000")
AUTH_FAILURE_LIMIT = 5
AUTH_FAILURE_WINDOW_SECONDS = 15 * 60
AUTH_LOCK_SECONDS = 15 * 60
REVENUE_GROWTH_MODES = frozenset({"cumulative_ytd", "monthly", "trailing_3m_avg"})
CSRF_HEADER_NAME = "x-stock-scanner-csrf"
CSRF_HEADER_VALUE = "1"
UNSAFE_API_METHODS = frozenset({"POST", "PUT", "DELETE"})

DEFAULT_SETTINGS = {
    "auto_scan_full_market": True,
    "manual_scan_enabled": True,
    "exclude_financial_industry": True,
    "use_mock_data": False,
    "scan_twse": True,
    "scan_tpex": True,
    "spring_festival_guard": True,
    "revenue_growth_mode": "cumulative_ytd",
}
MIN_CACHE_COMPANIES = 1000
MIN_CACHE_ANALYSIS = 1000
HOLDING_EXIT_CODES = ("X1", "X2", "X3", "X4", "X5")

SECURITY_HEADERS = {
    "strict-transport-security": "max-age=31536000; includeSubDomains; preload",
    "content-security-policy": (
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
    "x-frame-options": "DENY",
    "x-content-type-options": "nosniff",
    "referrer-policy": "strict-origin-when-cross-origin",
    "permissions-policy": "camera=(), microphone=(), geolocation=(), payment=(), usb=()",
}


class ForbiddenError(Exception):
    pass


class BadRequestError(Exception):
    pass


class NotFoundError(Exception):
    pass


class ValidationError(Exception):
    pass


class RateLimitError(Exception):
    def __init__(self, retry_after_seconds: int):
        self.retry_after_seconds = retry_after_seconds
        super().__init__("登入嘗試過多，請稍後再試。")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_time(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None


def env_value(env, name: str, default=None):
    if env is None:
        return default
    if isinstance(env, dict):
        value = env.get(name, default)
    else:
        value = getattr(env, name, default)
    return js_to_py(value)


def env_flag(env, name: str) -> bool:
    return str(env_value(env, name, "") or "").strip().lower() in {"1", "true", "yes", "on"}


def csv_env_value(env, name: str) -> list[str]:
    raw = env_value(env, name, "")
    if isinstance(raw, list):
        return [str(item).strip() for item in raw if str(item).strip()]
    return [item.strip() for item in str(raw or "").split(",") if item.strip()]


def runtime_environment(env) -> str:
    value = env_value(env, "APP_ENV", env_value(env, "ENVIRONMENT", "development"))
    return str(value or "development").strip().lower() or "development"


def is_production_environment(env) -> bool:
    return runtime_environment(env) in {"prod", "production"}


def configured_super_user_username(env) -> str:
    return str(env_value(env, "SUPER_USER_USERNAME", "") or "").strip().lower()


def validate_runtime_security(env, super_user: str) -> None:
    if is_production_environment(env) and not super_user:
        raise RuntimeError("SUPER_USER_USERNAME must be configured when APP_ENV=production")


def origin_host(origin: str) -> str:
    return (urlparse(str(origin or "")).hostname or "").lower()


def is_local_cors_origin(origin: str) -> bool:
    return origin_host(origin) in LOCAL_CORS_HOSTS


def is_https_origin(origin: str) -> bool:
    return urlparse(str(origin or "")).scheme.lower() == "https"


def cache_key_from_manifest(manifest):
    seed = json.dumps(
        {
            "generatedAt": manifest.get("generatedAt"),
            "counts": manifest.get("counts", {}),
            "latestRevenuePeriod": manifest.get("latestRevenuePeriod"),
            "latestFinancialPeriod": manifest.get("latestFinancialPeriod"),
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]


def json_response(payload, status=200, headers=None, public_cache_seconds=0):
    cache_control = (
        f"public, s-maxage={public_cache_seconds}, stale-while-revalidate=60"
        if public_cache_seconds > 0
        else "no-store"
    )
    response_headers = {
        "content-type": "application/json; charset=utf-8",
        "cache-control": cache_control,
        **SECURITY_HEADERS,
    }
    if headers:
        response_headers.update(headers)
    return Response.new(
        json.dumps(payload, ensure_ascii=False),
        to_js({"status": status, "headers": response_headers}, dict_converter=Object.fromEntries),
    )


def text_response(content, status=200, media_type="text/plain; charset=utf-8", headers=None):
    response_headers = {"content-type": media_type, "cache-control": "no-store", **SECURITY_HEADERS}
    if headers:
        response_headers.update(headers)
    return Response.new(content, to_js({"status": status, "headers": response_headers}, dict_converter=Object.fromEntries))


def error_response(detail, status=400, headers=None):
    return json_response({"detail": detail}, status=status, headers=headers)


def manifest_quality(manifest):
    counts = manifest.get("counts") if isinstance(manifest, dict) else {}
    counts = counts if isinstance(counts, dict) else {}
    companies = int(counts.get("companies") or 0)
    analysis = int(counts.get("analysis") or 0)
    universe = sum(int(counts.get(key) or 0) for key in ("entry", "watch", "excluded"))
    problems = []
    if companies < MIN_CACHE_COMPANIES:
        problems.append(f"companies below {MIN_CACHE_COMPANIES}")
    if analysis < MIN_CACHE_ANALYSIS:
        problems.append(f"analysis below {MIN_CACHE_ANALYSIS}")
    if universe < MIN_CACHE_ANALYSIS:
        problems.append(f"universe below {MIN_CACHE_ANALYSIS}")
    return {
        "ok": not problems,
        "problems": problems,
        "counts": {"companies": companies, "analysis": analysis, "universe": universe},
        "minimums": {"companies": MIN_CACHE_COMPANIES, "analysis": MIN_CACHE_ANALYSIS, "universe": MIN_CACHE_ANALYSIS},
    }


def empty_backtest_status():
    return {
        "status": "NO_DATA",
        "sourcePath": "cloudflare-cache",
        "trades": [],
        "metrics": {"tradeCount": 0, "winRate": None, "totalReturn": None, "maxDrawdown": None},
        "note": "Backtest history is not bundled with the Cloudflare deployment.",
    }


def holding_note(holding: dict) -> dict:
    average_cost = holding.get("averageCost")
    cost_text = average_cost if average_cost is not None else "未填"
    return {
        "code": "HOLDING",
        "title": "目前持股",
        "passed": True,
        "severity": "INFO",
        "message": f"Cloudflare D1 / 本機同步持股 {holding.get('shares', 0)} 股，平均成本 {cost_text}。",
    }


def missing_exit_rule(code: str) -> dict:
    titles = {
        "X1": "月營收年增率不可低於 30%",
        "X2": "月營收年增率不可突然降溫超過 20 個百分點",
        "X3": "EPS 不可衰退",
        "X4": "季度 EPS 不可減少超過 10%",
        "X5": "淨利不可衰退",
    }
    return {
        "code": code,
        "title": titles[code],
        "passed": False,
        "severity": "INSUFFICIENT_DATA",
        "message": "Cloudflare 快取尚未包含此持股出場規則，等待下一次官方種子刷新。",
    }


def has_holding_exit_rules(result: dict) -> bool:
    reasons = result.get("reasons") if isinstance(result, dict) else None
    if not isinstance(reasons, list):
        return False
    codes = {str(reason.get("code", "")).upper() for reason in reasons if isinstance(reason, dict)}
    return any(code in codes for code in HOLDING_EXIT_CODES)


def prepare_holding_result(result: dict, holding: dict) -> dict:
    copied = dict(result)
    reasons = [
        reason
        for reason in copied.get("reasons", [])
        if isinstance(reason, dict) and str(reason.get("code", "")).upper() != "HOLDING"
    ]
    if not has_holding_exit_rules(copied):
        copied["status"] = "INSUFFICIENT_DATA"
        copied["summary"] = "Cloudflare 快取尚未包含 X1-X5 持股出場分析，等待下一次官方種子刷新。"
        reasons = [*reasons, *[missing_exit_rule(code) for code in HOLDING_EXIT_CODES]]
    copied["reasons"] = [*reasons, holding_note(holding)]
    return copied


def settings_from_payload(payload, strict=False):
    if not isinstance(payload, dict):
        if strict:
            raise ValidationError("設定內容需為物件")
        payload = {}
    settings = {}
    for key, default in DEFAULT_SETTINGS.items():
        value = payload.get(key, default)
        if key == "revenue_growth_mode":
            if value in REVENUE_GROWTH_MODES:
                settings[key] = value
            elif strict:
                raise ValidationError("revenue_growth_mode 值不正確")
            else:
                settings[key] = default
            continue
        if isinstance(value, bool):
            settings[key] = value
        elif strict:
            raise ValidationError(f"{key} 必須為布林值")
        else:
            settings[key] = default
    return settings


def normalize_username(username: str) -> str:
    normalized = str(username or "").strip().lower()
    if not USERNAME_PATTERN.fullmatch(normalized):
        raise ValueError("帳號需為 3-80 字元，且不可包含空白或 < > \" ' ` ;")
    return normalized


def validate_password(password: str) -> None:
    if len(password or "") < 8:
        raise ValueError("密碼至少需要 8 個字元")
    if len(password) > 128:
        raise ValueError("密碼不可超過 128 個字元")


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PASSWORD_ITERATIONS)
    return f"{PASSWORD_ALGORITHM}${PASSWORD_ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored_hash: str) -> bool:
    try:
        algorithm, iterations, salt_hex, digest_hex = stored_hash.split("$", 3)
        if algorithm != PASSWORD_ALGORITHM:
            return False
        digest = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            bytes.fromhex(salt_hex),
            int(iterations),
        )
        return hmac.compare_digest(digest.hex(), digest_hex)
    except Exception:
        return False


def parse_cookies(cookie_header: str | None) -> dict[str, str]:
    cookies = {}
    for part in str(cookie_header or "").split(";"):
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        cookies[key.strip()] = value.strip()
    return cookies


def session_cookie(token: str) -> str:
    return (
        f"{SESSION_COOKIE_NAME}={token}; Max-Age={SESSION_MAX_AGE_SECONDS}; "
        "Path=/; HttpOnly; SameSite=Lax; Secure"
    )


def clear_session_cookie() -> str:
    return f"{SESSION_COOKIE_NAME}=; Max-Age=0; Path=/; HttpOnly; SameSite=Lax; Secure"


def js_to_py(value):
    if value is None:
        return None
    if type(value).__name__ in {"JsNull", "JsUndefined"}:
        return None
    if hasattr(value, "to_py"):
        return value.to_py()
    if isinstance(value, list):
        return [js_to_py(item) for item in value]
    if isinstance(value, dict):
        return {key: js_to_py(item) for key, item in value.items()}
    return value


def public_user(row, super_user: str | None = None):
    if not row:
        return None
    resolved_super_user = super_user or ""
    return {
        "id": row["id"],
        "username": row["username"],
        "displayName": row.get("display_name") or row["username"],
        "isSuperUser": bool(resolved_super_user and str(row["username"]).lower() == resolved_super_user),
    }


def normalize_holding(payload: dict) -> dict:
    stock_code = str(payload.get("stockCode") or payload.get("stock_code") or "").strip()
    if not re.fullmatch(r"\d{4,6}", stock_code):
        raise ValueError("股票代號格式不正確")
    shares = int(payload.get("shares") or 0)
    if shares < 0:
        raise ValueError("股數不可小於 0")
    average_cost = payload.get("averageCost", payload.get("average_cost"))
    parsed_cost = float(average_cost) if average_cost not in (None, "") else None
    if parsed_cost is not None and parsed_cost < 0:
        raise ValueError("平均成本不可小於 0")
    return {
        "stockCode": stock_code,
        "name": payload.get("name"),
        "shares": shares,
        "averageCost": parsed_cost,
    }


async def on_fetch(request, env):
    return await Api(env).fetch(request)


def set_response_header(response, key: str, value: str) -> None:
    headers = getattr(response, "headers", None)
    if hasattr(headers, "set"):
        headers.set(key, value)
    elif isinstance(headers, dict):
        headers[key] = value


class Api:
    def __init__(self, env):
        self.env = env
        self._r2_cache: dict = {}
        self._super_user = configured_super_user_username(env)
        validate_runtime_security(env, self._super_user)
        self._cors_allowed_origins = self.cors_allowed_origins()

    async def fetch(self, request):
        if request.method == "OPTIONS":
            return Response.new(
                "",
                to_js({"status": 204, "headers": {**SECURITY_HEADERS, **self.cors_headers(request)}}, dict_converter=Object.fromEntries),
            )

        parsed = urlparse(request.url)
        path = parsed.path.rstrip("/") or "/"
        query = parse_qs(parsed.query)

        try:
            if self.requires_csrf_header(request, path) and not self.has_valid_csrf_header(request):
                response = error_response("CSRF header required", status=403)
            else:
                response = await self.route(request, path, query)
        except BadRequestError as exc:
            response = error_response(str(exc), status=400)
        except NotFoundError as exc:
            response = error_response(str(exc), status=404)
        except ValidationError as exc:
            response = error_response(str(exc), status=422)
        except PermissionError as exc:
            response = error_response(str(exc), status=401)
        except ForbiddenError as exc:
            response = error_response(str(exc), status=403)
        except RateLimitError as exc:
            response = error_response(str(exc), status=429, headers={"retry-after": str(exc.retry_after_seconds)})
        except Exception as exc:
            print(f"Cloudflare Worker API error: {exc}")
            response = error_response("伺服器暫時無法處理請求，請稍後再試。", status=500)

        for key, value in {**SECURITY_HEADERS, **self.cors_headers(request)}.items():
            set_response_header(response, key, value)
        return response

    def requires_csrf_header(self, request, path: str) -> bool:
        return (
            is_production_environment(self.env)
            and str(getattr(request, "method", "")).upper() in UNSAFE_API_METHODS
            and path.startswith("/api/")
        )

    def has_valid_csrf_header(self, request) -> bool:
        return str(request.headers.get(CSRF_HEADER_NAME) or "") == CSRF_HEADER_VALUE

    def cors_allowed_origins(self):
        configured = (
            csv_env_value(self.env, "APP_CORS_ALLOW_ORIGINS")
            or csv_env_value(self.env, "WORKER_CORS_ALLOW_ORIGINS")
        )
        production = is_production_environment(self.env)
        allowed = list(dict.fromkeys(configured))
        if not production:
            allowed.extend(origin for origin in DEFAULT_DEVELOPMENT_CORS_ALLOW_ORIGINS if origin not in allowed)

        if production and not env_flag(self.env, "APP_ALLOW_LOCAL_CORS_IN_PRODUCTION"):
            allowed = [origin for origin in allowed if not is_local_cors_origin(origin)]
        if production and not env_flag(self.env, "APP_ALLOW_INSECURE_CORS_IN_PRODUCTION"):
            allowed = [origin for origin in allowed if is_https_origin(origin)]
        return tuple(allowed)

    def cors_headers(self, request=None):
        allowed_origins = self._cors_allowed_origins
        origin = allowed_origins[0] if allowed_origins else "null"
        if request is not None:
            req_origin = str(request.headers.get("origin") or "")
            if req_origin in allowed_origins or (not is_production_environment(self.env) and _LOCALHOST_ORIGIN_RE.match(req_origin)):
                origin = req_origin
        return {
            "access-control-allow-origin": origin,
            "access-control-allow-methods": "GET,POST,PUT,DELETE,OPTIONS",
            "access-control-allow-headers": f"content-type,{CSRF_HEADER_NAME}",
            "access-control-allow-credentials": "true",
            "vary": "Origin",
        }

    async def route(self, request, path: str, query: dict[str, list[str]]):
        method = request.method.upper()

        if path.startswith("/api/auth/"):
            return await self._route_auth(request, method, path)
        if path.startswith("/api/me/"):
            return await self._route_me(request, method, path)
        if path.startswith("/api/admin/"):
            return await self._route_admin(request, method, path)
        if path.startswith("/api/scan/") or path.startswith("/api/analyze/"):
            return await self._route_scan(request, method, path)
        if path.startswith("/api/reports/"):
            return await self._route_reports(request, method, path, query)
        if path.startswith("/api/cache/"):
            return await self._route_cache(request, method, path)
        if path.startswith("/api/scheduler/"):
            return await self._route_scheduler(method, path)
        if path.startswith("/api/data-sources/"):
            return await self._route_data_sources(method, path)
        if path.startswith("/api/calendar/"):
            return await self._route_calendar(method, path)
        if path.startswith("/api/companies"):
            return await self._route_companies(method, path, query)

        if path == "/api/health" and method == "GET":
            manifest = await self.r2_json("public/manifest.json", {})
            quality = manifest_quality(manifest)
            return json_response({
                "status": "ok" if quality["ok"] else "degraded",
                "runtime": "cloudflare-python-worker",
                "time": utc_now(),
                "cache": manifest,
                "cacheQuality": quality,
            }, public_cache_seconds=60)

        if path == "/api/app-status" and method == "GET":
            data_source, settings = await asyncio.gather(
                self.r2_json("public/data_sources_status.json", {"activeProvider": "CloudflareR2Seed"}),
                self.get_settings(),
            )
            return json_response({
                "dataSourceStatus": data_source,
                "schedulerStatus": {"status": "SLEEP", "reason": "Cloudflare deployment uses cached official data and scheduled increments."},
                "schedulerAutoScan": {"action": "ready", "autoScanEnabled": settings.get("auto_scan_full_market", True), "manualScanEnabled": settings.get("manual_scan_enabled", True), "scan": None},
                "integrationStatus": {"line": False, "telegram": False, "email": False, "broker": False},
                "backtestStatus": empty_backtest_status(),
            })

        if path == "/api/settings" and method == "GET":
            return json_response(await self.get_settings())

        if path == "/api/settings" and method == "PUT":
            await self.require_super_user(request)
            payload = await self.request_json(request)
            return json_response(await self.put_settings(payload))

        if path == "/api/integrations/status" and method == "GET":
            return json_response({"line": False, "telegram": False, "email": False, "broker": False}, public_cache_seconds=600)

        if path == "/api/backtest" and method == "GET":
            return json_response(empty_backtest_status(), public_cache_seconds=3600)

        return error_response("Not found", status=404)

    async def _route_auth(self, request, method: str, path: str):
        if path == "/api/auth/register" and method == "POST":
            return await self.register(request)
        if path == "/api/auth/login" and method == "POST":
            return await self.login(request)
        if path == "/api/auth/logout" and method == "POST":
            token = self.session_token(request)
            if token:
                await self.db_run("DELETE FROM sessions WHERE token_hash = ?", token_hash(token))
            return json_response({"authenticated": False}, headers={"set-cookie": clear_session_cookie()})
        if path == "/api/auth/me" and method == "GET":
            user = await self.current_user(request)
            holdings = await self.list_holdings(user["id"]) if user else []
            return json_response({"authenticated": bool(user), "user": public_user(user, self._super_user), "holdings": holdings})
        return error_response("Not found", status=404)

    async def _route_me(self, request, method: str, path: str):
        if path == "/api/me/holdings" and method == "GET":
            user = await self.require_user(request)
            return json_response({"holdings": await self.list_holdings(user["id"])})
        if path == "/api/me/holdings" and method == "PUT":
            user = await self.require_user(request)
            payload = await self.request_json(request)
            holdings = [normalize_holding(item) for item in payload.get("holdings", [])]
            return json_response({"holdings": await self.replace_holdings(user["id"], holdings)})
        if path == "/api/me/holdings" and method == "POST":
            user = await self.require_user(request)
            payload = await self.request_json(request)
            holding = normalize_holding(payload.get("holding", payload))
            await self.upsert_holding(user["id"], holding)
            return json_response({"holdings": await self.list_holdings(user["id"])})
        if path.startswith("/api/me/holdings/") and method == "DELETE":
            user = await self.require_user(request)
            stock_code = path.rsplit("/", 1)[-1]
            await self.db_run("DELETE FROM holdings WHERE user_id = ? AND stock_code = ?", user["id"], stock_code)
            return json_response({"holdings": await self.list_holdings(user["id"])})
        return error_response("Not found", status=404)

    async def _route_admin(self, request, method: str, path: str):
        if path == "/api/admin/users" and method == "GET":
            await self.require_super_user(request)
            return json_response(await self.admin_users_payload())
        if path.startswith("/api/admin/users/") and method == "DELETE":
            await self.require_super_user(request)
            user_id = int(path.rsplit("/", 1)[-1])
            return json_response(await self.delete_admin_user(user_id))
        return error_response("Not found", status=404)

    async def _route_scan(self, request, method: str, path: str):
        if path == "/api/scan/market" and method == "POST":
            payload = await self.request_json(request)
            refresh_mode = str(payload.get("refreshMode") or "auto").strip().lower()
            manifest = await self.r2_json("public/manifest.json", {})
            scan = await self.r2_json("public/market_scan_summary.json", None)
            refresh_status = await self.ensure_refresh_job(manifest, force=refresh_mode == "force")
            if not isinstance(scan, dict):
                scan = self.empty_market_scan()
            scan = self.compact_market_scan(scan)
            scan["cacheStatus"] = self.cache_status_from_manifest(manifest, refresh_status)
            return json_response(scan)
        if path == "/api/scan/holdings" and method == "POST":
            return await self.scan_holdings(request)
        if path.startswith("/api/analyze/") and method == "POST":
            stock_code = path.rsplit("/", 1)[-1]
            return await self.analyze_stock(stock_code)
        return error_response("Not found", status=404)

    async def _route_reports(self, request, method: str, path: str, query):
        if path == "/api/reports/market" and method == "POST":
            return await self.market_report(query)
        if path == "/api/reports/holdings" and method == "POST":
            return await self.holdings_report(request, query)
        return error_response("Not found", status=404)

    async def _route_cache(self, request, method: str, path: str):
        if path == "/api/cache/status" and method == "GET":
            return json_response(await self.cache_status())
        if path == "/api/cache/refresh" and method == "POST":
            manifest = await self.r2_json("public/manifest.json", {})
            return json_response(await self.ensure_refresh_job(manifest, force=True))
        return error_response("Not found", status=404)

    async def _route_scheduler(self, method: str, path: str):
        if path == "/api/scheduler/wakeup" and method == "GET":
            return json_response({"status": "SLEEP", "reason": "Cloudflare deployment uses cached official data and scheduled increments."})
        if path == "/api/scheduler/auto-scan" and method == "GET":
            settings = await self.get_settings()
            return json_response({
                "action": "ready",
                "autoScanEnabled": settings.get("auto_scan_full_market", True),
                "manualScanEnabled": settings.get("manual_scan_enabled", True),
                "scan": None,
            })
        return error_response("Not found", status=404)

    async def _route_data_sources(self, method: str, path: str):
        if path == "/api/data-sources/status" and method == "GET":
            return json_response(await self.r2_json("public/data_sources_status.json", {"activeProvider": "CloudflareR2Seed"}), public_cache_seconds=300)
        if path == "/api/data-sources/backfill-history" and method == "POST":
            progress = await self.r2_json("official/official_history_backfill_progress.json", {})
            return json_response({
                "status": "cloudflare_cache_seeded",
                "mode": "incremental backfill is prepared for scheduled implementation",
                "progress": progress,
            })
        return error_response("Not found", status=404)

    async def _route_calendar(self, method: str, path: str):
        if path.startswith("/api/calendar/") and method == "GET":
            year = path.rsplit("/", 1)[-1]
            return json_response({"year": int(year), "source": "cloudflare-cache", "closedDates": [], "springFestivalDates": []})
        return error_response("Not found", status=404)

    async def _route_companies(self, method: str, path: str, query):
        if path == "/api/companies" and method == "GET":
            return await self.list_companies(query)
        if path == "/api/companies/search" and method == "GET":
            return await self.search_companies(query)
        return error_response("Not found", status=404)

    async def request_json(self, request):
        text = await request.text()
        if not str(text or "").strip():
            return {}
        try:
            payload = json.loads(str(text))
        except Exception as exc:
            raise BadRequestError("JSON 格式錯誤") from exc
        if not isinstance(payload, dict):
            raise BadRequestError("JSON 內容需為物件")
        return payload

    async def r2_json(self, key: str, fallback):
        if key in self._r2_cache:
            return self._r2_cache[key]
        obj = js_to_py(await self.env.CACHE.get(key))
        if obj is None:
            self._r2_cache[key] = fallback
            return fallback
        try:
            result = json.loads(await obj.text())
        except Exception as exc:
            print(f"R2 JSON parse error for key={key}: {exc}")
            result = fallback
        self._r2_cache[key] = result
        return result

    def cache_policy(self):
        now = datetime.now(TAIPEI_TZ)
        financial_deadlines = {(3, 31), (5, 15), (5, 30), (8, 31), (11, 14)}
        in_financial_window = any(
            month == now.month and abs((now.date() - now.replace(month=month, day=day).date()).days) <= 3
            for month, day in financial_deadlines
        )
        if in_financial_window:
            return {"strategy": "stale_while_revalidate", "reason": "financial_report_window", "minIntervalSeconds": 7200}
        if 8 <= now.day <= 15:
            return {"strategy": "stale_while_revalidate", "reason": "monthly_revenue_window", "minIntervalSeconds": 10800}
        return {"strategy": "stale_while_revalidate", "reason": "routine_refresh", "minIntervalSeconds": 43200}

    def cache_status_from_manifest(self, manifest, refresh_status):
        policy = self.cache_policy()
        generated_at = manifest.get("generatedAt")
        generated_time = parse_time(generated_at)
        next_refresh = None
        is_stale = True
        if generated_time:
            next_refresh_time = generated_time.astimezone(timezone.utc) + timedelta(seconds=policy["minIntervalSeconds"])
            next_refresh = next_refresh_time.isoformat()
            is_stale = datetime.now(timezone.utc) >= next_refresh_time
        return {
            "strategy": "stale_while_revalidate",
            "source": "cloudflare_r2",
            "cacheKey": cache_key_from_manifest(manifest),
            "cacheHit": True,
            "storedAt": generated_at,
            "servedAt": utc_now(),
            "isStale": is_stale,
            "refreshStatus": refresh_status.get("status", "unknown"),
            "refreshReason": refresh_status.get("reason", policy["reason"]),
            "nextRefreshAfter": next_refresh,
            "latestRevenuePeriod": manifest.get("latestRevenuePeriod"),
            "latestFinancialPeriod": manifest.get("latestFinancialPeriod"),
            "quality": manifest_quality(manifest),
        }

    async def ensure_refresh_job(self, manifest, force=False):
        policy = self.cache_policy()
        status = self.cache_status_from_manifest(manifest, {"status": "checking", "reason": policy["reason"]})
        if not force and not status["isStale"]:
            return {"status": "fresh", "reason": policy["reason"]}
        cache_key = status["cacheKey"]
        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
        existing = await self.db_first(
            """
            SELECT id, status, reason, queued_at, owner_run_id
            FROM refresh_jobs
            WHERE job_type = ? AND status IN ('queued', 'running') AND queued_at > ?
            ORDER BY queued_at DESC
            LIMIT 1
            """,
            "market_scan",
            cutoff,
        )
        if existing:
            return {
                "status": existing["status"],
                "reason": existing["reason"],
                "jobId": existing["id"],
                "queuedAt": existing["queued_at"],
                "ownerRunId": existing.get("owner_run_id"),
                "ownerRunUrl": self.github_actions_run_url(existing.get("owner_run_id")),
            }
        job_id = secrets.token_hex(16)
        now = utc_now()
        stale_cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
        await self.db_run(
            "DELETE FROM refresh_jobs WHERE queued_at < ?",
            stale_cutoff,
        )
        await self.db_run(
            """
            INSERT INTO refresh_jobs (id, job_type, cache_key, status, reason, queued_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            job_id,
            "market_scan",
            cache_key,
            "queued",
            policy["reason"],
            now,
            now,
        )
        return {"status": "queued", "reason": policy["reason"], "jobId": job_id, "queuedAt": now}

    def github_actions_run_url(self, owner_run_id):
        if not owner_run_id:
            return None
        repository = str(env_value(self.env, "GITHUB_REPOSITORY", "") or "").strip()
        if not repository or "/" not in repository:
            return None
        return f"https://github.com/{repository}/actions/runs/{owner_run_id}"

    def refresh_job_payload(self, row):
        payload = dict(row)
        owner_run_id = payload.get("owner_run_id")
        payload["hasError"] = payload.pop("error", None) is not None
        payload["ownerRunId"] = owner_run_id
        payload["ownerRunUrl"] = self.github_actions_run_url(owner_run_id)
        return payload

    async def cache_status(self):
        manifest = await self.r2_json("public/manifest.json", {})
        jobs = await self.db_all(
            """
            SELECT id, job_type, cache_key, status, reason, queued_at, started_at, finished_at, updated_at, error, owner_run_id
            FROM refresh_jobs
            ORDER BY queued_at DESC
            LIMIT 10
            """
        )
        return {
            "marketScan": self.cache_status_from_manifest(manifest, {"status": "not_requested"}),
            "manifest": manifest,
            "recentJobs": [self.refresh_job_payload(job) for job in jobs],
        }

    async def db_run(self, sql: str, *params):
        statement = self.env.DB.prepare(sql)
        if params:
            statement = statement.bind(*params)
        return js_to_py(await statement.run())

    async def db_first(self, sql: str, *params):
        statement = self.env.DB.prepare(sql)
        if params:
            statement = statement.bind(*params)
        return js_to_py(await statement.first())

    async def db_all(self, sql: str, *params):
        statement = self.env.DB.prepare(sql)
        if params:
            statement = statement.bind(*params)
        result = js_to_py(await statement.all())
        if isinstance(result, dict):
            return result.get("results", [])
        return result or []

    def session_token(self, request):
        return parse_cookies(request.headers.get("cookie")).get(SESSION_COOKIE_NAME)

    async def current_user(self, request):
        token = self.session_token(request)
        if not token:
            return None
        return await self.db_first(
            """
            SELECT users.id, users.username, users.display_name
            FROM sessions
            JOIN users ON users.id = sessions.user_id
            WHERE sessions.token_hash = ? AND sessions.expires_at > ?
            """,
            token_hash(token),
            utc_now(),
        )

    async def require_user(self, request):
        user = await self.current_user(request)
        if not user:
            raise PermissionError("請先登入")
        return user

    async def require_super_user(self, request):
        user = await self.require_user(request)
        if str(user.get("username", "")).lower() != self._super_user:
            raise ForbiddenError("Only the super user can manage users")
        return user

    async def admin_users_payload(self):
        rows = await self.db_all(
            """
            SELECT
                users.id,
                users.username,
                users.display_name,
                users.created_at,
                COUNT(DISTINCT holdings.stock_code) AS holdings_count,
                COUNT(DISTINCT sessions.token_hash) AS active_session_count
            FROM users
            LEFT JOIN holdings ON holdings.user_id = users.id
            LEFT JOIN sessions ON sessions.user_id = users.id AND sessions.expires_at > ?
            GROUP BY users.id, users.username, users.display_name, users.created_at
            ORDER BY
                CASE WHEN users.username = ? THEN 0 ELSE 1 END,
                users.created_at DESC,
                users.username ASC
            """,
            utc_now(),
            self._super_user,
        )
        return {
            "superUser": self._super_user,
            "users": [
                {
                    "id": row["id"],
                    "username": row["username"],
                    "displayName": row.get("display_name") or row["username"],
                    "createdAt": row.get("created_at"),
                    "holdingsCount": row.get("holdings_count") or 0,
                    "activeSessionCount": row.get("active_session_count") or 0,
                    "isSuperUser": str(row["username"]).lower() == self._super_user,
                    "canDelete": str(row["username"]).lower() != self._super_user,
                }
                for row in rows
            ],
        }

    async def delete_admin_user(self, user_id: int):
        target = await self.db_first("SELECT id, username FROM users WHERE id = ?", user_id)
        if not target:
            raise NotFoundError("User not found")
        if str(target["username"]).lower() == self._super_user:
            raise BadRequestError("super user cannot be deleted")
        await self.db_run("DELETE FROM users WHERE id = ?", user_id)
        return await self.admin_users_payload()

    async def create_session(self, user_id: int):
        token = secrets.token_urlsafe(32)
        now = utc_now()
        expires_at = (datetime.now(timezone.utc) + timedelta(seconds=SESSION_MAX_AGE_SECONDS)).isoformat()
        await self.db_run(
            "INSERT INTO sessions (token_hash, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
            token_hash(token),
            user_id,
            now,
            expires_at,
        )
        return token

    def auth_source(self, request):
        forwarded_for = str(request.headers.get("x-forwarded-for") or "").split(",", 1)[0].strip()
        return (
            forwarded_for
            or str(request.headers.get("cf-connecting-ip") or "").strip()
            or str(request.headers.get("x-real-ip") or "").strip()
            or "unknown"
        )

    def auth_attempt_identifier(self, username: str, request):
        source = self.auth_source(request).lower()
        normalized = str(username or "").strip().lower()
        return hashlib.sha256(f"{source}|{normalized}".encode("utf-8")).hexdigest()

    async def require_auth_attempt_allowed(self, username: str, request):
        identifier = self.auth_attempt_identifier(username, request)
        now = datetime.now(timezone.utc)
        stale_before = (now - timedelta(seconds=AUTH_FAILURE_WINDOW_SECONDS)).isoformat()
        await self.db_run(
            "DELETE FROM auth_attempts WHERE locked_until IS NULL AND first_failed_at <= ?",
            stale_before,
        )
        row = await self.db_first("SELECT locked_until FROM auth_attempts WHERE identifier = ?", identifier)
        locked_until = parse_time(row.get("locked_until")) if row else None
        if locked_until and locked_until > now:
            retry_after = max(1, int((locked_until - now).total_seconds()))
            raise RateLimitError(retry_after)
        if locked_until:
            await self.db_run("DELETE FROM auth_attempts WHERE identifier = ?", identifier)

    async def record_auth_failure(self, username: str, request):
        identifier = self.auth_attempt_identifier(username, request)
        now = datetime.now(timezone.utc)
        row = await self.db_first(
            "SELECT failure_count, first_failed_at FROM auth_attempts WHERE identifier = ?",
            identifier,
        )
        first_failed_at = parse_time(row.get("first_failed_at")) if row else None
        if not first_failed_at or first_failed_at <= now - timedelta(seconds=AUTH_FAILURE_WINDOW_SECONDS):
            failure_count = 1
            first_failed_at = now
        else:
            failure_count = int(row.get("failure_count") or 0) + 1
        locked_until = (now + timedelta(seconds=AUTH_LOCK_SECONDS)).isoformat() if failure_count >= AUTH_FAILURE_LIMIT else None
        await self.db_run(
            """
            INSERT INTO auth_attempts (identifier, failure_count, first_failed_at, last_failed_at, locked_until)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(identifier) DO UPDATE SET
                failure_count = excluded.failure_count,
                first_failed_at = excluded.first_failed_at,
                last_failed_at = excluded.last_failed_at,
                locked_until = excluded.locked_until
            """,
            identifier,
            failure_count,
            first_failed_at.isoformat(),
            now.isoformat(),
            locked_until,
        )

    async def clear_auth_failures(self, username: str, request):
        await self.db_run("DELETE FROM auth_attempts WHERE identifier = ?", self.auth_attempt_identifier(username, request))

    async def register(self, request):
        payload = await self.request_json(request)
        try:
            username = normalize_username(payload.get("username"))
            password = str(payload.get("password") or "")
            validate_password(password)
        except ValueError as exc:
            return error_response(str(exc), status=400)
        await self.require_auth_attempt_allowed(username, request)
        existing = await self.db_first("SELECT id FROM users WHERE username = ?", username)
        if existing:
            await self.record_auth_failure(username, request)
            return error_response("帳號已存在", status=400)
        now = utc_now()
        await self.db_run(
            "INSERT INTO users (username, display_name, password_hash, created_at) VALUES (?, ?, ?, ?)",
            username,
            payload.get("displayName") or username,
            hash_password(password),
            now,
        )
        user = await self.db_first("SELECT id, username, display_name FROM users WHERE username = ?", username)
        await self.clear_auth_failures(username, request)
        token = await self.create_session(user["id"])
        holdings = await self.list_holdings(user["id"])
        return json_response({"authenticated": True, "user": public_user(user, self._super_user), "holdings": holdings}, headers={"set-cookie": session_cookie(token)})

    async def login(self, request):
        payload = await self.request_json(request)
        try:
            username = normalize_username(payload.get("username"))
        except ValueError:
            return error_response("帳號或密碼錯誤", status=401)
        password = str(payload.get("password") or "")
        await self.require_auth_attempt_allowed(username, request)
        row = await self.db_first("SELECT id, username, display_name, password_hash FROM users WHERE username = ?", username)
        if not row or not verify_password(password, row["password_hash"]):
            await self.record_auth_failure(username, request)
            return error_response("帳號或密碼錯誤", status=401)
        await self.clear_auth_failures(username, request)
        token = await self.create_session(row["id"])
        holdings = await self.list_holdings(row["id"])
        return json_response({"authenticated": True, "user": public_user(row, self._super_user), "holdings": holdings}, headers={"set-cookie": session_cookie(token)})

    async def list_holdings(self, user_id: int):
        rows = await self.db_all(
            """
            SELECT stock_code, name, shares, average_cost
            FROM holdings
            WHERE user_id = ?
            ORDER BY stock_code
            """,
            user_id,
        )
        return [
            {
                "stockCode": row["stock_code"],
                "name": row.get("name"),
                "shares": row.get("shares") or 0,
                "averageCost": row.get("average_cost"),
            }
            for row in rows
        ]

    async def upsert_holding(self, user_id: int, holding: dict):
        now = utc_now()
        await self.db_run(
            """
            INSERT INTO holdings (user_id, stock_code, name, shares, average_cost, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id, stock_code) DO UPDATE SET
                name = excluded.name,
                shares = excluded.shares,
                average_cost = excluded.average_cost,
                updated_at = excluded.updated_at
            """,
            user_id,
            holding["stockCode"],
            holding.get("name") or "",
            holding.get("shares", 0),
            holding.get("averageCost"),
            now,
            now,
        )

    async def replace_holdings(self, user_id: int, holdings: list[dict]):
        now = utc_now()
        stmts = [self.env.DB.prepare("DELETE FROM holdings WHERE user_id = ?").bind(user_id)]
        for h in holdings:
            stmts.append(
                self.env.DB.prepare(
                    """INSERT INTO holdings (user_id, stock_code, name, shares, average_cost, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(user_id, stock_code) DO UPDATE SET
                        name = excluded.name,
                        shares = excluded.shares,
                        average_cost = excluded.average_cost,
                        updated_at = excluded.updated_at"""
                ).bind(
                    user_id,
                    h["stockCode"],
                    h.get("name") or "",
                    h.get("shares", 0),
                    h.get("averageCost"),
                    now,
                    now,
                )
            )
        await self.env.DB.batch(to_js(stmts))
        return await self.list_holdings(user_id)

    async def get_settings(self):
        row = await self.db_first("SELECT value FROM app_kv WHERE key = ?", "settings")
        if not row:
            return DEFAULT_SETTINGS
        try:
            return settings_from_payload({**DEFAULT_SETTINGS, **json.loads(row["value"])})
        except Exception:
            return DEFAULT_SETTINGS

    async def put_settings(self, payload):
        settings = settings_from_payload(payload, strict=True)
        await self.db_run(
            """
            INSERT INTO app_kv (key, value, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
            """,
            "settings",
            json.dumps(settings, ensure_ascii=False),
            utc_now(),
        )
        return settings

    async def search_companies(self, query):
        raw_q = (query.get("q") or [""])[0].strip().lower()
        limit = min(max(int((query.get("limit") or ["20"])[0]), 1), 20)
        companies = (await self.r2_json("public/companies.json", {"items": []})).get("items", [])
        if not raw_q:
            return json_response({"items": companies[:limit]}, public_cache_seconds=1800)
        items = [
            company
            for company in companies
            if raw_q in str(company.get("stockCode", "")).lower()
            or raw_q in str(company.get("name", "")).lower()
            or raw_q in str(company.get("industryName", "")).lower()
        ][:limit]
        return json_response({"items": items}, public_cache_seconds=300)

    async def list_companies(self, query):
        companies = (await self.r2_json("public/companies.json", {"items": []})).get("items", [])
        try:
            page = max(int((query.get("page") or ["1"])[0]), 1)
            limit = min(max(int((query.get("limit") or ["100"])[0]), 1), 500)
        except Exception as exc:
            raise BadRequestError("page 與 limit 需為正整數") from exc
        total = len(companies)
        start = (page - 1) * limit
        end = start + limit
        return json_response(
            {
                "items": companies[start:end],
                "page": page,
                "limit": limit,
                "total": total,
                "hasMore": end < total,
            },
            public_cache_seconds=1800,
        )

    async def analyze_stock(self, stock_code: str):
        result = await self.analysis_for_stock(stock_code)
        if not result:
            return error_response("查無此股票快取", status=404)
        return json_response(result)

    async def scan_holdings(self, request):
        payload = await self.request_json(request)
        return json_response(await self.holdings_scan_payload(payload))

    async def holdings_scan_payload(self, payload):
        holdings = [normalize_holding(item) for item in payload.get("holdings", [])]
        analyses = await asyncio.gather(*[self.holding_analysis_for_stock(h["stockCode"]) for h in holdings])
        results = []
        missing = []
        for holding, result in zip(holdings, analyses):
            if not result:
                result = await self.analysis_for_stock(holding["stockCode"])
            if not result:
                missing.append({"stockCode": holding["stockCode"], "name": holding.get("name"), "reason": "Cloudflare 快取中查無此股票"})
                continue
            results.append(prepare_holding_result(result, holding))
        return {"generatedAt": utc_now(), "dataSource": "cloudflare_r2_seed", "results": results, "missing": missing}

    async def holding_analysis_for_stock(self, stock_code: str):
        normalized = str(stock_code or "").strip()
        if not re.fullmatch(r"\d{4,6}", normalized):
            return None
        shard = await self.r2_json(f"public/holding_analysis_shards/{normalized[:2]}.json", {})
        return shard.get(normalized) if isinstance(shard, dict) else None

    async def analysis_for_stock(self, stock_code: str):
        normalized = str(stock_code or "").strip()
        if not re.fullmatch(r"\d{4,6}", normalized):
            return None
        shard = await self.r2_json(f"public/analysis_shards/{normalized[:2]}.json", {})
        return shard.get(normalized) if isinstance(shard, dict) else None

    def empty_market_scan(self):
        return {
            "generatedAt": utc_now(),
            "dataSource": "cloudflare_r2_seed",
            "universeSize": 0,
            "note": "尚未上傳 Cloudflare R2 掃描快取。",
            "entry": [],
            "watch": [],
            "excluded": [],
        }

    def compact_market_scan(self, scan):
        if not isinstance(scan, dict):
            return self.empty_market_scan()
        compact = dict(scan)
        for category in ("entry", "watch", "excluded", "results"):
            items = compact.get(category)
            if isinstance(items, list):
                compact[category] = [self.compact_scan_result(item) for item in items]
        compact["detailMode"] = "summary"
        return compact

    def compact_scan_result(self, result):
        if not isinstance(result, dict):
            return {}
        compact = {}
        for key in ("stockCode", "companyName", "status", "summary", "company"):
            if key in result:
                compact[key] = result.get(key)
        reasons = result.get("reasons")
        if isinstance(reasons, list):
            compact["reasons"] = []
            for reason in reasons:
                if not isinstance(reason, dict):
                    continue
                compact["reasons"].append(
                    {
                        key: reason.get(key)
                        for key in ("code", "title", "passed", "severity", "message")
                        if key in reason
                    }
                )
        compact["detailsAvailable"] = True
        compact["hasFullDetails"] = False
        return compact

    async def market_report(self, query):
        report_format = (query.get("report_format") or ["markdown"])[0]
        scan = await self.r2_json("public/market_scan_summary.json", self.empty_market_scan())
        return self.report_response(scan, report_format, "台股市場掃描報告", "market_scan")

    async def holdings_report(self, request, query):
        report_format = (query.get("report_format") or ["markdown"])[0]
        payload = await self.request_json(request)
        return self.report_response(await self.holdings_scan_payload(payload), report_format, "台股持股掃描報告", "holdings_scan")

    def report_response(self, payload, report_format: str, title: str, filename_prefix: str):
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        if report_format == "csv":
            output = io.StringIO()
            writer = csv.writer(output)
            writer.writerow(["category", "stockCode", "companyName", "status", "summary"])
            for category in ("entry", "watch", "excluded", "results"):
                for item in payload.get(category, []):
                    writer.writerow([category, item.get("stockCode"), item.get("companyName"), item.get("status"), item.get("summary")])
            return text_response(
                output.getvalue(),
                media_type="text/csv; charset=utf-8",
                headers={"content-disposition": f'attachment; filename="{filename_prefix}_{timestamp}.csv"'},
            )
        lines = [f"# {title}", "", f"- 產生時間：{payload.get('generatedAt', utc_now())}", f"- 資料來源：{payload.get('dataSource', 'cloudflare_r2_seed')}", ""]
        for category, label in (("entry", "適合進場"), ("watch", "接近觀察"), ("excluded", "排除清單"), ("results", "持股")):
            items = payload.get(category, [])
            if not items:
                continue
            lines.append(f"## {label} ({len(items)})")
            for item in items:
                lines.append(f"- {item.get('stockCode')} {item.get('companyName')}：{item.get('summary')}")
            lines.append("")
        return text_response(
            "\n".join(lines),
            media_type="text/markdown; charset=utf-8",
            headers={"content-disposition": f'attachment; filename="{filename_prefix}_{timestamp}.md"'},
        )
