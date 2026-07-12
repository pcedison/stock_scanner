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

### Task 1A: Repair observability findings before proceeding

**Files:**
- Modify: `tests/test_cloudflare_worker.py`
- Modify: `tests/test_deployment_preflight.py`
- Modify: `tests/test_code_size_budgets.py`
- Modify: `cloudflare/worker.py`
- Modify: `cloudflare/worker_observability.py`
- Modify: `scripts/check_deployment_preflight.py`
- Modify: `scripts/check_code_size_budgets.py`

**Blocking review findings:**
- D1 read failures classified as `OVERLOADED` or `TIMEOUT` are safe fixed log classifications, but they are not advertised as retryable. Only network loss, reset/code-update/storage-reset, and transient remote-node failures are retryable read failures.
- An exception from `await r2_object.text()` is a retryable `r2_read` dependency failure. A successfully read but malformed JSON artifact may retain the existing fallback behavior.
- Sanitized `DependencyFailure` objects must not retain the raw exception through `__context__`, `__cause__`, or a reachable traceback. Build the safe failure inside the handler, leave the `except` block, and only then raise it.
- An `Api(env)` initialization failure for an allowed Pages origin must retain safe CORS/security headers and `X-Request-ID`, using CORS calculation that does not depend on successful API initialization.
- Malformed observability TOML types return actionable preflight problems instead of raising `AttributeError`.
- The 200-line budget for `cloudflare/worker_observability.py` is enforced by the normal size gate. Do not raise any existing budget.

- [ ] **Step 1: Add RED regression tests**

Add focused tests that prove:

1. `OVERLOADED` and `TIMEOUT` D1 reads return HTTP 500, `DEPENDENCY_FAILURE`, and `retryable=false`; `NETWORK_LOST`, `RESET`, and `TRANSIENT_REMOTE_NODE` remain HTTP 503 and retryable.
2. R2 object-body I/O failure returns the safe `r2_read` 503 envelope, while valid body I/O followed by malformed JSON retains the documented fallback.
3. A secret sentinel placed in a raw R2/D1 exception is absent from the response/log and unreachable through `DependencyFailure.__context__`, `__cause__`, and traceback frames.
4. Runtime-security initialization failure with the configured Pages `Origin` returns the safe envelope with matching request ID, security headers, and `Access-Control-Allow-Origin`.
5. Scalar/list values at `observability`, `observability.logs`, or `observability.traces` produce validation problems rather than an exception.
6. The automated size-budget map contains `cloudflare/worker_observability.py: 200`.

- [ ] **Step 2: Run RED**

```powershell
..\..\.venv\Scripts\python.exe -m pytest -q -o filterwarnings= --basetemp C:\tmp\pytest-plan-a-task1a-red tests\test_cloudflare_worker.py tests\test_deployment_preflight.py tests\test_code_size_budgets.py
```

Expected: each new regression fails for the reviewed reason.

- [ ] **Step 3: Implement the narrow repairs**

Keep fixed error classification separate from retryability, split R2 body I/O from JSON decoding, raise sanitized failures outside active exception handlers, and reuse a pure CORS calculation in both normal and initialization-failure paths. Preserve successful payloads and all Task 1 security constraints.

- [ ] **Step 4: Run all Task 1 gates**

```powershell
..\..\.venv\Scripts\python.exe -m pytest -q -o filterwarnings= --basetemp C:\tmp\pytest-plan-a-task1a-green tests\test_cloudflare_worker.py tests\test_deployment_preflight.py tests\test_code_size_budgets.py
..\..\.venv\Scripts\python.exe scripts\check_code_size_budgets.py
..\..\.venv\Scripts\python.exe scripts\check_deployment_preflight.py
npx.cmd wrangler deploy --config cloudflare\wrangler.toml --dry-run --outdir C:\tmp\stock-worker-task1a-dry-run
..\..\.venv\Scripts\python.exe -m pytest -q -o filterwarnings= --basetemp C:\tmp\pytest-plan-a-task1a-full
```

Expected: focused tests, all static gates, Wrangler bundling, and full pytest pass.

