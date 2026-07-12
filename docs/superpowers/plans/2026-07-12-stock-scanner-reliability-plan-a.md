# Stock Scanner Reliability Plan A Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stabilize the existing production contract with traceable Worker failures, safe market-refresh degradation, policy-aligned health checks, proactive refresh scheduling, bounded idempotent-read retries, and last-good UI rendering.

**Architecture:** Keep the existing Pages + Python Worker + D1 + R2 architecture and all successful API payloads. Add observability and safe additive error metadata at the Worker boundary, degrade only the legacy public market-refresh enqueue path, expose the Worker's existing dynamic cache policy to health automation, and make the browser retry only safe reads while preserving in-memory market results.

**Tech Stack:** Python 3.12+ and pytest; Cloudflare Python Workers, D1, R2, Wrangler 4.103+; vanilla JavaScript, ESLint, Prettier, and Playwright; GitHub Actions YAML.

## Global Constraints

- Preserve existing authentication, CSRF, CORS, cookie, settings, holdings, reports, and legacy market-scan contracts unless an explicitly versioned replacement is introduced.
- Never expose exception messages, SQL, credentials, request bodies, cookies, usernames, tokens, or Authorization headers to clients or structured logs.
- Retry only operations that are safe to repeat. Browser retries are limited to idempotent reads. D1 writes require an idempotency key before application-level retry is allowed.
- Continue serving last-known-good market data while refresh or dependency operations are degraded.
- Plan A must pass its full gate before any Plan B production code is started.
- No production deployment is part of this local implementation unless separately requested; commits remain on `codex/reliability-hardening`.
- Follow RED → verify RED → minimal GREEN → verify GREEN for every behavior change.
- Run pytest with a `C:\tmp` basetemp because the repository is inside `.worktrees` and the encoding scanner intentionally ignores `.worktrees` paths.
- Run pytest and Playwright sequentially; both suites use shared local application state and are not isolation-safe when executed concurrently.

---

### Task 1: Worker request identity, structured errors, and observability configuration

**Files:**
- Modify: `tests/test_cloudflare_worker.py:1396`
- Modify: `tests/test_deployment_preflight.py`
- Modify: `scripts/check_deployment_preflight.py`
- Create: `cloudflare/worker_observability.py`
- Modify: `cloudflare/worker_support.py:80-110,178-205`
- Modify: `cloudflare/worker.py:19-80,332-345,468-487`
- Modify: `cloudflare/wrangler.toml`

**Interfaces:**
- Produces: `DependencyFailure(stage: str, retryable: bool, cause: Exception)` in `worker_support.py`.
- Produces: focused `worker_observability.py` (maximum 200 lines) for request IDs, fixed safe dependency classifications, duration, and structured JSON logging.
- Produces: additive `error_response(detail, status, headers, *, code, request_id, retryable, stage)`.
- Produces: one server-generated request ID per Worker invocation, returned as `X-Request-ID` on every response.
- Produces: `validate_worker_observability(path: Path) -> list[str]` used by deployment preflight.
- Preserves: existing `detail` values and all existing success response shapes.

- [ ] **Step 1: Extend the unexpected-error test before implementation**

Update the existing test so the new contract is explicit:

```python
def test_worker_fetch_maps_unexpected_error_to_500(monkeypatch, capsys):
    worker, api, _db = build_router_api(monkeypatch)

    async def boom(_key, _fallback):
        raise RuntimeError("secret-value must never be returned")

    api.r2_json = boom
    response = asyncio.run(api.fetch(RouteRequest(path="/api/health", headers={"authorization": "Bearer hidden"})))
    payload = json.loads(response.body)

    assert response.init["status"] == 500
    assert payload["detail"] == "伺服器暫時無法處理請求，請稍後再試。"
    assert payload["code"] == "INTERNAL_ERROR"
    assert payload["retryable"] is False
    assert payload["stage"] == "route"
    assert payload["requestId"]
    assert response.headers["x-request-id"] == payload["requestId"]

    record = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert record["event"] == "worker_request_failed"
    assert record["requestId"] == payload["requestId"]
    assert record["method"] == "GET"
    assert record["path"] == "/api/health"
    assert record["errorType"] == "RuntimeError"
    assert record["durationMs"] >= 0
    serialized = json.dumps(record)
    assert "secret-value" not in serialized
    assert "Bearer hidden" not in serialized
```

Add one read dependency test and one ambiguous write test:

