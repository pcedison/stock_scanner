# Current Architecture Snapshot

Last reviewed: 2026-07-16

This project is no longer a localStorage-only prototype. The current shape is:

- Frontend: static Pages-compatible app in `frontend/`, with local fallback only for degraded/offline use.
- Local API: FastAPI in `backend/`, backed by SQLite auth/session state and gitignored JSON settings at `data/settings.local.json` by default. Use `SETTINGS_PATH` to override this for isolated runs.
- Edge API: Python Cloudflare Worker in `cloudflare/`, backed by D1 sessions/holdings/settings and R2 cache objects.
- Market scan API: v2 only. Both runtimes serve paginated reads through `/api/scan/market/index` and `/api/scan/market/results`, and the browser no longer negotiates a market API version at all, because v1 is gone; `/api/runtime-config` is kept for the deployed smoke (`scripts/run_remote_smoke.py`) and the FastAPI/Worker contract tests, and is no longer requested by the frontend. The v1 whole-scan `GET/POST /api/scan/market` endpoints were retired on 2026-09-15 and now return 404: decoding the multi-megabyte payload inside Pyodide exceeded Worker resource limits and crashed the Worker in production. `public/market_scan_summary.json` is still built, uploaded and read at request time, but only by the `/api/reports/market` export. The only market-read rollback is `wrangler rollback` to the previous Worker version. Local FastAPI serves the same `/api/scan/market/refresh` command and `/api/scan/market/refresh/{jobId}` status read, so the manual refresh button works in local development. There is no queue and no GitHub Actions run locally: the job runs in-process on a background thread (`backend/services/refresh_jobs.py`) and reports the Worker's `queued`/`running`/`success`/`failed` vocabulary. The dispatch and owner-run fields that exist only for the Worker's D1 jobs are omitted rather than faked. The job rebuilds data only in real-data mode, where it refreshes the official sources first and then stores the result under the cache key of the refreshed files; in mock mode it re-runs the scan and completes without changing what is served. In real-data mode the refresh command is also throttled after any terminal job (success or failed) for 60 seconds, returning 429 with a `Retry-After` header when a forced refresh lands inside that cooldown; the Worker applies the same rule with a one-hour window. Mock mode has no cooldown. The local cache still refreshes itself at publication slots through `backend/services/scan_cache.py`.
- Browser API routing: Production browser mode normally calls the Worker directly. The Pages `/api/*` Function is a status-preserving compatibility proxy, so healthy `/api/health` JSON is returned with HTTP 200 and other API responses retain their upstream Worker status.
- Shared expectation: FastAPI and Worker responses are guarded by shape and behavior contract tests under `tests/test_api_worker_contracts.py`.
- Deployment: feature branches run validation only; `main`, scheduled, and manual workflow runs perform the actual Cloudflare upload/deploy with a production environment gate, concurrency control, operational readiness checks, a pre-mutation production data-safety gate, D1 pre-deploy export, D1 migrations, Worker dry-run validation, post-deploy `/api/health` checks, manifest count verification, deployed public smoke checks, and Worker rollback on failed post-deploy verification. Deployment enforces the 36-hour absolute cache-age and quality ceilings but allows routine refresh-deadline delay up to the remaining 24 hours; the independent health monitor keeps the stricter 15-minute refresh SLA.
- Seed data: CI/deploy resolve the newest committed dated seed zip with `python scripts/seed_utils.py data --print` and use it in offline mode. The zip contains both official history inputs and the generated `cloudflare_seed/*` payload, including immutable market scan v2 pages and the `market_scan_index.json` pointer, so deploy validation does not depend on live TWSE/TPEx/MOPS APIs.
- Quarterly fundamentals history: the R2 refresh persists `official_fundamentals_history.json` across runs and backfills the prior-year same quarter from MOPS (`backend/services/official_history_backfill.py`, cumulative year-to-date columns preferred) so EPS/net-income YoY rules (X3–X5) have their comparison period after each filing window advances.
- Seed refresh: `.github/workflows/refresh-cloudflare-seed.yml` can rebuild from official sources on a schedule, package the cache zip, validate quality gates, enforce manifest freshness, summarize missing-data reasons/status, write Markdown/JSON quality summaries, and open a PR. The production R2 refresh separately downloads and atomically merges the previous monthly-revenue history before rebuilding; publication requires at least 1,000 companies with consecutive months and rejects systemic X2 missing-data output. `latestFinancialPeriod` records the latest cached/usable report period, not a future filing window.
- Runtime health: Worker `/api/health` and app status expose cache manifest quality counts and mark undersized cache payloads as degraded. `.github/workflows/cloudflare-health-monitor.yml` can poll this endpoint on a schedule and use GitHub Actions failure notifications as the baseline alerting channel.
- Security defaults: production FastAPI and Worker deployments must use explicit HTTPS CORS origins. FastAPI also requires secure session cookies in production. Worker production CORS is driven by `APP_CORS_ALLOW_ORIGINS` and filters localhost/non-HTTPS origins unless an explicit break-glass flag is set. Production unsafe `/api/*` methods require the frontend's `X-Stock-Scanner-CSRF: 1` header.
- D1 schema: `cloudflare/schema.sql` is mirrored by versioned D1 migrations in `cloudflare/migrations/`; deployment applies migrations through Wrangler instead of directly applying the schema snapshot.
- Worker runtime smoke: CI can start `wrangler dev` locally through `scripts/run_wrangler_dev_smoke.py` and verify the Python Worker/workerd/Pyodide boundary. An empty local R2 cache is allowed to report `degraded`; runtime startup and JSON health response are still required.
- Frontend maintainability: shared concerns are split out of `frontend/app.js` into `frontend/dom.js` (HTML escaping, small DOM helpers, empty-state rendering), `frontend/renderers.js` (pure HTML renderers), `frontend/strategy_content.js`, `frontend/storage.js`, `frontend/reference_data.js`, and `frontend/auth.js`. `frontend/app.js` now contains zero raw `innerHTML` assignments and uses `setSafeHtml` exclusively; new renderer work should keep reusable DOM/escaping behavior in `dom.js`/`renderers.js`. CI hygiene gate (`scripts/check_frontend_hygiene.py`) enforces `--max-inner-html 0` and rejects any dangerous sink without escapeHtml/render-helper coverage, with `frontend/app.js` line budget tracked in `scripts/check_code_size_budgets.py`.

