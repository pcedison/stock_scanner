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
- `EDGE_CACHE_ENABLED = "true"`
- `[cache].enabled = false`
- `[triggers].crons = ["*/20 21-23 * * SUN-THU", "*/20 0-13 * * MON-FRI"]` (UTC; Cloudflare day-of-week is `1=SUN`, so weekdays are spelled by name). Together they tick every 20 minutes Monday-Friday 05:00-21:59 Taipei (ticks after 21:59 only ever served the hourly failed-build retry, so they were dropped); the first is `SUN-THU` because Monday's 06:30 Taipei slot is still Sunday in UTC. A tick only dispatches a rebuild once `nextRefreshAfter` has passed.
- `GITHUB_DISPATCH_ENABLED = "true"`

Production must not allow localhost, 127.0.0.1, or non-HTTPS origins. Production unsafe `/api/*` methods also require `X-Stock-Scanner-CSRF: 1`; the frontend sends this header automatically. Cloudflare Worker session cookies use `SameSite=None; Secure` so authenticated cross-origin fetches from Pages to the Worker can include the account session.

The Worker serves paginated v2 market reads with edge cache headers and nothing else. The whole-scan `GET/POST /api/scan/market` payload (several MB) exhausted Python Worker resource limits in production (2026-09), so those routes were retired on 2026-09-15 and now return 404; there is no market API version switch in the config any more, and the only market-read rollback is step 15 above (`wrangler rollback --yes`). `public/market_scan_summary.json` is still built and uploaded because the `/api/reports/market` export reads it. The Worker cron and GitHub dispatch are enabled in the committed config (see "Server-Side R2 Seed Rebuild"); `scripts/check_deployment_preflight.py` rejects a config that turns either of them off. The Worker `[cache]` binding is still enabled only through generated full configs from `scripts/render_wrangler_release_config.py`.

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

Every source the seed is built from - daily valuation ratios (`BWIBBU_d`, TPEX P/E), company profiles (`t187ap03`), MOPS monthly revenue (`t187ap05`) and quarterly filings (`t187ap06/07`) - only publishes on a trading day, and none of the OpenAPI datasets change intraday (HTTP `Last-Modified`, 2026-09-14):

| Source | Regenerated (Taipei) |
|---|---|
| TWSE OpenAPI `t187ap03/05/06/07_L` | ~05:25, MOPS filings through the previous day |
| TPEX OpenAPI `mopsfin_*_O`, P/E | ~16:00 |
| TWSE `BWIBBU_d` | after the 13:30 close, same day |

So the seed is rebuilt only at **publication slots** (`backend/services/cache_policy.py`): **18:00** every trading day, plus **06:30** inside filing windows. `next_refresh_after(generatedAt)` is the first active slot after a build; the seed build writes it to the manifest as `nextRefreshAfter` and the Worker uses that value as-is. Nothing is queued ahead of a slot (`REFRESH_AHEAD_SECONDS = 0`) - before it the datasets are the ones the seed already has.

Filing windows follow the statutory calendar (`backend/services/filing_calendar.py`; 證券交易法 §36 and the FSC special-scope rules), with a deadline on a closed day moved to the next business day:

| Report | General listed/OTC | Financial holding / bank / insurance (KY for Q2) | Window |
|---|---|---|---|
| Annual | 3/31 (large caps ~3/16) | 3/31 | 3/1 - 3/31 |
| Q1 | 5/15 | 5/30 | 5/1 - 5/30 |
| Q2 | 8/14 | 8/31 | 7/31 - 8/31 |
| Q3 | 11/14 | 11/29 | 10/31 - 11/29 |
| Monthly revenue | by the 10th | insurers by the 15th | 1st - 10th |

The freshness gate expects a period only after its final deadline has passed. The Worker's policy label mirrors these windows (`cloudflare/worker_trading_calendar.refresh_reason`, parity-tested day by day).

Consequences:

- **Nothing is fetched while the market is shut.** Worker crons, slots and the workflow's `schedule` backstop are weekday-only; weekends and `marketClosedDates` have no slots.
- **At most two rebuilds per trading day** (one outside filing windows), instead of every 2-3 hours inside windows.
- **A failed rebuild is retried after an hour** (`worker_refresh_jobs.TERMINAL_JOB_COOLDOWN_SECONDS`), not after the whole policy interval.
- **The 36-hour cache-age ceiling counts trading-day hours only.** `scripts/check_cloudflare_health.trading_hours_between` (used by the health monitor and the remote smoke) skips weekends and `marketClosedDates`. With weekday-only crons the last refresh before a weekend lands Saturday morning, which is ~54 wall-clock hours old by Monday noon; measured in wall-clock time that failed the monitor every Sunday evening (2026-09-13/14). The summary reports both `cacheAgeHours` (trading) and `cacheWallAgeHours`.