```python
def test_worker_r2_read_failure_returns_retryable_503(monkeypatch):
    worker, api, _db = build_router_api(monkeypatch)

    async def fail_get(_key):
        raise RuntimeError("R2 temporarily unavailable")

    api.env.CACHE.get = fail_get
    response = asyncio.run(api.fetch(RouteRequest(path="/api/health")))
    payload = json.loads(response.body)
    assert response.init["status"] == 503
    assert payload["code"] == "DEPENDENCY_UNAVAILABLE"
    assert payload["retryable"] is True
    assert payload["stage"] == "r2_read"


def test_worker_d1_write_failure_is_not_marked_safe_to_retry(monkeypatch):
    worker, api, _db = build_router_api(monkeypatch, r2={"public/manifest.json": {}})

    async def fail_refresh(_manifest, force=False):
        raise worker.DependencyFailure("d1_write", False, RuntimeError("Network connection lost"))

    api.ensure_refresh_job = fail_refresh
    response = asyncio.run(api.fetch(RouteRequest(method="POST", path="/api/cache/refresh", body="")))
    payload = json.loads(response.body)
    assert response.init["status"] == 500
    assert payload["retryable"] is False
    assert payload["stage"] == "d1_write"
```

- [ ] **Step 2: Add the observability preflight test**

```python
def test_worker_observability_requires_persisted_logs(tmp_path):
    wrangler = tmp_path / "wrangler.toml"
    wrangler.write_text('name = "demo"\nmain = "worker.py"\n', encoding="utf-8")
    problems = validate_worker_observability(wrangler)
    assert any("observability" in problem for problem in problems)

    wrangler.write_text(
        '[observability]\nenabled = true\n'
        '[observability.logs]\nenabled = true\nhead_sampling_rate = 1\ninvocation_logs = true\npersist = true\n'
        '[observability.traces]\nenabled = true\nhead_sampling_rate = 0.1\npersist = true\n',
        encoding="utf-8",
    )
    assert validate_worker_observability(wrangler) == []
```

- [ ] **Step 3: Run the RED tests and confirm the expected failures**

Run:

```powershell
..\..\.venv\Scripts\python.exe -m pytest -q -o filterwarnings= --basetemp C:\tmp\pytest-plan-a-task1-red tests\test_cloudflare_worker.py tests\test_deployment_preflight.py
```

Expected: failures for missing additive error fields, missing structured JSON record, unwrapped dependency errors, and missing `validate_worker_observability`.

- [ ] **Step 4: Implement the safe error primitives**

Add the following shape to `worker_support.py` without removing existing exception classes:

```python
class DependencyFailure(Exception):
    def __init__(self, stage: str, retryable: bool, cause: Exception):
        self.stage = stage
        self.retryable = retryable
        self.error_type = type(cause).__name__
        super().__init__(self.error_type)


def error_response(
    detail,
    status=400,
    headers=None,
    *,
    code=None,
    request_id=None,
    retryable=None,
    stage=None,
):
    payload = {"detail": detail}
    if code is not None:
        payload["code"] = code
    if request_id is not None:
        payload["requestId"] = request_id
    if retryable is not None:
        payload["retryable"] = bool(retryable)
    if stage is not None:
        payload["stage"] = stage
    response_headers = dict(headers or {})
    if request_id:
        response_headers["x-request-id"] = request_id
    return json_response(payload, status=status, headers=response_headers)
```

Add `DependencyFailure` to `worker_support.__all__`, because `worker.py` imports support symbols through the existing star-import boundary.

Create `worker_observability.py` and import it from `worker.py` through flat-worker and package fallbacks, matching the existing support import style. Generate request IDs with `secrets.token_hex(12)`, store them only on the per-request `Api` instance during `fetch`, add them to all returned responses, and log failures as one JSON object. The observability module must use only Python standard-library imports, remain at or below 200 lines, and must never receive headers, query text, request body, D1 parameters, or the original exception string:

```python
def log_worker_failure(*, request_id, request, path, stage, error_type, error_code, status, duration_ms):
    print(json.dumps({
        "event": "worker_request_failed",
        "requestId": request_id,
        "method": str(getattr(request, "method", ""))[:12],
        "path": path[:160],
        "stage": stage,
        "status": status,
        "errorType": str(error_type)[:120],
        "errorCode": str(error_code)[:80],
        "durationMs": max(0, round(float(duration_ms), 2)),
}, ensure_ascii=False, sort_keys=True))
```

Measure duration with `time.perf_counter()`. Classify known dependency messages into fixed safe codes (`NETWORK_LOST`, `RESET`, `TRANSIENT_REMOTE_NODE`, `OVERLOADED`, `TIMEOUT`, or `UNCLASSIFIED`) and log only that code, never the original exception text. Catch `DependencyFailure` before the generic exception. Use HTTP 503 and `DEPENDENCY_UNAVAILABLE` only when `retryable=True`; otherwise use HTTP 500 and `DEPENDENCY_FAILURE`. Wrap R2 `CACHE.get` as retryable `r2_read`, D1 reads as `d1_read` with retryability limited to documented transient error names, and every D1 write as non-retryable `d1_write` because the commit outcome can be ambiguous.

