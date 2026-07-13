# Stock Scanner Structural Reliability Upgrade Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the browser's 2.88 MB all-at-once market scan path with bounded, versioned R2 pages; make refresh enqueue atomic and idempotent; add a safe Cloudflare control-plane trigger; and roll the new path out behind a reversible runtime flag.

**Architecture:** The GitHub batch job remains the only heavy official-data executor. It publishes immutable v2 generation pages first and a small mutable pointer last. Worker and FastAPI query endpoints serve bounded index/page responses, while refresh commands enqueue one D1 job atomically. The browser persists only a validated index shell, lazy-loads active pages, and can immediately fall back to legacy v1.

**Tech Stack:** Python 3.13, FastAPI, Cloudflare Python Workers, R2, D1/SQLite migrations, Wrangler 4, vanilla JavaScript, Playwright, pytest, GitHub Actions.

## Global Constraints

- Keep legacy `GET/POST /api/scan/market` and `POST /api/reports/market` working throughout rollout.
- Do not persist full scan pages or result lists in `localStorage`; persist only the v2 index shell.
- `market_scan_index.json` must be smaller than `50 KiB`; every uncompressed page must be smaller than `500 KiB`.
- Use immutable generation IDs and upload generation pages before indexes; upload `public/market_scan_index.json` last.
- Fix API page size at `100`; UI page size remains `6`, including the cross-boundary case starting at row `96`.
- Frontend overview and tab counts come from the index, never from the number of pages already loaded.
- Keep heavy official-data rebuilds in GitHub Actions; Workers only decide, enqueue, dispatch, and serve.
- Store only hashed idempotency keys. Never log tokens, Authorization headers, raw GitHub errors, or user filesystem paths.
- Production starts with `MARKET_SCAN_API_VERSION=v1`; switch to v2 only after staging parity and canary gates pass.
- All production mutations, resource provisioning, secret writes, deploys, and runtime-flag changes require a separately authorized release step.
- Every behavior change starts with a failing test, ends with focused verification, independent review, and a small commit.
- Do not add logic to budget-saturated files. Create focused modules for v2 query, refresh jobs, refresh control, and frontend paging.
- Workers Caching uses the current official `cache.enabled` Wrangler interface, which requires Wrangler `4.69.0+`; the verified local CLI is `4.103.0`. Keep a schema/dry-run gate so older or incompatible CLIs fail before release.
- Production cron remains empty in committed configuration. A cron-enabled full production config is rendered only during a separately authorized rollout step.

## File and Responsibility Map

- Create `backend/services/market_query.py`: canonical v2 generation builder and in-process query contract.
- Create `cloudflare/worker_market_query.py`: query validation and R2 page assembly for Worker routes.
- Create `cloudflare/worker_refresh_jobs.py`: idempotency keys, atomic enqueue/read-back, safe job payloads.
- Create `cloudflare/worker_refresh_control.py`: scheduled due decision, dispatch state, GitHub request classification.
- Create `frontend/market_query.js`: index validation, page cache, LKG shell, UI-window loading.
- Create `tests/test_market_query.py`: generation, paging, hash, size, and parity tests.
- Create `tests/test_worker_market_query.py`: Worker query parser and page assembly tests.
- Create `tests/test_cloudflare_refresh_control.py`: scheduled and dispatch state-machine tests.
- Create `tests/test_package_cloudflare_seed_cache.py`: package generation inclusion/orphan exclusion tests.
- Create `scripts/check_market_scan_v2_canary.py`: v1/v2 parity, response budget, hash, cache, and privacy checks.
- Modify `scripts/build_cloudflare_seed.py`: emit v2 artifacts and manifest metadata.
- Modify `scripts/package_cloudflare_seed_cache.py`: package only the referenced generation.
- Modify `scripts/validate_cloudflare_seed_inputs.py`: validate v2 schema, hashes, size, cursor continuity, and parity.
- Modify `scripts/cloudflare_seed_upload_plan.py`: enforce pointer-last R2 order.
- Modify `cloudflare/worker.py`: thin route delegation and scheduled entry point only.
- Modify `cloudflare/schema.sql` and add migrations `0003` and `0004`.
- Modify `frontend/app.js`, `frontend/market_scan.js`, `frontend/market_render.js`, `frontend/storage.js`, and `frontend/api_client.js`: integrate v2 without growing new responsibilities.
- Modify `cloudflare/wrangler.toml`: runtime flag, cron, cache boundary, and isolated staging environment.

---

### Task 1: Canonical v2 Generation Builder

**Files:**

- Create: `backend/services/market_query.py`
- Create: `tests/test_market_query.py`
- Modify: `scripts/build_cloudflare_seed.py`
- Modify: `tests/test_cloudflare_seed_build.py`
- Modify: `scripts/check_code_size_budgets.py`
- Test: `tests/test_market_query.py`

**Interfaces:**

- Consumes: compact legacy scan dictionaries produced by `compact_market_scan_payload(scan_payload)`.
- Produces: `MarketGeneration(generation_id: str, index: dict[str, Any], files: dict[str, bytes])`.
- Produces: `build_market_generation(scan, page_size=100, cache_status_inputs=None) -> MarketGeneration`.
- Produces: `query_market_generation(index, pages, disclosure, category, cursor, limit) -> dict[str, Any]` for FastAPI parity.

- [x] **Step 1: Write generation contract tests**

Add tests that prove freshness period beats a future active period, canonical key order yields the same 24-hex generation ID, a changed cache-status envelope yields a different generation ID, `100/101` rows produce `1/2` pages, all page hashes match exact bytes, entry sorting happens before slicing, and announced plus pending identities exactly equal the legacy universe. Query tests must reject boolean cursors/limits, negative totals, unsafe or mismatched object keys, wrong byte counts, reversed/duplicate/gapped/overlapping references, and page metadata that disagrees with the reference.

```python
def test_generation_is_canonical_and_preserves_identity():
    first = build_market_generation(sample_scan(order="forward"))
    second = build_market_generation(sample_scan(order="reverse"))
    assert first.generation_id == second.generation_id
    assert re.fullmatch(r"[0-9a-f]{24}", first.generation_id)
    assert generation_identities(first) == legacy_identities(sample_scan())


def test_generation_uses_freshness_period_and_splits_101_rows():
    scan = sample_scan(watch_rows=101, active_period="2026Q2", freshness_period="2026Q1")
    generation = build_market_generation(scan, page_size=100)
    refs = generation.index["disclosures"]["announced"]["watch"]["pages"]
    assert [ref["cursor"] for ref in refs] == [0, 100]
    assert [ref["count"] for ref in refs] == [100, 1]
```

- [x] **Step 2: Run RED generation tests**

Run:

```powershell
python -m pytest tests\test_market_query.py -q --basetemp (Join-Path $env:TEMP 'pytest-plan-b-task1-red')
```

Expected: FAIL because `backend.services.market_query` and its interfaces do not exist.

- [x] **Step 3: Implement the pure generation module**

Implement these exact public types and constants:

```python
PAGE_SIZE = 100
MAX_INDEX_BYTES = 50 * 1024
MAX_PAGE_BYTES = 500 * 1024
DISCLOSURES = ("announced", "pending")
CATEGORIES = ("entry", "watch", "excluded")


@dataclass(frozen=True)
class MarketGeneration:
    generation_id: str
    index: dict[str, Any]
    files: dict[str, bytes]


def canonical_json_bytes(payload: Any) -> bytes:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
```

Define `build_market_generation(scan: Mapping[str, Any], *, page_size: int = PAGE_SIZE, cache_status_inputs: Mapping[str, Any] | None = None) -> MarketGeneration`. The completed function must:

1. validate `page_size == 100`;
2. canonicalize a content envelope containing the normalized compact scan and validated `cache_status_inputs`, then derive `generation_id = sha256(bytes).hexdigest()[:24]`; this prevents the same immutable object key from receiving different index bytes;
3. classify disclosure using `freshnessFinancialReport.period` before `activeFinancialReport.period`;
4. sort the full entry list by the existing E4 PER ordering before slicing;
5. write page payloads with `schemaVersion`, `generationId`, `disclosure`, `category`, `cursor`, `limit`, `total`, `nextCursor`, and `items`;
6. put exact object keys and SHA-256 hashes in the generation index;
7. reject an index at or above `50 KiB` and a page at or above `500 KiB`.

`generation.files` must contain the exact canonical bytes for both the immutable generation index and every page. The generation-index entry is always:

```python
generation_index_key = f"public/market_scan/v2/{generation_id}/index.json"
files[generation_index_key] = canonical_json_bytes(generation_index)
```

- [x] **Step 4: Wire the builder into the seed build**

After `write_market_scan_summary(scan_payload)`, write the following in both the live-build path and `copy_offline_seed_payload()` path. The default `offline_first` execution must not return before v2 files and manifest metadata exist. Validate the recursive-delete target before deleting any existing output. Write all immutable page and generation-index bytes first, then atomically replace `market_scan_index.json` from a same-directory temporary file only after every immutable write succeeds:

```python
generation = build_market_generation(
    compact_market_scan_payload(scan_payload),
    cache_status_inputs={
        "generatedAt": scan_payload.get("generatedAt"),
        "latestRevenuePeriod": scan_payload.get("filingContext", {}).get("monthlyRevenuePeriod"),
        "latestFinancialPeriod": latest_financial_period(scan_payload),
    },
)
for object_key, content in generation.files.items():
    target = OUT_DIR / object_key.removeprefix("public/")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
atomic_write_bytes(OUT_DIR / "market_scan_index.json", canonical_json_bytes(generation.index))
```

Update `clear_seed_output()` to remove `cloudflare/seed/market_scan/v2` recursively only after resolving and checking that the target remains under `OUT_DIR`. Add manifest fields `marketApiSchemaVersion`, `marketGenerationId`, `marketIndexBytes`, `marketPageCount`, and `marketMaxPageBytes`.