Useful commands:

```powershell
$seedZip = python scripts\seed_utils.py data --print
python scripts\validate_cloudflare_seed_inputs.py --zip $seedZip
python scripts\validate_cloudflare_seed_inputs.py --zip $seedZip --summary-md .tmp\seed-quality.md --summary-json .tmp\seed-quality.json --max-age-days 45
$env:CLOUDFLARE_SEED_MODE='offline'; python scripts\build_cloudflare_seed.py; Remove-Item Env:\CLOUDFLARE_SEED_MODE
python scripts\check_deployment_preflight.py
python scripts\check_operational_readiness.py
python scripts\check_frontend_hygiene.py
python scripts\run_wrangler_dev_smoke.py
python scripts\render_wrangler_release_config.py staging --database-id 11111111-1111-1111-1111-111111111111 --bucket-name stock-scanner-beta-cache-staging-test --output cloudflare\wrangler.staging.generated.toml
python scripts\check_market_scan_v2_canary.py --base-url https://stock-scanner-beta-api-staging.<account>.workers.dev
python scripts\plan_cloudflare_recovery.py --output .tmp\cloudflare-recovery.md
python -m pytest -q
npm run test:e2e
```

When refreshing committed seed inputs, rebuild `cloudflare/seed` first, then run:

```powershell
python scripts\package_cloudflare_seed_cache.py
```

The package script rewrites the zip and its `.sha256` sidecar from the current committed-data snapshot.

For production Cloudflare verification, set the GitHub repository variable `CF_WORKER_HEALTH_URL` to the deployed Worker `/api/health` URL, for example `https://stock-scanner-beta-api.<account>.workers.dev/api/health`.
After deployment, `scripts/run_remote_smoke.py` checks deployed `/api/health`, `/api/app-status`, `/api/data-sources/status`, `/api/runtime-config` and `/api/scan/market/index` using that URL; the runtime config must advertise `marketScanApiVersion=v2` and the market index must carry a valid `generationId`, a `cacheStatus`, and at least 1,000 rows across its category counts. It also POSTs `/api/reports/market?report_format=csv` (the `market_report` export in `cloudflare/worker.py`, which reads `public/market_scan_summary.json`) and asserts its `entry`/`watch`/`excluded` row counts match the index's `counts.categories`, since PR #173 dropped the only other place that cross-checked them and they otherwise drift silently between the two independently served views of the same generation.