- [ ] **Step 5: Commit and re-review Task 1A**

```powershell
git add cloudflare\worker.py cloudflare\worker_observability.py scripts\check_deployment_preflight.py scripts\check_code_size_budgets.py tests\test_cloudflare_worker.py tests\test_deployment_preflight.py tests\test_code_size_budgets.py
git commit -m "fix: harden worker failure boundaries"
```

Do not begin Task 2 until an independent reviewer reports no Critical or Important findings for the combined Task 1 range.

---

### Task 2: Preserve the market scan when refresh enqueue fails

**Files:**
- Modify: `tests/test_cloudflare_worker.py:1255-1267`
- Modify: `tests/test_code_size_budgets.py`
- Modify: `cloudflare/worker.py:249-259`
- Modify: `cloudflare/worker_observability.py`
- Create: `cloudflare/worker_market_resilience.py` (maximum 160 lines; no Worker/JS globals)
- Modify: `scripts/check_code_size_budgets.py`

**Interfaces:**
- Consumes: Task 1 request ID and structured failure logger.
- Produces: legacy market POST success with `cacheStatus.refreshStatus="unavailable"` only when a verified last-good R2 scan was already loaded and `ensure_refresh_job` reports a D1 dependency failure.
- Produces: a focused market-resilience helper that owns only scan verification, refresh enqueue degradation, safe logging, and unavailable cache-status metadata.
- Preserves: the same compacted arrays as the successful GET, the dependency's original retryability, and no fail-open behavior for malformed/missing R2 scans, non-D1 failures, unexpected exceptions, or any other route.

- [ ] **Step 1: Write the failing degradation test**

```python
@pytest.mark.parametrize(
    ("stage", "retryable"),
    (("d1_read", True), ("d1_write", False)),
)
def test_worker_scan_market_post_serves_last_good_when_refresh_queue_fails(
    monkeypatch, capsys, stage, retryable
):
    manifest = {"generatedAt": "2026-02-19T00:00:00+00:00", "counts": {"companies": 1000, "analysis": 1000}}
    scan = {"entry": [{"stockCode": "2330", "status": "ENTRY", "summary": "last good"}], "watch": [], "excluded": []}
    worker, api, _db = build_router_api(
        monkeypatch,
        r2={"public/manifest.json": manifest, "public/market_scan_summary.json": scan},
    )
    expected = worker.compact_market_scan(scan)

    async def fail_refresh(_manifest, force=False):
        failure = worker.DependencyFailure(stage, retryable, RuntimeError("secret sentinel"))
        failure.error_code = "NETWORK_LOST"
        raise failure

    api.ensure_refresh_job = fail_refresh
    response = asyncio.run(api.fetch(RouteRequest(
        method="POST",
        path="/api/scan/market",
        body='{"refreshMode":"force"}',
    )))
    payload = json.loads(response.body)

    assert response.init["status"] == 200
    assert payload["entry"] == expected["entry"]
    assert payload["watch"] == expected["watch"]
    assert payload["excluded"] == expected["excluded"]
    assert payload["cacheStatus"]["refreshStatus"] == "unavailable"
    assert payload["cacheStatus"]["retryable"] is retryable
    assert payload["cacheStatus"]["requestId"] == response.headers["x-request-id"]
    record = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert record["event"] == "worker_dependency_degraded"
    assert record["stage"] == "refresh_queue"
    assert record["status"] == 200
    assert record["errorCode"] == "NETWORK_LOST"
    assert record["requestId"] == response.headers["x-request-id"]
    assert record["durationMs"] >= 0
    assert "secret sentinel" not in json.dumps(record)
```

The request uses `refreshMode="force"`; no clock pin is needed. Also add negative tests proving that a missing/malformed scan plus the same queue failure, a non-D1 `DependencyFailure`, and an unexpected `RuntimeError` do not return HTTP 200. Keep or extend sibling-route regressions so `/api/cache/refresh`, auth, settings, holdings, reports, and admin failures remain normal failures.

- [ ] **Step 2: Run RED**

