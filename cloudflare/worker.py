from __future__ import annotations

import asyncio
import hashlib
import json
import re
import secrets
from datetime import UTC, datetime, timedelta
from typing import Protocol, cast
from urllib.parse import parse_qs, urlparse

from js import Object, Response
from pyodide.ffi import to_js

try:
    from worker_support import *  # noqa: F403
except ModuleNotFoundError:
    from cloudflare.worker_support import *  # noqa: F403

try:
    import worker_health
    import worker_market_legacy as market_legacy
    import worker_market_query
    import worker_market_resilience as market_resilience
    import worker_observability as observability
    import worker_refresh_control
    import worker_refresh_jobs
    import worker_trading_calendar as trading_calendar
except ModuleNotFoundError:
    from cloudflare import worker_health, worker_market_query, worker_refresh_control, worker_refresh_jobs
    from cloudflare import worker_market_legacy as market_legacy
    from cloudflare import worker_market_resilience as market_resilience
    from cloudflare import worker_observability as observability
    from cloudflare import worker_trading_calendar as trading_calendar

CLIENT_ERROR_STATUS = {BadRequestError: 400, NotFoundError: 404, ValidationError: 422, PermissionError: 401, ForbiddenError: 403}
set_response_header = observability.set_response_header


class _DependencyFailureWithCode(Protocol):
    error_code: str


async def on_fetch(request, env):
    request_id = observability.new_request_id()
    started = observability.start_timer()
    try:
        return await Api(env).fetch(request, request_id=request_id)
    except Exception as exc:
        path = urlparse(str(getattr(request, "url", ""))).path.rstrip("/") or "/"
        response = observability.failure_response(
            error_response, request_id, request, path, exc, started, dependency=False, stage="worker"
        )
        headers = {**SECURITY_HEADERS, **cors_headers_for_env(env, request)}
        observability.add_response_headers(response, headers, request_id)
        return response


async def on_scheduled(controller, env, ctx):
    scheduled_time = getattr(controller, "scheduledTime", None)
    return await worker_refresh_control.run_scheduled_refresh(Api(env), scheduled_time)


def d1_param(value):
    # Pyodide may pass Python None to JS as undefined, which D1 rejects.
    # Empty strings round-trip as "not set" for the nullable fields we bind.
    return "" if value is None else value


def cors_allowed_origins_for_env(env):
    configured = csv_env_value(env, "APP_CORS_ALLOW_ORIGINS")
    configured = configured or csv_env_value(env, "WORKER_CORS_ALLOW_ORIGINS")
    return observability.cors_allowed_origins(
        configured,
        DEFAULT_DEVELOPMENT_CORS_ALLOW_ORIGINS,
        production=is_production_environment(env),
        allow_local=env_flag(env, "APP_ALLOW_LOCAL_CORS_IN_PRODUCTION"),
        allow_insecure=env_flag(env, "APP_ALLOW_INSECURE_CORS_IN_PRODUCTION"),
        is_local_origin=is_local_cors_origin,
        is_https_origin=is_https_origin,
    )


def cors_headers_for_env(env, request=None, allowed_origins=None):
    if allowed_origins is None:
        allowed_origins = cors_allowed_origins_for_env(env)
    request_origin = str(request.headers.get("origin") or "") if request is not None else ""
    return observability.cors_headers(
        allowed_origins,
        request_origin,
        production=is_production_environment(env),
        local_request_origin=bool(_LOCALHOST_ORIGIN_RE.match(request_origin)),
        csrf_header_name=CSRF_HEADER_NAME,
    )


