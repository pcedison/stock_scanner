from __future__ import annotations

import csv
import hashlib
import hmac
import io
import json
import re
import secrets
from datetime import UTC, datetime, timedelta, timezone
from typing import cast

from js import Object, Response
from pyodide.ffi import to_js

# Contract constants shared with the FastAPI backend (single source of truth in
# cloudflare/contract.py). The flat import is what the bundled Worker uses; the
# package-path fallback is for CPython (tests / local tooling).
try:
    from contract import (
        AUTH_FAILURE_LIMIT,
        AUTH_FAILURE_WINDOW_SECONDS,
        AUTH_LOCK_SECONDS,
        CSRF_HEADER_NAME,
        CSRF_HEADER_VALUE,
        PASSWORD_ALGORITHM,
        PASSWORD_ITERATIONS,
        UNSAFE_API_METHODS,
        USERNAME_PATTERN,
        is_https_origin,
        is_local_cors_origin,
        origin_host,
    )
except (ModuleNotFoundError, ImportError):
    from cloudflare.contract import (
        AUTH_FAILURE_LIMIT,
        AUTH_FAILURE_WINDOW_SECONDS,
        AUTH_LOCK_SECONDS,
        CSRF_HEADER_NAME,
        CSRF_HEADER_VALUE,
        PASSWORD_ALGORITHM,
        PASSWORD_ITERATIONS,
        UNSAFE_API_METHODS,
        USERNAME_PATTERN,
        is_https_origin,
        is_local_cors_origin,
        origin_host,
    )

SESSION_COOKIE_NAME = "stock_scanner_session"
SESSION_MAX_AGE_SECONDS = 60 * 60 * 24 * 30
TAIPEI_TZ = timezone(timedelta(hours=8))
_LOCALHOST_ORIGIN_RE = re.compile(r"^http://(localhost|127\.0\.0\.1):\d{1,5}$")
DEFAULT_DEVELOPMENT_CORS_ALLOW_ORIGINS = ("http://localhost:8000", "http://127.0.0.1:8000")
REVENUE_GROWTH_MODES = frozenset({"cumulative_ytd", "monthly", "trailing_3m_avg"})

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


class DependencyFailure(Exception):
    def __init__(self, stage: str, retryable: bool, cause: Exception):
        self.stage = stage
        self.retryable = retryable
        self.error_type = type(cause).__name__
        super().__init__(self.error_type)


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
    return datetime.now(UTC).isoformat()


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
    value = env.get(name, default) if isinstance(env, dict) else getattr(env, name, default)
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


def error_response(detail, status=400, headers=None, *, code=None, request_id=None, retryable=None, stage=None):
    payload = {"detail": detail}
    metadata = (("code", code), ("requestId", request_id), ("retryable", retryable), ("stage", stage))
    for key, value in metadata:
        if value is not None:
            payload[key] = bool(value) if key == "retryable" else value
    response_headers = dict(headers or {})
    if request_id:
        response_headers["x-request-id"] = request_id
    return json_response(payload, status=status, headers=response_headers)


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
    freshness = manifest.get("financialFreshness") if isinstance(manifest, dict) else None
    if not isinstance(freshness, dict):
        problems.append("financial freshness missing from manifest")
    elif freshness.get("blocksDeployment") is True:
        problems.append(
            "financial freshness blocks deployment: "
            f"expected {freshness.get('expectedFinancialPeriod')}, "
            f"latest cached {freshness.get('latestCachedFinancialPeriod')}"
        )
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
        "X1": "當年度的累計營收年增率 >= 最新當月份營收年增率的 50%",
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
        "Path=/; HttpOnly; SameSite=None; Secure"
    )


def clear_session_cookie() -> str:
    return f"{SESSION_COOKIE_NAME}=; Max-Age=0; Path=/; HttpOnly; SameSite=None; Secure"


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
    parsed_cost = float(average_cost) if average_cost is not None and average_cost != "" else None
    if parsed_cost is not None and parsed_cost < 0:
        raise ValueError("平均成本不可小於 0")
    return {
        "stockCode": stock_code,
        "name": payload.get("name"),
        "shares": shares,
        "averageCost": parsed_cost,
    }


def empty_market_scan() -> dict:
    return {
        "generatedAt": utc_now(),
        "dataSource": "cloudflare_r2_seed",
        "universeSize": 0,
        "note": "尚未上傳 Cloudflare R2 掃描快取。",
        "entry": [],
        "watch": [],
        "excluded": [],
    }


def compact_scan_result(result):
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
            cast(list, compact["reasons"]).append(
                {
                    key: reason.get(key)
                    for key in ("code", "title", "passed", "severity", "message")
                    if key in reason
                }
            )
    compact["detailsAvailable"] = True
    compact["hasFullDetails"] = False
    return compact


def compact_market_scan(scan):
    if not isinstance(scan, dict):
        return empty_market_scan()
    compact = dict(scan)
    for category in ("entry", "watch", "excluded", "results"):
        items = compact.get(category)
        if isinstance(items, list):
            compact[category] = [compact_scan_result(item) for item in items]
    compact["detailMode"] = "summary"
    return compact


def report_response(payload, report_format: str, title: str, filename_prefix: str):
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
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


__all__ = (
    "AUTH_FAILURE_LIMIT", "AUTH_FAILURE_WINDOW_SECONDS",
    "AUTH_LOCK_SECONDS", "BadRequestError",
    "CSRF_HEADER_NAME", "CSRF_HEADER_VALUE",
    "DEFAULT_DEVELOPMENT_CORS_ALLOW_ORIGINS", "DEFAULT_SETTINGS",
    "DependencyFailure", "ForbiddenError",
    "HOLDING_EXIT_CODES", "MIN_CACHE_ANALYSIS",
    "MIN_CACHE_COMPANIES", "NotFoundError",
    "PASSWORD_ALGORITHM", "PASSWORD_ITERATIONS",
    "RateLimitError",
    "REVENUE_GROWTH_MODES",
    "SECURITY_HEADERS",
    "SESSION_COOKIE_NAME",
    "SESSION_MAX_AGE_SECONDS",
    "TAIPEI_TZ",
    "UNSAFE_API_METHODS",
    "USERNAME_PATTERN",
    "ValidationError",
    "_LOCALHOST_ORIGIN_RE",
    "cache_key_from_manifest",
    "clear_session_cookie",
    "compact_market_scan",
    "compact_scan_result",
    "configured_super_user_username",
    "csv_env_value",
    "empty_backtest_status",
    "empty_market_scan",
    "env_flag",
    "env_value",
    "error_response",
    "has_holding_exit_rules",
    "hash_password",
    "holding_note",
    "is_https_origin",
    "is_local_cors_origin",
    "is_production_environment",
    "json_response",
    "js_to_py",
    "manifest_quality",
    "missing_exit_rule",
    "normalize_holding",
    "normalize_username",
    "origin_host",
    "parse_cookies",
    "parse_time",
    "prepare_holding_result",
    "public_user",
    "report_response",
    "runtime_environment",
    "session_cookie",
    "settings_from_payload",
    "text_response",
    "token_hash",
    "utc_now",
    "validate_password",
    "validate_runtime_security",
    "verify_password",
)
