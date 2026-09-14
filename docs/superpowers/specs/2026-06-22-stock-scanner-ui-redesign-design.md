# Stock Scanner UI Redesign Design

Date: 2026-06-22
Status: Historical design record; implemented and merged in PR #107. It is not current work authorization or agent guidance.

## Source Material

- Open Design project: `stock-scanner-interface-redesign`
- Open Design preview: http://127.0.0.1:7456/api/projects/stock-scanner-interface-redesign/raw/index.html
- Open Design project directory: local tool state outside this repository (not required to use this historical record)
- Generated artifact: `index.html`, single-file HTML/CSS/JS prototype, about 64 KB
- Validation screenshots: `.tmp/od-visual-scan/`
- Validation audit: `.tmp/od-visual-scan/audit.json`

## Goal

Redesign the visible UI of the Taiwan stock financial-report event scanner as a quieter, denser operational dashboard. The redesign should make the app easier to scan on desktop and mobile, while preserving the existing product strategy, workflow, data fetching, API behavior, authentication behavior, cache behavior, reports, and screening logic.

The approved direction is "方案 B: Operational Dashboard Redesign".

## Non-Negotiable Scope Guards

The implementation must not change:

- Screening strategy, thresholds, rule evaluation, status mapping, or rule IDs.
- Data collection, scraping, import, refresh, official data provider, MOPS/TWSE/TPEx behavior, cache invalidation, scheduler, or report/export workflows.
- FastAPI route contracts, Cloudflare Worker route contracts, D1/R2/session semantics, or localStorage fallback behavior.
- Auth/session logic, holdings persistence, settings persistence, or server-backed state semantics.
- Existing tests' business assertions.

The UI must preserve and display these status and rule identifiers exactly:

- Statuses: `ENTRY`, `WATCH`, `HOLD`, `ADD_WATCH`, `WARNING`, `EXIT`, `EXCLUDED`, `INSUFFICIENT_DATA`, `PARTIAL_DATA`, `PARTIAL_HOLDING`
- Rule groups: `E1-E6`, `A1-A7`, `X1-X5`, `T3`
- Guards: spring-festival guard, financial-industry exclusion, insufficient-data/partial-data guards

## Existing Repo Context

The app currently uses a static frontend under `frontend/` backed by FastAPI locally and a Cloudflare Python Worker in production. The visual implementation should stay inside the existing frontend surface:

- `frontend/index.html` for structural hooks already present in the app.
- `frontend/styles.css` for visual system, layout, responsive behavior, cards, chips, badges, nav, forms, and data-density rules.
- `frontend/app.js`, `frontend/renderers.js`, `frontend/market_render.js`, `frontend/market_scan.js`, and `frontend/navigation.js` only when visual state or DOM composition needs minor alignment.

No backend, worker, scraper, rules, cache, or persistence modules are part of this redesign.

## Design Direction

The redesigned app is an internal operations tool, not a landing page.

Use a restrained light dashboard system:

- Off-white app background.
- White data surfaces.
- Slate text hierarchy.
- Restrained blue primary actions.
- Semantic green, amber, and red status treatment.
- Maximum 8px radius for cards and buttons, except compact pills.
- No decorative orbs, bokeh, hero sections, promotional layouts, or card-inside-card compositions.

The first viewport must show the usable dashboard, not explanatory marketing content.

## App Shell

Desktop:

- Quiet left navigation with groups for operations and system views.
- Compact sticky top bar with current view title, small subtitle, source/cache/filing-window status pills, refresh action, and account affordance.
- Main content constrained to readable operational width, with dense but breathable cards and rows.

Mobile:

- Top bar with menu button, view title, refresh action, and compact account affordance.
- Drawer navigation.
- One-column content.
- Action buttons wrap or stack without horizontal scrolling.
- Result rows, forms, rule tables, and holding cards become stacked mobile-first blocks.

## View Design

### 總覽

Show the operator's daily state immediately:

- KPI cards for suitable entries, near-watch candidates, and exclusions.
- Operational status strip for data source, cache, filing window, and holding risk.
- "Recommended next steps" list ordered by risk and action priority.
- Primary action to inspect scan results.

### 掃描結果

Keep this as the primary working view:

- Header action row for filter, export, and rescan.
- Segmented controls for all market/listed/OTC and market/holdings scope.
- Cache/source strip with hit rate, immediate fetch count, data timestamp, and next scan time.
- Tabs for announced reports, pending reports, and excluded items.
- One active result column.
- Each row shows code, name, market, industry, status badge, and rule chips.
- Rows can expand to show passed rules, missing/warning rules, and rule evidence.
- Pending and excluded states need intentional empty or guarded states rather than looking broken.

### 我的持股

Make exit-risk work obvious:

- Warning banner for holdings that trigger `X1-X5`.
- Add/update holding action.
- Explicit `X1-X5` holding scan action.
- Holding list with code/name, shares, cost, current price, unrealized P/L, status, triggered rule chips, edit/evidence/exit actions.
- `PARTIAL_HOLDING`, `WARNING`, and `EXIT` states must remain visually distinct.

### 資料與排程

Treat this as a compact operations console:

- Source health for TWSE listed data, TPEx OTC data, MOPS public info, and financial note fetching.
- Official universe totals, financial-industry exclusions, and included scan count.
- Historical/backfill coverage, insufficient-data count, partial-data count, and last backfill result.
- Cache job health, hit rate, key count/memory, next cleanup, and cache-clear action.
- Scheduler window for post-market scan, pre-market review, spring-festival guard, and next execution.
- Integration/report/backtest readout.

### 策略規則

Use this as read-only reference:

- Rule groups for `E1-E6`, `A1-A7`, `X1-X5`, `T3`, and guards.
- Rule code badges and threshold pills.
- Plain-language descriptions that explain evidence without altering thresholds.
- Mobile tables convert into bordered compact cards.

### 設定

Show settings as compact rows:

- Eight setting rows.
- Analyst-adjustable toggles for safe user preferences.
- Locked read-only rows for admin-only strategy, scheduler, data source/cache, and financial exclusion settings.
- Non-admin state must be visible without suggesting the user can change locked logic.

## Responsive Requirements

Target desktop and mobile browsers.

Required viewport checks:

- Desktop: `1366x900`
- Mobile: `390x844`
- Small mobile: `360x740`

Acceptance:

- `document.scrollingElement.scrollWidth` equals `clientWidth` on every core view.
- No visible element left/right boundary exceeds the viewport.
- No visible control, border, badge, chip, row, or card causes horizontal overflow.
- Buttons retain readable labels or compact icon-only treatment.
- Long labels wrap cleanly; no text overlaps adjacent controls.
- Mobile drawer opens without creating root horizontal overflow.

The OD prototype passed these checks on all six views and the mobile drawer:

- Views checked: overview, scan, holdings, data, rules, settings
- Viewports checked: `1366x900`, `390x844`, `360x740`
- Horizontal overflow: `0` for every checked view
- Boundary overflow count: `0` for every checked view
- Element scroll overflow count: `0` for every checked view

## Implementation Strategy

After user approval of this spec, create a detailed implementation plan before touching repo UI code.

Implementation should proceed conservatively:

1. Map OD visual primitives to existing frontend components and classes.
2. Introduce or consolidate CSS tokens for background, surface, text, borders, primary, and semantic status colors.
3. Restyle the existing app shell, navigation, top controls, cards, badges, chips, tabs, segmented controls, result rows, holding rows, forms, settings rows, and mobile breakpoints.
4. Adjust renderer markup only where existing DOM structure cannot express the approved layout.
5. Keep all fetch calls, state transitions, API shapes, rule labels, status values, persistence, and workflows unchanged.
6. Run frontend hygiene, size budget, lint, unit/API contract tests, e2e tests, and Playwright visual overflow checks.

## Verification Plan

Implementation is not complete until these pass:

- `python scripts\check_frontend_hygiene.py`
- `python scripts\check_code_size_budgets.py`
- `npm run lint`
- `python -m pytest tests\test_rules.py tests\test_frontend_parser.py tests\test_api_worker_contracts.py tests\test_cloudflare_worker.py -q`
- `npm run test:e2e`
- Playwright desktop/mobile visual overflow scan across the core views

## Open Questions

None for the current visual redesign. Future enhancements such as dark mode or result sorting are explicitly out of scope for this pass unless the user requests them separately.
