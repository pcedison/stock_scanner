# Stock Scanner Reliability Hardening Design

Date: 2026-07-12
Implementation branch (historical): `codex/reliability-hardening`
Status: Historical design record; implemented through Plans A/B and merged in PR #121. It is not current work authorization or agent guidance.

## Objective

Prevent transient Cloudflare Worker, R2, D1, browser-network, and delayed-scheduler failures from becoming an untraceable full-page error. Preserve the last successful market scan whenever possible, make each failure diagnosable by a safe request identifier, and remove the request/refresh coupling and oversized market response that make the current path fragile.

## Incident Evidence Driving the Design

- The 2026-07-12 screenshot occurred one to two minutes after the R2 manifest crossed its three-hour `nextRefreshAfter` boundary.
- The last R2 refresh workflow before the screenshot ran while the cache was still fresh; the nominal 15-minute schedule then did not run again for almost two hours.
- The health workflow accepted a 4.05-hour-old cache because it used a fixed 36-hour limit while the Worker used a dynamic 2/3/12-hour cache policy.
- Worker exceptions are collapsed to one generic 500 response and the browser collapses network failures and all 5xx responses to the same text.
- Production direct API mode has one endpoint candidate, so its existing fallback loop performs no same-endpoint retry.
- The current market response is 2,881,825 uncompressed bytes and the force-refresh request performs R2 reads plus D1 reads/writes before returning that full payload.

## Global Constraints

- Preserve existing authentication, CSRF, CORS, cookie, settings, holdings, reports, and legacy market-scan contracts unless an explicitly versioned replacement is introduced.
- Never expose exception messages, SQL, credentials, request bodies, cookies, usernames, tokens, or Authorization headers to clients or structured logs.
- Retry only operations that are safe to repeat. Browser retries are limited to idempotent reads. D1 writes require an idempotency key before application-level retry is allowed.
- Continue serving last-known-good market data while refresh or dependency operations are degraded.
- Plan A must pass its full gate before any Plan B production code is started.
- No production deployment is part of this local implementation unless separately requested; commits remain on `codex/reliability-hardening`.

## Plan A: Minimal-Risk Stabilization

### A1. Worker observability and safe error envelope

Enable Workers Logs and traces in `cloudflare/wrangler.toml`, with full invocation/error logging and sampled traces. Every request receives a safe `X-Request-ID`; Cloudflare's ray identifier is used when valid, otherwise a cryptographically random identifier is generated.

Unexpected dependency failures are wrapped at the R2/D1 boundary and returned as a stable envelope:

```json
{
  "detail": "伺服器暫時無法處理請求，請稍後再試。",
  "code": "DEPENDENCY_UNAVAILABLE",
  "requestId": "safe-id",
  "retryable": true,
  "stage": "d1_read"
}
```

Unknown application failures use `INTERNAL_ERROR`, HTTP 500, `retryable: false`, and stage `worker`. R2 body-I/O failures and D1 read failures caused by network loss, storage/code reset, or a transient remote node use HTTP 503 and a dependency stage. D1 overload and query-timeout classifications remain non-retryable because the platform guidance is to optimize, split, or shed load rather than add retries. Logs contain only fixed safe classifications plus event, request ID, method, pathname (never query text), status, duration, dependency stage, and exception class; raw exception text and exception chains are not retained.

The legacy market refresh POST has one deliberate degradation exception: after a verified R2 scan has already been loaded, a D1 read/write failure while checking or enqueueing its refresh job does not discard that data. It returns the last-good scan with HTTP 200 and `cacheStatus.refreshStatus="unavailable"`, while emitting the structured dependency event. `cacheStatus.retryable` preserves the original dependency classification, so an ambiguous D1 write is never advertised as safe to repeat. Missing/malformed scans, non-D1 failures, authentication, settings, holdings, and other routes never receive this fail-open behavior.

### A2. Idempotent browser retry and last-good rendering

`frontend/api_client.js` uses bounded retry rounds only for GET/HEAD requests and only for network failures or 502/503/504 responses. A round walks each safe candidate at most once; status 500 may fail over to the next candidate but never retries the same endpoint. It performs at most two further rounds with short abortable backoff. POST/PUT/PATCH/DELETE select one primary candidate and make exactly one fetch in Plan A, eliminating the existing ambiguous cross-candidate replay.

The UI parses a strictly validated `requestId` from safe JSON errors and includes it in the user-facing support reference. When a market refresh fails and `state.marketScan` already exists, the existing results are rendered again and a non-blocking status warning is prepended. The same warning path handles a successful HTTP 200 response whose refresh enqueue is unavailable. A failed refresh must not replace successful results or their page/expanded state with an error-only element.

Plan A deliberately does not persist the 2.88 MB payload to localStorage because UTF-16 storage can exceed mobile quotas. Cross-reload persistence is implemented only after Plan B reduces the payload.

### A3. Dynamic health and scheduler alignment

Worker `/api/health` includes the computed `cacheStatus` and reports `degraded` when either manifest quality fails or the dynamic cache policy says the seed is stale. The R2 refresh decision treats `cacheStatus.isStale`, a passed `nextRefreshAfter`, or a non-OK health state as a rebuild trigger instead of waiting 36 hours.