Generate the request ID in `on_fetch`, pass it into `Api.fetch`, and let direct test calls to `Api.fetch` generate one when omitted. Wrap `Api(env)` construction in `on_fetch` so runtime-security initialization failures also receive the safe envelope and `X-Request-ID`. Add a test whose production environment omits `SUPER_USER_USERNAME` and assert `on_fetch` returns the additive `INTERNAL_ERROR` envelope without exposing configuration values.

- [ ] **Step 5: Enable persisted logs and sampled traces**

Add the schema-validated configuration:

```toml
[observability]
enabled = true

[observability.logs]
enabled = true
head_sampling_rate = 1
invocation_logs = true
persist = true

[observability.traces]
enabled = true
head_sampling_rate = 0.1
persist = true
```

Implement `validate_worker_observability` with `tomllib`, require the exact booleans and the exact trace sampling rate `0.1` shown above, and include its problems in deployment preflight output.

- [ ] **Step 6: Run GREEN tests and static gates**

```powershell
..\..\.venv\Scripts\python.exe -m pytest -q -o filterwarnings= --basetemp C:\tmp\pytest-plan-a-task1-green tests\test_cloudflare_worker.py tests\test_deployment_preflight.py
..\..\.venv\Scripts\python.exe scripts\check_deployment_preflight.py
npx.cmd wrangler deploy --config cloudflare\wrangler.toml --dry-run --outdir C:\tmp\stock-worker-task1-dry-run
```

Expected: all selected tests pass, preflight prints `Deployment preflight passed.`, and Wrangler dry-run exits 0.

- [ ] **Step 7: Commit Task 1**

```powershell
git add cloudflare\worker.py cloudflare\worker_observability.py cloudflare\worker_support.py cloudflare\wrangler.toml scripts\check_deployment_preflight.py tests\test_cloudflare_worker.py tests\test_deployment_preflight.py
git commit -m "feat: add traceable worker errors"
```

---

### Task 2: Preserve the market scan when refresh enqueue fails

**Files:**
- Modify: `tests/test_cloudflare_worker.py:1255-1267`
- Modify: `cloudflare/worker.py:249-259`

**Interfaces:**
- Consumes: Task 1 request ID and structured failure logger.
- Produces: legacy market POST success with `cacheStatus.refreshStatus="unavailable"` when only refresh enqueue fails.
- Preserves: the same HTTP 200 market arrays and no fail-open behavior for any other write path.

- [ ] **Step 1: Write the failing degradation test**

```python
def test_worker_scan_market_post_serves_last_good_when_refresh_queue_fails(monkeypatch, capsys):
    manifest = {"generatedAt": "2026-02-19T00:00:00+00:00", "counts": {"companies": 1000, "analysis": 1000}}
    scan = {"entry": [{"stockCode": "2330", "status": "ENTRY", "summary": "last good"}], "watch": [], "excluded": []}
    worker, api, _db = build_router_api(
        monkeypatch,
        r2={"public/manifest.json": manifest, "public/market_scan_summary.json": scan},
    )

    async def fail_refresh(_manifest, force=False):
        raise worker.DependencyFailure("d1_write", False, RuntimeError("Network connection lost"))

    api.ensure_refresh_job = fail_refresh
    response = asyncio.run(api.fetch(RouteRequest(
        method="POST",
        path="/api/scan/market",
        body='{"refreshMode":"force"}',
    )))
    payload = json.loads(response.body)

    assert response.init["status"] == 200
    assert payload["entry"] == scan["entry"]
    assert payload["watch"] == scan["watch"]
    assert payload["excluded"] == scan["excluded"]
    assert payload["cacheStatus"]["refreshStatus"] == "unavailable"
    assert payload["cacheStatus"]["retryable"] is True
    assert payload["cacheStatus"]["requestId"] == response.headers["x-request-id"]
    record = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert record["stage"] == "refresh_queue"
```

Call `pin_worker_time(monkeypatch, worker, "2026-02-20T00:00:00+00:00")` before `api.fetch` so this test deterministically enters the stale refresh path.

- [ ] **Step 2: Run RED**

```powershell
..\..\.venv\Scripts\python.exe -m pytest -q -o filterwarnings= --basetemp C:\tmp\pytest-plan-a-task2-red tests\test_cloudflare_worker.py -k "scan_market_post_serves_last_good"
```

Expected: the current Worker returns 500.

- [ ] **Step 3: Implement the narrow fail-open boundary**

Load the manifest and scan before enqueueing, as today. Wrap only `ensure_refresh_job`:

