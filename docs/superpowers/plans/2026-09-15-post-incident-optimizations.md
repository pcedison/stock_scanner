# Post-incident Optimizations Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship the seven follow-ups identified after the 2026-09-14 health-monitor / R2-seed incident as six independently mergeable PRs, each verified by a fresh-context verifier before push, PR, CI and merge.

**Architecture:** Nothing here changes what the seed contains or when it is rebuilt (that was #162/#163). The PRs remove code that only served pre-#163 manifests, stop the weekly committed-seed workflow from failing silently, make the R2 publish step parallel and change-aware, trim scheduled ticks that can never do work, retire the v1 market-scan path (a documented emergency valve; the user chose to give it up), and archive stale artifacts.

**Tech Stack:** Python 3.12/3.13 (pytest, ruff, mypy), Cloudflare Python Worker, GitHub Actions, wrangler 4, Playwright 1.60 (Chromium installed locally), `gh` CLI (authenticated as the repo owner).

**Execution order and PR grouping** (each PR branches from the freshly merged `main`):

| PR | Tasks | Executor | Risk |
|---|---|---|---|
| A `chore/validator-default-and-archive` | 3, 4 | main session | low |
| B `chore/trim-idle-cron-ticks` | 5 | main session | low |
| C `ci/weekly-seed-cache-failure-alert` | 7 | main session, then live `workflow_dispatch` | low |
| D `refactor/drop-legacy-manifest-fallback` | 2 | sonnet subagent | medium (Worker) |
| E `perf/parallel-change-aware-r2-publish` | 1 | sonnet subagent | medium (R2 publish path, rollback path) |
| F `refactor/retire-market-scan-v1` | 6 | opus subagent | high (Worker + FastAPI + frontend + deploy smoke) |

**Per-PR delivery protocol** (applies to every PR, not repeated in each task):

1. `git checkout main && git pull --ff-only && git checkout -b <branch>`.
2. Implement with TDD as written in the task.
3. Local gates named in the task's "Verification" list must pass. Never claim a gate passed without its output.
4. Dispatch the `verifier` agent with the task's **Acceptance criteria** only (never the implementation notes). It reads files and runs the listed commands. Fix and re-verify until it reports all criteria pass.
5. `git push -u origin <branch>`, `gh pr create --fill --body-file <body>` with the PR body ending in the session attribution block, then `gh pr checks <n> --watch --interval 30`. All required checks (Validate, CodeQL, Deploy to Cloudflare where it runs on PRs, nightly-date-sweep when it triggers on the touched paths) must be green.
6. `gh pr merge <n> --squash --delete-branch`. For PRs D, E, F: wait for the `Deploy to Cloudflare` run on `main` to succeed, then confirm production with `python scripts/check_cloudflare_health.py --url "$CF_WORKER_HEALTH_URL" --max-cache-age-hours 36 --max-refresh-delay-minutes 75 --reject-offline-seed` (the URL is the repository variable `CF_WORKER_HEALTH_URL`, readable with `gh variable get CF_WORKER_HEALTH_URL`).
7. Commit messages end with:
   ```
   Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
   Claude-Session: https://claude.ai/code/session_01D5wHXNDuBWKdwzG7VvCbPo
   ```
   PR bodies end with:
   ```
   🤖 Generated with [Claude Code](https://claude.com/claude-code)

   https://claude.ai/code/session_01D5wHXNDuBWKdwzG7VvCbPo
   ```

---

## PR A — Task 3: validator summarizes the latest quarter's failed-company CSV

**Files:**
- Modify: `scripts/validate_cloudflare_seed_inputs.py:28` (constant) and the `main()` argparse default near line 827
- Test: `tests/test_cloudflare_seed_inputs.py`

**Why:** `DEFAULT_FAILED_COMPANIES_CSV` is hardcoded to `2026Q1`, so every CI seed-quality summary reports the May follow-up list instead of the current quarter's.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_cloudflare_seed_inputs.py` (the module alias `validator_module` already exists at the top of the file):

```python
def test_default_failed_companies_csv_picks_the_latest_quarter(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    for label in ("2026Q1", "2026Q3", "2026Q2"):
        (data_dir / f"official_history_failed_companies_{label}.csv").write_text("stock_code\n", encoding="utf-8")

    chosen = validator_module.default_failed_companies_csv(data_dir)

    assert chosen.name == "official_history_failed_companies_2026Q3.csv"


def test_default_failed_companies_csv_without_reports_points_at_the_conventional_name(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()

    chosen = validator_module.default_failed_companies_csv(data_dir)

    assert chosen == data_dir / "official_history_failed_companies_latest.csv"
    assert validator_module.failed_company_summary(chosen)["failedCompanies"] == 0
```

- [ ] **Step 2: Run it to verify it fails**

Run: `python -m pytest tests/test_cloudflare_seed_inputs.py -k default_failed_companies_csv -q`
Expected: FAIL with `AttributeError: ... has no attribute 'default_failed_companies_csv'`

- [ ] **Step 3: Implement**

In `scripts/validate_cloudflare_seed_inputs.py` replace line 28:

```python
def default_failed_companies_csv(data_dir: Path = Path("data")) -> Path:
    """The newest quarterly follow-up CSV; labels are ``YYYYQn`` so lexical order is chronological."""
    candidates = sorted(data_dir.glob("official_history_failed_companies_*.csv"))
    return candidates[-1] if candidates else data_dir / "official_history_failed_companies_latest.csv"


DEFAULT_FAILED_COMPANIES_CSV = default_failed_companies_csv()
```

Nothing else changes: `failed_company_summary(path=DEFAULT_FAILED_COMPANIES_CSV)` and the `--failed-companies-csv` default keep working.

- [ ] **Step 4: Run the file's tests**

Run: `python -m pytest tests/test_cloudflare_seed_inputs.py -q && python -m ruff check scripts/validate_cloudflare_seed_inputs.py tests/test_cloudflare_seed_inputs.py`
Expected: all pass, ruff clean.

- [ ] **Step 5: Commit**

```bash
git add scripts/validate_cloudflare_seed_inputs.py tests/test_cloudflare_seed_inputs.py
git commit -m "fix: summarize the latest quarter's failed-company report in seed validation"
```

## PR A — Task 4: archive stale docs and drop superseded quarterly reports

**Files:**
- Move (git mv): `docs/tasks.md`, `docs/spec.md`, `docs/proposals/market-scan-cacheability.md`, `docs/reports/2026-05-29-healthcheck-followup.md`, `docs/reports/OPTIMIZATION_REPORT.md` → `docs/archive/` (keep sub-structure: `docs/archive/proposals/…`, `docs/archive/reports/…`)
- Delete: `data/official_history_failed_companies_2026Q1.csv`, `data/official_history_failed_companies_2026Q2.csv`, `docs/official_history_failed_companies_2026Q1.md`, `docs/official_history_failed_companies_2026Q2.md`
- Modify: `AGENTS.md:26`, `README.md:116-119`, `backend/routers/market.py:187` (comment), `frontend/app.js:1487` and `:1809` (comments)
- Do NOT touch `docs/superpowers/` (AGENTS.md already declares it historical and #164 stamped the plans) or the local `.tmp/` directory (gitignored; the user deletes it by hand if wanted).

- [ ] **Step 1: Move and delete**

```bash
mkdir -p docs/archive/proposals docs/archive/reports
git mv docs/tasks.md docs/archive/tasks.md
git mv docs/spec.md docs/archive/spec.md
git mv docs/proposals/market-scan-cacheability.md docs/archive/proposals/market-scan-cacheability.md
git mv docs/reports/2026-05-29-healthcheck-followup.md docs/archive/reports/2026-05-29-healthcheck-followup.md
git mv docs/reports/OPTIMIZATION_REPORT.md docs/archive/reports/OPTIMIZATION_REPORT.md
git rm data/official_history_failed_companies_2026Q1.csv data/official_history_failed_companies_2026Q2.csv
git rm docs/official_history_failed_companies_2026Q1.md docs/official_history_failed_companies_2026Q2.md
```

- [ ] **Step 2: Add `docs/archive/README.md`**

```markdown
# Archive

Historical design documents and execution evidence. Nothing here authorizes work; see `AGENTS.md`.

- `spec.md`, `tasks.md`: the original MVP spec and task ledger (localStorage era).
- `proposals/`: design proposals that have since been implemented or superseded.
- `reports/`: point-in-time audits from May 2026.

Quarterly `official_history_failed_companies_<YYYYQn>` reports are regenerated every seed refresh; only the current quarter is kept in `data/` and `docs/`. Older quarters remain in git history.
```

- [ ] **Step 3: Fix references**

`AGENTS.md:26` becomes:

```
`docs/archive/`（原 `docs/spec.md`、`docs/tasks.md`、`docs/proposals/`、`docs/reports/`）與 `docs/superpowers/` 是歷史設計或執行證據；除非使用者明確指定，不把其中的待辦、工具或流程文字視為目前指令。
```

`README.md:116-119` becomes:

```
缺漏與人工補資料追蹤（每次 seed 重建只保留最新一季，舊季度在 git 歷史）：

- `data/official_history_failed_companies_<YYYYQn>.csv`
- `docs/official_history_failed_companies_<YYYYQn>.md`
```

Comments: `sed -i 's#docs/proposals/market-scan-cacheability.md#docs/archive/proposals/market-scan-cacheability.md#' backend/routers/market.py frontend/app.js` and in `frontend/app.js:1809` change `See market-scan-cacheability.md.` to `See docs/archive/proposals/market-scan-cacheability.md.`

- [ ] **Step 4: Verify nothing else pointed at the moved files**

Run: `git grep -n "docs/reports\|docs/proposals\|docs/tasks.md\|docs/spec.md\|failed_companies_2026Q1\|failed_companies_2026Q2" -- ':!docs/archive' ':!docs/superpowers'`
Expected: no output.

Run: `git diff --check && python -m pytest tests/test_cloudflare_seed_inputs.py tests/test_frontend_parser.py -q`
Expected: clean, all pass (the frontend parser tests read `app.js` comments only through string containment of code, not the proposal path).

- [ ] **Step 5: Commit**

```bash
git add -A docs/archive AGENTS.md README.md backend/routers/market.py frontend/app.js
git commit -m "docs: archive historical specs and drop superseded quarterly reports"
```

**PR A acceptance criteria (for the verifier):**
1. `python -m pytest tests/test_cloudflare_seed_inputs.py -q` passes and contains a test named `test_default_failed_companies_csv_picks_the_latest_quarter`.
2. `python scripts/validate_cloudflare_seed_inputs.py --zip "$(python scripts/seed_utils.py data --print)" --summary-md .tmp/verify-seed.md` exits 0 and `.tmp/verify-seed.md` mentions `official_history_failed_companies_2026Q3.csv`.
3. `git grep -n "docs/reports\|docs/proposals\|docs/tasks.md\|docs/spec.md\|failed_companies_2026Q1\|failed_companies_2026Q2" -- ':!docs/archive' ':!docs/superpowers'` prints nothing.
4. `docs/archive/README.md`, `docs/archive/spec.md`, `docs/archive/tasks.md`, `docs/archive/proposals/market-scan-cacheability.md`, `docs/archive/reports/OPTIMIZATION_REPORT.md` exist; `docs/reports/`, `docs/proposals/` no longer exist; `data/` contains exactly one `official_history_failed_companies_*.csv` (2026Q3).
5. `python -m ruff check scripts tests` and `git diff --check` are clean.

---

## PR B — Task 5: stop scheduled ticks that can never do work

**Files:**
- Modify: `cloudflare/wrangler.toml:38-41`, `scripts/render_wrangler_release_config.py:19`, `.github/workflows/cloudflare-health-monitor.yml:4-5`, `docs/cloudflare_deployment.md:45` and `:250`
- Test: `tests/test_deployment_preflight.py:146` and `:294`

**Why:** After the 18:00 Taipei slot, Worker ticks until 23:59 only exist for the 1-hour failed-build retry; three retries (until 21:59) are enough. The health monitor runs every 4 hours on weekends although nothing can change; two weekend checks a day still catch a dead Worker.

- [ ] **Step 1: Update the tests first**

`tests/test_deployment_preflight.py:146`:

```python
    # Evening slot is 18:00 Taipei (10:00 UTC); a failed build retries hourly, so ticks stop
    # after three retries (21:59 Taipei = 13:59 UTC) instead of running until midnight.
    assert config["triggers"]["crons"] == ["*/20 21-23 * * SUN-THU", "*/20 0-13 * * MON-FRI"]
```

`tests/test_deployment_preflight.py:294`:

```python
    # Weekdays every 4 hours; weekends only twice a day (nothing is published, but a dead
    # Worker or a revoked dispatch credential should still be noticed within 12 hours).
    assert _workflow_schedule_crons(health_path) == ["11 */4 * * 1-5", "11 8,20 * * 0,6"]
```

Run: `python -m pytest tests/test_deployment_preflight.py -q` → expected: 2 failures on the two assertions.

- [ ] **Step 2: Change the configs**

`cloudflare/wrangler.toml:38-41`:

```toml
crons = [
  "*/20 21-23 * * SUN-THU", # Taipei 05:00-07:59 Mon-Fri: morning publication slot (06:30)
  "*/20 0-13 * * MON-FRI",  # Taipei 08:00-21:59 Mon-Fri: evening slot (18:00) + three hourly failed-build retries
]
```

`scripts/render_wrangler_release_config.py:19`:

```python
PRODUCTION_CRONS = ["*/20 21-23 * * SUN-THU", "*/20 0-13 * * MON-FRI"]
```

`.github/workflows/cloudflare-health-monitor.yml:4-6`:

```yaml
  schedule:
    - cron: "11 */4 * * 1-5" # weekdays every 4 hours (UTC)
    - cron: "11 8,20 * * 0,6" # weekends twice a day: 16:11 and 04:11 Taipei
```

`docs/cloudflare_deployment.md:45`: replace the cron literal with `["*/20 21-23 * * SUN-THU", "*/20 0-13 * * MON-FRI"]` and `05:00-23:59 Taipei` with `05:00-21:59 Taipei`, appending: `Ticks after 21:59 only ever served the hourly failed-build retry, so they were dropped.`
`docs/cloudflare_deployment.md:250`: replace `polls ... every 4 hours` with `polls ... every 4 hours on weekdays and twice a day on weekends`.

- [ ] **Step 3: Verify**

Run:
```
python -m pytest tests/test_deployment_preflight.py tests/test_render_wrangler_release_config.py -q
python scripts/check_deployment_preflight.py
python scripts/check_operational_readiness.py
python scripts/render_wrangler_release_config.py production-v2 --enable-production-cron --enable-production-v2 --output .tmp/wrangler.production-v2.generated.toml
npx wrangler deploy --config .tmp/wrangler.production-v2.generated.toml --dry-run --outdir .tmp/worker-dry-run
```
Expected: all pass; dry-run prints the two cron expressions.

- [ ] **Step 4: Commit**

```bash
git add cloudflare/wrangler.toml scripts/render_wrangler_release_config.py .github/workflows/cloudflare-health-monitor.yml docs/cloudflare_deployment.md tests/test_deployment_preflight.py
git commit -m "chore: stop Worker ticks after the last failed-build retry and halve weekend health polls"
```

**PR B acceptance criteria:** (1) `python -m pytest tests/test_deployment_preflight.py tests/test_render_wrangler_release_config.py -q` passes; (2) `cloudflare/wrangler.toml` and `scripts/render_wrangler_release_config.py` both carry exactly `"*/20 0-13 * * MON-FRI"` as the second cron; (3) the health-monitor workflow has two schedule entries, `11 */4 * * 1-5` and `11 8,20 * * 0,6`; (4) `python scripts/check_deployment_preflight.py` exits 0; (5) the Worker dry-run above succeeds.

---

## PR C — Task 7: the weekly committed-seed refresh must not fail silently

**Files:**
- Modify: `.github/workflows/refresh-cloudflare-seed.yml` (permissions block, new final step)
- Test: `tests/test_deployment_preflight.py` (new test using the existing `yaml` helper)

**Why:** `Refresh Cloudflare Seed Cache` failed on 08-30, 09-06 and 09-13 (the last one with the TPEX TLS error fixed by #162) and nobody noticed because a failed scheduled run only emails the repo owner. The fix itself (#162) already covers this workflow's fetch path; what is missing is (a) a visible failure signal and (b) proof, by a live run, that it passes again.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_deployment_preflight.py`:

```python
def test_weekly_seed_cache_refresh_opens_an_issue_when_it_fails():
    path = Path(".github/workflows/refresh-cloudflare-seed.yml")
    workflow = yaml.safe_load(path.read_text(encoding="utf-8"))

    assert workflow["permissions"]["issues"] == "write"
    steps = workflow["jobs"]["refresh"]["steps"]
    failure_steps = [step for step in steps if str(step.get("if", "")).startswith("failure()")]
    assert len(failure_steps) == 1
    assert "gh issue" in failure_steps[0]["run"]
    assert failure_steps[0] is steps[-1]
```

Run: `python -m pytest tests/test_deployment_preflight.py -k weekly_seed_cache -q` → expected: FAIL (`KeyError: 'issues'`).

- [ ] **Step 2: Edit the workflow**

Permissions block becomes:

```yaml
permissions:
  contents: write
  pull-requests: write
  issues: write
```

Append as the last step of the `refresh` job:

```yaml
      - name: Open or update a failure issue
        if: failure()
        env:
          RUN_URL: ${{ github.server_url }}/${{ github.repository }}/actions/runs/${{ github.run_id }}
        run: |
          title="Weekly seed cache refresh is failing"
          body="The scheduled committed-seed refresh failed: $RUN_URL

          Three consecutive silent failures went unnoticed in 2026-08/09, so this issue is opened automatically. Close it once a run succeeds (the workflow does not close it by itself)."
          existing="$(gh issue list --state open --search "\"$title\" in:title" --json number --jq '.[0].number')"
          if [ -n "$existing" ]; then
            gh issue comment "$existing" --body "Failed again: $RUN_URL"
          else
            gh issue create --title "$title" --body "$body"
          fi
```

- [ ] **Step 3: Verify**

Run: `python -m pytest tests/test_deployment_preflight.py -q && git diff --check`
Expected: pass. Also run `python - <<'EOF'
import yaml; yaml.safe_load(open('.github/workflows/refresh-cloudflare-seed.yml', encoding='utf-8')); print('yaml ok')
EOF`

- [ ] **Step 4: Commit, PR, merge** (per protocol).

- [ ] **Step 5: Live verification after merge (this is the real test of #162 on this workflow)**

```bash
gh workflow run refresh-cloudflare-seed.yml --ref main
sleep 60; run_id=$(gh run list --workflow "Refresh Cloudflare Seed Cache" --limit 1 --json databaseId -q '.[0].databaseId')
gh run watch "$run_id" --exit-status
```
Expected: success. If the seed changed, the run opens a PR titled `Refresh Cloudflare seed cache 2026-09-15` from branch `automation/cloudflare-seed-refresh-20260915`; wait for its checks and merge it with `gh pr merge --squash --delete-branch`. If it prints `No seed changes detected.` there is nothing to merge. Either outcome closes the task; record which one happened in the final report.

**PR C acceptance criteria:** (1) the new test passes; (2) the workflow's last step runs only on `failure()` and calls `gh issue`; (3) `permissions.issues == write`; (4) after merge, a manual run of `Refresh Cloudflare Seed Cache` on `main` concludes `success` (record the run URL).

---

## PR D — Task 2: remove the legacy-manifest freshness fallback

**Files:**
- Modify: `backend/services/cache_policy.py` (delete `TRADING_DAY_PUBLISH_HOUR` and `next_publication_time`, lines 35-36 and 101-117)
- Modify: `cloudflare/worker_trading_calendar.py` (delete `TRADING_DAY_PUBLISH_HOUR`, `next_publication_time`, `next_refresh_deadline`; keep `parse_closed_dates`, `is_trading_day`, `MAX_TRUSTED_REFRESH_SPAN`, `manifest_next_refresh`, `FILING_DEADLINES`, `next_open_day`, `refresh_reason`; rewrite the module docstring)
- Modify: `cloudflare/worker.py:398-403` (`cache_policy`) and `:425-436` (`cache_status_from_manifest`)
- Modify: `docs/cloudflare_deployment.md:100` (drop the parenthetical about legacy manifests)
- Tests: `tests/test_trading_calendar_parity.py`, `tests/test_cloudflare_worker.py` (fixtures that rely on interval semantics), `tests/test_cache_policy.py` (if any test imports the deleted names)

**Why:** Production has carried `nextRefreshAfter` since 2026-09-14T11:46Z. The interval rule (`generatedAt + 2h/3h/12h`, moved to 15:00 on the next trading day) now only runs for manifests that no longer exist and duplicates logic in two runtimes with a parity test to hold them together.

**New rule:** a manifest without a trusted `nextRefreshAfter` is stale. `minIntervalSeconds` stays in the policy payload (contract field, and `worker_refresh_jobs._terminal_cooldown_seconds` reads it) but is the constant `3600`.

- [ ] **Step 1: Rewrite `tests/test_trading_calendar_parity.py`**

Delete `test_both_implementations_agree`, `test_a_weekend_deadline_waits_for_monday_afternoon`, `test_a_weekday_deadline_is_left_alone`, `test_a_holiday_run_waits_for_the_next_open_day`, `test_the_publish_hour_is_after_the_close`, `test_the_walk_forward_crosses_a_year_boundary`, and the second assertion of `test_a_missing_or_broken_closed_date_list_degrades_to_weekend_only` (keep `assert worker_calendar.parse_closed_dates(values) == set()`). Keep `test_weekends_are_never_trading_days`, `test_closed_dates_are_read_from_iso_strings`, `test_refresh_reason_matches_the_backend_filing_windows_every_day`, `test_manifest_next_refresh_is_trusted_only_when_plausible`. Update the module docstring to say the file pins `is_trading_day` and `refresh_reason` only.

Run: `python -m pytest tests/test_trading_calendar_parity.py -q` → still passes (nothing deleted yet).

- [ ] **Step 2: Write the failing Worker test**

Append to `tests/test_cloudflare_worker.py` (helpers `build_router_api`, `pin_worker_time`, `RouteRequest`, `_healthy_worker_manifest` already exist in the file):

```python
def test_manifest_without_next_refresh_after_is_stale_immediately(monkeypatch):
    # Pre-#163 manifests carried no publication slot; the interval fallback that served
    # them is gone, so such a manifest is stale and the next tick rebuilds it.
    manifest = _healthy_worker_manifest("2026-09-15T10:00:00+00:00")
    manifest.pop("nextRefreshAfter", None)
    worker, api, _db = build_router_api(monkeypatch, r2={"public/manifest.json": manifest})
    pin_worker_time(monkeypatch, worker, "2026-09-15T10:00:01+00:00")

    response = asyncio.run(api.fetch(RouteRequest(path="/api/health")))
    payload = json.loads(response.body)

    assert payload["cacheStatus"]["isStale"] is True
    assert payload["cacheStatus"]["nextRefreshAfter"] == "2026-09-15T10:00:00+00:00"
    assert payload["cacheStatus"]["refreshPolicy"]["minIntervalSeconds"] == 3600
```

(If `refreshPolicy` is not the key under which `cache_status_from_manifest` nests the policy, read the function and use the actual key; do not weaken the assertion.)

Run: `python -m pytest tests/test_cloudflare_worker.py -k without_next_refresh_after -q` → expected FAIL (currently `isStale` is False for 12 hours).

- [ ] **Step 3: Implement**

`cloudflare/worker.py` `cache_policy`:

```python
    def cache_policy(self):
        reason = trading_calendar.refresh_reason(datetime.now(TAIPEI_TZ).date())
        # Freshness follows the manifest's publication slot (nextRefreshAfter); the interval
        # is only the terminal-job cooldown ceiling read by worker_refresh_jobs.
        return {
            "strategy": "stale_while_revalidate",
            "reason": reason,
            "minIntervalSeconds": worker_refresh_jobs.TERMINAL_JOB_COOLDOWN_SECONDS,
        }
```

`cache_status_from_manifest`:

```python
        if generated_time:
            # The seed builder writes the next publication slot; a manifest without a
            # trusted one (pre-#163 build, or garbage) is stale and gets rebuilt.
            next_refresh_time = trading_calendar.manifest_next_refresh(manifest, generated_time) or generated_time.astimezone(UTC)
            next_refresh = next_refresh_time.isoformat()
            is_stale = datetime.now(UTC) >= next_refresh_time
```

Delete the three functions/constants from `cloudflare/worker_trading_calendar.py` and the two from `backend/services/cache_policy.py`. Confirm with `git grep -n "next_publication_time\|next_refresh_deadline\|TRADING_DAY_PUBLISH_HOUR"` → only docs/superpowers plans may remain.

- [ ] **Step 4: Repair fixtures, never the rule**

Run: `python -m pytest tests/test_cloudflare_worker.py tests/test_cloudflare_refresh_control.py tests/test_cache_policy.py tests/test_scan_cache.py tests/test_api_worker_contracts.py -q`

Every failure will be a fixture manifest that lacks `nextRefreshAfter` and expected the old interval. Fix each by giving the fixture the slot it was implicitly testing (e.g. `test_worker_health_is_fresh_before_dynamic_cache_boundary` at `:513`: add `"nextRefreshAfter": "2026-07-08T00:52:48+00:00"` and update the comment; `test_refresh_job_ahead_window_enqueues_before_policy_staleness` at `:3444`: add `"nextRefreshAfter": "2026-07-08T13:00:00+00:00"`; `test_failed_rebuild_is_retried_after_an_hour_not_the_policy_interval` at `:3461`: add `"nextRefreshAfter": "2026-07-20T22:00:00+00:00"`; `test_worker_health_stays_fresh_while_the_market_is_shut`: give the Friday build a Monday slot). If `_healthy_worker_manifest` is used by many tests, add a `next_refresh_after: str | None = None` parameter that defaults to `generated_at + 24h` so unrelated tests stay fresh.

- [ ] **Step 5: Full gates**

```
python -m pytest -q
python -m ruff check backend cloudflare scripts tests
python -m mypy backend scripts cloudflare tests
python scripts/check_code_size_budgets.py
python scripts/check_operational_readiness.py
```

- [ ] **Step 6: Commit**

```bash
git add -A backend/services/cache_policy.py cloudflare/worker.py cloudflare/worker_trading_calendar.py docs/cloudflare_deployment.md tests/
git commit -m "refactor: drop the legacy-manifest freshness fallback from the Worker and seed builder"
```

**PR D acceptance criteria:**
1. `git grep -n "next_publication_time\|next_refresh_deadline\|TRADING_DAY_PUBLISH_HOUR" -- ':!docs/superpowers'` prints nothing.
2. `python -m pytest -q` passes; `python -m ruff check backend cloudflare scripts tests` and `python -m mypy backend scripts cloudflare tests` are clean.
3. A test exists whose name contains `without_next_refresh_after` and it asserts `isStale is True`.
4. `cloudflare/worker.py` `cache_policy()` no longer contains the literals `7200`, `10800` or `43200`.
5. `python scripts/check_operational_readiness.py` and `python scripts/check_code_size_budgets.py` exit 0.
6. After merge and deploy: `/api/health` on production reports `cacheStatus.isStale == false` and `cacheStatus.nextRefreshAfter` equal to the manifest's value.

---

## PR E — Task 1: parallel, change-aware R2 publication

**Files:**
- Modify: `scripts/r2_publication_backup.py`
- Modify: `.github/workflows/cloudflare-r2-seed-refresh.yml` publish step (~line 247-250): add `--backup-dir .tmp/r2-backup --manifest .tmp/r2-backup/manifest.json`
- Tests: `tests/test_r2_publication_backup.py`, `tests/test_r2_refresh_workflow_scripts.py` (workflow-text assertions around lines 160 and 234)
- Docs: `docs/cloudflare_deployment.md` "Server-Side R2 Seed Rebuild" section (one paragraph)

**Measured baseline (run 34838671427, 2026-09-14):** backup 4m51s (148 serial `wrangler r2 object get`), publish 6m08s (171 serial `wrangler r2 object put`), rebuild itself 3m07s. Each wrangler invocation is a Node process start; the objects are small (60 MB total).

**Design:**
- Keep wrangler as the transport (proven, credentials already wired). Run the per-object commands through `concurrent.futures.ThreadPoolExecutor(max_workers=8)`.
- `backup` records `sha256` of each downloaded object in the backup manifest (`schema_version` stays `1`; the field is additive and optional).
- `publish` accepts the backup manifest; an object whose local file's sha256 equals the recorded remote sha256 is skipped. Objects without a record (absent, or immutable `public/market_scan/v2/...` which are never backed up) are always uploaded.
- Ordering invariant: `public/manifest.json` is uploaded **last and alone**, after every other upload has succeeded, because the Worker's `cacheKey` and the market pointer are derived from it. Everything else may complete in any order. The existing test `test_publish_preserves_plan_order_and_retries` becomes "uploads every non-manifest object, then the manifest last".
- `restore` also runs through the pool; semantics unchanged (present → put backup file, absent → delete).
- Retry/backoff per object unchanged (`MAX_ATTEMPTS = 5`, 5s doubling). A failure in any worker aborts the step after the pool drains, with `PublicationError` naming the first failed key. `sleeper` stays injectable.

- [ ] **Step 1: Tests first** — rewrite `tests/test_r2_publication_backup.py` so that:

```python
def test_backup_records_sha256_of_present_objects(tmp_path):
    ...  # runner writes b"hello" to the --file target; assert entry["sha256"] == hashlib.sha256(b"hello").hexdigest()


def test_publish_skips_objects_whose_digest_matches_the_backup_and_puts_manifest_last(tmp_path):
    # plan: public/a.json (unchanged), public/b.json (changed), public/market_scan/v2/g/index.json (immutable, no record), public/manifest.json
    # backup manifest: a.json sha256 == local, b.json sha256 != local
    # assert: put commands == {b.json, market_scan..., manifest.json}; manifest.json is the last command issued; a.json never put


def test_publish_runs_uploads_concurrently_but_fails_closed(tmp_path):
    # runner for key "public/x.json" always returns returncode 1; others succeed
    # assert PublicationError mentions public/x.json and manifest.json was never put


def test_restore_uses_the_pool_and_preserves_semantics(tmp_path):
    # same assertions as today's restore test, order-insensitive
```

Write the full test bodies (use the existing `_result` helper and closures that append the `command` list; concurrency means assert on sets/counts plus the manifest-last invariant via a `threading.Lock`-protected list). Run them: expected failures on the new behaviours.

- [ ] **Step 2: Implement in `scripts/r2_publication_backup.py`**

Add:

```python
import hashlib
import threading
from concurrent.futures import ThreadPoolExecutor

MAX_WORKERS = 8
MANIFEST_KEY = "public/manifest.json"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _run_all(tasks, *, max_workers: int = MAX_WORKERS):
    """Run callables in a pool; re-raise the first PublicationError after all finish."""
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = [pool.submit(task) for task in tasks]
    errors = [f.exception() for f in futures if f.exception() is not None]
    if errors:
        raise errors[0]
    return [f.result() for f in futures]
```

`BackupEntry` gains `sha256: str | None = None`. `backup_publication` builds one task per mutable item (same command as today), each returning a `BackupEntry` with the digest of the downloaded file; entries are appended in plan order after the pool finishes. `_load_backup_entries` accepts an optional string `sha256` (64 lowercase hex) on present entries and rejects it on absent ones.

`publish_plan(plan_path, bucket, *, backup_manifest: Path | None = None, backup_dir: Path | None = None, runner, sleeper)`:

```python
    digests = {}
    if backup_manifest is not None and backup_dir is not None and backup_manifest.is_file():
        digests = {e.object_key: e.sha256 for e in _load_backup_entries(backup_dir, backup_manifest, bucket) if e.sha256}
    items = load_plan(plan_path)
    manifest_items = [i for i in items if i.object_key == MANIFEST_KEY]
    others = [i for i in items if i.object_key != MANIFEST_KEY]
    for item in items:
        if not item.file_path.is_file():
            raise PublicationError(f"R2 publication source is missing for {item.object_key}")
    to_upload = [i for i in others if digests.get(i.object_key) != _sha256(i.file_path)]
    skipped = len(others) - len(to_upload)
    _run_all([functools.partial(_put, bucket, i, runner, sleeper) for i in to_upload])
    for item in manifest_items:  # last, alone
        _put(bucket, item, runner, sleeper)
    print(f"R2 publish: uploaded {len(to_upload) + len(manifest_items)} object(s), skipped {skipped} unchanged")
```

(`_put` wraps today's `_run_with_retry([... "put" ...])`.) `restore_publication` maps `_run_all` over entries and collects failures as today. `parse_args`: `publish` gains optional `--backup-dir` and `--manifest`.

- [ ] **Step 3: Workflow and workflow-text tests**

Publish step run line becomes:

```
python scripts/r2_publication_backup.py publish --plan-json .tmp/r2-upload-plan.json --bucket "$CF_R2_BUCKET" --backup-dir .tmp/r2-backup --manifest .tmp/r2-backup/manifest.json
```

Update `tests/test_r2_refresh_workflow_scripts.py` assertions that pin the publish command text.

- [ ] **Step 4: Gates**

```
python -m pytest tests/test_r2_publication_backup.py tests/test_r2_refresh_workflow_scripts.py -q
python -m pytest -q
python -m ruff check scripts tests && python -m mypy backend scripts cloudflare tests
```

- [ ] **Step 5: Docs + commit**

In `docs/cloudflare_deployment.md`, in the R2 rebuild section, add: `Backup and publish run eight wrangler calls at a time; objects whose SHA-256 matches the backed-up copy are not re-uploaded, and public/manifest.json is always written last so the Worker never sees a new manifest before its shards.`

```bash
git add scripts/r2_publication_backup.py .github/workflows/cloudflare-r2-seed-refresh.yml tests/test_r2_publication_backup.py tests/test_r2_refresh_workflow_scripts.py docs/cloudflare_deployment.md
git commit -m "perf: publish R2 seed objects in parallel and skip unchanged ones"
```

- [ ] **Step 6: Live measurement after merge**

`gh workflow run cloudflare-r2-seed-refresh.yml --ref main -f force_refresh=true` (check the input name in the workflow's `workflow_dispatch.inputs` first), watch to success, then compare step durations against the baseline with `gh run view <id> --json jobs`. Record backup/publish durations and the `skipped N unchanged` line in the final report. If the run fails, the `Restore previous R2 publication after failure` step must show success; confirm production `/api/health` is still `ok` before doing anything else.

**PR E acceptance criteria:**
1. `python -m pytest tests/test_r2_publication_backup.py tests/test_r2_refresh_workflow_scripts.py -q` passes and includes tests named `*skips_objects_whose_digest_matches*` and `*puts_manifest_last*` (may be one test).
2. In `scripts/r2_publication_backup.py`, `publish_plan` never issues the `public/manifest.json` put before all other puts have returned (verify by reading the code: the manifest put must come after the pool context exits).
3. The workflow's publish step passes `--backup-dir` and `--manifest`.
4. `python -m pytest -q`, ruff and mypy are clean.
5. After merge: one forced R2 refresh run on `main` succeeds; its `Publish rebuilt seed cache to R2` step log contains `R2 publish: uploaded`; backup + publish together take under 4 minutes (baseline 11 minutes).

---

## PR F — Task 6: retire the v1 market-scan path

**Decision recorded:** v1 was the documented emergency rollback (`production-v1-rollback` profile, client-side `fallbackMarketApiToV1`). The user chose to remove it on 2026-09-15. After this PR the only rollback is `wrangler rollback` to the previous Worker version (already step 15 of the deployment runbook).

**What stays:** `public/market_scan_summary.json` is still built and uploaded because `worker.py:893` `market_report` (the `/api/reports/market` export) reads it. `scripts/validate_cloudflare_seed_inputs.py` `_legacy_market_identities` compares v2 pages against `market_scan_latest.json` — that is a source-of-truth check, not a v1 dependency: keep it, rename it `_market_scan_identities`, and reword the error to `market v2 pages do not match market_scan_latest.json`.

**Files (all must be touched; the implementer greps `scan/market"` and `marketApiVersion|MARKET_API_VERSION|MARKET_SCAN_API_VERSION|market_legacy|v1` before finishing to prove nothing is left):**

Worker
- Delete: `cloudflare/worker_market_legacy.py`
- Modify: `cloudflare/worker.py:22,31` (imports), `:283-302` (both v1 branches → removed; unknown `/api/scan/market` GET/POST falls through to the existing 404), `:375-376` (`r2_text` method removed), `:405-407` (`market_api_version` → returns `"v2"` unconditionally; `runtime_config` unchanged in shape)
- Modify: `cloudflare/wrangler.toml:28` and `cloudflare/wrangler.staging.template.toml:28` (delete `MARKET_SCAN_API_VERSION`)
- Modify: `scripts/render_wrangler_release_config.py` (drop `("vars", "MARKET_SCAN_API_VERSION")` from `ALLOWED_PRODUCTION_SWITCHES`, drop the `production-v1-rollback` profile and `--confirm-production-v1-rollback`, drop line 197)
- Modify: `scripts/check_deployment_preflight.py:99-103` (delete the v2 check and its comment)
- Modify: `scripts/check_code_size_budgets.py:43` (delete the entry)

FastAPI (parity rule in AGENTS.md)
- Modify: `backend/routers/market.py:165-198` (delete `POST /api/scan/market` and `GET /api/scan/market`; keep `/api/scan/market/index`, `/results`, and whatever refresh route exists). Delete helpers that become unused (`ScanMarketRequest` only if nothing else uses it; `_check_scan_rate_limit` if the refresh route does not use it — check first).
- Add: `GET /api/runtime-config` to FastAPI returning `{"schemaVersion": 1, "marketScanApiVersion": "v2", "edgeCacheEnabled": False}` so the browser gets the same contract locally as from the Worker. Add a contract test in `tests/test_api_worker_contracts.py` asserting FastAPI and Worker return identical keys and `marketScanApiVersion == "v2"`.

Frontend
- Modify: `frontend/index.html:8` meta content → `v2`
- Modify: `frontend/market_query.js:253-256` (`marketApiVersion()` returns `"v2"`; lines 467/486 simplify accordingly)
- Modify: `frontend/app.js`: delete `marketApiVersionFallbackApplied`, `fallbackMarketApiToV1`, the v1 branch in `refreshMarketQuery`'s catch (`:1441`), the whole v1 body of `refreshMarketScan` (`:1461-1505`; the function becomes `return refreshMarketQuery({ revealResults, refreshMode })`), `marketScanRefreshPromise`, and `handleMarketScanFailure` if now unused. `acceptRuntimeConfig` keeps writing `globalThis.StockScannerConfig.marketApiVersion = "v2"`. Check `MARKET_API_VERSION !== "v2" && state.schedulerAutoScan?.scan` (asserted by `tests/test_frontend_parser.py:2217`) — the condition collapses to the `state.schedulerAutoScan?.scan` branch only if that code path still makes sense in v2; read it before deciding and update the parser test to the new literal.
- Modify: `tests/test_frontend_parser.py:2210-2235` (`test_app_v2_integration_keeps_legacy_state_separate_and_exports_bounded`, `test_app_loads_runtime_config_before_initial_data_and_has_v1_fallback` → rename to `..._and_is_v2_only`, assert `fallbackMarketApiToV1` is absent and `"/api/scan/market"` followed by a quote does not appear as a request path)
- Modify: `tests/e2e/smoke.spec.ts:657-671` (delete `legacy market fallback uses v1 route only`); keep every `legacyCalls === 0` assertion.

Scripts and smoke
- Modify: `scripts/check_market_scan_v2_canary.py` (remove `legacy_identity_sets`, the legacy fetch, the parity `_require`, and `--legacy-path`; description → "Validate deployed market scan v2 pages and cache boundaries"); `tests/test_market_scan_v2_canary.py` accordingly.
- Modify: `scripts/run_remote_smoke.py:162` (`/api/scan/market` → `/api/scan/market/index`, and adapt whatever it asserts on the payload: the index has `generationId` and per-category counts, not an `entry` list); `tests/test_remote_smoke.py:17-24` fixture accordingly. **This is the post-deploy verification; if it still requests the removed route the deploy rolls back.**

Tests
- `tests/test_cloudflare_worker.py`: delete v1-only tests (`:2946` legacy POST headers, `:3370` legacy module unit tests, `:1650` "defaults invalid values to v1" → becomes "always v2"), and any test whose only purpose is the v1 route; keep `test_worker_market_v2_index_uses_pointer_and_manifest_without_legacy_summary`.
- `tests/test_api_worker_contracts.py:295-310` and `:500-510`: delete the two v1 contract tests; add the runtime-config contract test.
- `tests/test_api.py`: the six `"/api/scan/market"` references → delete or migrate to `/index`.
- `tests/test_render_wrangler_release_config.py`, `tests/test_deployment_preflight.py`: drop v1-rollback and `MARKET_SCAN_API_VERSION` assertions.
- Add a Worker test: `GET /api/scan/market` and `POST /api/scan/market` return 404.

Docs
- `docs/current_architecture.md:10` → v2 only; mention that `market_scan_summary.json` remains for report exports.
- `docs/cloudflare_deployment.md:42,50,208-243,260` → remove the v1/v2 cut-over and rollback runbook; state that the rollback path is `wrangler rollback` (step 15) and that `production-v1-rollback` no longer exists.

- [ ] **Step 1: Red** — write the new tests first (404 on v1 routes, runtime-config contract, remote-smoke fixture using `/index`, parser test asserting no v1 fallback). Run the targeted files; expected failures.
- [ ] **Step 2: Green** — apply the removals above.
- [ ] **Step 3: Gates**

```
python -m pytest -q
python -m ruff check backend cloudflare scripts tests
python -m mypy backend scripts cloudflare tests
npm run lint
python scripts/check_frontend_hygiene.py
npm run test:e2e
python scripts/check_code_size_budgets.py
python scripts/check_operational_readiness.py
python scripts/check_deployment_preflight.py
python scripts/render_wrangler_release_config.py production-v2 --enable-production-cron --enable-production-v2 --output .tmp/wrangler.production-v2.generated.toml
npx wrangler deploy --config .tmp/wrangler.production-v2.generated.toml --dry-run --outdir .tmp/worker-dry-run
python scripts/run_wrangler_dev_smoke.py
git grep -n "worker_market_legacy\|fallbackMarketApiToV1\|MARKET_SCAN_API_VERSION\|production-v1-rollback\|legacy_identity_sets" -- ':!docs/superpowers' ':!docs/archive'
```
The last command must print nothing.

- [ ] **Step 4: Commit**

```bash
git add -A
git commit -m "refactor: retire the v1 market-scan path and its rollback profile"
```

- [ ] **Step 5: After merge** — wait for `Deploy to Cloudflare` on `main`; then:

```
curl -s -o /dev/null -w "%{http_code}\n" "$WORKER_BASE/api/scan/market"        # expect 404
curl -s "$WORKER_BASE/api/runtime-config"                                       # expect marketScanApiVersion v2
curl -s -o /dev/null -w "%{http_code}\n" "$WORKER_BASE/api/scan/market/index"  # expect 200
```
and load `https://stock-scanner-beta.pages.dev` once in a browser session (gstack `/browse`) to confirm the market tab renders rows.

**PR F acceptance criteria:**
1. The grep in Step 3 prints nothing.
2. `python -m pytest -q`, ruff, mypy, `npm run lint`, `python scripts/check_frontend_hygiene.py`, `npm run test:e2e` all pass (run them; report the e2e pass/fail counts).
3. A Worker test asserts `GET /api/scan/market` → 404 and `POST /api/scan/market` → 404.
4. FastAPI `GET /api/runtime-config` returns `marketScanApiVersion == "v2"` (run `python -c` with `fastapi.testclient` against `backend.main:app`).
5. `scripts/run_remote_smoke.py` contains `/api/scan/market/index` and does not contain the string `"/api/scan/market",`.
6. `cloudflare/worker.py` `_route_scan` has no branch for `path == "/api/scan/market"`.
7. `frontend/app.js` has no request to `"/api/scan/market"` (exact string followed by `"`), and `frontend/index.html` meta content is `v2`.
8. `python scripts/check_deployment_preflight.py` exits 0 and the Worker dry-run succeeds.
9. After deploy: production `/api/scan/market` → 404, `/api/scan/market/index` → 200, `/api/health` `status == ok`.

---

## Self-review

- Spec coverage: Task 1 → PR E; Task 2 → PR D; Task 3, 4 → PR A; Task 5 → PR B; Task 6 → PR F; Task 7 → PR C. Live verifications are in PR C step 5, PR E step 6, PR F step 5.
- Placeholders: PR E step 1 gives test intents rather than full bodies because the concurrency assertions depend on the runner closure the implementer writes; every other code step is complete. PR F lists exact files and line ranges; the implementer must grep before finishing (criterion 1).
- Type consistency: `default_failed_companies_csv(data_dir: Path) -> Path` is used identically in test and implementation; `BackupEntry.sha256` is optional in both loader and writer; `MANIFEST_KEY == "public/manifest.json"` matches the plan builder (`scripts/cloudflare_seed_upload_plan.py:81`).