```powershell
..\..\.venv\Scripts\python.exe -m pytest -q -o filterwarnings= --basetemp C:\tmp\pytest-plan-a-task2-red tests\test_cloudflare_worker.py -k "scan_market_post_serves_last_good"
```

Expected: the current Worker returns a dependency error and lacks the narrow unavailable metadata/log event.

- [ ] **Step 3: Implement the narrow fail-open boundary**

Load the manifest and scan before enqueueing, as today. Before the call, record whether the R2 value is a verified scan: it must be a dict and each of `entry`, `watch`, and `excluded` must be a list. Wrap only the complete `ensure_refresh_job` call (its existing-job read, cleanup write, and insert write):

```python
try:
    refresh_status = await self.ensure_refresh_job(manifest, force=refresh_mode == "force")
except DependencyFailure as exc:
    if not has_last_good or exc.stage not in {"d1_read", "d1_write"}:
        raise
    log_worker_failure(
        event="worker_dependency_degraded",
        request_id=self._request_id,
        request=request,
        path=path,
        stage="refresh_queue",
        error_type=exc.error_type,
        error_code=getattr(exc, "error_code", "UNCLASSIFIED"),
        status=200,
        duration_ms=duration_ms(refresh_started),
    )
    refresh_status = {
        "status": "unavailable",
        "reason": self.cache_policy()["reason"],
        "retryable": exc.retryable,
        "requestId": self._request_id,
    }
```

Implement the catch in `worker_market_resilience.py` and call it narrowly from the legacy market POST route. After `cache_status_from_manifest` returns, use the helper to add `retryable` and `requestId` to that route-local cache-status dict only when refresh status is unavailable; do not expect `cache_status_from_manifest` to preserve arbitrary keys. Keep dependency classification (`errorCode`) distinct from the business degradation event. Do not apply this catch to `/api/cache/refresh`, settings, authentication, holdings, reports, or admin routes.

Add `cloudflare/worker_market_resilience.py: 160` to the normal size-budget map/test. Do not raise any existing budget, and keep both the Worker and new module readable rather than compressing long statements.

- [ ] **Step 4: Run GREEN and the full Worker tests**

```powershell
..\..\.venv\Scripts\python.exe -m pytest -q -o filterwarnings= --basetemp C:\tmp\pytest-plan-a-task2-green tests\test_cloudflare_worker.py tests\test_api_worker_contracts.py tests\test_code_size_budgets.py
..\..\.venv\Scripts\python.exe scripts\check_code_size_budgets.py
npx.cmd wrangler deploy --config cloudflare\wrangler.toml --dry-run --outdir C:\tmp\stock-worker-task2-dry-run
```

Expected: all selected tests pass.

- [ ] **Step 5: Commit Task 2**

```powershell
git add cloudflare\worker.py cloudflare\worker_observability.py cloudflare\worker_market_resilience.py scripts\check_code_size_budgets.py tests\test_cloudflare_worker.py tests\test_code_size_budgets.py
git commit -m "fix: serve cached scan when refresh queue fails"
```

---

### Task 3: Make health checks enforce the Worker's dynamic refresh policy

**Files:**
- Modify: `tests/test_cloudflare_worker.py:379-400`
- Modify: `tests/test_cloudflare_health_check.py`
- Modify: `tests/test_remote_smoke.py`
- Modify: `tests/test_code_size_budgets.py`
- Modify: `cloudflare/worker.py:146-155`
- Modify: `cloudflare/worker_support.py:127-133`
- Create: `cloudflare/worker_health.py` (maximum 120 lines; payload composition only, no Worker/JS globals)
- Modify: `scripts/check_cloudflare_health.py`
- Modify: `scripts/run_remote_smoke.py`
- Modify: `scripts/check_code_size_budgets.py`
- Modify: `.github/workflows/cloudflare-health-monitor.yml`
- Modify: `.github/workflows/cloudflare-r2-seed-refresh.yml`