class Api:
    def __init__(self, env):
        self.env = env
        self._r2_cache: dict = {}
        self._super_user = configured_super_user_username(env)
        validate_runtime_security(env, self._super_user)
        self._cors_allowed_origins = self.cors_allowed_origins()

    async def fetch(self, request, request_id=None):
        self._request_id = request_id or observability.new_request_id()
        started = observability.start_timer()
        make_failure_response = observability.failure_response
        parsed = urlparse(request.url)
        path = parsed.path.rstrip("/") or "/"
        query = parse_qs(parsed.query, keep_blank_values=path in worker_market_query.ROUTES)

        if request.method == "OPTIONS":
            response = Response.new(
                "",
                to_js({"status": 204, "headers": {**SECURITY_HEADERS, **self.cors_headers(request)}}, dict_converter=Object.fromEntries),
            )
        else:
            try:
                if self.requires_csrf_header(request, path) and not self.has_valid_csrf_header(request):
                    response = error_response("CSRF header required", status=403)
                else:
                    response = await self.route(request, path, query)
            except (BadRequestError, NotFoundError, ValidationError, PermissionError, ForbiddenError) as exc:
                status = next(status for error_type, status in CLIENT_ERROR_STATUS.items() if isinstance(exc, error_type))
                response = error_response(str(exc), status=status)
            except RateLimitError as exc:
                response = error_response(str(exc), status=429, headers={"retry-after": str(exc.retry_after_seconds)})
            except DependencyFailure as exc:
                response = make_failure_response(error_response, self._request_id, request, path, exc, started, dependency=True)
            except Exception as exc:
                response = make_failure_response(error_response, self._request_id, request, path, exc, started, dependency=False)

        observability.add_response_headers(response, {**SECURITY_HEADERS, **self.cors_headers(request)}, self._request_id)
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
        return cors_allowed_origins_for_env(self.env)

    def cors_headers(self, request=None):
        return cors_headers_for_env(self.env, request, self._cors_allowed_origins)

    async def route(self, request, path: str, query: dict[str, list[str]]):
        method = request.method.upper()

        if path.startswith("/api/auth/"):
            return await self._route_auth(request, method, path)
        if path.startswith("/api/me/"):
            return await self._route_me(request, method, path)
        if path.startswith("/api/admin/"):
            return await self._route_admin(request, method, path)
        if path.startswith("/api/scan/") or path.startswith("/api/analyze/"):
            return await self._route_scan(request, method, path, query)
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

        if path == "/api/runtime-config" and method == "GET":
            return json_response(self.runtime_config(), headers=self.runtime_config_cache_headers())

        if path == "/api/health" and method == "GET":
            dispatch_status = worker_health.refresh_dispatch_status(self, worker_refresh_control.dispatch_enabled(self.env))
            manifest, refresh_dispatch = await asyncio.gather(self.r2_json("public/manifest.json", {}), dispatch_status)
            payload = worker_health.health_payload(
                manifest, manifest_quality, self.cache_status_from_manifest, self.cache_policy, utc_now, refresh_dispatch
            )
            return json_response(payload, public_cache_seconds=60)

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

    async def _route_scan(self, request, method: str, path: str, query):
        if path == "/api/scan/market/refresh" and method == "POST":
            manifest = await self.r2_json("public/manifest.json", {})
            try:
                job = await self.ensure_refresh_job(
                    manifest, force=True, client_key=request.headers.get("idempotency-key")
                )
            except ValueError as exc:
                raise ValidationError(str(exc)) from None
            payload = worker_refresh_jobs.refresh_command_payload(job, self._request_id)
            return json_response(payload, status=202, headers={"Location": payload["statusUrl"]})
        refresh_status_prefix = "/api/scan/market/refresh/"
        if path.startswith(refresh_status_prefix) and method == "GET":
            payload = await worker_refresh_jobs.refresh_job_status(self, path.removeprefix(refresh_status_prefix))
            return json_response(payload) if payload else error_response("Not found", status=404)
        if method == "GET" and path in worker_market_query.ROUTES:
            return await worker_market_query.route(
                self, path, query, json_response, error_response, observability.dependency_call, DependencyFailure)
        if path == "/api/scan/market" and method == "GET":
            # Legacy v1 payload: streamed from R2 without JSON-decoding it (see worker_market_legacy).
            manifest = await self.r2_json("public/manifest.json", {})
            raw_scan = await self.r2_text("public/market_scan_summary.json")
            policy = self.cache_policy()
            cache_status = self.cache_status_from_manifest(manifest, {"status": "fresh", "reason": policy["reason"]})
            return market_legacy.market_summary_response(raw_scan, cache_status)
        if path == "/api/scan/market" and method == "POST":
            payload = await self.request_json(request)
            refresh_mode = str(payload.get("refreshMode") or "auto").strip().lower()
            manifest = await self.r2_json("public/manifest.json", {})
            raw_scan = await self.r2_text("public/market_scan_summary.json")
            scan_marker = empty_market_scan() if market_legacy.looks_like_market_scan_text(raw_scan) else None
            cache_status = await market_resilience.market_refresh_cache_status(
                api=self, request=request, manifest=manifest, scan=scan_marker, force=refresh_mode == "force",
                dependency_failure_type=DependencyFailure)
            return market_legacy.market_summary_response(
                raw_scan, cache_status,
                headers={"Deprecation": "true", "Link": '</api/scan/market/refresh>; rel="successor-version"'},
            )
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

    async def r2_text(self, key: str):
        return await market_legacy.r2_text(self, key, DependencyFailure)

    async def r2_json(self, key: str, fallback):
        if key in self._r2_cache:
            return self._r2_cache[key]
        async def get():
            return js_to_py(await self.env.CACHE.get(key))

        obj = await observability.dependency_call(get, DependencyFailure, "r2_read", True)
        if obj is None:
            self._r2_cache[key] = fallback
            return fallback
        text = await observability.dependency_call(
            lambda: obj.text(), DependencyFailure, "r2_read", True
        )
        try:
            result = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            result = fallback
        self._r2_cache[key] = result
        return result

    def cache_policy(self):
        reason = trading_calendar.refresh_reason(datetime.now(TAIPEI_TZ).date())
        # Freshness follows the manifest's publication slot; this interval is only the
        # legacy-manifest deadline and the terminal-job cooldown ceiling.
        legacy_interval = {"financial_report_window": 7200, "monthly_revenue_window": 10800}.get(reason, 43200)
        return {"strategy": "stale_while_revalidate", "reason": reason, "minIntervalSeconds": legacy_interval}

    def market_api_version(self):
        return "v2" if str(env_value(self.env, "MARKET_SCAN_API_VERSION", "v1")).strip().lower() == "v2" else "v1"

    def edge_cache_enabled(self):
        return env_flag(self.env, "EDGE_CACHE_ENABLED")

    def runtime_config(self):
        return {
            "schemaVersion": 1,
            "marketScanApiVersion": self.market_api_version(),
            "edgeCacheEnabled": self.edge_cache_enabled(),
        }

    def runtime_config_cache_headers(self):
        return public_edge_cache_headers(60, 60, 300)

    def market_v2_cache_headers(self):
        return public_edge_cache_headers(300, 60, 3600) if self.edge_cache_enabled() else {}

    def cache_status_from_manifest(self, manifest, refresh_status):
        policy = self.cache_policy()
        generated_at = manifest.get("generatedAt")
        generated_time = parse_time(generated_at)
        next_refresh = None
        is_stale = True
        if generated_time:
            # The seed builder's publication slot; legacy manifests fall back to the interval rule.
            next_refresh_time = trading_calendar.manifest_next_refresh(
                manifest, generated_time
            ) or trading_calendar.next_refresh_deadline(generated_time.astimezone(UTC), policy["minIntervalSeconds"], manifest)
            next_refresh = next_refresh_time.isoformat()
            is_stale = datetime.now(UTC) >= next_refresh_time
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

    async def ensure_refresh_job(self, manifest, force=False, client_key=None, *, refresh_ahead_seconds=0):
        failure = None
        try:
            return await worker_refresh_jobs.enqueue_or_reuse_refresh_job(
                self, manifest, force, client_key, datetime.now(UTC), refresh_ahead_seconds=refresh_ahead_seconds
            )
        except worker_refresh_jobs.RefreshJobReadBackError as exc:
            failure = DependencyFailure("d1_read", True, exc)
            failure_with_code = cast(_DependencyFailureWithCode, failure)
            failure_with_code.error_code = "REFRESH_JOB_READ_BACK_INVARIANT"
        except DependencyFailure as exc:
            if exc.stage == "d1_write" and getattr(exc, "error_code", "") in observability.RETRYABLE_D1_READ_CODES:
                exc.retryable = True
            raise
        raise failure from None

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
        error = payload.pop("error", None)
        payload["hasError"] = bool(str(error or "").strip())
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

    async def _db_call(self, operation: str, sql: str, params, *, write: bool):
        async def call():
            statement = self.env.DB.prepare(sql)
            if params:
                statement = statement.bind(*(d1_param(param) for param in params))
            return js_to_py(await getattr(statement, operation)())

        stage = "d1_write" if write else "d1_read"
        retryable = False if write else observability.d1_read_retryable
        return await observability.dependency_call(call, DependencyFailure, stage, retryable)

    async def db_run(self, sql: str, *params):
        return await self._db_call("run", sql, params, write=True)

    async def db_first(self, sql: str, *params):
        return await self._db_call("first", sql, params, write=False)

    async def db_all(self, sql: str, *params):
        result = await self._db_call("all", sql, params, write=False)
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
        expires_at = (datetime.now(UTC) + timedelta(seconds=SESSION_MAX_AGE_SECONDS)).isoformat()
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
        return hashlib.sha256(f"{source}|{normalized}".encode()).hexdigest()

    async def require_auth_attempt_allowed(self, username: str, request):
        identifier = self.auth_attempt_identifier(username, request)
        now = datetime.now(UTC)
        stale_before = (now - timedelta(seconds=AUTH_FAILURE_WINDOW_SECONDS)).isoformat()
        await self.db_run(
            "DELETE FROM auth_attempts WHERE (locked_until IS NULL OR locked_until = '') AND first_failed_at <= ?",
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
        now = datetime.now(UTC)
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
                "averageCost": None if row.get("average_cost") in {"", None} else row.get("average_cost"),
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
            d1_param(holding.get("averageCost")),
            now,
            now,
        )

    async def replace_holdings(self, user_id: int, holdings: list[dict]):
        now = utc_now()
        async def replace():
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
                        d1_param(h.get("averageCost")),
                        now,
                        now,
                    )
                )
            return await self.env.DB.batch(to_js(stmts))

        await observability.dependency_call(replace, DependencyFailure, "d1_write", False)
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
        for holding, result in zip(holdings, analyses, strict=False):
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

    async def market_report(self, query):
        report_format = (query.get("report_format") or ["markdown"])[0]
        scan = await self.r2_json("public/market_scan_summary.json", empty_market_scan())
        return report_response(scan, report_format, "台股市場掃描報告", "market_scan")

    async def holdings_report(self, request, query):
        report_format = (query.get("report_format") or ["markdown"])[0]
        payload = await self.request_json(request)
        return report_response(await self.holdings_scan_payload(payload), report_format, "台股持股掃描報告", "holdings_scan")