- [x] **Step 5: Run GREEN generation verification**

Run:

```powershell
python -m pytest tests\test_market_query.py tests\test_cloudflare_seed_build.py -q --basetemp (Join-Path $env:TEMP 'pytest-plan-b-task1-green')
python -m ruff check backend\services\market_query.py scripts\build_cloudflare_seed.py tests\test_market_query.py tests\test_cloudflare_seed_build.py
python scripts\check_code_size_budgets.py
git diff --check
```

Expected: all commands exit `0`; fixture index is under `50 KiB`, every page is under `500 KiB`, and aggregate identities match legacy.

- [x] **Step 6: Review and commit Task 1**

Request a read-only spec and quality review of Task 1, fix every Critical/Important finding, rerun Step 5, then commit:

```powershell
git add backend\services\market_query.py scripts\build_cloudflare_seed.py scripts\check_code_size_budgets.py tests\test_market_query.py tests\test_cloudflare_seed_build.py
git commit -m "feat: build versioned market generations"
```

Execution evidence (2026-07-13): commit `f64423b`; focused gate `65 passed`; full Python gate `775 passed`; real summary `1,752` identities across `21` pages with a `5,865`-byte index and `155,264`-byte maximum page; independent spec and quality reviews both reported Critical `0`, Important `0`, Minor `0`, Ready `Yes`. No push or deployment was performed.

---

### Task 2: Strict v2 Package, Validator, and Pointer-Last Upload

**Files:**

- Create: `tests/test_package_cloudflare_seed_cache.py`
- Modify: `scripts/package_cloudflare_seed_cache.py`
- Modify: `scripts/validate_cloudflare_seed_inputs.py`
- Modify: `tests/test_cloudflare_seed_inputs.py`
- Modify: `scripts/cloudflare_seed_upload_plan.py`
- Modify: `tests/test_r2_refresh_workflow_scripts.py`
- Modify: `.github/workflows/cloudflare-r2-seed-refresh.yml`

**Interfaces:**

- Consumes: `market_scan_index.json` and its referenced immutable generation files from Task 1.
- Produces: a ZIP containing exactly one referenced v2 generation plus legacy/official seed files.
- Produces: ordered `UploadPlanItem` rows where `public/market_scan_index.json` is last.
- Produces: strict validation summary fields `marketGenerationId`, `marketPageCount`, and `marketMaxPageBytes`.

- [x] **Step 1: Write package and validator mutation tests**

Cover missing page, wrong hash, wrong generation ID, index/page size overflow, duplicate cursor, cursor gap, wrong `nextCursor`, aggregate count mismatch, duplicate/missing stock identity, unsafe page path, unreferenced page, stale orphan generation in ZIP, and wrong upload order.

```python
def test_upload_plan_publishes_pointer_last(tmp_path):
    plan = build_upload_plan(seed_dir_with_generation(tmp_path), data_dir_with_official_files(tmp_path))
    keys = [item.object_key for item in plan]
    assert keys[-1] == "public/market_scan_index.json"
    assert max(keys.index(key) for key in keys if "/market_scan/v2/" in key) < len(keys) - 1


def test_validator_rejects_mutated_page_hash(tmp_path):
    seed_zip = valid_v2_seed_zip(tmp_path)
    mutate_zip_json(seed_zip, referenced_page_name(seed_zip), {"items": []})
    with pytest.raises(ValueError, match="page SHA-256"):
        validate_seed_zip(seed_zip)
```

- [x] **Step 2: Run RED package and validator tests**

Run:

```powershell
python -m pytest tests\test_package_cloudflare_seed_cache.py tests\test_cloudflare_seed_inputs.py tests\test_r2_refresh_workflow_scripts.py -q --basetemp (Join-Path $env:TEMP 'pytest-plan-b-task2-red')
```

Expected: FAIL because v2 files are not packaged, validated, or ordered.

- [x] **Step 3: Implement referenced-generation packaging**

Add a helper that reads the pointer and returns only safe referenced paths:

```python
def referenced_market_generation_files(seed_dir: Path) -> list[Path]:
    pointer = json.loads((seed_dir / "market_scan_index.json").read_text(encoding="utf-8"))
    generation_id = require_generation_id(pointer.get("generationId"))
    names = {f"market_scan/v2/{generation_id}/index.json"}
    for ref in iter_page_refs(pointer):
        relative = Path(str(ref["key"]).removeprefix("public/"))
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError(f"unsafe market page path: {ref['key']}")
        names.add(relative.as_posix())
    return [seed_dir / name for name in sorted(names)]
```

The package script must include `market_scan_index.json` and this exact list. It must not glob every directory under `market_scan/v2`.

- [x] **Step 4: Implement strict v2 validation and upload ordering**

Validation must use raw ZIP bytes for hash and size checks, verify every page reference exactly once, reconstruct all six disclosure/category lists, and compare both counts and `(stockCode, disclosure, category)` identities with legacy `market_scan_latest.json`.

Build upload rows in this order:

```python
generation_pages
generation_index
legacy_and_official_files
public_manifest
public_pointer
```

Keep `.github/workflows/cloudflare-r2-seed-refresh.yml` as a simple ordered TSV consumer; add a static test that it never sorts the TSV after generation.

- [x] **Step 5: Run GREEN package and validator verification**

Run:

```powershell
python -m pytest tests\test_package_cloudflare_seed_cache.py tests\test_cloudflare_seed_inputs.py tests\test_r2_refresh_workflow_scripts.py -q --basetemp (Join-Path $env:TEMP 'pytest-plan-b-task2-green')
python -m ruff check scripts\package_cloudflare_seed_cache.py scripts\validate_cloudflare_seed_inputs.py scripts\cloudflare_seed_upload_plan.py tests\test_package_cloudflare_seed_cache.py tests\test_cloudflare_seed_inputs.py tests\test_r2_refresh_workflow_scripts.py
python scripts\check_operational_readiness.py
python scripts\check_deployment_preflight.py
git diff --check
```

Expected: all commands exit `0`; valid generation passes and every mutant fails for its intended reason.

- [x] **Step 6: Review and commit Task 2**

After independent review and a fresh Step 5:

```powershell
git add scripts\package_cloudflare_seed_cache.py scripts\validate_cloudflare_seed_inputs.py scripts\cloudflare_seed_upload_plan.py tests\test_package_cloudflare_seed_cache.py tests\test_cloudflare_seed_inputs.py tests\test_r2_refresh_workflow_scripts.py .github\workflows\cloudflare-r2-seed-refresh.yml
git commit -m "feat: publish market generations atomically"
```

Execution evidence (2026-07-13): commit `66e8ae1`; focused gate `121 passed, 1 skipped` (Windows symlink privilege); full Python gate `816 passed, 1 skipped`; Ruff, operational readiness, deployment preflight, and `git diff --check` all exited `0`. The package contains only the pointer-referenced generation, validates canonical v2 bytes and archive budgets, uploads pages/index before manifest and pointer, and fails closed without overwriting retained rollback backups. Independent spec, quality, and verification reviews reported Critical `0`, Important `0`, Minor `0`, Ready `Yes`. No push or deployment was performed.

---

### Task 3: Worker and FastAPI v2 Query Endpoints

**Files:**

- Create: `cloudflare/worker_market_query.py`
- Create: `tests/test_worker_market_query.py`
- Modify: `cloudflare/worker.py`
- Modify: `backend/routers/market.py`
- Modify: `tests/test_cloudflare_worker.py`
- Modify: `tests/test_api_worker_contracts.py`
- Modify: `tests/test_api.py`
- Modify: `tests/test_routers_coverage.py`
- Modify: `frontend/api_client.js`
- Modify: `tests/test_frontend_parser.py`
- Modify: `scripts/check_code_size_budgets.py`

**Interfaces:**

- Produces: `GET /api/scan/market/index`.
- Produces: `GET /api/scan/market/results?disclosure=&category=&cursor=&limit=&generationId=`.
- Consumes: Task 1 pointer/generation index/page schemas.
- Keeps: legacy market GET/POST and full report contracts unchanged.

- [x] **Step 1: Write API contract and parser tests**

Test FastAPI/Worker parity for required keys and statuses; strict enum validation; `cursor >= 0`; `1 <= limit <= 100`; cursor beyond total; a window spanning two physical pages; requested generation mismatch; missing/malformed page; pointer missing; and proof that v2 routes never read `market_scan_summary.json`.

```python
@pytest.mark.parametrize(
    ("query", "message"),
    [
        ({"disclosure": ["other"], "category": ["watch"], "cursor": ["0"], "limit": ["100"]}, "disclosure"),
        ({"disclosure": ["announced"], "category": ["watch"], "cursor": ["-1"], "limit": ["100"]}, "cursor"),
        ({"disclosure": ["announced"], "category": ["watch"], "cursor": ["0"], "limit": ["101"]}, "limit"),
    ],
)
def test_parse_market_results_query_rejects_invalid_values(query, message):
    with pytest.raises(ValueError, match=message):
        parse_market_results_query(query)
```

- [x] **Step 2: Run RED API tests**

Run:

```powershell
python -m pytest tests\test_worker_market_query.py tests\test_cloudflare_worker.py tests\test_api_worker_contracts.py tests\test_api.py tests\test_routers_coverage.py tests\test_frontend_parser.py -q --basetemp (Join-Path $env:TEMP 'pytest-plan-b-task3-red')
```

Expected: FAIL because the v2 routes and Worker query module do not exist.

- [x] **Step 3: Implement the Worker query module**

Use immutable validated query data:

```python
@dataclass(frozen=True)
class MarketResultsQuery:
    disclosure: str
    category: str
    cursor: int
    limit: int
    generation_id: str | None


def parse_market_results_query(query: dict[str, list[str]]) -> MarketResultsQuery:
    disclosure = first_query_value(query, "disclosure")
    category = first_query_value(query, "category")
    cursor = parse_bounded_int(first_query_value(query, "cursor"), "cursor", 0, 2_000_000)
    limit = parse_bounded_int(first_query_value(query, "limit"), "limit", 1, 100)
    generation_id = optional_generation_id(first_query_value(query, "generationId"))
    if disclosure not in {"announced", "pending"}:
        raise ValueError("invalid disclosure")
    if category not in {"entry", "watch", "excluded"}:
        raise ValueError("invalid category")
    return MarketResultsQuery(disclosure, category, cursor, limit, generation_id)
```

`select_page_references()` must return at most two references for a 100-row API page size and 100-row request limit. `merge_market_pages()` must verify generation/disclosure/category/cursor identity on every loaded page before returning a sliced window.

- [x] **Step 4: Add thin Worker and FastAPI routes**

Worker route behavior:

```text
GET index:
  read public/market_scan_index.json
  validate schemaVersion=2
  attach cacheStatus from public/manifest.json
  return bounded JSON or safe 503 with requestId

GET results:
  parse query
  read pointer or requested immutable generation index
  read only referenced pages needed for the window
  return bounded JSON or 409 generation_mismatch / safe 503
```

FastAPI uses `build_market_generation()` and `query_market_generation()` over the existing `ScanCacheService` result so local development has the same success contract. Add both paths to `PUBLIC_DIRECT_FALLBACK_ROUTES` as GET-only routes.

- [x] **Step 5: Run GREEN API and runtime verification**

Run:

```powershell
python -m pytest tests\test_worker_market_query.py tests\test_cloudflare_worker.py tests\test_api_worker_contracts.py tests\test_api.py tests\test_routers_coverage.py tests\test_frontend_parser.py -q --basetemp (Join-Path $env:TEMP 'pytest-plan-b-task3-green')
python -m ruff check cloudflare\worker_market_query.py cloudflare\worker.py backend\routers\market.py tests\test_worker_market_query.py
python scripts\check_code_size_budgets.py
npx.cmd wrangler deploy --config cloudflare\wrangler.toml --dry-run --outdir (Join-Path $env:TEMP 'stock-worker-plan-b-task3')
python scripts\run_wrangler_dev_smoke.py
git diff --check
```

Expected: all commands exit `0`; legacy tests remain unchanged and v2 responses never exceed their budgets.

- [x] **Step 6: Review and commit Task 3**

After independent review and a fresh Step 5:

```powershell
git add cloudflare\worker_market_query.py cloudflare\worker.py backend\routers\market.py frontend\api_client.js scripts\check_code_size_budgets.py tests\test_worker_market_query.py tests\test_cloudflare_worker.py tests\test_api_worker_contracts.py tests\test_api.py tests\test_routers_coverage.py tests\test_frontend_parser.py
git commit -m "feat: serve paged market queries"
```

Execution evidence (2026-07-13): commit `879faf3`; focused gate `264 passed`; full Python gate `892 passed, 1 skipped` from `893 collected`; Ruff, ESLint, Prettier, code-size, Wrangler dry-run, Wrangler dev smoke, and `git diff --check` all exited `0`. The Worker reads exact R2 `arrayBuffer()` bytes, validates raw size/UTF-8/canonical JSON/hash plus strict index/page/item/reason schemas, keeps immutable generation pinning, and returns bounded `no-store` responses. FastAPI mirrors `200`/`409`/`422`/`503` contracts while retaining current and previous generations. Independent spec, quality, and verification reviews reported Critical `0`, Important `0`, Minor `0`, Ready `Yes`; the verifier also exercised index and cross-page pinned results through real local workerd/Pyodide. No push or deployment was performed.

---

### Task 4: Frontend Lazy Pages and Last-Known-Good Index Shell

**Files:**

- Create: `frontend/market_query.js`
- Modify: `frontend/index.html`
- Modify: `frontend/app.js`
- Modify: `frontend/market_scan.js`
- Modify: `frontend/market_render.js`
- Modify: `frontend/storage.js`
- Modify: `tests/test_frontend_parser.py`
- Modify: `tests/test_frontend_hygiene.py`
- Modify: `tests/e2e/smoke.spec.ts`
- Modify: `scripts/check_code_size_budgets.py`

**Interfaces:**

- Consumes: Task 3 index/results endpoints.
- Produces: `createMarketQueryClient({ apiJson, storage, apiPageSize: 100, uiPageSize: 6 })`.
- Produces: `loadIndex()`, `loadWindow(disclosure, category, uiPage)`, `findLoadedResult(stockCode)`, and `clearGeneration()`.
- Persists: validated index only at `tw_stock_scanner.market_index.v2`.

- [x] **Step 1: Write frontend helper and E2E RED tests**

Cover first load fetching only index plus active page, lazy tab/category loads, promise de-duplication, UI page start `96` loading API cursors `0` and `100`, index counts with only 100 loaded rows, generation cache invalidation, offline LKG shell, storage quota/JSON failure, page failure preserving rows and expansion, export not fetching pages, and v1 flag calling legacy only.

```javascript
test("UI page crossing API boundary loads both physical pages", async () => {
  const client = createClientWithRows(205);
  const window = await client.loadWindow("announced", "watch", 16);
  expect(client.requestedCursors()).toEqual([0, 100]);
  expect(window.items.map((item) => item.row)).toEqual([96, 97, 98, 99, 100, 101]);
});
```

- [x] **Step 2: Run RED frontend tests**

Run:

```powershell
python -m pytest tests\test_frontend_parser.py tests\test_frontend_hygiene.py -q --basetemp (Join-Path $env:TEMP 'pytest-plan-b-task4-red')
npm.cmd run test:e2e -- --grep "paged market|market index|last-known-good shell|legacy market fallback"
```

Expected: helper source and v2 UI behaviors are missing.

- [x] **Step 3: Implement the dedicated frontend query client**

Use these cache keys and window math:

```javascript
const INDEX_STORAGE_KEY = "tw_stock_scanner.market_index.v2";
const API_PAGE_SIZE = 100;
const UI_PAGE_SIZE = 6;

function requiredApiCursors(uiPage) {
  const start = uiPage * UI_PAGE_SIZE;
  const end = start + UI_PAGE_SIZE - 1;
  const first = Math.floor(start / API_PAGE_SIZE) * API_PAGE_SIZE;
  const last = Math.floor(end / API_PAGE_SIZE) * API_PAGE_SIZE;
  return first === last ? [first] : [first, last];
}

function pageCacheKey(generationId, disclosure, category, cursor) {
  return `${generationId}:${disclosure}:${category}:${cursor}`;
}
```

Validate `schemaVersion`, 24-hex generation ID, counts, `pageSize=100`, and bounded string/array fields before accepting network or stored indexes. Store no `items` anywhere outside the in-memory page map. Concurrent calls for the same key share one promise and remove it after settle.

- [x] **Step 4: Integrate v2 state without expanding saturated files**

Add `state.marketIndex`, `state.marketWindow`, and `state.marketQueryWarning`; keep legacy `state.marketScan` only for v1. Move all new page-fetch logic into `market_query.js`. Renderer input becomes:

```javascript
{
  items,
  total,
  windowStart,
  loading,
  error,
}
```

Overview/tab counts come from index counts. `findMarketResultById()` checks the query client's loaded-page map. Detail still calls the existing analyze endpoint. Export still posts directly to `/api/reports/market`. Add a meta flag with initial value `v1`:

```html
<meta name="stock-scanner-market-api-version" content="v1" />
```

- [x] **Step 5: Run GREEN frontend verification**

Run:

```powershell
python -m pytest tests\test_frontend_parser.py tests\test_frontend_hygiene.py -q --basetemp (Join-Path $env:TEMP 'pytest-plan-b-task4-green')
npm.cmd run lint
npm.cmd run format:check
npm.cmd run test:e2e
python scripts\check_frontend_hygiene.py
python scripts\check_code_size_budgets.py
git diff --check
```

Expected: pytest/lint/format/hygiene exit `0`; Playwright desktop/mobile pass; v1 remains the active default.

- [x] **Step 6: Review and commit Task 4**

After independent functional/UI review and a fresh Step 5:

```powershell
git add frontend\market_query.js frontend\index.html frontend\app.js frontend\market_scan.js frontend\market_render.js frontend\storage.js scripts\check_code_size_budgets.py tests\test_frontend_parser.py tests\test_frontend_hygiene.py tests\e2e\smoke.spec.ts
git commit -m "feat: lazy load market result pages"
```

Execution evidence (2026-07-13): commit `d0392cc`; focused frontend gate `50 passed`; full Python gate `913 passed, 1 skipped` from `914 collected`; Playwright desktop/mobile gate `33 passed, 1 skipped` from `34 total`. ESLint, Prettier, Node syntax, frontend hygiene, code-size budgets, and `git diff --check` all exited `0`. The v2 client validates bounded index/page contracts, loads only required physical pages, de-duplicates requests, pins generations, preserves an in-memory last-known-good window, and persists only the validated index. Storage access is fail-soft for SecurityError/quota/invalid or oversized values, while authenticated holding sync remains active. Six dependent frontend scripts share cache version `20260713-market-v2-pages`; the default remains v1. Independent specification review reported Critical `0`, Important `0`, Minor `0`, Ready `Yes`; independent quality review reported Critical `0`, Important `0`, Minor `1`, Ready `Yes`, with the sole non-blocking note that frontend line-count headroom remains narrow. Task 6 must add refresh behavior through a dedicated module rather than raising budgets or further compressing saturated files. No push or deployment was performed.

---

### Task 5: Atomic D1 Refresh Idempotency

**Files:**