**Interfaces:**
- Produces: `/api/health.cacheStatus` using `Api.cache_status_from_manifest`.
- Produces: a focused health payload composer so the 880-line Worker budget is not raised or compressed.
- Produces: `validate_health_payload(..., max_refresh_delay_minutes: float | None)` with legacy fallback.
- Produces: Worker timestamp parsing that treats naive ISO timestamps as UTC, matching both operational scripts.
- Preserves: raw manifest at `/api/health.cache`, fixed 36-hour maximum-age safety ceiling, strict default behavior for callers that omit the new grace, and rolling compatibility with old Workers lacking `cacheStatus`.

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
    pin_worker_time(monkeypatch, worker, "2026-07-12T00:52:48+00:00")
    response = asyncio.run(api.fetch(RouteRequest(path="/api/health")))
    payload = json.loads(response.body)
    assert response.init["status"] == 200
    assert payload["status"] == "degraded"
    assert payload["cacheStatus"]["isStale"] is True
    assert payload["cacheStatus"]["nextRefreshAfter"].startswith("2026-07-12T00:52:48")
    assert payload["cacheStatus"]["refreshReason"] == "monthly_revenue_window"
```

Add before/exact/after boundary cases. At exactly `nextRefreshAfter`, `isStale` is true; one second before it is false. Add `parse_time` parity tests for a naive timestamp, `Z`, `+08:00`, and invalid text. The existing health-entrypoint fixture must include a valid `generatedAt` so its `status="ok"` assertion still describes a fresh cache.

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


def test_validate_health_payload_accepts_exact_grace_boundary():
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
        now=datetime(2026, 7, 12, 1, 7, 48, tzinfo=UTC),
    )
    assert summary["refreshDelayMinutes"] == pytest.approx(15)


def test_validate_health_payload_new_worker_default_is_strict():
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
            now=datetime(2026, 7, 12, 1, 0, tzinfo=UTC),
        )


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

The test helper must include non-blocking financial freshness and cache quality. Add a raw-manifest/top-level `nextRefreshAfter` precedence test and a remote-smoke forwarding test. `run_public_smoke` and its CLI accept `max_refresh_delay_minutes=None` and pass it unchanged to `validate_health_payload`; the default remains strict for other callers and old Workers.

- [ ] **Step 3: Run RED**

```powershell
..\..\.venv\Scripts\python.exe -m pytest -q -o filterwarnings= --basetemp C:\tmp\pytest-plan-a-task3-red tests\test_cloudflare_worker.py tests\test_cloudflare_health_check.py tests\test_remote_smoke.py
```

Expected: missing `cacheStatus`, missing parameter, and current unconditional degraded rejection cause failures.

- [ ] **Step 4: Implement dynamic health status and grace**

In `worker_health.py`, compose the health payload from the manifest, quality, and the cache-status/policy callbacks. The Worker route delegates to that helper:

```python
def health_payload(manifest, manifest_quality, cache_status_from_manifest, cache_policy, utc_now):
    quality = manifest_quality(manifest)
    policy = cache_policy()
    cache_status = cache_status_from_manifest(
        manifest,
        {"status": "not_requested", "reason": policy["reason"]},
    )
    return {
        "status": "ok" if quality["ok"] and not cache_status["isStale"] else "degraded",
        "runtime": "cloudflare-python-worker",
        "time": utc_now(),
        "cache": manifest,
        "cacheStatus": cache_status,
        "cacheQuality": quality,
    }
