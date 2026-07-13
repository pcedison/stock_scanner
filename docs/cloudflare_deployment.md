# Cloudflare Deployment Runbook

Current target: Cloudflare Pages + Python Worker + D1 + R2.

## Resources

- Pages project: `stock-scanner-beta`
- Production URL: `https://stock-scanner-beta.pages.dev`
- Worker name: `stock-scanner-beta-api`
- Worker URL: `https://stock-scanner-beta-api.pcedison.workers.dev`
- D1 database: `stock-scanner-beta-db`
- R2 bucket: `stock-scanner-beta-cache`

Pages serves the frontend, while production browser API calls normally go directly to the Worker origin declared by `stock-scanner-api-origin`. The same-origin Pages `/api/*` Function remains a status-preserving proxy for stale clients and manual API visits: a healthy `/api/health` returns HTTP 200 with the Worker JSON body, and other `/api/*` responses preserve the upstream Worker status.

## Required Settings

GitHub Actions secrets:

- `CLOUDFLARE_ACCOUNT_ID`
- `CLOUDFLARE_API_TOKEN`

GitHub Actions repository variables:

- `CF_WORKER_HEALTH_URL`: deployed Worker HTTPS `/api/health` URL.

GitHub environment:

- Create a `production` environment.
- For team-owned repositories, require production environment reviewers before deployment.
- For solo-maintainer repositories where reviewer approval is not practical, document that exception and keep branch protection on `main`/`master` with required status checks. Production deploys and R2 seed refreshes still share the `cloudflare-production` concurrency group so they cannot update production at the same time.

Worker production CORS lives in `cloudflare/wrangler.toml`:

- `APP_ENV = "production"`
- `APP_CORS_ALLOW_ORIGINS = "https://stock-scanner-beta.pages.dev"`
- `MARKET_SCAN_API_VERSION = "v1"`
- `EDGE_CACHE_ENABLED = "false"`
- `[cache].enabled = false`
- `[triggers].crons = []`
- `GITHUB_DISPATCH_ENABLED = "false"`

Production must not allow localhost, 127.0.0.1, or non-HTTPS origins. Production unsafe `/api/*` methods also require `X-Stock-Scanner-CSRF: 1`; the frontend sends this header automatically. Cloudflare Worker session cookies use `SameSite=None; Secure` so authenticated cross-origin fetches from Pages to the Worker can include the account session.

The committed production config is intentionally conservative. Cron dispatch, v2 market reads, and Worker cache are enabled only through generated full configs from `scripts/render_wrangler_release_config.py`; do not hand-edit production release switches in `cloudflare/wrangler.toml`.

## Deploy Flow

`.github/workflows/cloudflare-deploy.yml` deploys on pushes to `main`/`master` or manual dispatch. Feature branch and pull request validation happens outside this production deploy workflow.

1. Install Python and Node dependencies.
2. Run pytest, frontend hygiene, operational readiness, pip check, and npm audit.
3. Run deployment preflight for CORS, production environment, concurrency, D1 migration, health, remote smoke, and rollback guardrails.
4. Run Worker dry-run to validate the Cloudflare Python Worker bundle boundary.
5. Run local Worker runtime smoke.
6. Install the Playwright Chromium browser and run browser smoke tests.
7. Export a D1 backup artifact before migrations.
8. Apply pending D1 migrations from `cloudflare/migrations/`.
9. Deploy Worker and Pages.
10. Verify deployed `/api/health` freshness.
11. Verify Worker CORS for `https://stock-scanner-beta.pages.dev`, including the POST preflight used by the browser fallback.
12. Run deployed public smoke against the Worker `/api/health`, `/api/app-status`, and `/api/data-sources/status`.
13. Verify `https://stock-scanner-beta.pages.dev/api/health` returns the healthy Worker JSON through the status-preserving Pages proxy.
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
2. Repack the newest `data/official_cache_seed_*.zip` seed artifact and its matching `.sha256`.
3. Regenerate missing-company reports.
4. Validate quality gates and freshness.
5. Open or update a refresh PR when seed artifacts changed.

This keeps production deploys deterministic while preventing the committed seed from silently going stale.

## Server-Side R2 Seed Rebuild

`.github/workflows/cloudflare-r2-seed-refresh.yml` is the production-side refresh worker for market scan jobs queued by the Cloudflare Worker. It runs every 15 minutes and can also be dispatched manually with `force=true`.

1. Poll D1 `refresh_jobs` for queued or running `market_scan` jobs and check deployed `/api/health` freshness.
2. Stop without touching R2 when no job is queued and the production seed is fresh, unless the workflow is manually forced.
3. Mark queued jobs as `running`.
4. Download the previous R2 `official/monthly_revenue_history.json`, then merge it atomically with the committed seed through `scripts/hydrate_cloudflare_seed_inputs.py`.
5. Rebuild `cloudflare/seed/*` from official sources with `CLOUDFLARE_SEED_MODE=online`.
6. Reject publication when fewer than 1,000 companies have consecutive revenue months or when more than 10% of scan rows lack the X2 previous-month signal.
7. Package and validate the newest `data/official_cache_seed_*.zip` seed artifact with freshness, monthly-history, universe, and analysis gates.
8. Generate the R2 upload manifest with `scripts/cloudflare_seed_upload_plan.py`, then upload the rebuilt manifest, market scan summary/latest payloads, analysis shards, holding shards, and required official cache artifacts to R2.
9. Verify the deployed Worker health endpoint and remote smoke checks against the rebuilt manifest.
10. Mark D1 refresh jobs as `success`, or `failed` if any step in the rebuild/upload/verify flow fails.