- Create: `cloudflare/migrations/0003_refresh_jobs_idempotency.sql`
- Create: `cloudflare/worker_refresh_jobs.py`
- Modify: `cloudflare/schema.sql`
- Modify: `cloudflare/worker.py`
- Modify: `tests/test_cloudflare_migrations.py`
- Modify: `tests/test_cloudflare_worker.py`
- Modify: `scripts/check_code_size_budgets.py`

**Interfaces:**

- Produces: `normalize_idempotency_key(value: str | None) -> str | None`.
- Produces: `derive_refresh_idempotency_key(job_type, cache_key, client_key, now) -> str`.
- Produces: `enqueue_or_reuse_refresh_job(api, manifest, force, client_key, now) -> dict`.
- Adds nullable `idempotency_key` and unique indexes for key and active `(job_type, cache_key)`.

- [x] **Step 1: Write migration and concurrent enqueue tests**

Cover duplicate cleanup before index creation, nullable legacy rows, same client key reuse, different client keys sharing one active cache job, new job after success, invalid key rejection, and 10 concurrent enqueue attempts yielding one active row.

```python
async def test_concurrent_enqueue_keeps_one_active_job(fake_api, manifest):
    jobs = await asyncio.gather(
        *[
            enqueue_or_reuse_refresh_job(fake_api, manifest, True, f"client-{index}", FIXED_NOW)
            for index in range(10)
        ]
    )
    assert len({job["jobId"] for job in jobs}) == 1
    assert fake_api.db.active_job_count("market_scan") == 1
```

- [x] **Step 2: Run RED D1 tests**

Run:

```powershell
python -m pytest tests\test_cloudflare_migrations.py tests\test_cloudflare_worker.py -q -k "idempotent or ensure_refresh or refresh_job" --basetemp (Join-Path $env:TEMP 'pytest-plan-b-task5-red')
```

Expected: migration and atomic enqueue interfaces do not exist; concurrency test creates duplicates.

- [x] **Step 3: Add migration and key derivation**

Migration `0003_refresh_jobs_idempotency.sql` must mark all but the newest active row per `(job_type, cache_key)` as failed/superseded before creating indexes:

```sql
ALTER TABLE refresh_jobs ADD COLUMN idempotency_key TEXT;

WITH ranked_active AS (
  SELECT id,
         ROW_NUMBER() OVER (
           PARTITION BY job_type, cache_key
           ORDER BY queued_at DESC, id DESC
         ) AS active_rank
  FROM refresh_jobs
  WHERE status IN ('queued', 'running')
)
UPDATE refresh_jobs
SET status = 'failed',
    error = 'superseded before active uniqueness migration',
    finished_at = COALESCE(finished_at, datetime('now')),
    updated_at = datetime('now')
WHERE id IN (SELECT id FROM ranked_active WHERE active_rank > 1);

CREATE UNIQUE INDEX IF NOT EXISTS ux_refresh_jobs_idempotency_key
ON refresh_jobs(idempotency_key)
WHERE idempotency_key IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS ux_refresh_jobs_active_cache
ON refresh_jobs(job_type, cache_key)
WHERE status IN ('queued', 'running');
```

The test must execute this SQL against local SQLite semantics, not only search strings.

- [x] **Step 4: Implement insert-or-ignore and read-back**

Use one deterministic hashed key and no request-path cleanup delete:

```sql
INSERT OR IGNORE INTO refresh_jobs
(id, job_type, cache_key, idempotency_key, status, reason, queued_at, updated_at)
VALUES (?, ?, ?, ?, 'queued', ?, ?, ?)
```

Then read by `idempotency_key`; if absent, read the active `(job_type, cache_key)`. Never return an ID that was not read back from D1.

- [x] **Step 5: Run GREEN D1 and local migration verification**

Run:

```powershell
python -m pytest tests\test_cloudflare_migrations.py tests\test_cloudflare_worker.py -q -k "idempotent or ensure_refresh or refresh_job" --basetemp (Join-Path $env:TEMP 'pytest-plan-b-task5-green')
python -m ruff check cloudflare\worker_refresh_jobs.py cloudflare\worker.py tests\test_cloudflare_worker.py tests\test_cloudflare_migrations.py
npx.cmd wrangler d1 migrations apply stock-scanner-beta-db --local --config cloudflare\wrangler.toml --persist-to .tmp\plan-b-d1
npx.cmd wrangler d1 execute stock-scanner-beta-db --local --config cloudflare\wrangler.toml --persist-to .tmp\plan-b-d1 --command "SELECT name, sql FROM sqlite_schema WHERE type='index' AND tbl_name='refresh_jobs';"
python scripts\check_code_size_budgets.py
git diff --check
```

Expected: one active unique index, one idempotency unique index, all focused tests pass.

- [x] **Step 6: Review and commit Task 5**

After a concurrency-focused review and fresh Step 5:

```powershell
git add cloudflare\migrations\0003_refresh_jobs_idempotency.sql cloudflare\schema.sql cloudflare\worker_refresh_jobs.py cloudflare\worker.py scripts\check_code_size_budgets.py tests\test_cloudflare_migrations.py tests\test_cloudflare_worker.py
git commit -m "fix: enqueue refresh jobs atomically"
```

Execution evidence (2026-07-13): commit `4bf8c89`; the focused atomic enqueue gate passed `28/28`, the related refresh/cache/scan gate passed `34/34`, migration tests passed `5/5`, Worker tests passed `131/131`, and the full Python gate passed `934` with `1` skipped from `935 collected`. A fresh Wrangler `4.103.0` local D1 applied migrations `0001` through `0003`, exposed both unique partial indexes, and a second apply reported `No migrations to apply`; the Worker bundle dry-run also exited `0` and included the new module and migration. Ruff, code-size budgets, and `git diff --check` exited `0`. Independent concurrency review reported Critical `0`, Important `0`, Minor `0`, Ready `Yes`; a real multi-connection SQLite 10-way probe returned one job ID, one active row, and only a 64-character hashed key. Task 5 intentionally guarantees coalescing while a `(job_type, cache_key)` job is active, not a permanent alias for every losing client key. Task 6 must poll only the returned job ID after a successful command response, and Task 10 must document and canary-check this bounded semantic plus the migration-before-Worker compatibility window. No push or deployment was performed.

---

### Task 6: Refresh Command/Status API and Frontend Polling

**Files:**

- Modify: `cloudflare/worker_refresh_jobs.py`
- Modify: `cloudflare/worker.py`
- Modify: `cloudflare/worker_observability.py`
- Modify: `scripts/check_cloudflare_cors.py`
- Modify: `frontend/api_client.js`
- Modify: `frontend/market_query.js`
- Create: `frontend/market_refresh.js`
- Modify: `frontend/app.js`
- Modify: `frontend/index.html`
- Modify: `scripts/check_code_size_budgets.py`
- Modify: `tests/test_cloudflare_worker.py`
- Modify: `tests/test_cloudflare_cors_check.py`
- Modify: `tests/test_api_worker_contracts.py`
- Modify: `tests/test_frontend_hygiene.py`
- Modify: `tests/test_frontend_parser.py`
- Modify: `tests/e2e/smoke.spec.ts`

**Interfaces:**

- Produces: `POST /api/scan/market/refresh` returning HTTP `202` without market lists.
- Produces: `GET /api/scan/market/refresh/{jobId}` returning a safe no-store status payload.
- Consumes: `Idempotency-Key` with `1..80` ASCII `[A-Za-z0-9._:-]` characters.
- Keeps: legacy POST response, adding only deprecation/successor headers.
- Frontend constraint: add refresh/polling behavior in a dedicated module; do not raise the current `app.js` or `market_query.js` budgets or remove more formatting whitespace to make it fit.
- Idempotency constraint: after any successful command response, poll only its returned `jobId`; do not POST again while that job is queued or running. Different client keys coalesce only while the same cache job is active and are not permanent replay aliases.

- [x] **Step 1: Write command/status/CORS tests**

Cover CSRF rejection; valid 202 payload; no `entry/watch/excluded`; `Location`; same-key reuse; invalid idempotency key; status 404; error redaction; no-store; legacy compatibility; OPTIONS accepting `Idempotency-Key`; frontend force refresh polling without clearing last-good rows; and the explicit contract that a coalesced loser key may create a new job if it is submitted again only after the shared active job has become terminal.

```python
def test_refresh_command_returns_202_without_market_payload(worker, csrf_request):
    response = run(worker.fetch(csrf_request("/api/scan/market/refresh", idempotency_key="refresh-1")))
    body = response_json(response)
    assert response.status == 202
    assert set(body) >= {"jobId", "status", "requestId", "statusUrl"}
    assert not {"entry", "watch", "excluded"} & set(body)
    assert response.headers.get("Location") == body["statusUrl"]
```

- [x] **Step 2: Run RED command/status tests**

Run:

```powershell
python -m pytest tests\test_cloudflare_worker.py tests\test_cloudflare_cors_check.py tests\test_api_worker_contracts.py tests\test_frontend_parser.py -q -k "refresh_command or refresh_status or idempotency or cors" --basetemp (Join-Path $env:TEMP 'pytest-plan-b-task6-red')
npm.cmd run test:e2e -- --grep "refresh command|refresh polling"
```

Expected: new paths and CORS header are absent.

- [x] **Step 3: Implement safe command and status responses**

Command response contract:

```json
{
  "jobId": "32-lowercase-hex",
  "status": "queued",
  "requestId": "safe-request-id",
  "statusUrl": "/api/scan/market/refresh/32-lowercase-hex"
}
```

Status response uses camelCase and may include `queuedAt`, `startedAt`, `finishedAt`, `updatedAt`, `ownerRunId`, `ownerRunUrl`, `hasError`, `dispatchStatus`, and `dispatchErrorCode`. It must remove raw `error` and reject non-hex job IDs before querying D1.

Add `idempotency-key` to `Access-Control-Allow-Headers`. Legacy POST adds:

```text
Deprecation: true
Link: </api/scan/market/refresh>; rel="successor-version"
```