```

The Worker passes its existing functions/methods to `health_payload` and wraps the returned dict with `json_response(..., public_cache_seconds=60)`.

In `validate_health_payload`, calculate refresh delay from `cacheStatus.nextRefreshAfter`. If `cacheStatus` exists, allow `status="degraded"` only while the delay is within the configured grace and cache quality remains good. Once grace is exceeded, report a refresh-policy problem. If `cacheStatus` is absent, retain the existing `status == "ok"` and maximum-age behavior for rolling deploys.

Normalize timestamps consistently: naive ISO values are UTC, aware values are converted to UTC, and invalid values return `None`.

Add `cloudflare/worker_health.py: 120` to the normal size-budget map/test. Do not raise any existing budget. The module must not import Worker/JS globals or duplicate cache policy/date logic.

Add CLI flag:

```python
parser.add_argument(
    "--max-refresh-delay-minutes",
    type=float,
    help="Allow this many minutes after cacheStatus.nextRefreshAfter before failing",
)
```

Pass `--max-refresh-delay-minutes 15` in both the direct health checker and `run_remote_smoke.py` invocation in the health workflow, and in the R2 workflow checker, while keeping `--max-cache-age-hours 36` everywhere.

- [ ] **Step 5: Run GREEN and health workflow gates**

```powershell
..\..\.venv\Scripts\python.exe -m pytest -q -o filterwarnings= --basetemp C:\tmp\pytest-plan-a-task3-green tests\test_cloudflare_worker.py tests\test_cloudflare_health_check.py tests\test_remote_smoke.py tests\test_operational_readiness.py
..\..\.venv\Scripts\python.exe scripts\check_operational_readiness.py
..\..\.venv\Scripts\python.exe scripts\check_code_size_budgets.py
npx.cmd wrangler deploy --config cloudflare\wrangler.toml --dry-run --outdir C:\tmp\stock-worker-task3-dry-run
```

Expected: all selected tests and operational readiness pass.

- [ ] **Step 6: Commit Task 3**

```powershell
git add cloudflare\worker.py cloudflare\worker_support.py cloudflare\worker_health.py scripts\check_cloudflare_health.py scripts\run_remote_smoke.py scripts\check_code_size_budgets.py .github\workflows\cloudflare-health-monitor.yml .github\workflows\cloudflare-r2-seed-refresh.yml tests\test_cloudflare_worker.py tests\test_cloudflare_health_check.py tests\test_remote_smoke.py tests\test_operational_readiness.py tests\test_code_size_budgets.py
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
- Extends: `early_refresh_decision(..., job_check_error: str = "", refresh_ahead_minutes: float = 60)` without breaking direct callers.
- Preserves: force, pending-job, unreachable/non-OK health, policy-stale, and unconditional 36-hour safety-ceiling paths.

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
    assert decision["staleRefresh"] is False
    assert any("pending-job" in message for message in decision["messages"])
