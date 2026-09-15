# Report Export Streaming and Local Refresh Cooldown Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove the last code path that JSON-decodes the multi-megabyte market summary inside the Python Worker (the failure mode that took production down on 2026-09-08), and give the local FastAPI refresh command the same cooldown protection the Worker has.

**Architecture:** The market report (`POST /api/reports/market`, CSV or Markdown) is fully determined by the seed build, so the seed builder renders both files once and uploads them as static R2 objects; the Worker streams the text through without parsing it. Once the Worker no longer reads `market_scan_summary.json`, that 3 MB object is dropped from the seed, the zip, the upload plan and the manifest. Shipped as two PRs so production never has a Worker that expects an object the seed has not published yet. The cooldown is a third, independent PR.

**Tech Stack:** Python 3.12/3.13 (pytest, ruff, mypy), Cloudflare Python Worker (Pyodide), GitHub Actions, wrangler 4.

**Why now (evidence):** commit `e5cae0a` (2026-09-08) records that decoding and re-encoding the market summary inside the Worker made the isolate answer 503 "Worker exceeded resource limits" for every route, including `/api/health`, after a single request. The v1 read was moved to streaming; `cloudflare/worker.py:867-870` `market_report` still does `self.r2_json("public/market_scan_summary.json", ...)` on the same 2.96 MB object (measured in the committed seed on 2026-09-15) and re-encodes it as CSV/Markdown. Every v2 read is capped at 500 KB per object (`cloudflare/worker_market_query.py:13-14`); this path has no cap. Since PR #175 every deploy's smoke also POSTs this endpoint, so the risk is exercised on every release.

**Execution order and PR grouping** (each PR branches from the freshly merged `main`; same delivery protocol as `2026-09-15-post-incident-optimizations.md`: TDD, gates, fresh-context verifier, CI green, squash merge, post-merge check):

| PR | Task | Branch | Risk |
|---|---|---|---|
| K1 | 1: shared report renderer + static report files in the seed and the upload plan (Worker untouched) | `feat/static-market-reports-in-seed` | low |
| K2 | 2: Worker streams the static reports; `market_scan_summary.json` removed from seed, zip, plan, manifest, validator | `refactor/worker-stream-market-report` | medium (Worker) |
| L | 3: local FastAPI refresh cooldown (429 + `Retry-After`) in real-data mode | `feat/fastapi-refresh-cooldown` | low |

K1 must be merged **and one R2 refresh must have published the new objects** (`gh workflow run cloudflare-r2-seed-refresh.yml --ref main -f force=true`, then confirm `public/reports/market_scan.csv` exists) before K2 is merged. L is independent and may run in parallel.

---

## PR K1 — Task 1: render the market report once, at seed build time

**Files:**
- Create: `backend/services/report_render.py` (pure renderers, no FastAPI or Worker imports)
- Not modified: `backend/dependencies.py:306` `_report_response` (the local FastAPI report keeps its own `group,...,rules` layout; see Execution notes)
- Modify: `scripts/build_cloudflare_seed.py` (write `reports/market_scan.csv` and `reports/market_scan.md` in both the online build near line 746 and the offline path near line 620; add `"reports/market_scan.csv"`, `"reports/market_scan.md"` to the manifest `files` list at line ~768)
- Modify: `scripts/cloudflare_seed_upload_plan.py:17-24` `PUBLIC_FILES` (+ the two report names; the loop already uses `seed_dir / name` and `public/{name}`, so the subdirectory works unchanged)
- Modify: `scripts/package_cloudflare_seed_cache.py:28-36` `SEED_FILES` (+ the two names; confirm the packer writes non-JSON members verbatim)
- Modify: `scripts/validate_cloudflare_seed_inputs.py:183-186` `_is_allowed_seed_member` (+ `cloudflare_seed/reports/market_scan.csv`, `cloudflare_seed/reports/market_scan.md`) and add a check that both members exist and that the CSV header is `category,stockCode,companyName,status,summary`
- Tests: `tests/test_report_render.py` (new), `tests/test_cloudflare_seed_build.py`, `tests/test_r2_refresh_workflow_scripts.py:84-157`, `tests/test_package_cloudflare_seed_cache.py`, `tests/test_cloudflare_seed_inputs.py`, `tests/test_api.py:697` (still passes), `tests/test_cloudflare_worker.py:988` parity test (new assertion, see Step 3)