- [x] **Step 4: Integrate frontend command polling**

Only v2 mode uses the command endpoint. Generate one client key per user action and reuse it only until a command response is received. After a successful response, do not POST again: poll the returned `jobId` with bounded delays `[1000, 2000, 4000, 8000, 15000]`. Stop on `success`, `failed`, tab abort, or 60 seconds. A queued/running result keeps current pages visible. Success reloads the pointer and clears page cache only if generation changes.

- [x] **Step 5: Run GREEN command/status verification**

Run:

```powershell
python -m pytest tests\test_cloudflare_worker.py tests\test_cloudflare_cors_check.py tests\test_api_worker_contracts.py tests\test_frontend_parser.py -q --basetemp (Join-Path $env:TEMP 'pytest-plan-b-task6-green')
npm.cmd run lint
npm.cmd run format:check
npm.cmd run test:e2e
python scripts\check_frontend_hygiene.py
python scripts\check_code_size_budgets.py
npx.cmd wrangler deploy --config cloudflare\wrangler.toml --dry-run --outdir (Join-Path $env:TEMP 'stock-worker-plan-b-task6')
git diff --check
```

Expected: all commands exit `0`; new POST is 202/no payload, polling preserves last-good UI, legacy POST still passes.

- [x] **Step 6: Review and commit Task 6**

After security/API review and a fresh Step 5:

```powershell
git add cloudflare\worker_refresh_jobs.py cloudflare\worker.py cloudflare\worker_observability.py scripts\check_cloudflare_cors.py scripts\check_code_size_budgets.py frontend\api_client.js frontend\market_query.js frontend\market_refresh.js frontend\app.js frontend\index.html tests\test_cloudflare_worker.py tests\test_cloudflare_cors_check.py tests\test_api_worker_contracts.py tests\test_frontend_hygiene.py tests\test_frontend_parser.py tests\e2e\smoke.spec.ts
git commit -m "feat: separate market refresh commands"
```

Execution evidence (2026-07-13): commit `4ee7c08`; the RED selector failed `12` tests with `6` passing and the desktop/mobile refresh E2E failed `2/2` before implementation. The expanded GREEN selector passed `41/41`; the full Python gate passed `960` with `1` skipped from `961 collected`; the full Playwright gate passed `35` with `1` skipped, and the focused refresh command gate passed `2/2` on Chromium desktop/mobile. ESLint, Prettier, Ruff, Node syntax, frontend hygiene, code-size budgets, `git diff --check`, and the Wrangler `4.103.0` bundle dry-run exited `0`. A real foreground Wrangler `4.110.0` Python Worker with an isolated local D1 and migrations `0001` through `0003` returned exact command HTTP `202` with matching `Location` and `no-store`, status HTTP `200` with `no-store`, the same job for a repeated idempotency key, and HTTP `404` for an uppercase/nonconforming job ID. Independent specification/security review reported Critical `0`, Important `1`, Minor `1`, Ready for local commit `Yes`, and Ready for deployment `No`. The Important release blocker is that CSRF/CORS is not authorization: the public command needs a server-side persisted global cooldown/rate/freshness or authorization gate, with terminal-cycle abuse tests, before this endpoint or Task 8 can be deployed. The minor note is narrow frontend line-budget headroom. No push or deployment was performed.

---

### Task 7: Dispatch State and GitHub Executor Claim Contract

**Files:**

- Create: `cloudflare/migrations/0004_refresh_jobs_dispatch_state.sql`
- Modify: `cloudflare/schema.sql`
- Modify: `.github/workflows/cloudflare-r2-seed-refresh.yml`
- Modify: `tests/test_cloudflare_migrations.py`
- Modify: `tests/test_deployment_preflight.py`
- Modify: `tests/test_operational_readiness.py`
- Modify: `scripts/check_operational_readiness.py`

**Interfaces:**

- Adds: `dispatch_status`, `dispatch_attempts`, `dispatched_at`, and `dispatch_error_code`.
- Produces state flow: `pending -> dispatching -> dispatched|failed|unknown -> workflow_claimed`.
- Keeps main job `status='queued'` for dispatch `failed` or `unknown` so the GitHub schedule remains a fallback.

- [x] **Step 1: Write migration and workflow state tests**

Test schema/migration equivalence, default pending state, dispatch index, workflow claim setting `workflow_claimed`, owner-run-scoped success/failure updates, and proof that dispatch failure never changes main status from queued.

```python
def test_refresh_workflow_claims_dispatch_state_with_owner_run():
    text = Path(".github/workflows/cloudflare-r2-seed-refresh.yml").read_text(encoding="utf-8")
    assert "dispatch_status = 'workflow_claimed'" in text
    assert "owner_run_id = '${GITHUB_RUN_ID}'" in text
    assert "status = 'queued'" in text
```

- [x] **Step 2: Run RED dispatch-state tests**

Run:

```powershell
python -m pytest tests\test_cloudflare_migrations.py tests\test_deployment_preflight.py tests\test_operational_readiness.py -q --basetemp (Join-Path $env:TEMP 'pytest-plan-b-task7-red')
```

Expected: migration and workflow fields are missing.

- [x] **Step 3: Implement migration and workflow claim**

Migration:

```sql
ALTER TABLE refresh_jobs ADD COLUMN dispatch_status TEXT NOT NULL DEFAULT 'pending';
ALTER TABLE refresh_jobs ADD COLUMN dispatch_attempts INTEGER NOT NULL DEFAULT 0;
ALTER TABLE refresh_jobs ADD COLUMN dispatched_at TEXT;
ALTER TABLE refresh_jobs ADD COLUMN dispatch_error_code TEXT;

CREATE INDEX IF NOT EXISTS idx_refresh_jobs_dispatch
ON refresh_jobs(job_type, status, dispatch_status, queued_at);
```

Backfill only running/terminal rows that already have an `owner_run_id` to `workflow_claimed`; queued and unclaimed historical rows remain `pending`. Update the GitHub claim SQL to set `status='running'`, `dispatch_status='workflow_claimed'`, `owner_run_id`, `started_at`, and `updated_at` in one statement. Keep final updates scoped by `job_type='market_scan'`, `status='running'`, and `owner_run_id='${GITHUB_RUN_ID}'`.

- [x] **Step 4: Run GREEN dispatch-state verification**

Run:

```powershell
python -m pytest tests\test_cloudflare_migrations.py tests\test_deployment_preflight.py tests\test_operational_readiness.py -q --basetemp (Join-Path $env:TEMP 'pytest-plan-b-task7-green')
python scripts\check_deployment_preflight.py
python scripts\check_operational_readiness.py
python -m ruff check cloudflare\migrations scripts\check_operational_readiness.py tests\test_cloudflare_migrations.py tests\test_deployment_preflight.py tests\test_operational_readiness.py
npx.cmd wrangler d1 migrations apply stock-scanner-beta-db --local --config cloudflare\wrangler.toml --persist-to (Join-Path $env:TEMP 'stock-plan-b-task7-d1')
npx.cmd wrangler deploy --config cloudflare\wrangler.toml --dry-run --outdir (Join-Path $env:TEMP 'stock-worker-plan-b-task7')
git diff --check
```

Expected: all commands exit `0`; workflow claim and fallback invariants are locked by tests.

- [x] **Step 5: Review and commit Task 7**

After migration/workflow review and fresh Step 4:

```powershell
git add cloudflare\migrations\0004_refresh_jobs_dispatch_state.sql cloudflare\schema.sql .github\workflows\cloudflare-r2-seed-refresh.yml scripts\check_operational_readiness.py tests\test_cloudflare_migrations.py tests\test_deployment_preflight.py tests\test_operational_readiness.py
git commit -m "feat: track refresh dispatch state"
```

Execution evidence (2026-07-13): commit `c0f5fc9`; the Task 7 RED gate produced the expected `5 failed, 38 passed` before migration/workflow implementation. The final focused migration/preflight/readiness gate passed `45/45`, and the full Python gate passed `966` with `1` skipped from `967 collected`. Deployment preflight, operational readiness, Ruff, `git diff --check`, and the Worker bundle dry-run exited `0`. A fresh Wrangler `4.103.0` local D1 applied migrations `0001` through `0004`, a second apply reported `No migrations to apply`, and the dispatch index was verified as `(job_type, status, dispatch_status, queued_at)`. A real Python Worker against that 0004 state returned command HTTP `202`, status HTTP `200` with `dispatchStatus="pending"`, matching no-store headers, and malformed-ID HTTP `404`, proving Task 6's strict status allowlist remains forward-compatible. SQLite execution tests prove pending/dispatched/failed/unknown queued rows transition to `workflow_claimed`, a later run cannot steal an earlier owner, and terminal updates cannot cross owner or job type. The production deploy migration-before-live-Worker order and refresh migration-before-claim order are both locked by tests; the standalone readiness checker now rejects missing workflow claims or unscoped terminal mutations. Two independent reviews reported Critical `0`, Important `0`, Minor `0`, Ready for local commit `Yes`. Production deployment remains blocked by the Task 6 public-command abuse gate assigned to Task 8. No push or deployment was performed.

---

### Task 8: Cloudflare Scheduled Control-Plane Dispatch

**Files:**

- Create: `cloudflare/worker_refresh_control.py`
- Create: `tests/test_cloudflare_refresh_control.py`
- Modify: `cloudflare/worker.py`
- Modify: `cloudflare/worker_refresh_jobs.py`
- Modify: `cloudflare/wrangler.toml`
- Modify: `scripts/check_cloudflare_worker_secrets.py`
- Modify: `scripts/run_wrangler_dev_smoke.py`
- Modify: `tests/test_wrangler_dev_smoke.py`
- Modify: `tests/test_cloudflare_worker.py`
- Modify: `.github/workflows/cloudflare-deploy.yml`
- Modify: `scripts/check_code_size_budgets.py`