The workflow shares the `cloudflare-production` concurrency group with production deploys so R2 seed uploads do not race with a deploy. When the Worker queues a D1 `market_scan` refresh job, this workflow performs the actual seed rebuild and R2 update on the next run.
Seed artifact Git policy is documented in `docs/seed_artifact_policy.md`; routine refresh output belongs in R2, and new committed seed zips require an explicit forced add and review note.

## Additive Market v2 Rollout and Rollback

Market scan v2 is released in staged, reversible steps. Each command below renders a complete Wrangler config; generated files are ignored by Git and must be dry-run before any real deploy.

1. Merge additive code/artifacts while production remains v1, dispatch disabled, cron empty, and Worker cache disabled.
2. Provision isolated staging D1/R2 resources and secrets under explicit release authority.
3. Render staging with v2/cache enabled and dispatch disabled:

```powershell
python scripts\render_wrangler_release_config.py staging --database-id $env:CF_STAGING_D1_DATABASE_ID --bucket-name $env:CF_STAGING_R2_BUCKET --output cloudflare\wrangler.staging.generated.toml
npx.cmd wrangler deploy --config cloudflare\wrangler.staging.generated.toml --dry-run --outdir .tmp\worker-staging-dry-run
```

4. Deploy staging only after dry-run passes, then run the canary:

```powershell
python scripts\check_market_scan_v2_canary.py --base-url https://stock-scanner-beta-api-staging.<account>.workers.dev --require-edge-hit
```

5. Deploy production code with the committed v1/cache-disabled config.
6. Under separate cron release authority, render and dry-run the full cron config:

```powershell
python scripts\render_wrangler_release_config.py production-cron --enable-production-cron --output cloudflare\wrangler.production-cron.generated.toml
npx.cmd wrangler deploy --config cloudflare\wrangler.production-cron.generated.toml --dry-run --outdir .tmp\worker-production-cron-dry-run
```

7. Under separate v2 release authority, render and dry-run the full v2 config:

```powershell
python scripts\render_wrangler_release_config.py production-v2 --enable-production-cron --enable-production-v2 --output cloudflare\wrangler.production-v2.generated.toml
npx.cmd wrangler deploy --config cloudflare\wrangler.production-v2.generated.toml --dry-run --outdir .tmp\worker-production-v2-dry-run
```

8. Observe one full cache window after v2 enablement: financial `2h`, monthly `3h`, routine `12h`.
9. Roll back by rendering and deploying the full rollback config; this atomically restores v1 and both cache controls false without deleting v2 artifacts:

```powershell
python scripts\render_wrangler_release_config.py production-v1-rollback --enable-production-cron --confirm-production-v1-rollback --output cloudflare\wrangler.production-v1-rollback.generated.toml
npx.cmd wrangler deploy --config cloudflare\wrangler.production-v1-rollback.generated.toml --dry-run --outdir .tmp\worker-production-v1-rollback-dry-run
```

Before each Worker rollout that depends on a new D1 migration, apply and verify the migration first, then deploy the matching Worker immediately in the same controlled release window. Monitor legacy Worker `5xx` responses during that short compatibility interval. Keep current and previous immutable market generations in R2 until the observation gate passes.

## Monitoring

`.github/workflows/cloudflare-health-monitor.yml` polls `CF_WORKER_HEALTH_URL` every 30 minutes. GitHub Actions failure notifications are the baseline alerting path. The same `/api/health` endpoint can be wired into Cloudflare notifications, Better Stack, UptimeRobot, or another external monitor.

## Local Verification

```powershell
python -m pytest -q
python scripts\check_frontend_hygiene.py
python scripts\check_operational_readiness.py
python scripts\check_deployment_preflight.py
npx wrangler deploy --config cloudflare\wrangler.toml --dry-run --outdir .tmp\worker-dry-run
python scripts\render_wrangler_release_config.py production-v1-rollback --enable-production-cron --confirm-production-v1-rollback --output cloudflare\wrangler.production-v1-rollback.generated.toml
python scripts\run_wrangler_dev_smoke.py
npm run test:e2e
```

Remote production smoke:

```powershell
$env:CF_WORKER_HEALTH_URL='https://stock-scanner-beta-api.<account>.workers.dev/api/health'
python scripts\run_remote_smoke.py --health-url $env:CF_WORKER_HEALTH_URL --manifest cloudflare\seed\manifest.json
python scripts\check_pages_api_redirect.py --url https://stock-scanner-beta.pages.dev/api/health
```