- [ ] **Step 1: Write the failing renderer tests**

`tests/test_report_render.py`:

```python
from backend.services import report_render


def _payload():
    return {
        "generatedAt": "2026-09-15T10:00:00+00:00",
        "dataSource": "OfficialDataProvider",
        "entry": [{"stockCode": "2330", "companyName": "台積電", "status": "ENTRY", "summary": "E4 PER 低"}],
        "watch": [{"stockCode": "2317", "companyName": "鴻海", "status": "WATCH", "summary": "a, b"}],
        "excluded": [],
    }


def test_csv_has_the_fixed_header_and_one_row_per_item_with_quoting():
    text = report_render.render_market_report_csv(_payload())
    lines = text.splitlines()
    assert lines[0] == "category,stockCode,companyName,status,summary"
    assert lines[1] == "entry,2330,台積電,ENTRY,E4 PER 低"
    assert lines[2] == 'watch,2317,鴻海,WATCH,"a, b"'
    assert len(lines) == 3


def test_markdown_lists_only_non_empty_categories_with_counts():
    text = report_render.render_market_report_markdown(_payload(), "台股市場掃描報告")
    assert text.startswith("# 台股市場掃描報告\n\n- 產生時間：2026-09-15T10:00:00+00:00\n- 資料來源：OfficialDataProvider\n")
    assert "## 適合進場 (1)" in text and "## 接近觀察 (1)" in text and "排除清單" not in text
    assert "- 2330 台積電：E4 PER 低" in text


def test_markdown_defaults_data_source_when_missing():
    payload = _payload(); payload.pop("dataSource")
    assert "- 資料來源：cloudflare_r2_seed" in report_render.render_market_report_markdown(payload, "t")
```

Run: `python -m pytest tests/test_report_render.py -q` → FAIL (`ModuleNotFoundError`).

- [ ] **Step 2: Implement the renderers**