```python
try:
    refresh_status = await self.ensure_refresh_job(manifest, force=refresh_mode == "force")
except DependencyFailure as exc:
    log_worker_failure(
        request_id=self._request_id,
        request=request,
        path=path,
        stage="refresh_queue",
        error_type=exc.error_type,
        error_code="REFRESH_QUEUE_UNAVAILABLE",
        status=503,
        duration_ms=0,
    )
    refresh_status = {
        "status": "unavailable",
        "reason": self.cache_policy()["reason"],
        "retryable": True,
        "requestId": self._request_id,
    }
```

Do not apply this catch to `/api/cache/refresh`, settings, authentication, holdings, reports, or admin routes.

- [ ] **Step 4: Run GREEN and the full Worker tests**

```powershell
..\..\.venv\Scripts\python.exe -m pytest -q -o filterwarnings= --basetemp C:\tmp\pytest-plan-a-task2-green tests\test_cloudflare_worker.py tests\test_api_worker_contracts.py
```

Expected: all selected tests pass.

- [ ] **Step 5: Commit Task 2**

```powershell
git add cloudflare\worker.py tests\test_cloudflare_worker.py
git commit -m "fix: serve cached scan when refresh queue fails"
```

---

### Task 3: Make health checks enforce the Worker's dynamic refresh policy

**Files:**
- Modify: `tests/test_cloudflare_worker.py:379-400`
- Modify: `tests/test_cloudflare_health_check.py`
- Modify: `cloudflare/worker.py:146-155`
- Modify: `scripts/check_cloudflare_health.py`
- Modify: `.github/workflows/cloudflare-health-monitor.yml`
- Modify: `.github/workflows/cloudflare-r2-seed-refresh.yml`

**Interfaces:**
- Produces: `/api/health.cacheStatus` using `Api.cache_status_from_manifest`.
- Produces: `validate_health_payload(..., max_refresh_delay_minutes: float | None)` with legacy fallback.
- Preserves: raw manifest at `/api/health.cache`, fixed 36-hour maximum-age safety ceiling, and rolling compatibility with old Workers lacking `cacheStatus`.

- [ ] **Step 1: Add Worker stale-boundary RED tests**

Freeze Worker time inside the monthly revenue window and use a manifest generated just over three hours earlier:

```python
def test_worker_health_degrades_at_dynamic_cache_boundary(monkeypatch):
    manifest = {
        "generatedAt": "2026-07-11T21:52:48+00:00",
        "sourceLastCheckedAt": "2026-07-11T21:52:48+00:00",
        "counts": {"companies": 1000, "analysis": 1000, "entry": 10, "watch": 980, "excluded": 10},
        "financialFreshness": {
            "status": "ok",
            "blocksDeployment": False,
            "expectedFinancialPeriod": "2026Q1",
            "latestCachedFinancialPeriod": "2026Q1",
        },
    }
    worker, api, _db = build_router_api(monkeypatch, r2={"public/manifest.json": manifest})
    pin_worker_time(monkeypatch, worker, "2026-07-12T00:52:49+00:00")
    response = asyncio.run(api.fetch(RouteRequest(path="/api/health")))
    payload = json.loads(response.body)
    assert response.init["status"] == 200
    assert payload["status"] == "degraded"
    assert payload["cacheStatus"]["isStale"] is True
    assert payload["cacheStatus"]["nextRefreshAfter"].startswith("2026-07-12T00:52:48")
    assert payload["cacheStatus"]["refreshReason"] == "monthly_revenue_window"
```

- [ ] **Step 2: Add health-script RED tests for grace and rolling fallback**

Add this complete helper to `tests/test_cloudflare_health_check.py`:

```python
def _healthy_health_payload() -> dict:
    return {
        "status": "ok",
        "runtime": "cloudflare-python-worker",
        "cache": {
            "generatedAt": "2026-07-11T21:52:48+00:00",
            "sourceLastCheckedAt": "2026-07-11T21:52:48+00:00",
            "counts": {"companies": 1000, "analysis": 1000, "entry": 10, "watch": 980, "excluded": 10},
            "financialFreshness": _fresh_financial_freshness(),
        },
        "cacheQuality": {"ok": True},
    }
```

```python
def test_validate_health_payload_rejects_policy_stale_after_grace():
    payload = _healthy_health_payload()
    payload["status"] = "degraded"
    payload["cacheStatus"] = {
        "isStale": True,
        "nextRefreshAfter": "2026-07-12T00:52:48+00:00",
    }
    with pytest.raises(RuntimeError, match="refresh policy"):
        validate_health_payload(
            payload,
            max_cache_age_hours=36,
            max_refresh_delay_minutes=15,
            now=datetime(2026, 7, 12, 1, 8, tzinfo=UTC),
        )


def test_validate_health_payload_accepts_policy_stale_inside_grace():
    payload = _healthy_health_payload()
    payload["status"] = "degraded"
    payload["cacheStatus"] = {
        "isStale": True,
        "nextRefreshAfter": "2026-07-12T00:52:48+00:00",
    }
    summary = validate_health_payload(
        payload,
        max_cache_age_hours=36,
        max_refresh_delay_minutes=15,
        now=datetime(2026, 7, 12, 1, 0, tzinfo=UTC),
    )
    assert summary["refreshDelayMinutes"] == pytest.approx(7.2)


def test_validate_health_payload_old_worker_uses_age_fallback():
    payload = _healthy_health_payload()
    payload.pop("cacheStatus", None)
    summary = validate_health_payload(
        payload,
        max_cache_age_hours=36,
        max_refresh_delay_minutes=15,
        now=datetime(2026, 7, 12, 1, 0, tzinfo=UTC),
    )
    assert summary["cacheAgeHours"] is not None
```