**Interfaces:**

- Produces: top-level `async def on_scheduled(controller, env, ctx)` delegated to `run_scheduled_refresh(env, scheduled_time)`.
- Consumes: Task 5 atomic enqueue and Task 7 dispatch state.
- Consumes secret: `GITHUB_ACTIONS_DISPATCH_TOKEN` only when `GITHUB_DISPATCH_ENABLED=true`.
- Consumes vars: `GITHUB_REPOSITORY`, `GITHUB_WORKFLOW_FILE`, `GITHUB_WORKFLOW_REF`, `GITHUB_DISPATCH_ENABLED`.
- Release blocker: the public refresh command remains unauthenticated; CSRF/CORS must never be treated as authorization. Before any Task 6 endpoint or scheduled dispatcher deployment, add a persisted server-side global cooldown/rate/freshness gate (or stronger authorization) that applies across cache generations and terminal jobs.

- [ ] **Step 1: Write due/dispatch/state-machine tests**

Cover fresh/no-op, ahead-window enqueue, dispatch disabled, pending conditional claim, GitHub 2xx success, 4xx failed, network/5xx unknown, no automatic retry after ambiguous response, safe error codes, and no secret/header/body in logs. Add abuse regressions proving that distinct anonymous/manual keys submitted after each terminal transition cannot create or dispatch another expensive rebuild inside the protected global window, that changing cache generation does not bypass the window, and that the fixed CSRF header alone grants no override. The 15-minute GitHub fallback may consume only jobs admitted by this authoritative gate.

```python
async def test_ambiguous_dispatch_keeps_job_queued_for_schedule_fallback(fake_control):
    fake_control.github.raises = TimeoutError()
    result = await run_scheduled_refresh(fake_control.env, FIXED_SCHEDULED_TIME)
    assert result["dispatchStatus"] == "unknown"
    assert fake_control.db.job["status"] == "queued"
    assert fake_control.github.calls == 1
```

- [ ] **Step 2: Run RED scheduled tests**

Run:

```powershell
python -m pytest tests\test_cloudflare_refresh_control.py tests\test_cloudflare_worker.py tests\test_wrangler_dev_smoke.py -q -k "scheduled or dispatch or refresh" --basetemp (Join-Path $env:TEMP 'pytest-plan-b-task8-red')
```

Expected: scheduled handler, dispatch module, vars, and secret checks are absent.

- [ ] **Step 3: Implement one-shot safe dispatch**

Scheduled flow:

```text
read public/manifest.json
derive cache status with existing policy
return fresh unless stale or within 60-minute ahead window
enforce the persisted cross-generation public-command cooldown before admitting paid work
atomic enqueue/reuse
conditionally update pending -> dispatching and attempts + 1
if dispatch disabled: restore pending and return disabled
await exactly one GitHub workflow_dispatch request
classify 2xx as dispatched, 4xx as failed, network/5xx as unknown
keep main job queued in every dispatch outcome
```

Send only:

```json
{
  "ref": "configured-ref",
  "inputs": {"force": "false"}
}
```

Do not retry the GitHub request because a timeout may occur after GitHub accepted it.

The abuse gate is a production release invariant, not an in-memory browser debounce. It must query persisted refresh history independently of client keys and cache generation. At minimum, successful/queued/running public work is globally bounded by the active cache policy interval; failed work receives a bounded retry cooldown. A rejected command returns a safe `429` with `Retry-After`. Any operator override must use real server-side authorization or a secret-gated control path and must not be enabled by the public CSRF value.

- [ ] **Step 4: Configure cron and secret boundaries**

Keep the committed production cron empty so deploying additive code cannot enqueue or mutate production state:

```toml
[triggers]
crons = []
```

Task 10's release-config renderer may produce `crons = ["2,17,32,47 * * * *"]` only with an explicit `--enable-production-cron` argument. That authorized cron runs five minutes before the existing GitHub fallback schedule. Deploy checks require `GITHUB_ACTIONS_DISPATCH_TOKEN` only when dispatch is enabled. Local smoke forces dispatch disabled and uses Wrangler `--test-scheduled`; it never reads a real token. Extend the local smoke to apply all migrations to an isolated persistence directory and exercise the real Python Worker command/status routes: POST `202` with exact payload, `Location`, and `no-store`; GET returned status `200` and `no-store`; repeated key returns the same job; malformed/uppercase job ID returns `404`.

- [ ] **Step 5: Run GREEN scheduled verification**

Run:

```powershell
python -m pytest tests\test_cloudflare_refresh_control.py tests\test_cloudflare_worker.py tests\test_wrangler_dev_smoke.py -q -k "scheduled or dispatch or refresh" --basetemp (Join-Path $env:TEMP 'pytest-plan-b-task8-green')
python -m ruff check cloudflare\worker_refresh_control.py cloudflare\worker.py tests\test_cloudflare_refresh_control.py
python scripts\check_deployment_preflight.py
python scripts\check_operational_readiness.py
npx.cmd wrangler deploy --config cloudflare\wrangler.toml --dry-run --outdir (Join-Path $env:TEMP 'stock-worker-plan-b-task8')
python scripts\run_wrangler_dev_smoke.py
python scripts\check_code_size_budgets.py
git diff --check
```

Expected: all commands exit `0`; mock dispatch occurs once; local runtime never contacts GitHub.

- [ ] **Step 6: Review and commit Task 8**

After security/Cloudflare review and fresh Step 5:

```powershell
git add cloudflare\worker_refresh_control.py cloudflare\worker.py cloudflare\worker_refresh_jobs.py cloudflare\wrangler.toml scripts\check_cloudflare_worker_secrets.py scripts\run_wrangler_dev_smoke.py scripts\check_code_size_budgets.py .github\workflows\cloudflare-deploy.yml tests\test_cloudflare_refresh_control.py tests\test_cloudflare_worker.py tests\test_wrangler_dev_smoke.py
git commit -m "feat: dispatch due refresh jobs"
```

---

### Task 9: Edge Cache Boundaries and Runtime Rollback Flag

**Files:**

- Modify: `cloudflare/worker_support.py`
- Modify: `cloudflare/worker.py`
- Modify: `cloudflare/wrangler.toml`
- Modify: `frontend/api_client.js`
- Modify: `frontend/market_query.js`
- Modify: `frontend/app.js`
- Modify: `tests/test_cloudflare_worker.py`
- Modify: `tests/test_frontend_parser.py`
- Modify: `tests/test_remote_smoke.py`
- Modify: `scripts/run_remote_smoke.py`
- Modify: `scripts/check_code_size_budgets.py`

**Interfaces:**

- Produces: `GET /api/runtime-config` with `marketScanApiVersion: "v1" | "v2"`.
- Produces: explicit edge-cache headers for bounded public v2 GETs.
- Keeps: all POST, auth/private, refresh status, and error responses `Cache-Control: no-store`.
- Keeps: legacy 2.88 MB market GET `no-store` while it is the rollback path.

- [ ] **Step 1: Write cache and runtime-flag RED tests**

Cover config defaults to v1; invalid env value becomes v1; config TTL at most 60 seconds; v2 index/results use edge-only cache headers; no `s-maxage`; legacy market GET no-store; private/status/error no-store; query canonicalization; config failure frontend defaults v1; v2 contract failure preserves last-good and switches to v1 once without dual background downloads.

```python
def test_v2_cache_headers_use_cloudflare_control_without_s_maxage(worker):
    response = run(worker.fetch(get_request("/api/scan/market/index")))
    assert response.headers.get("Cache-Control") == "public, max-age=0"
    edge = response.headers.get("Cloudflare-CDN-Cache-Control")
    assert "stale-while-revalidate=" in edge
    assert "stale-if-error=" in edge
    assert "s-maxage" not in edge
```

- [ ] **Step 2: Run RED cache/flag tests**

Run:

```powershell
python -m pytest tests\test_cloudflare_worker.py tests\test_frontend_parser.py tests\test_remote_smoke.py -q -k "runtime_config or edge_cache or market_api_version" --basetemp (Join-Path $env:TEMP 'pytest-plan-b-task9-red')
```

Expected: runtime config and route-specific edge-cache headers do not exist.

- [ ] **Step 3: Implement explicit response cache policies**

Add separate helpers instead of extending every `json_response()` call:

```python
def public_edge_cache_headers(max_age: int, stale_while_revalidate: int, stale_if_error: int) -> dict[str, str]:
    return {
        "Cache-Control": "public, max-age=0",
        "Cloudflare-CDN-Cache-Control": (
            f"public, max-age={max_age}, "
            f"stale-while-revalidate={stale_while_revalidate}, "
            f"stale-if-error={stale_if_error}"
        ),
    }


def no_store_headers() -> dict[str, str]:
    return {"Cache-Control": "no-store"}
```

Apply edge headers only to valid v2 index/results and runtime-config GETs. Errors use no-store. Add `MARKET_SCAN_API_VERSION="v1"` and `EDGE_CACHE_ENABLED="false"` to production vars; code ignores edge headers unless enabled. Keep Workers Caching itself disabled in production during additive rollout:

```toml
[cache]
enabled = false
```

