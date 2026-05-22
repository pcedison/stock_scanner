# Current Architecture Snapshot

Last reviewed: 2026-05-18

This project is no longer a localStorage-only prototype. The current shape is:

- Frontend: static Pages-compatible app in `frontend/`, with local fallback only for degraded/offline use.
- Local API: FastAPI in `backend/`, backed by SQLite auth/session state and gitignored JSON settings at `data/settings.local.json` by default. Use `SETTINGS_PATH` to override this for isolated runs.
- Edge API: Python Cloudflare Worker in `cloudflare/`, backed by D1 sessions/holdings/settings and R2 cache objects.
- Shared expectation: FastAPI and Worker responses are guarded by shape and behavior contract tests under `tests/test_api_worker_contracts.py`.
- Deployment: feature branches run validation only; `main`, scheduled, and manual workflow runs perform the actual Cloudflare upload/deploy with a production environment gate, concurrency control, operational readiness checks, D1 pre-deploy export, D1 migrations, Worker dry-run validation, post-deploy `/api/health` checks, manifest count verification, deployed public smoke checks, and Worker rollback on failed post-deploy verification.
- Seed data: CI/deploy use the committed `data/official_cache_seed_2026-05-14.zip` in offline mode. The zip contains both official history inputs and the generated `cloudflare_seed/*` payload, so deploy validation does not depend on live TWSE/TPEx/MOPS APIs.
- Seed refresh: `.github/workflows/refresh-cloudflare-seed.yml` can rebuild from official sources on a schedule, package the cache zip, validate quality gates, enforce manifest freshness, summarize missing-data reasons/status, write Markdown/JSON quality summaries, and open a PR.
- Runtime health: Worker `/api/health` and app status expose cache manifest quality counts and mark undersized cache payloads as degraded. `.github/workflows/cloudflare-health-monitor.yml` can poll this endpoint on a schedule and use GitHub Actions failure notifications as the baseline alerting channel.
- Security defaults: production FastAPI and Worker deployments must use explicit HTTPS CORS origins. FastAPI also requires secure session cookies in production. Worker production CORS is driven by `APP_CORS_ALLOW_ORIGINS` and filters localhost/non-HTTPS origins unless an explicit break-glass flag is set. Production unsafe `/api/*` methods require the frontend's `X-Stock-Scanner-CSRF: 1` header.
- D1 schema: `cloudflare/schema.sql` is mirrored by versioned D1 migrations in `cloudflare/migrations/`; deployment applies migrations through Wrangler instead of directly applying the schema snapshot.
- Worker runtime smoke: CI can start `wrangler dev` locally through `scripts/run_wrangler_dev_smoke.py` and verify the Python Worker/workerd/Pyodide boundary. An empty local R2 cache is allowed to report `degraded`; runtime startup and JSON health response are still required.
- Frontend maintainability: `frontend/dom.js` now owns shared HTML escaping and small DOM helpers, including empty-state rendering. `frontend/app.js` still contains most renderers, but new renderer work should move reusable DOM/escaping behavior out of the single-file surface instead of adding more ad hoc `innerHTML`. CI currently budgets `frontend/app.js` at 19 `innerHTML` references.

Useful commands:

```powershell
python scripts\validate_cloudflare_seed_inputs.py --zip data\official_cache_seed_2026-05-14.zip
python scripts\validate_cloudflare_seed_inputs.py --zip data\official_cache_seed_2026-05-14.zip --summary-md .tmp\seed-quality.md --summary-json .tmp\seed-quality.json --max-age-days 45
$env:CLOUDFLARE_SEED_MODE='offline'; python scripts\build_cloudflare_seed.py; Remove-Item Env:\CLOUDFLARE_SEED_MODE
python scripts\check_deployment_preflight.py
python scripts\check_operational_readiness.py
python scripts\check_frontend_hygiene.py
python scripts\run_wrangler_dev_smoke.py
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
After deployment, `scripts/run_remote_smoke.py` checks deployed `/api/health`, `/api/app-status`, and `/api/data-sources/status` using that URL.
