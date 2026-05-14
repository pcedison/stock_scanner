from __future__ import annotations

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
USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.@+-]{2,79}$")

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


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def json_response(payload, status=200, headers=None):
    response_headers = {"content-type": "application/json; charset=utf-8", "cache-control": "no-store"}
    if headers:
        response_headers.update(headers)
    return Response.new(
        json.dumps(payload, ensure_ascii=False),
        to_js({"status": status, "headers": response_headers}, dict_converter=Object.fromEntries),
    )


def text_response(content, status=200, media_type="text/plain; charset=utf-8", headers=None):
    response_headers = {"content-type": media_type, "cache-control": "no-store"}
    if headers:
        response_headers.update(headers)
    return Response.new(content, to_js({"status": status, "headers": response_headers}, dict_converter=Object.fromEntries))


def error_response(detail, status=400):
    return json_response({"detail": detail}, status=status)


def normalize_username(username: str) -> str:
    normalized = str(username or "").strip().lower()
    if not USERNAME_PATTERN.fullmatch(normalized):
        raise ValueError("帳號需為 3-80 字元，且只能使用英數字與 . @ + - _")
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
    if hasattr(value, "to_py"):
        return value.to_py()
    if isinstance(value, list):
        return [js_to_py(item) for item in value]
    if isinstance(value, dict):
        return {key: js_to_py(item) for key, item in value.items()}
    return value


def public_user(row):
    if not row:
        return None
    return {
        "id": row["id"],
        "username": row["username"],
        "displayName": row.get("display_name") or row["username"],
    }


def normalize_holding(payload: dict) -> dict:
    stock_code = str(payload.get("stockCode") or payload.get("stock_code") or "").strip()
    if not re.fullmatch(r"\d{4,6}", stock_code):
        raise ValueError("股票代號格式不正確")
    shares = int(payload.get("shares") or 0)
    if shares < 0:
        raise ValueError("股數不可小於 0")
    average_cost = payload.get("averageCost", payload.get("average_cost"))
    return {
        "stockCode": stock_code,
        "name": payload.get("name"),
        "shares": shares,
        "averageCost": float(average_cost) if average_cost not in (None, "") else None,
    }


async def on_fetch(request, env):
    return await Api(env).fetch(request)