Add a configuration contract test that checks the active Wrangler version is at least `4.69.0`, parses `[cache].enabled`, and runs the dry-run bundle. This follows the current [Workers Caching configuration](https://developers.cloudflare.com/workers/cache/configuration/) contract rather than relying on a legacy Cache API assumption.

- [ ] **Step 4: Integrate runtime flag and one-way fallback**

Frontend loads runtime config before market data. v1 calls legacy only. v2 calls index/results and new refresh command. If v2 schema/generation validation fails, set the session mode to v1, keep current UI/LKG, and issue at most one legacy request. Do not silently switch production config; this is a per-session safety fallback.

- [ ] **Step 5: Run GREEN cache/flag verification**

Run:

```powershell
python -m pytest tests\test_cloudflare_worker.py tests\test_frontend_parser.py tests\test_remote_smoke.py -q --basetemp (Join-Path $env:TEMP 'pytest-plan-b-task9-green')
npm.cmd run lint
npm.cmd run format:check
npm.cmd run test:e2e
python scripts\check_frontend_hygiene.py
python scripts\check_code_size_budgets.py
npx.cmd wrangler deploy --config cloudflare\wrangler.toml --dry-run --outdir (Join-Path $env:TEMP 'stock-worker-plan-b-task9')
git diff --check
```

Expected: all commands exit `0`; production config still selects v1 and disables edge caching.

- [ ] **Step 6: Review and commit Task 9**

After cache/security/frontend review and fresh Step 5:

```powershell
git add cloudflare\worker_support.py cloudflare\worker.py cloudflare\wrangler.toml frontend\api_client.js frontend\market_query.js frontend\app.js scripts\run_remote_smoke.py scripts\check_code_size_budgets.py tests\test_cloudflare_worker.py tests\test_frontend_parser.py tests\test_remote_smoke.py
git commit -m "feat: gate v2 reads and edge caching"
```

---

### Task 10: Staging Canary, Real Artifact, Rollout, and Documentation

**Files:**

- Create: `scripts/check_market_scan_v2_canary.py`
- Create: `tests/test_market_scan_v2_canary.py`
- Create: `cloudflare/wrangler.staging.template.toml`
- Create: `scripts/render_wrangler_release_config.py`
- Create: `tests/test_render_wrangler_release_config.py`
- Modify: `cloudflare/wrangler.toml`
- Modify: `.gitignore`
- Modify: `scripts/check_deployment_preflight.py`
- Modify: `tests/test_deployment_preflight.py`
- Modify: `scripts/run_remote_smoke.py`
- Modify: `tests/test_remote_smoke.py`
- Modify: `docs/current_architecture.md`
- Modify: `docs/cloudflare_deployment.md`
- Modify: `docs/seed_artifact_policy.md`
- Modify: `data/official_cache_seed_2026-05-14.zip`
- Modify: `data/official_cache_seed_2026-05-14.sha256`

**Interfaces:**

- Produces: a complete standalone staging Wrangler config with v2/cache enabled and dispatch disabled.
- Produces: complete standalone `production-cron`, `production-v2`, and `production-v1-rollback` configs only after mode-specific release acknowledgements.
- Produces: canary CLI that validates v1/v2 parity, budgets, hashes, generation consistency, and cache/privacy headers.
- Produces: committed real v2 artifact while retaining legacy files.
- Documents: additive rollout and exact rollback sequence.

- [ ] **Step 1: Write staging and canary RED tests**

Cover complete staging D1/R2/vars, no production dispatch secret in staging, empty staging cron, full v1/v2 count/identity parity across every page, hash mismatch, oversized index/page, mixed generation, private endpoint cache hit, v2 failure with v1 flag recovery, pointer-last generation switch, and refusal to render a live config from sentinel or production resource IDs. Production profile tests must prove that D1/R2 bindings and all unrelated settings are byte-for-byte equivalent after parsing, while only the explicitly authorized release switches change.

```python
def test_rendered_staging_config_is_complete_and_isolated(tmp_path):
    rendered = render_staging_config(
        template_path=Path("cloudflare/wrangler.staging.template.toml"),
        database_id="11111111-1111-1111-1111-111111111111",
        bucket_name="stock-scanner-beta-cache-staging-test",
        production_database_id="c29c7b4a-4a2d-4711-badd-1aaad43ce3b6",
        production_bucket_name="stock-scanner-beta-cache",
    )
    config = tomllib.loads(rendered)
    assert config["vars"]["GITHUB_DISPATCH_ENABLED"] == "false"
    assert config["vars"]["MARKET_SCAN_API_VERSION"] == "v2"
    assert config["d1_databases"][0]["database_id"] != "c29c7b4a-4a2d-4711-badd-1aaad43ce3b6"
    assert config["r2_buckets"][0]["bucket_name"] != "stock-scanner-beta-cache"
    assert config["triggers"]["crons"] == []


def test_production_cron_profile_enables_cron_and_dispatch_atomically():
    config = render_production_profile("production-cron", enable_production_cron=True)
    assert config["triggers"]["crons"] == ["2,17,32,47 * * * *"]
    assert config["vars"]["GITHUB_DISPATCH_ENABLED"] == "true"
    assert config["vars"]["MARKET_SCAN_API_VERSION"] == "v1"
    assert config["vars"]["EDGE_CACHE_ENABLED"] == "false"
    assert config["cache"]["enabled"] is False


def test_production_v2_and_rollback_profiles_change_only_release_switches():
    v2 = render_production_profile(
        "production-v2",
        enable_production_cron=True,
        enable_production_v2=True,
    )
    assert v2["triggers"]["crons"] == ["2,17,32,47 * * * *"]
    assert v2["vars"]["GITHUB_DISPATCH_ENABLED"] == "true"
    assert v2["vars"]["MARKET_SCAN_API_VERSION"] == "v2"
    assert v2["vars"]["EDGE_CACHE_ENABLED"] == "true"
    assert v2["cache"]["enabled"] is True

    rollback = render_production_profile(
        "production-v1-rollback",
        enable_production_cron=True,
        confirm_production_v1_rollback=True,
    )
    assert rollback["triggers"]["crons"] == ["2,17,32,47 * * * *"]
    assert rollback["vars"]["GITHUB_DISPATCH_ENABLED"] == "true"
    assert rollback["vars"]["MARKET_SCAN_API_VERSION"] == "v1"
    assert rollback["vars"]["EDGE_CACHE_ENABLED"] == "false"
    assert rollback["cache"]["enabled"] is False
```

- [ ] **Step 2: Run RED staging/canary tests**

Run:

```powershell
python -m pytest tests\test_market_scan_v2_canary.py tests\test_render_wrangler_release_config.py tests\test_deployment_preflight.py tests\test_remote_smoke.py -q --basetemp (Join-Path $env:TEMP 'pytest-plan-b-task10-red')
```

Expected: standalone staging template, release-config renderer, and canary script are absent.

- [ ] **Step 3: Add full standalone release configs with fail-closed sentinels**

Commit `cloudflare/wrangler.staging.template.toml` as a full config in the same directory as `worker.py`, so `main="worker.py"` and `migrations_dir="migrations"` resolve correctly. It includes explicit sentinel tokens:

```toml
name = "stock-scanner-beta-api-staging"
main = "worker.py"
compatibility_date = "2026-05-14"
compatibility_flags = ["python_workers", "disable_python_no_global_handlers", "disable_python_external_sdk"]
workers_dev = true

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

[vars]
APP_ENV = "staging"
APP_CORS_ALLOW_ORIGINS = "https://stock-scanner-beta-staging.pages.dev"
MARKET_SCAN_API_VERSION = "v2"
EDGE_CACHE_ENABLED = "true"
GITHUB_DISPATCH_ENABLED = "false"
GITHUB_REPOSITORY = "pcedison/stock_scanner"
GITHUB_WORKFLOW_FILE = "cloudflare-r2-seed-refresh.yml"
GITHUB_WORKFLOW_REF = "main"

[triggers]
crons = []

[cache]
enabled = true

[[d1_databases]]
binding = "DB"
database_name = "stock-scanner-beta-db-staging"
database_id = "__STAGING_D1_DATABASE_ID__"
migrations_dir = "migrations"

[[r2_buckets]]
binding = "CACHE"
bucket_name = "__STAGING_R2_BUCKET_NAME__"
```

`render_wrangler_release_config.py` writes a complete generated file beside the template, never an overlay. It rejects sentinel values, production D1/R2 values, output paths outside `cloudflare/`, and generated text still containing double-underscore sentinel tokens. `.gitignore` excludes `cloudflare/wrangler.*.generated.toml`.

Staging render and dry-run:

```powershell
python scripts\render_wrangler_release_config.py staging --database-id $env:CF_STAGING_D1_DATABASE_ID --bucket-name $env:CF_STAGING_R2_BUCKET --output cloudflare\wrangler.staging.generated.toml
npx.cmd wrangler deploy --config cloudflare\wrangler.staging.generated.toml --dry-run --outdir (Join-Path $env:TEMP 'stock-worker-plan-b-staging')
```

The same script reads the complete committed production config and supports three fail-closed, full-config profiles. It must parse the generated TOML, validate it against the installed Wrangler schema, require Wrangler `>=4.69`, and verify that D1/R2 bindings plus every setting outside the named release switches remain unchanged:

- `production-cron` requires `--enable-production-cron`, atomically sets `crons=["2,17,32,47 * * * *"]` and `GITHUB_DISPATCH_ENABLED="true"`, and keeps API v1 plus both cache switches false.
- `production-v2` requires both `--enable-production-cron` and `--enable-production-v2`; it keeps cron/dispatch active and atomically sets `MARKET_SCAN_API_VERSION="v2"`, `EDGE_CACHE_ENABLED="true"`, and `[cache].enabled=true`.
- `production-v1-rollback` requires both `--enable-production-cron` and `--confirm-production-v1-rollback`; it keeps cron/dispatch active and atomically restores `MARKET_SCAN_API_VERSION="v1"`, `EDGE_CACHE_ENABLED="false"`, and `[cache].enabled=false` without deleting v2 artifacts.

Each mode refuses a missing acknowledgement, a different output filename, or any binding mutation. No generated config is committed.

- [ ] **Step 4: Implement the canary CLI and rebuild the real artifact**

Canary CLI inputs:

```text
--base-url
--legacy-path /api/scan/market
--index-path /api/scan/market/index
--results-path /api/scan/market/results
--expected-generation optional
--require-edge-hit
--timeout-seconds
```

It must issue bounded GETs and never send credentials. For parity it must fetch every page reference declared by the index, validate each raw response size/hash/generation, reconstruct all six disclosure/category identity sets, and compare them exactly with the full legacy v1 identity sets. First/last-page sampling may be reported as extra smoke evidence but cannot satisfy parity. When `--require-edge-hit` is set, call public responses twice and assert status/private endpoints are no-store.

Rebuild:

```powershell
python scripts\hydrate_cloudflare_seed_inputs.py --data-dir data
$env:CLOUDFLARE_SEED_MODE='online'
python scripts\build_cloudflare_seed.py
if ($LASTEXITCODE -ne 0) { Remove-Item Env:\CLOUDFLARE_SEED_MODE -ErrorAction SilentlyContinue; exit $LASTEXITCODE }
Remove-Item Env:\CLOUDFLARE_SEED_MODE
python scripts\package_cloudflare_seed_cache.py
python scripts\validate_cloudflare_seed_inputs.py --zip data\official_cache_seed_2026-05-14.zip --max-age-days 3
```

- [ ] **Step 5: Document rollout and rollback**

Document this exact release order:

1. merge additive artifacts/endpoints with production flag v1 and cache disabled;
2. provision isolated staging resources and secrets under explicit release authority;
3. deploy staging v2/cache enabled and run canary;
4. deploy production code with flag v1/cache disabled;
5. under separate release authority, render the full production-cron config with `--enable-production-cron`, dry-run it, deploy it, and verify only then that cron and `GITHUB_DISPATCH_ENABLED=true` are active together;
6. under separate v2 release authority, render the full production-v2 config with `--enable-production-cron --enable-production-v2`, schema-validate and dry-run it, then deploy it to switch API and both cache controls together;
7. observe one full cache window: financial `2h`, monthly `3h`, routine `12h`;
8. rollback by rendering, schema-validating, dry-running, and deploying the full production-v1-rollback config with `--enable-production-cron --confirm-production-v1-rollback`; this atomically restores v1 and both cache controls false without deleting v2 artifacts;
9. retain current and previous immutable generations until the observation gate passes.

Before each Worker rollout that depends on a new D1 migration, apply and verify the migration first, then deploy the matching Worker immediately in the same controlled release window. Monitor legacy Worker `5xx` responses during that short compatibility interval. The staging canary must also submit different client keys concurrently for one cache key and prove there is never more than one `queued/running` row; document this as active-work coalescing, not a permanent per-client-key replay guarantee.

The authorized cron-enable preparation is explicit and produces a full config:

```powershell
python scripts\render_wrangler_release_config.py production-cron --enable-production-cron --output cloudflare\wrangler.production-cron.generated.toml
npx.cmd wrangler deploy --config cloudflare\wrangler.production-cron.generated.toml --dry-run --outdir (Join-Path $env:TEMP 'stock-worker-production-cron-dry-run')
```

The actual deploy repeats the second command without `--dry-run` only after release approval and secret/binding checks.

The authorized v2 switch and rollback preparations are also full configs, never partial variable edits:

```powershell
python scripts\render_wrangler_release_config.py production-v2 --enable-production-cron --enable-production-v2 --output cloudflare\wrangler.production-v2.generated.toml
npx.cmd wrangler deploy --config cloudflare\wrangler.production-v2.generated.toml --dry-run --outdir (Join-Path $env:TEMP 'stock-worker-production-v2-dry-run')
python scripts\render_wrangler_release_config.py production-v1-rollback --enable-production-cron --confirm-production-v1-rollback --output cloudflare\wrangler.production-v1-rollback.generated.toml
npx.cmd wrangler deploy --config cloudflare\wrangler.production-v1-rollback.generated.toml --dry-run --outdir (Join-Path $env:TEMP 'stock-worker-production-v1-rollback-dry-run')
```

Only after each generated config passes parser/schema checks, Wrangler `>=4.69`, dry-run, exact binding-equivalence tests, and the corresponding release approval may the same command be repeated without `--dry-run`.

- [ ] **Step 6: Run GREEN staging/artifact verification**

Run:

```powershell
python -m pytest tests\test_market_scan_v2_canary.py tests\test_render_wrangler_release_config.py tests\test_deployment_preflight.py tests\test_remote_smoke.py tests\test_cloudflare_seed_inputs.py -q --basetemp (Join-Path $env:TEMP 'pytest-plan-b-task10-green')
python scripts\check_deployment_preflight.py
python scripts\check_operational_readiness.py
python scripts\render_wrangler_release_config.py staging --database-id 11111111-1111-1111-1111-111111111111 --bucket-name stock-scanner-beta-cache-staging-test --output cloudflare\wrangler.staging.generated.toml
npx.cmd wrangler deploy --config cloudflare\wrangler.staging.generated.toml --dry-run --outdir (Join-Path $env:TEMP 'stock-worker-plan-b-staging')
python scripts\render_wrangler_release_config.py production-cron --enable-production-cron --output cloudflare\wrangler.production-cron.generated.toml
npx.cmd wrangler deploy --config cloudflare\wrangler.production-cron.generated.toml --dry-run --outdir (Join-Path $env:TEMP 'stock-worker-production-cron-plan-b')
python scripts\render_wrangler_release_config.py production-v2 --enable-production-cron --enable-production-v2 --output cloudflare\wrangler.production-v2.generated.toml
npx.cmd wrangler deploy --config cloudflare\wrangler.production-v2.generated.toml --dry-run --outdir (Join-Path $env:TEMP 'stock-worker-production-v2-plan-b')
python scripts\render_wrangler_release_config.py production-v1-rollback --enable-production-cron --confirm-production-v1-rollback --output cloudflare\wrangler.production-v1-rollback.generated.toml
npx.cmd wrangler deploy --config cloudflare\wrangler.production-v1-rollback.generated.toml --dry-run --outdir (Join-Path $env:TEMP 'stock-worker-production-v1-rollback-plan-b')
python -m ruff check scripts\check_market_scan_v2_canary.py scripts\render_wrangler_release_config.py tests\test_market_scan_v2_canary.py tests\test_render_wrangler_release_config.py
git diff --check
```

Expected: local tests and dry-runs exit `0`; no external resource is created and no production endpoint is mutated.

- [ ] **Step 7: Review and commit Task 10**

After artifact/security/deployment review and a fresh Step 6:

```powershell
git add scripts\check_market_scan_v2_canary.py tests\test_market_scan_v2_canary.py cloudflare\wrangler.staging.template.toml scripts\render_wrangler_release_config.py tests\test_render_wrangler_release_config.py cloudflare\wrangler.toml .gitignore scripts\check_deployment_preflight.py tests\test_deployment_preflight.py scripts\run_remote_smoke.py tests\test_remote_smoke.py docs\current_architecture.md docs\cloudflare_deployment.md docs\seed_artifact_policy.md data\official_cache_seed_2026-05-14.zip data\official_cache_seed_2026-05-14.sha256
git commit -m "feat: prepare v2 staging rollout"
```

---

## Plan B Final Gate

Run sequentially from the isolated worktree after every Task review is Ready Yes:

```powershell
$base = Join-Path $env:TEMP 'pytest-plan-b-final'
python -m pytest -q --basetemp $base
npm.cmd run lint
npm.cmd run format:check
python scripts\check_frontend_hygiene.py
python scripts\check_code_size_budgets.py
python scripts\check_operational_readiness.py
python scripts\check_deployment_preflight.py
python scripts\validate_cloudflare_seed_inputs.py --zip data\official_cache_seed_2026-05-14.zip --max-age-days 3
npx.cmd wrangler deploy --config cloudflare\wrangler.toml --dry-run --outdir (Join-Path $env:TEMP 'stock-worker-plan-b-final')
python scripts\render_wrangler_release_config.py staging --database-id 11111111-1111-1111-1111-111111111111 --bucket-name stock-scanner-beta-cache-staging-test --output cloudflare\wrangler.staging.generated.toml
npx.cmd wrangler deploy --config cloudflare\wrangler.staging.generated.toml --dry-run --outdir (Join-Path $env:TEMP 'stock-worker-plan-b-staging-final')
python scripts\run_wrangler_dev_smoke.py
npm.cmd run test:e2e
```

Then perform read-only production observations only. Production is expected to remain v1 until a separately authorized rollout, so these status prints are recorded as observations and are not part of the local pass/fail gate:

```powershell
curl.exe -sS -o NUL -w "health=%{http_code} bytes=%{size_download} time=%{time_total}\n" https://stock-scanner-beta-api.pcedison.workers.dev/api/health
curl.exe -sS -o NUL -w "legacy=%{http_code} bytes=%{size_download} time=%{time_total}\n" https://stock-scanner-beta-api.pcedison.workers.dev/api/scan/market
curl.exe -sS -o NUL -w "runtime=%{http_code} bytes=%{size_download} time=%{time_total}\n" https://stock-scanner-beta-api.pcedison.workers.dev/api/runtime-config
```

Record final commit, artifact generation/SHA, exact command results, independent review findings, branch/PR state, and the explicit statement `not deployed` in the Plan B execution ledger.

## Self-Review Checklist

- [ ] Every B1 requirement maps to Tasks 1–4 and Task 10.
- [ ] Every B2 requirement maps to Tasks 5–6.
- [ ] Every B3 requirement maps to Tasks 7–8.
- [ ] Every B4 requirement maps to Tasks 9–10.
- [ ] Legacy success/report contracts remain available through the rollout window.
- [ ] All new code responsibilities live in focused modules under existing size budgets.
- [ ] Every mutation path is idempotent or explicitly non-retried.
- [ ] Every public artifact has schema, size, hash, identity, and privacy validation.
- [ ] Every cacheable route is bounded and every private/error route is no-store.
- [ ] Staging cannot inherit production D1, R2, cron, or dispatch credentials.
- [ ] No production mutation is included in local implementation steps.