```

Add before/exact/after proactive-boundary tests (one second before the 60-minute window does not refresh; exactly entering it does). Add cases for `cacheStatus.isStale=true` with a future timestamp, `status="degraded"` with a future timestamp, age 37 hours with a future timestamp, and healthy/future/no-other-trigger not refreshing. Add one function test and one CLI test that omit `refresh_ahead_minutes`/`--refresh-ahead-minutes` at 55 minutes before the boundary and prove the real default is 60. Add a CLI test passing `--job-check-error` and asserting `run_refresh=true` but `stale_refresh=false` in the GitHub output file.

- [ ] **Step 2: Add workflow static RED tests**

Parse the active YAML schedules and require:

```python
assert r2_schedules == ["7,22,37,52 * * * *"]
assert health_schedules == ["11,41 * * * *"]
assert "--refresh-ahead-minutes 60" in r2_workflow
assert "--job-check-error" in r2_workflow
assert "--max-refresh-delay-minutes 15" in health_workflow
assert "steps.early-check.outputs.stale_refresh" in r2_workflow
```

Also assert the lightweight Cloudflare API request records a fixed non-secret error marker for any of: curl nonzero, malformed/non-object JSON, outer `success` other than exact `true`, query-level failure, missing/non-integer count, or explicit `success:false`. Valid `cnt=0` remains a successful zero-pending result. Use a secret sentinel in the rejected body/error and assert it is absent from decision JSON, stdout, stderr, GitHub output, and summary.

- [ ] **Step 3: Run RED**

```powershell
..\..\.venv\Scripts\python.exe -m pytest -q -o filterwarnings= --basetemp C:\tmp\pytest-plan-a-task4-red tests\test_r2_refresh_workflow_scripts.py tests\test_deployment_preflight.py tests\test_operational_readiness.py
```

Expected: missing parameters, missing proactive policy, and old cron expressions fail.

- [ ] **Step 4: Implement the decision logic**

Parse `health_payload.cacheStatus.nextRefreshAfter`, falling back to `health_payload.cache.nextRefreshAfter`. Compute `stale_refresh` as the OR of:

- health fetch error;
- non-`ok` health status;
- `cacheStatus.isStale is True`;
- the proactive next-refresh boundary;
- cache age exceeding 36 hours.

Treat the proactive boundary as due when:

```python
current >= next_refresh_after - timedelta(minutes=refresh_ahead_minutes)
```

If `job_check_error` is non-empty, set `runRefresh=True` but keep `staleRefresh=False`: this enters heavy setup so the authoritative D1 query can run, without claiming that the seed itself is stale. The cache-age ceiling is unconditional even when a valid but incorrect future next-refresh timestamp exists; cache age is merely the sole normal-policy fallback when there is no valid next-refresh timestamp.

Add CLI options:

```python
early.add_argument("--job-check-error", default="")
early.add_argument("--refresh-ahead-minutes", type=float, default=60)
```

In the workflow, capture curl exit status and validate the response shape without printing the payload. Pass only the fixed marker `D1 pending-job query unavailable` for every invalid/error shape listed above. Initialize the authoritative `refresh-check` step's `stale_refresh` from `steps.early-check.outputs.stale_refresh`, then OR in its own health-check failure before computing `run_refresh`. This carries proactive/non-OK/`isStale`/36-hour evidence through to the actual rebuild. A job-check error alone keeps early `stale_refresh=false`, so the second D1 query still decides whether to rebuild. Add a workflow regression that validates this data flow and final OR, not merely the presence of the string. Change R2 cron to `7,22,37,52 * * * *` and health cron to `11,41 * * * *`.

The R2 workflow is already near its fixed 260-line budget. Keep it within that existing limit by moving response validation/decision logic into `r2_refresh_decision.py` and simplifying shell/comments; do not raise the workflow budget or compress shell into opaque one-liners.

This fail-open guarantee is deliberately scoped to the lightweight early decision: the workflow must not incorrectly skip a needed rebuild because the pending-job check failed. Later migrations/full D1 operations retain their existing failure semantics; a sustained D1 outage is not claimed to permit a complete R2 rebuild in Task 4.

- [ ] **Step 5: Run GREEN and workflow gates**

```powershell
..\..\.venv\Scripts\python.exe -m pytest -q -o filterwarnings= --basetemp C:\tmp\pytest-plan-a-task4-green tests\test_r2_refresh_workflow_scripts.py tests\test_deployment_preflight.py tests\test_operational_readiness.py
..\..\.venv\Scripts\python.exe scripts\check_deployment_preflight.py
..\..\.venv\Scripts\python.exe scripts\check_operational_readiness.py
..\..\.venv\Scripts\python.exe scripts\check_code_size_budgets.py
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
- Modify: `tests/test_pages_api_redirect.py`
- Modify: `tests/e2e/smoke.spec.ts`
- Modify: `frontend/api_client.js:1-149`
- Modify: `frontend/app.js:45,510-552,1374-1428`
- Modify: `frontend/market_render.js:137-139`
- Modify: `frontend/index.html:417-422`
- Modify: `docs/cloudflare_deployment.md`
- Modify: `docs/current_architecture.md`

**Interfaces:**
- Produces: same-origin/direct GET/HEAD retry with default two retry rounds for network failures and 502/503/504 only.
- Produces: round-based safe-method candidate ordering, `state.marketScanWarning`, and a visible `role="status"` `.market-scan-warning` prepended after last-good rendering.
- Preserves: status 500 as cross-candidate failover only (never same-endpoint retry), no retry/failover for 400/401/403/404/429, force-refresh ordering, page/expanded UI state on failure, and safe generic 5xx text.
- Changes intentionally: POST/PUT/PATCH/DELETE now use exactly one primary candidate and one fetch call. The legacy same-origin-to-Worker mutation replay is removed because an upstream timeout can leave commit outcome ambiguous.
- Scope: Plan A bounds attempt count but does not add a new per-attempt wall-clock timeout. Caller `AbortError` is terminal and aborts any pending backoff.

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