`backend/services/report_render.py` (this is the existing logic from `cloudflare/worker_support.py:510-536` lifted out, byte-for-byte compatible so the Worker's holdings report and the static market report stay identical):

```python
"""Pure market/holdings report renderers shared by FastAPI and the seed builder.

The Cloudflare Worker cannot import this module; ``cloudflare/worker_support.report_response``
keeps an equivalent copy for the holdings report and ``tests/test_cloudflare_worker.py``
pins the two outputs together.
"""

from __future__ import annotations

import csv
import io
from datetime import UTC, datetime

CSV_HEADER = ["category", "stockCode", "companyName", "status", "summary"]
CATEGORIES = ("entry", "watch", "excluded", "results")
LABELS = (("entry", "適合進場"), ("watch", "接近觀察"), ("excluded", "排除清單"), ("results", "持股"))


def render_market_report_csv(payload: dict) -> str:
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(CSV_HEADER)
    for category in CATEGORIES:
        for item in payload.get(category, []) or []:
            writer.writerow([category, item.get("stockCode"), item.get("companyName"), item.get("status"), item.get("summary")])
    return output.getvalue()


def render_market_report_markdown(payload: dict, title: str) -> str:
    generated = payload.get("generatedAt") or datetime.now(UTC).isoformat()
    lines = [f"# {title}", "", f"- 產生時間：{generated}", f"- 資料來源：{payload.get('dataSource', 'cloudflare_r2_seed')}", ""]
    for category, label in LABELS:
        items = payload.get(category, []) or []
        if not items:
            continue
        lines.append(f"## {label} ({len(items)})")
        for item in items:
            lines.append(f"- {item.get('stockCode')} {item.get('companyName')}：{item.get('summary')}")
        lines.append("")
    return "\n".join(lines)
```

`backend/dependencies.py:306` `_report_response` stays as it is (superseded: the local FastAPI report uses a different layout than the production export; see Execution notes).

- [ ] **Step 3: Pin the Worker copy to the shared renderer**

Append to `tests/test_cloudflare_worker.py` next to `test_worker_report_response_renders_markdown_and_csv` (line 988; reuse whatever fixture/monkeypatch that test uses to import `worker_support` under CPython):

```python
def test_worker_report_response_matches_the_shared_renderer(monkeypatch):
    from backend.services import report_render
    payload = {"generatedAt": "2026-09-15T10:00:00+00:00", "entry": [{"stockCode": "2330", "companyName": "台積電", "status": "ENTRY", "summary": "x"}], "results": [{"stockCode": "2317", "companyName": "鴻海", "status": "HOLD", "summary": "y"}]}
    csv_body = <body text of worker_support.report_response(payload, "csv", "t", "p")>
    md_body = <body text of worker_support.report_response(payload, "markdown", "t", "p")>
    assert csv_body == report_render.render_market_report_csv(payload)
    assert md_body == report_render.render_market_report_markdown(payload, "t")
```
(Extract the body the same way the neighbouring test does; the placeholder is only for the body-extraction call.)

- [ ] **Step 4: Seed builder writes the static reports**

In `scripts/build_cloudflare_seed.py` add near `write_market_scan_summary`:

```python
REPORT_DIR_NAME = "reports"
MARKET_REPORT_CSV = f"{REPORT_DIR_NAME}/market_scan.csv"
MARKET_REPORT_MD = f"{REPORT_DIR_NAME}/market_scan.md"
MARKET_REPORT_TITLE = "台股市場掃描報告"


def write_market_reports(scan_payload: dict) -> None:
    """Static market report exports; the Worker streams these instead of decoding the scan."""
    compact = compact_market_scan_payload(scan_payload)
    _atomic_write_bytes(OUT_DIR / MARKET_REPORT_CSV, render_market_report_csv(compact).encode("utf-8"))
    _atomic_write_bytes(OUT_DIR / MARKET_REPORT_MD, render_market_report_markdown(compact, MARKET_REPORT_TITLE).encode("utf-8"))
```
Import `render_market_report_csv, render_market_report_markdown` from `backend.services.report_render` next to the existing `backend.services.cache_policy` import (line 43). Call `write_market_reports(scan_payload)` right after `write_market_scan_summary(scan_payload)` at line 746 and at line 620 (offline path). Add both names to the manifest `files` list after `MARKET_SCAN_SUMMARY_FILE` (line ~770).

Test in `tests/test_cloudflare_seed_build.py`: after a build (use the existing fixture that produces `OUT_DIR`), `reports/market_scan.csv` exists, its first line is the fixed header, its row count equals the sum of entry/watch/excluded items of `market_scan_latest.json`, and `manifest["files"]` contains both report names.

- [ ] **Step 5: Upload plan, packager, validator**

- `scripts/cloudflare_seed_upload_plan.py` `PUBLIC_FILES`: append `"reports/market_scan.csv", "reports/market_scan.md"`. Update `tests/test_r2_refresh_workflow_scripts.py:84-157`: the fixture creates the two files; assert `"public/reports/market_scan.csv"` and `"public/reports/market_scan.md"` are in `keys`.
- `scripts/package_cloudflare_seed_cache.py` `SEED_FILES`: append both; if the packer JSON-validates members, exempt the two text members explicitly. Test in `tests/test_package_cloudflare_seed_cache.py`: the zip contains `cloudflare_seed/reports/market_scan.csv`.
- `scripts/validate_cloudflare_seed_inputs.py`: allow both members; require both; check the CSV header. Test in `tests/test_cloudflare_seed_inputs.py`: a zip without the CSV fails with a clear message; a CSV with a wrong header fails.

- [ ] **Step 6: Gates**

```
python -m pytest tests/test_report_render.py tests/test_cloudflare_seed_build.py tests/test_r2_refresh_workflow_scripts.py tests/test_package_cloudflare_seed_cache.py tests/test_cloudflare_seed_inputs.py tests/test_api.py tests/test_routers_coverage.py tests/test_cloudflare_worker.py -q
python -m pytest -q
python -m ruff check backend cloudflare scripts tests
python -m mypy backend scripts cloudflare tests
python scripts/build_cloudflare_seed.py            # offline mode (default); then:
ls cloudflare/seed/reports/ && head -1 cloudflare/seed/reports/market_scan.csv
python scripts/package_cloudflare_seed_cache.py && python scripts/validate_cloudflare_seed_inputs.py --zip "$(python scripts/seed_utils.py data --print)" --max-age-days 45
python scripts/check_code_size_budgets.py
```
Note: the offline rebuild rewrites the committed seed zip; do not commit the zip (only the weekly workflow does that) — `git checkout -- data/` before committing if it changed.

- [ ] **Step 7: Commit, PR, merge, then publish the objects**

```bash
git commit -m "feat(seed): render the market report exports at build time and upload them as static objects"
```
After merge: `gh workflow run cloudflare-r2-seed-refresh.yml --ref main -f force=true`, wait for success, then confirm with `npx wrangler r2 object get stock-scanner-beta-cache/public/reports/market_scan.csv --remote --file .tmp/market_scan.csv` that the object exists and its header is correct. K2 may start only after this.

**PR K1 acceptance criteria:**
1. `backend/services/report_render.py` exists with `render_market_report_csv` and `render_market_report_markdown`; `tests/test_report_render.py` passes; `backend/dependencies.py` is unchanged.
2. A test proves `cloudflare/worker_support.report_response` produces byte-identical CSV and Markdown bodies to the shared renderer for the same payload.
3. After `python scripts/build_cloudflare_seed.py`, `cloudflare/seed/reports/market_scan.csv` and `.md` exist; the CSV header is `category,stockCode,companyName,status,summary`; `manifest.json` `files` lists both.
4. `build_upload_plan` output contains `public/reports/market_scan.csv` and `.md`; the packaged zip contains `cloudflare_seed/reports/market_scan.csv` and `.md`; `validate_cloudflare_seed_inputs.py` requires them and rejects a wrong header.
5. `python -m pytest -q`, ruff, mypy, code-size budgets: pass. `cloudflare/worker.py` is unchanged.
6. After merge and one forced R2 refresh: both objects exist in the production bucket.

---

## PR K2 — Task 2: the Worker streams the static report; the summary object goes away

**Files:**
- Modify: `cloudflare/worker.py:867-870` `market_report`; add `r2_text` (streaming read, no JSON, no cache)
- Modify: `cloudflare/worker_support.py:510-536` `report_response` stays for the holdings report only (rename nothing; add a docstring line)
- Modify: `scripts/build_cloudflare_seed.py` (delete `MARKET_SCAN_SUMMARY_FILE`, `add_market_scan_summary_to_manifest`, `write_market_scan_summary`, both call sites, the manifest entry; keep `compact_market_scan_payload`, which `write_market_generation` and `write_market_reports` use)
- Modify: `scripts/cloudflare_seed_upload_plan.py:21`, `scripts/package_cloudflare_seed_cache.py:32`, `scripts/validate_cloudflare_seed_inputs.py:185` (remove the summary entry; the validator must now reject a zip that still contains it, so an old zip cannot slip through)
- Modify: `scripts/run_remote_smoke.py` (no change expected; the CSV parity check keeps working against the static file, confirm)
- Modify: `docs/current_architecture.md:10` (the sentence "`public/market_scan_summary.json` is still built, uploaded and read at request time, but only by the `/api/reports/market` export" → the report exports are rendered at build time and streamed; the summary object no longer exists)
- Tests: `tests/test_cloudflare_worker.py:2046` `test_worker_reports_market_and_holdings` (fixture switches from `public/market_scan_summary.json` to the two text objects), `:1662`, `:1712`, `:3017-3027` (fixtures that seeded the summary), `:1986` dependency-failure matrix row for `market_report`, `tests/test_cloudflare_seed_build.py:360-381`, `tests/test_r2_refresh_workflow_scripts.py:95,144`, `tests/test_cloudflare_seed_inputs.py`

- [ ] **Step 1: Failing Worker tests**

In `tests/test_cloudflare_worker.py` (fixtures `build_router_api`, `RouteRequest` exist; R2 fixtures accept string values for text objects — check `api.env.CACHE` fake; if it only stores JSON, extend the fake to return `.text()` for `str` values):

```python
def test_worker_market_report_streams_the_static_exports(monkeypatch):
    csv_text = "category,stockCode,companyName,status,summary\r\nentry,2330,台積電,ENTRY,x\r\n"
    md_text = "# 台股市場掃描報告\n\n- 產生時間：t\n"
    worker, api, _db = build_router_api(monkeypatch, r2={"public/reports/market_scan.csv": csv_text, "public/reports/market_scan.md": md_text})

    csv_response = asyncio.run(api.fetch(RouteRequest(method="POST", path="/api/reports/market?report_format=csv", body="")))
    md_response = asyncio.run(api.fetch(RouteRequest(method="POST", path="/api/reports/market?report_format=markdown", body="")))

    assert csv_response.init["status"] == 200 and csv_response.body == csv_text
    assert csv_response.init["headers"]["content-type"].startswith("text/csv")
    assert csv_response.init["headers"]["content-disposition"].startswith('attachment; filename="market_scan_')
    assert csv_response.init["headers"]["cache-control"] == "no-store"
    assert md_response.body == md_text and md_response.init["headers"]["content-type"].startswith("text/markdown")
    assert "public/market_scan_summary.json" not in api.env.CACHE.calls


def test_worker_market_report_is_503_until_the_seed_publishes_the_exports(monkeypatch):
    worker, api, _db = build_router_api(monkeypatch, r2={"public/manifest.json": _healthy_worker_manifest("2026-09-15T10:00:00+00:00")})
    response = asyncio.run(api.fetch(RouteRequest(method="POST", path="/api/reports/market?report_format=csv", body="")))
    payload = json.loads(response.body)
    assert response.init["status"] == 503
    assert payload["code"] == "report_unavailable" and payload["retryable"] is True
    assert response.init["headers"]["cache-control"] == "no-store"
```

Run: `python -m pytest tests/test_cloudflare_worker.py -k market_report -q` → FAIL.

- [ ] **Step 2: Implement in `cloudflare/worker.py`**

```python
    MARKET_REPORT_OBJECTS = {"csv": ("public/reports/market_scan.csv", "text/csv; charset=utf-8", "csv"),
                             "markdown": ("public/reports/market_scan.md", "text/markdown; charset=utf-8", "md")}

    async def r2_text(self, key: str):
        """Stream an R2 text object without parsing it (the report exports are ~MB of text;
        JSON-decoding objects this size inside Pyodide exhausted the isolate on 2026-09-08)."""
        obj = await observability.dependency_call(lambda: self.env.CACHE.get(key), DependencyFailure, "r2_read", True)
        if obj is None:
            return None
        return await observability.dependency_call(lambda: obj.text(), DependencyFailure, "r2_read", True)

    async def market_report(self, query):
        report_format = (query.get("report_format") or ["markdown"])[0]
        key, media_type, extension = self.MARKET_REPORT_OBJECTS.get(report_format, self.MARKET_REPORT_OBJECTS["markdown"])
        text = await self.r2_text(key)
        if text is None:
            return error_response("市場報表尚未產生，請稍後再試", status=503, code="report_unavailable", retryable=True, request_id=self._request_id)
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        return text_response(text, media_type=media_type,
                             headers={"content-disposition": f'attachment; filename="market_scan_{timestamp}.{extension}"'})
```
Mirror the exact `dependency_call` signature used by `r2_json` (lines ~380-392). The unknown-format fallback to Markdown matches today's behaviour (`report_response` treats anything but `csv` as Markdown).

- [ ] **Step 3: Drop the summary from the seed pipeline**

Delete the three helpers and both call sites in `scripts/build_cloudflare_seed.py`, the manifest entry, `PUBLIC_FILES` entry, `SEED_FILES` entry, and the validator allowance; make the validator fail with `Seed zip still contains cloudflare_seed/market_scan_summary.json; rebuild with the current builder` when present. Delete `tests/test_cloudflare_seed_build.py:360-381` tests that only covered `add_market_scan_summary_to_manifest`; update the fixtures listed above. `git grep -n "market_scan_summary" -- ':!docs/superpowers' ':!docs/archive'` must print nothing.

- [ ] **Step 4: Gates**

```
python -m pytest -q
python -m ruff check backend cloudflare scripts tests
python -m mypy backend scripts cloudflare tests
python scripts/build_cloudflare_seed.py && python scripts/package_cloudflare_seed_cache.py && python scripts/validate_cloudflare_seed_inputs.py --zip "$(python scripts/seed_utils.py data --print)" --max-age-days 45
python scripts/check_code_size_budgets.py
python scripts/check_operational_readiness.py
python scripts/check_deployment_preflight.py
python scripts/render_wrangler_release_config.py production-v2 --enable-production-cron --enable-production-v2 --output cloudflare/wrangler.production-v2.generated.toml && npx wrangler deploy --config cloudflare/wrangler.production-v2.generated.toml --dry-run --outdir .tmp/worker-dry-run
python scripts/run_wrangler_dev_smoke.py
git grep -n "market_scan_summary" -- ':!docs/superpowers' ':!docs/archive'
```

- [ ] **Step 5: Commit, PR, merge, verify**

```bash
git commit -m "refactor(worker): stream the static market report exports and retire market_scan_summary.json"
```
After merge: wait for `Deploy to Cloudflare` (its public smoke POSTs the CSV report and compares counts, so a broken stream fails the deploy and rolls back); then `curl -s -X POST -H 'x-stock-scanner-csrf: 1' "https://stock-scanner-beta-api.pcedison.workers.dev/api/reports/market?report_format=csv" | head -2` shows the header; `/api/health` is ok. Trigger one more forced R2 refresh and confirm `public/market_scan_summary.json` is no longer in the upload plan (the object may linger in R2 until manually deleted; that is harmless — note it in the PR).

**PR K2 acceptance criteria:**
1. `cloudflare/worker.py` `market_report` does not call `r2_json`; it calls `r2_text` on `public/reports/market_scan.csv` or `.md` and returns 503 `report_unavailable` when the object is missing.
2. `git grep -n "market_scan_summary" -- ':!docs/superpowers' ':!docs/archive'` prints nothing.
3. Full `pytest`, ruff, mypy, seed build + package + validate, code-size budgets, readiness, preflight, Worker dry-run and dev smoke all pass.
4. `scripts/run_remote_smoke.py` still passes against production after deploy, with `marketReportRows == marketScanRows`.
5. After deploy: the CSV export downloads in the browser (Pages site, 匯出市場 CSV) and `/api/health` stays `ok` afterwards (this is the exact sequence that broke on 2026-09-08).

---

## PR L — Task 3: local FastAPI refresh cooldown

**Files:**
- Modify: `backend/services/refresh_jobs.py` (`RefreshJobService.start(...)` gains `cooldown_seconds: int = 0`; `_reusable_job` unchanged; new `_recent_success(scope, cooldown_seconds)`; raise `RefreshCooldownError(retry_after_seconds)` defined in the same module)
- Modify: `backend/routers/market.py:358-380` `refresh_market_scan` (real-data mode passes `LOCAL_REFRESH_COOLDOWN_SECONDS = 60`; mock mode passes 0; map `RefreshCooldownError` to `429` with `Retry-After` and the shared no-store headers)
- Tests: `tests/test_api.py` (429 after a successful real-data job, not after a failed one, not in mock mode, idempotent repeat still returns the same job; existing real-data tests set `cooldown_seconds` to 0 through a monkeypatched module constant), `tests/test_api_worker_contracts.py` (429 parity: both runtimes return 429 with a `Retry-After` header and a `detail` key; the Worker side uses a recent terminal row in the fake D1 like `cloudflare/worker_refresh_jobs.py:226-230`)
- Docs: `docs/current_architecture.md:10` one sentence: local refresh is throttled to one successful rebuild per minute in real-data mode.

- [ ] **Step 1: Failing test**

```python
def test_forced_refresh_is_throttled_after_a_recent_success(monkeypatch, tmp_path):
    # real-data settings with stubbed builders, as in test_forced_refresh_job_caches_under_the_key_of_the_data_it_refreshed
    first = client.post("/api/scan/market/refresh", headers={CSRF_HEADER: "1", "idempotency-key": "k1"})
    assert first.status_code == 202
    _wait_for_terminal_refresh_job(first.json()["statusUrl"])
    second = client.post("/api/scan/market/refresh", headers={CSRF_HEADER: "1", "idempotency-key": "k2"})
    assert second.status_code == 429
    assert int(second.headers["retry-after"]) >= 1
    assert second.headers["cache-control"] == "no-store"
    repeat = client.post("/api/scan/market/refresh", headers={CSRF_HEADER: "1", "idempotency-key": "k1"})
    assert repeat.status_code == 202 and repeat.json()["jobId"] == first.json()["jobId"]
```
Plus: after a `failed` job the next POST is 202 (no cooldown on failure, matching the Worker's retry-after-an-hour rule being about *failed* rebuilds handled by the Worker cron, not by the client); in mock mode two consecutive POSTs are both 202.

- [ ] **Step 2: Implement**

```python
class RefreshCooldownError(RuntimeError):
    def __init__(self, retry_after_seconds: int) -> None:
        super().__init__("市場掃描剛完成，請稍後再重新整理")
        self.retry_after_seconds = max(1, int(retry_after_seconds))
```
In `start(...)`: after the reusable-job lookup misses, if `cooldown_seconds > 0` and the newest job for the scope has `status == "success"` and `finishedAt` within `cooldown_seconds`, raise `RefreshCooldownError(remaining)`. In the router: `except RefreshCooldownError as exc: raise _market_query_error(429, str(exc), headers={"Retry-After": str(exc.retry_after_seconds)})` (extend `_market_query_error` to accept extra headers if it does not already).

- [ ] **Step 3: Gates and commit**

```
python -m pytest tests/test_api.py tests/test_api_worker_contracts.py -q
python -m pytest -q && python -m ruff check backend tests && python -m mypy backend scripts cloudflare tests
npm run test:e2e   # mock mode: unaffected, must stay 31 passed
git commit -m "feat(api): throttle the local market refresh command after a recent success"
```

**PR L acceptance criteria:** (1) the four API tests above pass; (2) a contract test shows both runtimes answer 429 with `Retry-After` and `detail`; (3) e2e unchanged; (4) `docs/current_architecture.md` mentions the one-per-minute local throttle.

---

## Self-review

- Coverage: the Worker decode path (K2) is the risk that motivated this plan; K1 exists only so the objects are in R2 before the Worker depends on them. The cooldown (L) is the one "accepted" item the user asked to strengthen.
- Placeholders: K1 Step 3 leaves the body-extraction call to the neighbouring test's convention (the fake `Response` differs between test files); everything else is complete code.
- Type consistency: `render_market_report_csv(payload) -> str` and `render_market_report_markdown(payload, title) -> str` are used with the same signatures in K1 Steps 1, 2, 4 and the K1 parity test; `MARKET_REPORT_OBJECTS` keys match the `report_format` values the frontend sends (`frontend/app.js:1687-1688`: `markdown`, `csv`).
- Rollback: K2 is the only Worker change; if the deploy's smoke fails it rolls back automatically, and the K1 objects are harmless extras.

## Execution notes (K1, 2026-09-15)

- The local FastAPI report (`backend/services/reporting.py`, header `group,...,rules`) already differed from the Worker export (`category,...`). The static objects use the Worker format because production users and the deploy smoke depend on it, so K1 adds `backend/services/report_render.py` for the seed builder only and leaves `backend/dependencies._report_response` untouched. Unifying the two local/production layouts is out of scope.
- CI validates the committed seed zip, which is only regenerated by the weekly workflow. The validator therefore accepts a zip with neither export in K1 (and requires both, with the fixed header, when either is present). K2 makes them mandatory after the next committed-seed refresh.
- Measured on the current seed: `reports/market_scan.csv` 224 KB, `reports/market_scan.md` 206 KB, versus 2.96 MB for the JSON summary the Worker decodes today.