The test helper must include non-blocking financial freshness and cache quality.

- [ ] **Step 3: Run RED**

```powershell
..\..\.venv\Scripts\python.exe -m pytest -q -o filterwarnings= --basetemp C:\tmp\pytest-plan-a-task3-red tests\test_cloudflare_worker.py tests\test_cloudflare_health_check.py
```

Expected: missing `cacheStatus`, missing parameter, and current unconditional degraded rejection cause failures.

- [ ] **Step 4: Implement dynamic health status and grace**

In the Worker health route:

```python
quality = manifest_quality(manifest)
cache_status = self.cache_status_from_manifest(
    manifest,
    {"status": "not_requested", "reason": self.cache_policy()["reason"]},
)
return json_response({
    "status": "ok" if quality["ok"] and not cache_status["isStale"] else "degraded",
    "runtime": "cloudflare-python-worker",
    "time": utc_now(),
    "cache": manifest,
    "cacheStatus": cache_status,
    "cacheQuality": quality,
}, public_cache_seconds=60)
```

In `validate_health_payload`, calculate refresh delay from `cacheStatus.nextRefreshAfter`. If `cacheStatus` exists, allow `status="degraded"` only while the delay is within the configured grace and cache quality remains good. Once grace is exceeded, report a refresh-policy problem. If `cacheStatus` is absent, retain the existing `status == "ok"` and maximum-age behavior for rolling deploys.

Add CLI flag:

```python
parser.add_argument(
    "--max-refresh-delay-minutes",
    type=float,
    help="Allow this many minutes after cacheStatus.nextRefreshAfter before failing",
)
```

Pass `--max-refresh-delay-minutes 15` in both health and R2 workflows while keeping `--max-cache-age-hours 36`.

- [ ] **Step 5: Run GREEN and health workflow gates**

```powershell
..\..\.venv\Scripts\python.exe -m pytest -q -o filterwarnings= --basetemp C:\tmp\pytest-plan-a-task3-green tests\test_cloudflare_worker.py tests\test_cloudflare_health_check.py tests\test_remote_smoke.py tests\test_operational_readiness.py
..\..\.venv\Scripts\python.exe scripts\check_operational_readiness.py
```

Expected: all selected tests and operational readiness pass.

- [ ] **Step 6: Commit Task 3**

```powershell
git add cloudflare\worker.py scripts\check_cloudflare_health.py .github\workflows\cloudflare-health-monitor.yml .github\workflows\cloudflare-r2-seed-refresh.yml tests\test_cloudflare_worker.py tests\test_cloudflare_health_check.py tests\test_remote_smoke.py tests\test_operational_readiness.py
git commit -m "fix: align health with cache refresh policy"
```

---

### Task 4: Proactively refresh and fail open when the lightweight D1 check fails

**Files:**
- Modify: `tests/test_r2_refresh_workflow_scripts.py`
- Modify: `tests/test_deployment_preflight.py`
- Modify: `tests/test_operational_readiness.py`
- Modify: `scripts/r2_refresh_decision.py`
- Modify: `.github/workflows/cloudflare-r2-seed-refresh.yml`
- Modify: `.github/workflows/cloudflare-health-monitor.yml`

**Interfaces:**
- Produces: `refresh_due_at(health_payload) -> datetime | None`.
- Extends: `early_refresh_decision(..., job_check_error: str, refresh_ahead_minutes: float)`.
- Preserves: force, pending-job, unreachable-health, and 36-hour legacy paths.

- [ ] **Step 1: Add RED decision tests**

```python
def test_early_refresh_decision_refreshes_before_next_refresh_boundary():
    decision = r2.early_refresh_decision(
        force=False,
        pending_count=0,
        health_payload={"cacheStatus": {"nextRefreshAfter": "2026-07-12T02:00:00+00:00"}},
        health_error="",
        job_check_error="",
        health_url_configured=True,
        max_cache_age_hours=36,
        refresh_ahead_minutes=60,
        now=datetime(2026, 7, 12, 1, 5, tzinfo=UTC),
    )
    assert decision["runRefresh"] is True
    assert decision["staleRefresh"] is True


def test_early_refresh_decision_job_check_error_fails_open():
    decision = r2.early_refresh_decision(
        force=False,
        pending_count=0,
        health_payload={"cacheStatus": {"nextRefreshAfter": "2026-07-12T12:00:00+00:00"}},
        health_error="",
        job_check_error="D1 pending-job query unavailable",
        health_url_configured=True,
        max_cache_age_hours=36,
        refresh_ahead_minutes=60,
        now=datetime(2026, 7, 12, 1, 0, tzinfo=UTC),
    )
    assert decision["runRefresh"] is True
    assert any("pending-job" in message for message in decision["messages"])
```