Add the complete matrix below; assert exact URLs/methods and call counts:

- direct and same-origin `503 → 502 → 200`: three calls to the same URL, including a case that uses the real default retry count rather than injecting it;
- fallback `same 503 → worker 503 → same 200`: three calls in that exact order, proving rounds rather than per-candidate loops;
- fallback `same 500 → worker 200`: two calls, while `same 503 → worker 500` returns that 500 after two calls and does not open another round;
- direct/same-origin all-transient: at most three calls; fallback all-transient: at most six calls;
- 400 and 429 return after one candidate/call; direct 500 returns after one call;
- omitted method and explicit GET retry; HEAD retries safely; POST/PUT/PATCH/DELETE make exactly one fetch and never select the second fallback candidate;
- a normal `TypeError` transport failure may retry, but a caller `AbortError` rejects after one call and aborting during backoff prevents the next call;
- mutating JSON/string and one-shot `ReadableStream` bodies are each observed at most once.

Update the legacy POST fallback tests to the new one-call safety contract. Use injected zero delays for deterministic tests; do not add a per-attempt timeout in Plan A.

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
          stage: "r2_read",
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

Use a fixture with at least seven results. Before the failed refresh, navigate the active column to page 2 and expand a row. After failure, assert the page indicator, expanded row/content, scan timestamp, and result data are unchanged; toggle tab/column to force a rerender and assert the warning remains the first visible child. On mobile, also assert `scrollWidth <= clientWidth` and that the warning is visible, not merely present in the DOM.

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
    const activePeriod = successfulScan.filingContext?.activeFinancialReport?.period;
    successfulScan.entry = successfulScan.entry.map((item, index) => {
      if (index !== 0) return item;
      const reasons = (item.reasons || []).filter((reason) => reason?.code !== "OFFICIAL_Q");
      reasons.push({ code: "OFFICIAL_Q", passed: true, severity: "INFO", message: `${activePeriod} 官方季報已公告` });
      return { ...item, stockCode: "2454", companyName: "聯發科", reasons };
    });
    successfulScan.generatedAt = "2026-07-12T02:00:00+00:00";
    await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(successfulScan) });
  });

  await page.locator("#refresh-market-scan-btn").click();
  await expect(page.locator(".market-scan-warning")).toHaveCount(1);
  fail = false;
  await page.locator("#refresh-market-scan-btn").click();
  await expect(page.locator(".market-scan-warning")).toHaveCount(0);
  await expect(page.locator(".market-result-code strong").filter({ hasText: "2454" })).toHaveCount(1);
});
```

Cover warning lifecycle outside the two E2E cases with focused tests/source harnesses: scheduler-provided scans clear/derive the warning through the same acceptance helper; background GET/shared-promise failures use the same failure helper; and a Task 2 HTTP 200 payload with `cacheStatus.refreshStatus="unavailable"` displays a Chinese non-blocking warning plus a valid request ID. A later normal success clears it.

The successful fixture must replace the first entry's `OFFICIAL_Q` reason with a non-insufficient message containing `filingContext.activeFinancialReport.period`, so it deterministically remains in the active `announced` group. All other reasons, entries, and groups remain intact.

- [ ] **Step 3: Run RED**

```powershell
..\..\.venv\Scripts\python.exe -m pytest -q -o filterwarnings= --basetemp C:\tmp\pytest-plan-a-task5-red tests\test_frontend_parser.py tests\test_frontend_hygiene.py
npm.cmd run test:e2e -- --grep "market scan warning|last successful market scan|last-good warning"
```

Expected: retry-round/mutation/abort tests and both last-good warning lifecycle cases fail.

- [ ] **Step 4: Implement bounded read retry**

Keep `RETRYABLE_API_STATUSES` for cross-candidate failover and add a separate set:

```javascript
const SAME_ENDPOINT_RETRYABLE_STATUSES = new Set([502, 503, 504]);
const IDEMPOTENT_API_METHODS = new Set(["GET", "HEAD"]);
const DEFAULT_GET_RETRY_DELAYS_MS = [150, 450];
```

Implement retry rounds, not per-candidate retry loops. For GET/HEAD, walk the candidate list once per round so fallback mode moves immediately from same-origin to Worker. Start another round only after every candidate in that round ended in a non-abort network exception or 502/503/504. Status 500 returns immediately in direct/same-origin mode but may move once to the next candidate in the current fallback round; a terminal last-candidate 500 never opens another round. Status 429 and other listed client statuses never move candidates or retry.

For POST/PUT/PATCH/DELETE, select only the primary mode candidate and call fetch once, including on network errors and 5xx responses. Remove mutation routes from direct-fallback eligibility or enforce the same rule centrally before candidate construction.

Use injected `retryDelaysMs` for deterministic zero-delay tests. Production delay must be abortable with the caller's signal; an `AbortError` before fetch or during backoff is terminal. Ordinary transport `TypeError` remains retryable for safe methods. This task deliberately does not create an internal per-attempt timeout, so its guarantee is bounded calls, not bounded wall-clock duration.

- [ ] **Step 5: Implement last-good warning rendering**

Add `marketScanWarning: null` to state. On a reveal refresh, call `setEmptyState` only when `state.marketScan` is absent. Centralize every market-scan assignment in `acceptMarketScan(scan, { resetUi })`: update the scan, reset UI only for a genuine new success, and derive either a Task 2 unavailable warning or `null`. Use it in both `loadDataStatus()` and `refreshMarketScan()`.

Centralize both refresh catch paths in `handleMarketScanFailure(error, { revealResults, target })`. Add `renderMarketScanWarning(target)` that prepends the warning after every `renderMarketResults()` render while `state.marketScanWarning` is non-null. On failure with existing data:

```javascript
state.marketScanWarning = error.message || "更新失敗";
renderMarketResults();
return state.marketScan;
```

The helper creates a `p`, assigns `className = "data-source-note market-scan-warning"` and `role="status"`, sets `textContent` with the unchanged `state.marketScan.generatedAt` time plus safe error text, and prepends it to `#market-results`. This keeps it visible after paging or view rerender. When there is no last-good state, retain the current blocking `setFormError`. Never call `resetMarketListUi()` on failure.