A single broken company-profile endpoint no longer zeroes the scan universe: `OfficialMonthlyRevenueAdapter.fetch_company_profiles` derives a market's profiles from that market's monthly-revenue file when its profile endpoint fails (2026-09-13, TPEX `mopsfin_t187ap03_O` reset every connection mid-body for hours and blocked every R2 rebuild). The fallback is recorded as `companyProfilesFallback` in the provider source status; the build still fails if the revenue file is unavailable too.

Market holidays come from `data/market_calendar_<year>.json` (built from the TWSE holiday schedule). The seed build copies them into the manifest as `marketClosedDates` for the current and next year, which is how the Worker sees them - it cannot read the repo. A missing or unreadable list degrades to weekend-only (plus New Year's Day) rather than failing the request.

The files maintain themselves: `.github/workflows/update-market-calendar.yml` runs `scripts/update_market_calendars.py` every Monday from October to January (when TWSE publishes the next year's schedule) and on the 1st of every other month (revisions). It fetches this year and next; an unpublished year writes nothing, a published one is validated (weekday closures inside the year, New Year's Day when it is a weekday, Spring Festival in January/February, at least 6 closed days, a revision may not drop more than 2 days), checked with the calendar-dependent tests, and merged through an automated PR. A fetch error or failed validation fails the run and writes nothing. No manual step is needed each year.

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
13. Run deployed public smoke against the Worker `/api/health`, `/api/app-status`, `/api/data-sources/status`, `/api/runtime-config` (must advertise `marketScanApiVersion=v2`), and `/api/scan/market/index` (must carry a 24-character `generationId`, a `cacheStatus`, and at least 1,000 rows across the category counts).
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
6. Dispatch the `Validate` workflow (`ci.yml`) on the refresh branch explicitly, because GitHub does not trigger `pull_request`/`push` workflows for events created by `GITHUB_TOKEN`, so a PR opened by this workflow would otherwise show no checks.

This keeps production deploys deterministic while preventing the committed seed from silently going stale.

## Server-Side R2 Seed Rebuild

`.github/workflows/cloudflare-r2-seed-refresh.yml` is the production-side refresh worker for market scan jobs queued by the Cloudflare Worker. GitHub delivered scheduled runs hours late or not at all (2026-09, roughly one run per 3-4 hours against a 20-minute cron), so scheduling lives in the Worker; the workflow's own `schedule` is only a backstop of three ticks per weekday just after the publication slots (07:07, 18:07, 20:07 Taipei).

Worker cron (`cloudflare/wrangler.toml` `[triggers].crons`, every 20 minutes Monday-Friday 05:00-21:59 Taipei, covering both publication slots and three hourly failed-build retries) runs `on_scheduled` -> `cloudflare/worker_refresh_control.run_scheduled_refresh`, which on every tick:

1. Re-queues a `running` job whose workflow run started more than 2 hours ago (cancelled or timed-out run) - `worker_refresh_schedule.ORPHANED_RUNNING_SECONDS`.
2. Resets a `failed` / `unknown` / never-claimed `dispatched` job back to `pending` after 20 minutes so the dispatch is retried - `DISPATCH_RETRY_SECONDS`.
3. Queues a new `market_scan` job once the deployed seed reaches `cacheStatus.nextRefreshAfter` (the next publication slot; `REFRESH_AHEAD_SECONDS = 0`). After a failed rebuild the next job waits `TERMINAL_JOB_COOLDOWN_SECONDS` (1 hour).
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
9. Generate the R2 upload manifest with `scripts/cloudflare_seed_upload_plan.py`, then upload the rebuilt manifest, market scan summary/latest payloads, analysis shards, holding shards, and required official cache artifacts to R2. Backup and publish run eight wrangler calls at a time; objects whose SHA-256 matches the backed-up copy are not re-uploaded, and public/manifest.json is always written last so the Worker never sees a new manifest before its shards.
10. Verify the deployed Worker health endpoint and remote smoke checks against the rebuilt manifest.
11. Mark D1 refresh jobs as `success`, or `failed` if any step in the rebuild/upload/verify flow fails.

The workflow shares the `cloudflare-production` concurrency group with production deploys so R2 seed uploads do not race with a deploy. When the Worker queues a D1 `market_scan` refresh job, this workflow performs the actual seed rebuild and R2 update on the next run.
Seed artifact Git policy is documented in `docs/seed_artifact_policy.md`; routine refresh output belongs in R2, and new committed seed zips require an explicit forced add and review note.

## Market v2 Release Configs

Production and staging have served market v2 since 2026-09. Each command below renders a complete Wrangler config; generated files are ignored by Git and must be dry-run before any real deploy.

1. Provision isolated staging D1/R2 resources and secrets under explicit release authority.
2. Render staging with v2/cache enabled and dispatch disabled:

```powershell
python scripts\render_wrangler_release_config.py staging --database-id $env:CF_STAGING_D1_DATABASE_ID --bucket-name $env:CF_STAGING_R2_BUCKET --output cloudflare\wrangler.staging.generated.toml
npx.cmd wrangler deploy --config cloudflare\wrangler.staging.generated.toml --dry-run --outdir .tmp\worker-staging-dry-run
```

3. Deploy staging only after dry-run passes, then run the canary:

```powershell
python scripts\check_market_scan_v2_canary.py --base-url https://stock-scanner-beta-api-staging.<account>.workers.dev --require-edge-hit
```

4. Deploy production code with the committed cache-disabled config.
5. Under separate cron release authority, render and dry-run the full cron config:

```powershell
python scripts\render_wrangler_release_config.py production-cron --enable-production-cron --output cloudflare\wrangler.production-cron.generated.toml
npx.cmd wrangler deploy --config cloudflare\wrangler.production-cron.generated.toml --dry-run --outdir .tmp\worker-production-cron-dry-run
```

6. Under separate v2 release authority, render and dry-run the full v2 config:

```powershell
python scripts\render_wrangler_release_config.py production-v2 --enable-production-cron --enable-production-v2 --output cloudflare\wrangler.production-v2.generated.toml
npx.cmd wrangler deploy --config cloudflare\wrangler.production-v2.generated.toml --dry-run --outdir .tmp\worker-production-v2-dry-run
```

7. Observe one full cache window after a cache change: financial `2h`, monthly `3h`, routine `12h`.
8. Roll back with `wrangler rollback --yes` (step 15 of the deploy runbook above). That is the only rollback: the market API version switch and its release profile were removed with the v1 routes on 2026-09-15.

Before each Worker rollout that depends on a new D1 migration, apply and verify the migration first, then deploy the matching Worker immediately in the same controlled release window. Monitor Worker `5xx` responses during that short compatibility interval. Keep current and previous immutable market generations in R2 until the observation gate passes.

## Monitoring

`.github/workflows/cloudflare-health-monitor.yml` polls `CF_WORKER_HEALTH_URL` every 4 hours on weekdays and twice a day on weekends (GitHub may deliver it late). It fails when the seed is more than 75 minutes past `cacheStatus.nextRefreshAfter`, and - via `--require-dispatch-healthy` - as soon as `/api/health` `refreshDispatch` reports a `failed` dispatch or three unacknowledged attempts, before the seed itself goes stale. A revoked App key, a wrong installation id, or a renamed workflow all land there; `scripts/check_cloudflare_health.py` names the secret to fix in the failure message. GitHub Actions failure notifications are the baseline alerting path. The same `/api/health` endpoint can be wired into Cloudflare notifications, Better Stack, UptimeRobot, or another external monitor.

## Local Verification

```powershell
python -m pytest -q
python scripts\check_frontend_hygiene.py
python scripts\check_operational_readiness.py
python scripts\check_deployment_preflight.py
npx wrangler deploy --config cloudflare\wrangler.toml --dry-run --outdir .tmp\worker-dry-run
python scripts\render_wrangler_release_config.py production-v2 --enable-production-cron --enable-production-v2 --output cloudflare\wrangler.production-v2.generated.toml
python scripts\run_wrangler_dev_smoke.py
npm run test:e2e
```

Remote production smoke:

```powershell
$env:CF_WORKER_HEALTH_URL='https://stock-scanner-beta-api.<account>.workers.dev/api/health'
python scripts\run_remote_smoke.py --health-url $env:CF_WORKER_HEALTH_URL --manifest cloudflare\seed\manifest.json
python scripts\check_pages_api_redirect.py --url https://stock-scanner-beta.pages.dev/api/health
```
