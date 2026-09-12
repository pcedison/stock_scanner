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

Cloudflare Worker secrets (`npx wrangler secret put <NAME> --config cloudflare/wrangler.toml`):

- `SUPER_USER_USERNAME`
- `GITHUB_APP_ID`, `GITHUB_APP_INSTALLATION_ID`, `GITHUB_APP_PRIVATE_KEY`: credentials for a GitHub App that can `POST /repos/{owner}/{repo}/actions/workflows/cloudflare-r2-seed-refresh.yml/dispatches`. See [Dispatch credentials](#dispatch-credentials-github-app) for how to create them. A personal access token would also work, but every fine-grained token expires within 366 days and the seed silently stops refreshing when it does; an App private key has no expiry. All three are secrets rather than vars because this repository is public. Deploys fail preflight (`scripts/check_cloudflare_worker_secrets.py`) while any is missing.

GitHub Actions repository variables:

- `CF_WORKER_HEALTH_URL`: deployed Worker HTTPS `/api/health` URL.

GitHub environment:

- Create a `production` environment.
- For team-owned repositories, require production environment reviewers before deployment.
- For solo-maintainer repositories where reviewer approval is not practical, document that exception and keep branch protection on `main`/`master` with required status checks. Production deploys and R2 seed refreshes still share the `cloudflare-production` concurrency group so they cannot update production at the same time.

Worker production CORS lives in `cloudflare/wrangler.toml`:

- `APP_ENV = "production"`
- `APP_CORS_ALLOW_ORIGINS = "https://stock-scanner-beta.pages.dev"`
- `MARKET_SCAN_API_VERSION = "v2"`
- `EDGE_CACHE_ENABLED = "true"`
- `[cache].enabled = false`
- `[triggers].crons = ["*/20 0-10 * * MON-FRI", "0 11-23 * * MON-FRI"]` (UTC; Cloudflare day-of-week is `1=SUN`, so weekdays are spelled by name). Both are weekday-only: every source the seed is built from publishes on trading days only, so a weekend tick can only re-fetch data that cannot have changed.
- `GITHUB_DISPATCH_ENABLED = "true"`

Production must not allow localhost, 127.0.0.1, or non-HTTPS origins. Production unsafe `/api/*` methods also require `X-Stock-Scanner-CSRF: 1`; the frontend sends this header automatically. Cloudflare Worker session cookies use `SameSite=None; Secure` so authenticated cross-origin fetches from Pages to the Worker can include the account session.

The committed production config serves paginated v2 market reads with edge cache headers: the legacy v1 `GET /api/scan/market` payload (several MB) exhausted Python Worker resource limits in production (2026-09), so v1 is kept only as a streamed pass-through fallback for stale clients. The Worker cron and GitHub dispatch are enabled in the committed config (see "Server-Side R2 Seed Rebuild"); `scripts/check_deployment_preflight.py` rejects a config that turns either of them off. The Worker `[cache]` binding is still enabled only through generated full configs from `scripts/render_wrangler_release_config.py`.

## Dispatch credentials (GitHub App)

The Worker cron triggers the refresh workflow through the GitHub API, which needs a
credential. A GitHub App is used rather than a personal access token because every
fine-grained PAT expires within 366 days and the seed stops refreshing the moment it
does; an App private key has no expiry, and the token actually used on the wire is a
one-hour installation token the Worker mints for itself on each dispatch.

One-time setup:

1. Create the App at <https://github.com/settings/apps/new>.
   - **GitHub App name**: anything unique, e.g. `stock-scanner-seed-refresh`.
   - **Homepage URL**: this repository's URL.
   - **Webhook**: untick **Active**. The Worker calls GitHub, never the reverse.
   - **Repository permissions -> Actions**: **Read and write**. Leave everything else
     at *No access*; `Metadata: Read-only` is added automatically.
   - **Where can this GitHub App be installed?**: *Only on this account*.
2. On the App's page note the **App ID** -> `GITHUB_APP_ID`.
3. **Generate a private key** at the bottom of the same page. A `.pem` downloads; it is
   shown only once. It is already PKCS#8, which is what WebCrypto imports.
4. **Install App** -> this account -> **Only select repositories** -> this repository.
   After installing, the URL ends in `/installations/<id>`; that number is
   `GITHUB_APP_INSTALLATION_ID`.
5. Store all three as Worker secrets:

   ```bash
   npx wrangler secret put GITHUB_APP_ID --config cloudflare/wrangler.toml
   npx wrangler secret put GITHUB_APP_INSTALLATION_ID --config cloudflare/wrangler.toml
   npx wrangler secret put GITHUB_APP_PRIVATE_KEY --config cloudflare/wrangler.toml < app.private-key.pem
   ```

   The private key is multi-line, so pipe the `.pem` in rather than pasting it. Delete
   the local `.pem` afterwards; it never belongs in the repository.

Rotation is only needed if the key is exposed: generate a new one on the App page,
`wrangler secret put` it, then delete the old key in GitHub.


## Refresh cadence and the trading calendar

Every source the seed is built from - daily valuation ratios (`BWIBBU_d`, TPEX P/E), company profiles (`t187ap03`), MOPS monthly revenue (`t187ap05`) and quarterly filings (`t187ap06/07`) - only publishes on a trading day. The market closes at 13:30 and the official post-close files land shortly after, so a trading day's data is complete at **15:00 Taipei**.

Two things follow from that, and both are implemented:

- **Nothing is fetched while the market is shut.** Both Worker crons and the workflow's `schedule` backstop are weekday-only. A weekend tick could only re-fetch Friday's data, burning Actions minutes and adding load to sources that rate-limit.
- **A refresh deadline landing on a non-trading day waits for the next trading day.** `backend/services/cache_policy.next_publication_time` and its Worker mirror `cloudflare/worker_trading_calendar.py` push such a deadline to the next trading day at 15:00 Taipei. Without this the seed was reported stale every three hours all weekend even though the data was complete and current, which failed the health monitor for no reason.

A deadline already on a trading day is left alone, so the intraday cadence during the monthly-revenue (day 8-15) and financial-report windows is unchanged.

Market holidays come from `data/market_calendar_<year>.json` (built from the TWSE holiday schedule). The seed build copies them into the manifest as `marketClosedDates` for the current and next year, which is how the Worker sees them - it cannot read the repo. A missing or unreadable list degrades to weekend-only rather than failing the request.

`tests/test_trading_calendar_parity.py` pins the backend and Worker implementations to the same answers; they are duplicated because the Worker cannot import `backend`.


## Deploy Flow

`.github/workflows/cloudflare-deploy.yml` deploys on pushes to `main`/`master` or manual dispatch. Feature branch and pull request validation happens outside this production deploy workflow.

1. Install Python and Node dependencies.
2. Run pytest, frontend hygiene, operational readiness, pip check, and npm audit.
3. Run deployment preflight for CORS, production environment, concurrency, D1 migration, health, remote smoke, and rollback guardrails.
4. Run Worker dry-run to validate the Cloudflare Python Worker bundle boundary.
5. Run local Worker runtime smoke.
6. Install the Playwright Chromium browser and run browser smoke tests.
7. Before any production mutation, reject bad-quality, offline, financially blocked, or more than 36-hour-old production cache data.
8. Export a D1 backup artifact before migrations.
9. Apply pending D1 migrations from `cloudflare/migrations/`.
10. Deploy Worker and Pages.
11. Verify deployed `/api/health` data safety.
12. Verify Worker CORS for `https://stock-scanner-beta.pages.dev`, including the POST preflight used by the browser fallback.
13. Run deployed public smoke against the Worker `/api/health`, `/api/app-status`, and `/api/data-sources/status`.
14. Verify `https://stock-scanner-beta.pages.dev/api/health` returns the healthy Worker JSON through the status-preserving Pages proxy.
15. Roll back the Worker with `wrangler rollback --yes` if post-deploy verification fails.

Deployment and refresh use different failure budgets. The R2 refresh workflow and health monitor enforce the routine refresh deadline (including the monitor's 15-minute delay limit). Deployment allows up to 1,440 minutes after `nextRefreshAfter`, while independently enforcing the 36-hour absolute cache-age ceiling and all quality gates. Since the routine cache interval is 12 hours, these limits converge at the same 36-hour hard ceiling. This prevents delayed GitHub scheduled runs from rolling back unrelated application releases without allowing truly stale production data.

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

`.github/workflows/cloudflare-r2-seed-refresh.yml` is the production-side refresh worker for market scan jobs queued by the Cloudflare Worker. It has no GitHub `schedule` trigger: GitHub delivered scheduled runs hours late or not at all (2026-09, roughly one run per 3-4 hours against a 20-minute cron), which let the seed pass its policy deadline and tripped the health monitor. Scheduling lives in the Worker instead.

Worker cron (`cloudflare/wrangler.toml` `[triggers].crons`, every 20 minutes during Taipei weekday business hours and hourly through weekday evenings) runs `on_scheduled` -> `cloudflare/worker_refresh_control.run_scheduled_refresh`, which on every tick:

1. Re-queues a `running` job whose workflow run started more than 2 hours ago (cancelled or timed-out run) - `worker_refresh_schedule.ORPHANED_RUNNING_SECONDS`.
2. Resets a `failed` / `unknown` / never-claimed `dispatched` job back to `pending` after 20 minutes so the dispatch is retried - `DISPATCH_RETRY_SECONDS`.
3. Queues a new `market_scan` job when the deployed seed is stale or will be stale within 2 hours (`REFRESH_AHEAD_SECONDS`); the terminal-job cooldown is shortened by the same window so the rebuild lands before `cacheStatus.nextRefreshAfter`.
4. Dispatches the pending job with `POST .../actions/workflows/cloudflare-r2-seed-refresh.yml/dispatches` (`inputs.force=false`), authenticating as the GitHub App: it signs a short-lived RS256 JWT with `GITHUB_APP_PRIVATE_KEY` through WebCrypto, exchanges it for a one-hour installation token, and uses that for the dispatch. A token is minted per dispatch, so nothing is cached and nothing expires between runs. The job records `dispatch_status` / `dispatch_error_code`, and `/api/health` exposes this as `refreshDispatch`.

The workflow can still be dispatched manually; `force=true` bypasses the D1/freshness guards and unlocks the larger MOPS backfill budget. On each run it:

1. Poll D1 `refresh_jobs` for queued or running `market_scan` jobs and check deployed `/api/health` freshness.
2. Stop without touching R2 when no job is queued and the production seed is fresh, unless the workflow is manually forced.
3. Mark queued jobs as `running`.
4. Download the previous R2 `official/monthly_revenue_history.json`, `official/official_fundamentals_history.json`, and `official/official_history_backfill_progress.json`, then merge them with the committed seed through `scripts/hydrate_cloudflare_seed_inputs.py` (the R2 quarterly history wins over the zip snapshot so backfilled prior-year quarters persist across runs).
5. Backfill prior-year same-quarter fundamentals from MOPS with `python -m backend.services.official_history_backfill --retry-failed` (bounded by `OFFICIAL_HISTORY_BACKFILL_LIMIT`, 400 for Worker-cron dispatches / `backfill_limit` input on manual `force=true` runs). This supplies the EPS and net-income YoY inputs for the X3–X5 rules; a MOPS outage degrades those rules to INSUFFICIENT_DATA instead of blocking the refresh.
6. Rebuild `cloudflare/seed/*` from official sources with `CLOUDFLARE_SEED_MODE=online`.
7. Reject publication when fewer than 1,000 companies have consecutive revenue months or when more than 10% of scan rows lack the X2 previous-month signal.
8. Package and validate the newest `data/official_cache_seed_*.zip` seed artifact with freshness, monthly-history, universe, and analysis gates.
9. Generate the R2 upload manifest with `scripts/cloudflare_seed_upload_plan.py`, then upload the rebuilt manifest, market scan summary/latest payloads, analysis shards, holding shards, and required official cache artifacts to R2.
10. Verify the deployed Worker health endpoint and remote smoke checks against the rebuilt manifest.
11. Mark D1 refresh jobs as `success`, or `failed` if any step in the rebuild/upload/verify flow fails.

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

`.github/workflows/cloudflare-health-monitor.yml` polls `CF_WORKER_HEALTH_URL` every 4 hours (GitHub may deliver it late). It fails when the seed is more than 75 minutes past `cacheStatus.nextRefreshAfter`, and - via `--require-dispatch-healthy` - as soon as `/api/health` `refreshDispatch` reports a `failed` dispatch or three unacknowledged attempts, before the seed itself goes stale. A revoked App key, a wrong installation id, or a renamed workflow all land there; `scripts/check_cloudflare_health.py` names the secret to fix in the failure message. GitHub Actions failure notifications are the baseline alerting path. The same `/api/health` endpoint can be wired into Cloudflare notifications, Better Stack, UptimeRobot, or another external monitor.

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