The R2 decision begins a refresh up to 60 minutes before `nextRefreshAfter`, providing one missed-run cushion. A failed D1 pending-job query fails open into the heavy refresh decision instead of being converted to `pending_count=0`. GitHub cron expressions are moved away from the start of the hour while preserving their nominal frequencies. The 36-hour check remains as a corruption/safety ceiling and rolling-deployment fallback, not the normal refresh policy.

### A4. Runbook and regression coverage

The deployment runbook is corrected to describe the production Pages API proxy behavior observed in code, tests, and the live environment. Tests cover:

- request IDs and safe structured 500/503 envelopes;
- R2 and D1 transient failures;
- GET retry success/exhaustion and zero POST retry;
- last-good market rendering after refresh failure;
- dynamic health degradation at the exact stale boundary;
- R2 refresh decisions based on policy stale state;
- off-peak cron expressions and observability config.

### Plan A completion gate

Plan A is complete only when targeted RED/GREEN cycles are recorded and all of the following pass sequentially:

1. full pytest suite;
2. ESLint and formatting check;
3. frontend hygiene, operational readiness, and deployment preflight;
4. Wrangler deploy dry-run and local Worker smoke;
5. Playwright desktop/mobile suite;
6. read-only production GET/OPTIONS smoke to confirm the unchanged public contract.

## Plan B: Structural Reliability Upgrade

Plan B starts only after the Plan A completion gate is green.

### B1. Versioned market query API and R2 paging

Add backward-compatible v2 query endpoints while retaining legacy `/api/scan/market` during migration:

```text
GET  /api/scan/market/index
GET  /api/scan/market/results?disclosure=announced|pending&category=entry|watch|excluded&cursor=0&limit=100
```

The seed build emits:

```text
public/market_scan_index.json
public/market_scan/v2/{generationId}/index.json
public/market_scan/v2/{generationId}/{disclosure}/{category}/{page}.json
```

The index contains a generation ID, generated time, disclosure/freshness metadata, announced/pending counts, category counts, cache status inputs, page size, page references, and page hashes. Generation pages are uploaded first and the small index pointer is uploaded last, so readers never observe a half-published generation. Page responses contain only list fields needed by the renderer. Per-company evidence remains available through the existing analyze endpoint. Response budgets are less than 50 KB for the index and less than 500 KB uncompressed per page.

The frontend loads the index first, lazy-loads the active disclosure/category page, and caches successful pages in memory. Existing server-side `/api/reports/market` remains the low-frequency full export path; the browser must not download every page just to produce an export. Once the small index is available, it is persisted as the cross-reload last-known-good shell.

### B2. Command/query separation and idempotent refresh

Add:

```text
POST /api/scan/market/refresh
GET  /api/scan/market/refresh/{jobId}
```

The POST validates CSRF, creates or reuses one idempotent job, and returns HTTP 202 without a market payload:

```json
{
  "jobId": "...",
  "status": "queued",
  "requestId": "...",
  "statusUrl": "/api/scan/market/refresh/..."
}
```

A D1 migration adds a unique `idempotency_key` to refresh jobs and a partial unique index on active `(job_type, cache_key)` rows. The active index prevents two different clients from queuing duplicate work for the same generation. The server derives a deterministic fallback key from job type, current cache key, and a bounded time bucket; clients may provide a validated `Idempotency-Key` header. `INSERT OR IGNORE` plus a read-back removes the current select-then-insert race. Legacy POST remains temporarily compatible but is marked deprecated.

### B3. Cloudflare control-plane trigger

Add a Cloudflare scheduled handler that evaluates the same cache policy and creates/reuses a refresh job. When a job is due, it dispatches the existing GitHub batch executor through an explicitly configured minimal-scope Worker secret. The existing GitHub schedule remains a fallback, not the sole trigger. Dispatch failures are logged, reflected in job status, and never prevent reads of last-good R2 data.

This phase does not move the heavy official-data rebuild into a Worker. That job exceeds the desirable request/runtime boundary and remains in the existing GitHub executor until a separately justified Workflows migration.

### B4. Edge caching and staged rollout

After the v2 public responses are bounded and contract-tested, enable Workers Caching for public `GET` responses. Private/authenticated responses retain `Cache-Control: no-store`. Add staging configuration and canary verification before changing the production frontend to v2.

Rollout order:

1. deploy additive R2 artifacts and v2 endpoints;
2. verify v1/v2 parity and response budgets;
3. switch frontend reads to v2 behind a runtime flag;
4. enable scheduled dispatch and caching;
5. observe one full refresh window;
6. retain v1 rollback path until the observation gate passes.

## Failure Semantics

- R2 read failure: serve already-held browser data; API returns safe 503 with request ID if no edge stale response is available.
- D1 refresh enqueue failure: query endpoints still return data; refresh command returns safe 503 and existing market results remain visible.
- GitHub dispatch failure: job records dispatch failure/retry state; public reads continue from R2.
- Malformed page/index artifact: fail that artifact validation and retain the previous verified R2 generation.
- Browser offline/network failure: bounded GET retries, then last-good UI plus support reference.

## Verification and Rollback

Each behavior is introduced test-first. Configuration and workflow changes receive static contract tests before edits. Deployment dry-runs validate Wrangler schema and bundle compatibility. The branch is locally committed in small reviewed units; no production mutation occurs from this design phase.

Plan A can be reverted independently because it does not change public success payloads. Plan B is additive until the frontend flag changes; rollback switches the flag to legacy v1 and leaves v2 artifacts/endpoints unused.