Add a CLI test passing `--job-check-error` and `--refresh-ahead-minutes 60` and asserting `run_refresh=true` in the GitHub output file.

- [ ] **Step 2: Add workflow static RED tests**

Require:

```python
assert 'cron: "7,22,37,52 * * * *"' in r2_workflow
assert 'cron: "11,41 * * * *"' in health_workflow
assert "--refresh-ahead-minutes 60" in r2_workflow
assert "--job-check-error" in r2_workflow
assert "--max-refresh-delay-minutes 15" in health_workflow
```

Also assert the lightweight Cloudflare API request records a non-secret error marker when curl exits nonzero or returns `success:false`; never copy the response body into the marker.

- [ ] **Step 3: Run RED**

```powershell
..\..\.venv\Scripts\python.exe -m pytest -q -o filterwarnings= --basetemp C:\tmp\pytest-plan-a-task4-red tests\test_r2_refresh_workflow_scripts.py tests\test_deployment_preflight.py tests\test_operational_readiness.py
```

Expected: missing parameters, missing proactive policy, and old cron expressions fail.

- [ ] **Step 4: Implement the decision logic**

Parse `health_payload.cacheStatus.nextRefreshAfter`, falling back to `health_payload.cache.nextRefreshAfter`. Treat the refresh as due when:

```python
current >= next_refresh_after - timedelta(minutes=refresh_ahead_minutes)
```

If `job_check_error` is non-empty, set `stale_refresh=True` and enter heavy setup. Continue using `health_age_hours > max_cache_age_hours` only when no valid next-refresh timestamp exists.

Add CLI options:

```python
early.add_argument("--job-check-error", default="")
early.add_argument("--refresh-ahead-minutes", type=float, default=60)
```

In the workflow, capture curl exit status and Cloudflare API `success` without printing the payload. Pass the fixed marker `D1 pending-job query unavailable` when either check fails. Change R2 cron to `7,22,37,52 * * * *` and health cron to `11,41 * * * *`.

- [ ] **Step 5: Run GREEN and workflow gates**

```powershell
..\..\.venv\Scripts\python.exe -m pytest -q -o filterwarnings= --basetemp C:\tmp\pytest-plan-a-task4-green tests\test_r2_refresh_workflow_scripts.py tests\test_deployment_preflight.py tests\test_operational_readiness.py
..\..\.venv\Scripts\python.exe scripts\check_deployment_preflight.py
..\..\.venv\Scripts\python.exe scripts\check_operational_readiness.py
```

Expected: all selected tests and both static gates pass.

- [ ] **Step 6: Commit Task 4**

```powershell
git add scripts\r2_refresh_decision.py .github\workflows\cloudflare-r2-seed-refresh.yml .github\workflows\cloudflare-health-monitor.yml tests\test_r2_refresh_workflow_scripts.py tests\test_deployment_preflight.py tests\test_operational_readiness.py
git commit -m "fix: refresh seed ahead of policy expiry"
```

---

### Task 5: Retry safe browser reads and preserve last-good market rendering

**Files:**
- Modify: `tests/test_frontend_parser.py`
- Modify: `tests/test_frontend_hygiene.py`
- Modify: `tests/e2e/smoke.spec.ts`
- Modify: `frontend/api_client.js:1-149`
- Modify: `frontend/app.js:45,510-552,1374-1428`
- Modify: `frontend/index.html:417-422`
- Modify: `docs/cloudflare_deployment.md`
- Modify: `docs/current_architecture.md`

**Interfaces:**
- Produces: same-origin/direct GET retry with default two retries for network failures and 502/503/504 only.
- Produces: `state.marketScanWarning` and `.market-scan-warning` appended after last-good rendering.
- Preserves: no retry for status 400/401/403/404/429/500, no retry for mutations, existing cross-candidate failover including status 500, force-refresh ordering, page/expanded UI state on failure, and safe generic 5xx text.

- [ ] **Step 1: Add API-client RED tests**

Execute `frontend/api_client.js` in the existing Node harness and add these exact behaviors:

```javascript
const calls = [];
let attempt = 0;
global.fetch = async (url, options) => {
  calls.push({ url, method: options.method || "GET" });
  attempt += 1;
  return response(attempt === 1 ? 503 : attempt === 2 ? 502 : 200);
};
const client = createApiClient({
  apiMode: "direct",
  fallbackOrigin: "https://worker.example",
  getRetryCount: 2,
  retryDelaysMs: [0, 0],
});
const finalResponse = await client.request("/api/scan/market", { method: "GET" });
assert.equal(finalResponse.status, 200);
assert.deepEqual(calls.map((call) => call.url), [
  "https://worker.example/api/scan/market",
  "https://worker.example/api/scan/market",
  "https://worker.example/api/scan/market",
]);
```

Add separate cases for one transport exception then success, three 503 responses capped at three attempts, direct GET 500 called once, direct GET 400 called once, and direct POST 503 called once. Keep the existing fallback test and its same-origin → Worker ordering unchanged.

- [ ] **Step 2: Add last-good Playwright RED test**

```typescript
test("manual refresh failure preserves last successful market scan", async ({ page, isMobile }) => {
  await page.goto("/");
  await closeBlockingModals(page);
  await showView(page, isMobile, "scan");
  const oldTime = await page.locator("#scan-time").textContent();
  const oldFirstRow = await page.locator("#market-results .market-result-item").first().textContent();

  await page.route("**/api/scan/market", async (route) => {
    if (route.request().method() === "POST") {
      await route.fulfill({
        status: 503,
        contentType: "application/json",
        body: JSON.stringify({
          detail: "伺服器暫時無法處理請求，請稍後再試。",
          code: "DEPENDENCY_UNAVAILABLE",
          requestId: "test-request-id",
          retryable: true,
          stage: "d1_write",
        }),
      });
      return;
    }
    await route.continue();
  });

  await page.locator("#refresh-market-scan-btn").click();
  await expect(page.locator(".market-scan-warning")).toContainText("仍顯示");
  await expect(page.locator(".market-scan-warning")).toContainText("test-request-id");
  await expect(page.locator("#scan-time")).toHaveText(oldTime || "");
  await expect(page.locator("#market-results .market-result-item").first()).toContainText(oldFirstRow || "");
  await expect(page.locator("#market-results > .form-error")).toHaveCount(0);
});
```

Add this second test; reuse the existing valid market fixture returned by the E2E web server and replace only its first entry:

```typescript
test("successful market refresh clears last-good warning", async ({ page, isMobile }) => {
  await page.goto("/");
  await closeBlockingModals(page);
  await showView(page, isMobile, "scan");
  const successfulScan = await page.evaluate(async () => (await fetch("/api/scan/market")).json());

  let fail = true;
  await page.route("**/api/scan/market", async (route) => {
    if (route.request().method() !== "POST") {
      await route.continue();
      return;
    }
    if (fail) {
      await route.fulfill({
        status: 503,
        contentType: "application/json",
        body: JSON.stringify({ detail: "伺服器暫時無法處理請求，請稍後再試。", requestId: "retry-id" }),
      });
      return;
    }
    successfulScan.entry = [{ stockCode: "2454", companyName: "聯發科", status: "ENTRY", reasons: [] }];
    successfulScan.watch = [];
    successfulScan.excluded = [];
    successfulScan.generatedAt = "2026-07-12T02:00:00+00:00";
    await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(successfulScan) });
  });

  await page.locator("#refresh-market-scan-btn").click();
  await expect(page.locator(".market-scan-warning")).toHaveCount(1);
  fail = false;
  await page.locator("#refresh-market-scan-btn").click();
  await expect(page.locator(".market-scan-warning")).toHaveCount(0);
  await expect(page.locator(".market-result-code strong").first()).toHaveText("2454");
});
```

- [ ] **Step 3: Run RED**

```powershell
..\..\.venv\Scripts\python.exe -m pytest -q -o filterwarnings= --basetemp C:\tmp\pytest-plan-a-task5-red tests\test_frontend_parser.py tests\test_frontend_hygiene.py
npm.cmd run test:e2e -- --grep "last successful market scan"
```

Expected: same-origin retry tests and last-good warning test fail.

- [ ] **Step 4: Implement bounded read retry**

Keep `RETRYABLE_API_STATUSES` for cross-candidate failover and add a separate set:

```javascript
const SAME_ENDPOINT_RETRYABLE_STATUSES = new Set([502, 503, 504]);
const IDEMPOTENT_API_METHODS = new Set(["GET", "HEAD"]);
const DEFAULT_GET_RETRY_DELAYS_MS = [150, 450];
```

Implement retry rounds, not per-candidate retry loops. Within each round, walk the existing candidate list once so fallback mode still moves immediately from same-origin to Worker. Start another round only after every candidate in that round ended in a network exception or 502/503/504 and the method is idempotent. Status 500 returns immediately in direct mode but still participates in the existing same-origin → Worker failover. Use injected `retryDelaysMs` for deterministic zero-delay tests and native `setTimeout` for production delay. Mutating methods always get the existing single round and are never retried.