class Api:
    def __init__(self, env):
        self.env = env

    async def fetch(self, request):
        if request.method == "OPTIONS":
            return Response.new("", to_js({"status": 204, "headers": self.cors_headers()}, dict_converter=Object.fromEntries))

        parsed = urlparse(request.url)
        path = parsed.path.rstrip("/") or "/"
        query = parse_qs(parsed.query)

        try:
            response = await self.route(request, path, query)
        except PermissionError as exc:
            response = error_response(str(exc), status=401)
        except Exception as exc:
            response = error_response(f"Cloudflare Worker API error: {exc}", status=500)

        for key, value in self.cors_headers().items():
            response.headers.set(key, value)
        return response

    def cors_headers(self):
        return {
            "access-control-allow-origin": "*",
            "access-control-allow-methods": "GET,POST,PUT,DELETE,OPTIONS",
            "access-control-allow-headers": "content-type,cookie",
        }

    async def route(self, request, path: str, query: dict[str, list[str]]):
        if path == "/api/health" and request.method == "GET":
            manifest = await self.r2_json("public/manifest.json", {})
            return json_response({"status": "ok", "runtime": "cloudflare-python-worker", "time": utc_now(), "cache": manifest})

        if path == "/api/data-sources/status" and request.method == "GET":
            return json_response(await self.r2_json("public/data_sources_status.json", {"activeProvider": "CloudflareR2Seed"}))

        if path == "/api/companies" and request.method == "GET":
            return json_response(await self.r2_json("public/companies.json", {"items": []}))

        if path == "/api/companies/search" and request.method == "GET":
            return await self.search_companies(query)

        if path == "/api/scan/market" and request.method == "POST":
            return json_response(await self.r2_json("public/market_scan_latest.json", self.empty_market_scan()))

        if path.startswith("/api/analyze/") and request.method == "POST":
            stock_code = path.rsplit("/", 1)[-1]
            return await self.analyze_stock(stock_code)

        if path == "/api/scan/holdings" and request.method == "POST":
            return await self.scan_holdings(request)

        if path == "/api/reports/market" and request.method == "POST":
            return await self.market_report(query)

        if path == "/api/reports/holdings" and request.method == "POST":
            return await self.holdings_report(request, query)

        if path == "/api/settings" and request.method == "GET":
            return json_response(await self.get_settings())

        if path == "/api/settings" and request.method == "PUT":
            payload = await self.request_json(request)
            return json_response(await self.put_settings(payload))

        if path == "/api/auth/register" and request.method == "POST":
            return await self.register(request)

        if path == "/api/auth/login" and request.method == "POST":
            return await self.login(request)

        if path == "/api/auth/logout" and request.method == "POST":
            token = self.session_token(request)
            if token:
                await self.db_run("DELETE FROM sessions WHERE token_hash = ?", token_hash(token))
            return json_response({"authenticated": False}, headers={"set-cookie": clear_session_cookie()})

        if path == "/api/auth/me" and request.method == "GET":
            user = await self.current_user(request)
            return json_response({"authenticated": bool(user), "user": public_user(user)})

        if path == "/api/me/holdings" and request.method == "GET":
            user = await self.require_user(request)
            return json_response({"holdings": await self.list_holdings(user["id"])})

        if path == "/api/me/holdings" and request.method == "PUT":
            user = await self.require_user(request)
            payload = await self.request_json(request)
            holdings = [normalize_holding(item) for item in payload.get("holdings", [])]
            return json_response({"holdings": await self.replace_holdings(user["id"], holdings)})

        if path == "/api/me/holdings" and request.method == "POST":
            user = await self.require_user(request)
            payload = await self.request_json(request)
            holding = normalize_holding(payload.get("holding", payload))
            await self.upsert_holding(user["id"], holding)
            return json_response({"holdings": await self.list_holdings(user["id"])})

        if path.startswith("/api/me/holdings/") and request.method == "DELETE":
            user = await self.require_user(request)
            stock_code = path.rsplit("/", 1)[-1]
            await self.db_run("DELETE FROM holdings WHERE user_id = ? AND stock_code = ?", user["id"], stock_code)
            return json_response({"holdings": await self.list_holdings(user["id"])})

        if path == "/api/data-sources/backfill-history" and request.method == "POST":
            progress = await self.r2_json("official/official_history_backfill_progress.json", {})
            return json_response({
                "status": "cloudflare_cache_seeded",
                "mode": "incremental backfill is prepared for scheduled implementation",
                "progress": progress,
            })

        if path == "/api/scheduler/wakeup" and request.method == "GET":
            return json_response({"status": "SLEEP", "reason": "Cloudflare deployment uses cached official data and scheduled increments."})

        if path == "/api/scheduler/auto-scan" and request.method == "GET":
            return json_response({"action": "ready", "autoScanEnabled": True, "manualScanEnabled": True, "scan": None})

        if path.startswith("/api/calendar/") and request.method == "GET":
            year = path.rsplit("/", 1)[-1]
            return json_response({"year": int(year), "source": "cloudflare-cache", "closedDates": [], "springFestivalDates": []})

        if path == "/api/integrations/status" and request.method == "GET":
            return json_response({"line": False, "telegram": False, "email": False, "broker": False})

        if path == "/api/backtest" and request.method == "GET":
            return json_response({"status": "not_configured", "annualizedReturn": None})

        return error_response("Not found", status=404)

    async def request_json(self, request):
        try:
            return js_to_py(await request.json()) or {}
        except Exception:
            text = await request.text()
            return json.loads(str(text) or "{}")

    async def r2_json(self, key: str, fallback):
        obj = await self.env.CACHE.get(key)
        if obj is None:
            return fallback
        text = await obj.text()
        return json.loads(text)

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

    async def register(self, request):
        payload = await self.request_json(request)
        try:
            username = normalize_username(payload.get("username"))
            password = str(payload.get("password") or "")
            validate_password(password)
        except ValueError as exc:
            return error_response(str(exc), status=400)
        existing = await self.db_first("SELECT id FROM users WHERE username = ?", username)
        if existing:
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
        token = await self.create_session(user["id"])
        return json_response({"authenticated": True, "user": public_user(user)}, headers={"set-cookie": session_cookie(token)})

    async def login(self, request):
        payload = await self.request_json(request)
        username = normalize_username(payload.get("username"))
        password = str(payload.get("password") or "")
        row = await self.db_first("SELECT id, username, display_name, password_hash FROM users WHERE username = ?", username)
        if not row or not verify_password(password, row["password_hash"]):
            return error_response("帳號或密碼錯誤", status=401)
        token = await self.create_session(row["id"])
        return json_response({"authenticated": True, "user": public_user(row)}, headers={"set-cookie": session_cookie(token)})

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
            holding.get("averageCost") if holding.get("averageCost") is not None else 0,
            now,
            now,
        )

    async def replace_holdings(self, user_id: int, holdings: list[dict]):
        await self.db_run("DELETE FROM holdings WHERE user_id = ?", user_id)
        for holding in holdings:
            await self.upsert_holding(user_id, holding)
        return await self.list_holdings(user_id)

    async def get_settings(self):
        row = await self.db_first("SELECT value FROM app_kv WHERE key = ?", "settings")
        if not row:
            return DEFAULT_SETTINGS
        try:
            return {**DEFAULT_SETTINGS, **json.loads(row["value"])}
        except Exception:
            return DEFAULT_SETTINGS

    async def put_settings(self, payload):
        settings = {**DEFAULT_SETTINGS, **{key: payload.get(key, value) for key, value in DEFAULT_SETTINGS.items()}}
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
            return json_response({"items": companies[:limit]})
        items = [
            company
            for company in companies
            if raw_q in str(company.get("stockCode", "")).lower()
            or raw_q in str(company.get("name", "")).lower()
            or raw_q in str(company.get("industryName", "")).lower()
        ][:limit]
        return json_response({"items": items})

    async def analyze_stock(self, stock_code: str):
        analysis = await self.r2_json("public/analysis_by_code.json", {})
        result = analysis.get(stock_code)
        if not result:
            return error_response("查無此股票快取", status=404)
        return json_response(result)

    async def scan_holdings(self, request):
        payload = await self.request_json(request)
        return json_response(await self.holdings_scan_payload(payload))

    async def holdings_scan_payload(self, payload):
        holdings = [normalize_holding(item) for item in payload.get("holdings", [])]
        analysis = await self.r2_json("public/analysis_by_code.json", {})
        results = []
        missing = []
        for holding in holdings:
            result = analysis.get(holding["stockCode"])
            if not result:
                missing.append({"stockCode": holding["stockCode"], "name": holding.get("name"), "reason": "Cloudflare 快取中查無此股票"})
                continue
            copied = json.loads(json.dumps(result, ensure_ascii=False))
            copied.setdefault("reasons", []).insert(
                0,
                {
                    "code": "HOLDING",
                    "title": "目前持股",
                    "passed": True,
                    "severity": "INFO",
                    "message": f"Cloudflare D1 / 本機同步持股 {holding['shares']} 股。",
                },
            )
            results.append(copied)
        return {"generatedAt": utc_now(), "dataSource": "cloudflare_r2_seed", "results": results, "missing": missing}

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

    async def market_report(self, query):
        report_format = (query.get("report_format") or ["markdown"])[0]
        scan = await self.r2_json("public/market_scan_latest.json", self.empty_market_scan())
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
