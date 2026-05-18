# Cloudflare Deployment Runbook

Current target: Cloudflare Pages + Python Worker + D1 + R2.

## Resources

- Pages project: `stock-scanner-beta`
- Production URL: `https://stock-scanner-beta.pages.dev`
- Worker name: `stock-scanner-beta-api`
- Worker URL: `https://stock-scanner-beta-api.pcedison.workers.dev`
- D1 database: `stock-scanner-beta-db`
- R2 bucket: `stock-scanner-beta-cache`

Pages proxies `/api/*` through `frontend/functions/api/[[path]].js`. The frontend still calls same-origin `/api`, so local FastAPI and the Cloudflare Worker share the same browser flows.

## Required Settings

GitHub Actions secrets:

- `CLOUDFLARE_ACCOUNT_ID`
- `CLOUDFLARE_API_TOKEN`

GitHub Actions repository variables:

- `CF_WORKER_HEALTH_URL`: deployed Worker HTTPS `/api/health` URL.

GitHub environment:

- Create a `production` environment.
- Add required reviewers or equivalent approval protection.

Worker production CORS lives in `cloudflare/wrangler.toml`:

- `APP_ENV = "production"`
- `APP_CORS_ALLOW_ORIGINS = "https://stock-scanner-beta.pages.dev"`

Production must not allow localhost, 127.0.0.1, or non-HTTPS origins. Production unsafe `/api/*` methods also require `X-Stock-Scanner-CSRF: 1`; the frontend sends this header automatically.

## Deploy Flow

`.github/workflows/cloudflare-deploy.yml` deploys only from `main`, `master`, schedule, or manual dispatch. Feature branches validate only.

1. Install Python and Node dependencies.
2. Run pytest, frontend hygiene, operational readiness, pip check, and npm audit.
3. Run deployment preflight for CORS, production environment, concurrency, D1 migration, health, remote smoke, and rollback guardrails.
4. Run Playwright browser smoke.
5. Validate committed seed zip, enforce freshness, and write Markdown/JSON seed quality summaries.
6. Rebuild `cloudflare/seed/*` from the committed offline zip.
7. Run Worker dry-run to validate the Cloudflare Python Worker bundle boundary.
8. Upload seed payloads to R2.
9. Export a D1 backup artifact before migrations.
10. Apply pending D1 migrations from `cloudflare/migrations/`.
11. Deploy Worker and Pages.
12. Verify deployed `/api/health` and manifest counts.
13. Run deployed public smoke against `/api/health`, `/api/app-status`, and `/api/data-sources/status`.
14. Roll back the Worker with `wrangler rollback --yes` if post-deploy verification fails.

D1 restore remains an operator-reviewed recovery action. Generate a non-destructive plan with:

```powershell
python scripts\plan_cloudflare_recovery.py --output .tmp\cloudflare-recovery.md
```

If a deploy exported a D1 artifact, pass it explicitly:

```powershell
python scripts\plan_cloudflare_recovery.py --d1-backup .tmp\d1-backups\pre-deploy-123.sql --output .tmp\cloudflare-recovery.md
```

## Seed Refresh

`.github/workflows/refresh-cloudflare-seed.yml` runs weekly and can also be dispatched manually.

1. Rebuild seed from official sources in online mode.
2. Repack `data/official_cache_seed_2026-05-14.zip` and its `.sha256`.
3. Regenerate missing-company reports.
4. Validate quality gates and freshness.
5. Open or update a refresh PR when seed artifacts changed.

This keeps production deploys deterministic while preventing the committed seed from silently going stale.

## Monitoring

`.github/workflows/cloudflare-health-monitor.yml` polls `CF_WORKER_HEALTH_URL` every 30 minutes. GitHub Actions failure notifications are the baseline alerting path. The same `/api/health` endpoint can be wired into Cloudflare notifications, Better Stack, UptimeRobot, or another external monitor.

## Local Verification

```powershell
python -m pytest -q
python scripts\check_frontend_hygiene.py
python scripts\check_operational_readiness.py
python scripts\check_deployment_preflight.py
npx wrangler deploy --config cloudflare\wrangler.toml --dry-run --outdir .tmp\worker-dry-run
python scripts\run_wrangler_dev_smoke.py
npm run test:e2e
```

Remote production smoke:

```powershell
$env:CF_WORKER_HEALTH_URL='https://stock-scanner-beta-api.<account>.workers.dev/api/health'
python scripts\run_remote_smoke.py --health-url $env:CF_WORKER_HEALTH_URL --manifest cloudflare\seed\manifest.json
```