- [ ] **Step 5: Implement last-good warning rendering**

Add `marketScanWarning: null` to state. On a reveal refresh, call `setEmptyState` only when `state.marketScan` is absent. On success, clear the warning before updating `state.marketScan`.

Add a `renderMarketScanWarning(target)` helper that appends the warning whenever `renderMarketResults()` renders while `state.marketScanWarning` is non-null. On failure with existing data:

```javascript
state.marketScanWarning = error.message || "更新失敗";
renderMarketResults();
return state.marketScan;
```

The helper creates a `p`, assigns `className = "data-source-note market-scan-warning"`, sets text with the unchanged `state.marketScan.generatedAt` time plus the safe error text, and prepends it to `#market-results`. This keeps the warning visible after paging or view re-render. When there is no last-good state, retain the current blocking `setFormError`. Do not call `resetMarketListUi()` on failure. Parse the safe JSON `requestId` in `apiErrorMessage` and append `（錯誤編號：<id>）` only when it matches `/^[A-Za-z0-9._:-]{1,80}$/`.

- [ ] **Step 6: Update cache busters and correct the proxy runbook**

Set `api_client.js` to `v=20260712-resilient-api` and `app.js` to `v=20260712-last-good-scan` in `frontend/index.html`. Add `API_CLIENT_VERSION = "20260712-resilient-api"`, set `APP_VERSION = "20260712-last-good-scan"`, and update their assertions in `tests/test_frontend_hygiene.py`. Correct `docs/cloudflare_deployment.md` and `docs/current_architecture.md` to state that the live Pages Function proxies `/api/*` with 200 responses while the production browser normally uses direct Worker mode; remove the obsolete 307 verification step and command.

- [ ] **Step 7: Run GREEN frontend verification sequentially**

```powershell
..\..\.venv\Scripts\python.exe -m pytest -q -o filterwarnings= --basetemp C:\tmp\pytest-plan-a-task5-green tests\test_frontend_parser.py tests\test_frontend_hygiene.py tests\test_pages_api_redirect.py
npm.cmd run lint
npm.cmd run format:check
npm.cmd run test:e2e
..\..\.venv\Scripts\python.exe scripts\check_frontend_hygiene.py
```

Expected: pytest passes, lint/format pass, Playwright reports 23 passed and 1 desktop-only skip after the two new cross-project tests, and hygiene passes.

- [ ] **Step 8: Commit Task 5**

```powershell
git add frontend\api_client.js frontend\app.js frontend\index.html docs\cloudflare_deployment.md docs\current_architecture.md tests\test_frontend_parser.py tests\test_frontend_hygiene.py tests\test_pages_api_redirect.py tests\e2e\smoke.spec.ts
git commit -m "fix: preserve scans across transient read failures"
```

---

## Plan A Final Gate

After Tasks 1–5 have passed task review, run these commands sequentially from the worktree. Do not start Plan B if any command fails.

```powershell
..\..\.venv\Scripts\python.exe -m pytest -q -o filterwarnings= --basetemp C:\tmp\pytest-plan-a-final
npm.cmd run lint
npm.cmd run format:check
..\..\.venv\Scripts\python.exe scripts\check_frontend_hygiene.py
..\..\.venv\Scripts\python.exe scripts\check_operational_readiness.py
..\..\.venv\Scripts\python.exe scripts\check_deployment_preflight.py
npx.cmd wrangler deploy --config cloudflare\wrangler.toml --dry-run --outdir C:\tmp\stock-worker-plan-a-final
..\..\.venv\Scripts\python.exe scripts\run_wrangler_dev_smoke.py
npm.cmd run test:e2e
```

Then perform read-only production GET/OPTIONS smoke without asserting that the undeployed Plan A behavior is live:

```powershell
curl.exe -sS -o NUL -w "health=%{http_code} bytes=%{size_download} time=%{time_total}\n" https://stock-scanner-beta-api.pcedison.workers.dev/api/health
curl.exe -sS -o NUL -w "market=%{http_code} bytes=%{size_download} time=%{time_total}\n" https://stock-scanner-beta-api.pcedison.workers.dev/api/scan/market
curl.exe -sS -X OPTIONS -H "Origin: https://stock-scanner-beta.pages.dev" -H "Access-Control-Request-Method: POST" -H "Access-Control-Request-Headers: content-type,x-stock-scanner-csrf" -o NUL -w "cors=%{http_code}\n" https://stock-scanner-beta-api.pcedison.workers.dev/api/scan/market
```

Expected: local gate commands exit 0; current production reads return health 200, market 200, and CORS 204. Record the Plan A final commit, review result, and exact command outputs in the SDD progress ledger before writing the Plan B implementation plan.