Parse a safe JSON `requestId` in `apiErrorMessage` and append `（錯誤編號：<id>）` only when it is a string matching `/^[A-Za-z0-9._:-]{1,80}$/`. Add boundary/negative cases for 80 and 81 characters, empty/whitespace, CR/LF, HTML, Unicode, number/object, invalid JSON, and non-JSON content type. For all 5xx cases, retain the generic text, append only a valid ID, and never expose raw `detail`. Apply the same request-ID sanitizer to the HTTP 200 unavailable warning. Add `unavailable: "暫時無法更新"` to the cache-status label map.

- [ ] **Step 6: Update cache busters and correct the proxy runbook**

Set `api_client.js` to `v=20260712-resilient-api` and both `market_render.js` and `app.js` to `v=20260712-last-good-scan` in `frontend/index.html`. Add `API_CLIENT_VERSION = "20260712-resilient-api"`, set `MARKET_RENDER_VERSION` and `APP_VERSION` to `"20260712-last-good-scan"`, and update their assertions in `tests/test_frontend_hygiene.py`.

Correct `docs/cloudflare_deployment.md` and `docs/current_architecture.md` precisely: Pages `/api/*` is a status-preserving proxy rather than a 307 redirect; a healthy `/api/health` returns HTTP 200 Worker JSON, while other API paths preserve their upstream status. Production browser mode normally calls the Worker directly. Keep the proxy verification gate and document the valid command `python scripts\check_pages_api_redirect.py --url https://stock-scanner-beta.pages.dev/api/health`; remove only the unsupported `--expected-origin` argument and stale 307 wording.

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
git add frontend\api_client.js frontend\app.js frontend\market_render.js frontend\index.html docs\cloudflare_deployment.md docs\current_architecture.md tests\test_frontend_parser.py tests\test_frontend_hygiene.py tests\test_pages_api_redirect.py tests\e2e\smoke.spec.ts
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
