# Stock Scanner UI Redesign Implementation Plan

> **Archive notice (2026-07-16):** This is a historical implementation record, not current work authorization or agent guidance. Use the repository `AGENTS.md` and current user request; execute plan items only when explicitly requested.

**Goal:** Write the approved Open Design operational-dashboard UI back into the existing stock scanner frontend without changing strategy, workflow, data fetching, API/auth/cache/report behavior, or persistence.

**Architecture:** Keep the existing static frontend and all business-state modules. Add one visual override stylesheet loaded after `frontend/styles.css`, make small HTML structure updates for the shell and overview, and adjust only presentation-oriented renderers where current markup cannot express the approved rows/cards. Preserve every existing data/test selector used by e2e tests.

**Tech Stack:** Static HTML, vanilla CSS, vanilla browser JavaScript, existing FastAPI e2e server, Playwright, pytest, eslint.

## Global Constraints

- Do not change screening strategy, thresholds, rule evaluation, status mapping, or rule IDs.
- Do not change data collection, scraping, import, refresh, official data provider, MOPS/TWSE/TPEx behavior, cache invalidation, scheduler, or report/export workflows.
- Do not change FastAPI route contracts, Cloudflare Worker route contracts, D1/R2/session semantics, or localStorage fallback behavior.
- Do not change auth/session logic, holdings persistence, settings persistence, or server-backed state semantics.
- Preserve statuses: `ENTRY`, `WATCH`, `HOLD`, `ADD_WATCH`, `WARNING`, `EXIT`, `EXCLUDED`, `INSUFFICIENT_DATA`, `PARTIAL_DATA`, `PARTIAL_HOLDING`.
- Preserve rule groups: `E1-E6`, `A1-A7`, `X1-X5`, `T3`.
- Preserve guards: spring-festival guard, financial-industry exclusion, insufficient-data/partial-data guards.
- Preserve existing selectors used by tests: `#mobile-menu-toggle`, `#mobile-nav-backdrop`, `[data-view]`, `[data-view-panel]`, `[data-nav-group-toggle="scan"]`, `[data-market-column-nav]`, `[data-tab]`, `[data-market-disclosure-tab]`, `[data-market-result-toggle]`, `.rule-evidence`, `[data-evidence-width]`, `#overview-entry-count`, `#overview-watch-count`, `#overview-excluded-count`, `#refresh-market-scan-btn`, `#settings-status`, `[data-setting]`, `#manual-add-form`, `#holdings-list`, `#data-source-status`, `#scheduler-status`.
- Keep `frontend/app.js` at or below the budget enforced by `scripts/check_code_size_budgets.py` (`2010` lines).
- Keep `frontend/market_render.js` at or below `220` lines.
- Keep `frontend/renderers.js` at or below `300` lines.
- Keep `frontend/styles.css` unchanged except for deleting obsolete overrides if an implementer chooses to simplify. New visual rules belong in `frontend/ops_dashboard.css`.
- Do not use inline styles or inline event handlers; CSP must remain strict.
- Use escaped HTML through existing render helpers. Do not add `innerHTML =` assignments.
- Desktop and mobile acceptance: root horizontal overflow is `0`; visible element boundary overflow is `0`; visible element internal scroll overflow is `0`.

---

## File Structure

- Create: `frontend/ops_dashboard.css`
  - Owns the approved operational-dashboard visual system: tokens, app shell, navigation, top bar, cards, result rows, holdings, settings, data console, strategy rules, and responsive behavior.
- Create: `frontend/ops_status.js`
  - Owns small presentation-only text synchronization for the overview operations strip and compact top status strip.
- Create: `frontend/ops_view_renderers.js`
  - Owns presentation-only HTML templates for holdings rows and the data/scheduler console, keeping `frontend/app.js` inside its line budget.
- Create: `tests/e2e/visual-layout.spec.ts`
  - Owns Playwright checks for the new shell markers and no horizontal overflow across desktop/mobile views.
- Modify: `frontend/index.html`
  - Load `ops_dashboard.css`.
  - Add `data-ui-version="ops-dashboard"` to `<body>`.
  - Add navigation group labels, compact top status strip, overview operations status strip, and next-actions section while keeping existing selectors.
- Modify: `frontend/app.js`
  - Presentation-only wiring: call ops status and ops view renderer helpers.
  - Do not change fetch calls, state shape, persistence, auth, reports, scan functions, settings save semantics, or event wiring.
- Modify: `frontend/market_render.js`
  - Presentation-only updates to result row/detail/cache markup.
  - Keep exports and function signatures unchanged.
- Read-only reference: `docs/superpowers/specs/2026-06-22-stock-scanner-ui-redesign-design.md`
  - Source of approved visual constraints and validation requirements.
- Read-only reference: Open Design preview at `http://127.0.0.1:7456/api/projects/stock-scanner-interface-redesign/raw/index.html`
  - Visual target for layout density and responsive behavior.

## Task 1: Add Visual Layout Guardrails

**Files:**
- Create: `tests/e2e/visual-layout.spec.ts`
- Test: `tests/e2e/visual-layout.spec.ts`

**Interfaces:**
- Consumes: existing e2e server from `playwright.config.js`.
- Produces: a failing guard that requires the new `data-ui-version="ops-dashboard"` marker, `.top-status-strip`, `.ops-status-strip`, and `.ops-next-actions`.

- [ ] **Step 1: Write the failing visual layout test**

Create `tests/e2e/visual-layout.spec.ts` with this exact content:

```ts
import { expect, test } from "@playwright/test";

const coreViews = ["overview", "scan", "holdings", "data", "strategy", "settings"];

async function closeBlockingModals(page) {
  await page.locator("#auth-modal:not(.hidden)").waitFor({ state: "attached", timeout: 3000 }).catch(() => {});
  const authCloseButton = page.locator("#close-auth-modal-btn");
  if (await authCloseButton.isVisible()) await authCloseButton.click();
  await page.locator("#onboarding-modal:not(.hidden)").waitFor({ state: "attached", timeout: 500 }).catch(() => {});
  const onboardingDeferButton = page.locator("#defer-onboarding-btn");
  if (await onboardingDeferButton.isVisible()) await onboardingDeferButton.click();
}

async function openMobileNavIfNeeded(page, isMobile: boolean) {
  if (!isMobile) return;
  await page.locator("#mobile-menu-toggle").click();
}

async function showView(page, isMobile: boolean, view: string) {
  await openMobileNavIfNeeded(page, isMobile);
  if (isMobile && view === "scan") {
    await page.locator('[data-nav-group-toggle="scan"]').click();
    await page.locator('[data-market-column-nav="entry"]').click();
    return;
  }
  await page.locator(`[data-view="${view}"]`).click();
}

async function assertNoHorizontalOverflow(page) {
  const audit = await page.evaluate(() => {
    const root = document.scrollingElement || document.documentElement;
    const viewportWidth = document.documentElement.clientWidth;
    const isMobile = viewportWidth <= 680;
    const boundary: string[] = [];
    const scrollOverflow: string[] = [];

    for (const el of Array.from(document.querySelectorAll("body *"))) {
      if (!(el instanceof HTMLElement || el instanceof SVGElement)) continue;
      if (isMobile && el.closest(".app-sidebar") && !document.body.classList.contains("mobile-menu-open")) continue;
      if (el.id === "mobile-nav-backdrop") continue;
      const style = getComputedStyle(el);
      if (style.display === "none" || style.visibility === "hidden" || Number(style.opacity) === 0) continue;
      const rect = el.getBoundingClientRect();
      if (rect.width < 1 || rect.height < 1) continue;
      if (rect.left < -1 || rect.right > viewportWidth + 1) {
        boundary.push(`${el.tagName}.${String(el.className || "")} ${Math.round(rect.left)}:${Math.round(rect.right)}`);
      }
      if (el instanceof HTMLElement && el.clientWidth > 0 && el.scrollWidth > el.clientWidth + 2 && style.overflowX !== "visible") {
        scrollOverflow.push(`${el.tagName}.${String(el.className || "")} ${el.clientWidth}:${el.scrollWidth}`);
      }
    }

    return {
      overflow: root.scrollWidth - root.clientWidth,
      boundary,
      scrollOverflow,
    };
  });

  expect(audit.overflow, JSON.stringify(audit, null, 2)).toBe(0);
  expect(audit.boundary, JSON.stringify(audit, null, 2)).toEqual([]);
  expect(audit.scrollOverflow, JSON.stringify(audit, null, 2)).toEqual([]);
}

test("approved operations dashboard shell exists and core views do not overflow", async ({ page, isMobile }) => {
  await page.goto("/");
  await closeBlockingModals(page);

  await expect(page.locator('body[data-ui-version="ops-dashboard"]')).toBeVisible();
  await expect(page.locator(".top-status-strip")).toBeVisible();

  for (const view of coreViews) {
    await showView(page, isMobile, view);
    await expect(page.locator(`[data-view-panel="${view}"]`)).toBeVisible();
    await assertNoHorizontalOverflow(page);
  }

  await showView(page, isMobile, "overview");
  await expect(page.locator(".ops-status-strip")).toBeVisible();
  await expect(page.locator(".ops-next-actions")).toBeVisible();

  await showView(page, isMobile, "data");
  await expect(page.locator(".ops-console-grid")).toBeVisible();

  await showView(page, isMobile, "settings");
  await expect(page.locator(".settings-permission-note")).toBeVisible();
});

test("mobile drawer opens without creating root overflow", async ({ page, isMobile }) => {
  test.skip(!isMobile, "drawer is a mobile behavior");
  await page.goto("/");
  await closeBlockingModals(page);

  await page.locator("#mobile-menu-toggle").click();
  await expect(page.locator("#mobile-navigation")).toBeVisible();
  await assertNoHorizontalOverflow(page);
});
```

- [ ] **Step 2: Run the new test to verify it fails before implementation**

Run:

```bash
npx playwright test tests/e2e/visual-layout.spec.ts --project=chromium
```

Expected: FAIL because `body[data-ui-version="ops-dashboard"]`, `.top-status-strip`, `.ops-status-strip`, `.ops-next-actions`, and `.ops-console-grid` do not exist yet.

- [ ] **Step 3: Commit the failing guardrail test**

```bash
git add tests/e2e/visual-layout.spec.ts
git commit -m "test: add visual layout guardrails"
```

## Task 2: Add Operational App Shell And Visual Stylesheet

**Files:**
- Create: `frontend/ops_dashboard.css`
- Modify: `frontend/index.html`
- Test: `tests/e2e/visual-layout.spec.ts`

**Interfaces:**
- Consumes: existing app shell selectors and mobile navigation JS.
- Produces: `body[data-ui-version="ops-dashboard"]`, `.top-status-strip`, grouped nav labels, and base operational visual tokens.

- [ ] **Step 1: Load the new stylesheet and mark the body**

Modify `frontend/index.html` head and body:

```html
<link rel="preload" href="/styles.css?v=20260519-design-refresh" as="style" />
<link rel="stylesheet" href="/styles.css?v=20260519-design-refresh" />
<link rel="preload" href="/ops_dashboard.css?v=20260622-ops-dashboard" as="style" />
<link rel="stylesheet" href="/ops_dashboard.css?v=20260622-ops-dashboard" />
```

Change the body opener to:

```html
<body data-ui-version="ops-dashboard">
```

- [ ] **Step 2: Add navigation group labels without changing navigation selectors**

In `frontend/index.html`, inside `<nav class="side-nav">`, keep every existing button and data attribute. Add these labels around the current buttons:

```html
<div class="nav-group-label">營運</div>
<button class="nav-item active" type="button" data-view="overview">總覽</button>
<button class="nav-item" type="button" data-view="search">搜尋與單檔分析</button>
<button class="nav-item" type="button" data-view="holdings">我的持股</button>
<button id="scan-nav-toggle" class="nav-item nav-group-toggle" type="button" data-view="scan" data-nav-group-toggle="scan" aria-expanded="false" aria-controls="scan-nav-subitems">
  <span>掃描結果</span>
  <span class="nav-group-icon" aria-hidden="true">+</span>
</button>
<div id="scan-nav-subitems" class="nav-subitems nav-subitems-collapsible" aria-label="掃描結果分類">
  <button class="nav-subitem active" type="button" data-market-column-nav="entry">適合進場</button>
  <button class="nav-subitem" type="button" data-market-column-nav="watch">接近觀察</button>
  <button class="nav-subitem" type="button" data-market-column-nav="excluded">排除清單</button>
</div>
<div class="nav-group-label">系統</div>
<button id="admin-nav-item" class="nav-item hidden" type="button" data-view="admin">使用者管理</button>
<button class="nav-item" type="button" data-view="settings">設定</button>
<button class="nav-item" type="button" data-view="strategy">策略規則</button>
<button class="nav-item" type="button" data-view="data">資料與排程</button>
```

- [ ] **Step 3: Replace the top header status area with a compact status strip**

In `frontend/index.html`, inside `<header class="top-header">`, keep `#view-title`, `#view-subtitle`, `#open-onboarding-btn`, and `#account-panel`. Move `#api-status` into this strip inside the first header column:

```html
<div>
  <div class="top-status-strip" aria-label="系統狀態">
    <span id="api-status" class="status-pill neutral">連線中</span>
    <span id="top-source-status" class="status-pill neutral">資料源檢查中</span>
    <span id="top-cache-status" class="status-pill neutral">快取狀態待讀取</span>
    <span id="top-filing-status" class="status-pill neutral">申報窗待讀取</span>
  </div>
  <h2 id="view-title">總覽</h2>
  <p id="view-subtitle">快速查看資料來源、持股狀態與策略完成度。</p>
</div>
```

- [ ] **Step 4: Create the base visual stylesheet**

Create `frontend/ops_dashboard.css` with this starting content:

```css
:root {
  --ops-bg: #f6f7f9;
  --ops-surface: #ffffff;
  --ops-surface-soft: #fbfcfd;
  --ops-text: #1e2430;
  --ops-text-soft: #3a4250;
  --ops-muted: #697186;
  --ops-muted-2: #8a93a3;
  --ops-line: #e4e7ec;
  --ops-line-strong: #d3d8e0;
  --ops-primary: #2563c9;
  --ops-primary-soft: #eaf1fc;
  --ops-primary-ink: #1a4ea3;
  --ops-green: #1f9d57;
  --ops-green-bg: #e7f6ee;
  --ops-green-ink: #176e3e;
  --ops-amber: #c6841a;
  --ops-amber-bg: #fcf2df;
  --ops-amber-ink: #8a5a10;
  --ops-red: #d2483b;
  --ops-red-bg: #fcebe9;
  --ops-red-ink: #9a2f25;
  --ops-slate-bg: #eef0f3;
  --ops-slate-ink: #404a5c;
  --ops-radius: 8px;
  --ops-radius-sm: 6px;
  --ops-shadow: 0 1px 2px rgba(20, 28, 44, 0.04), 0 1px 1px rgba(20, 28, 44, 0.03);
  --ops-font-mono: "SF Mono", ui-monospace, "JetBrains Mono", Menlo, Consolas, monospace;
}

body[data-ui-version="ops-dashboard"] {
  background: var(--ops-bg);
  color: var(--ops-text);
  letter-spacing: 0;
}

body[data-ui-version="ops-dashboard"] .mobile-topbar,
body[data-ui-version="ops-dashboard"] .app-sidebar,
body[data-ui-version="ops-dashboard"] .top-header,
body[data-ui-version="ops-dashboard"] .panel,
body[data-ui-version="ops-dashboard"] .card,
body[data-ui-version="ops-dashboard"] .market-result-item,
body[data-ui-version="ops-dashboard"] .strategy-rule-card,
body[data-ui-version="ops-dashboard"] .admin-user-card {
  border-color: var(--ops-line);
  border-radius: var(--ops-radius);
  background: var(--ops-surface);
  box-shadow: var(--ops-shadow);
}

body[data-ui-version="ops-dashboard"] .app-shell {
  grid-template-columns: 236px minmax(0, 1fr);
  min-height: 100vh;
}

body[data-ui-version="ops-dashboard"] .app-sidebar {
  position: sticky;
  top: 0;
  height: 100vh;
  border-radius: 0;
  border-width: 0 1px 0 0;
  box-shadow: none;
}

body[data-ui-version="ops-dashboard"] .brand-block {
  padding: 16px 18px;
  border-bottom: 1px solid var(--ops-line);
}

body[data-ui-version="ops-dashboard"] .brand-block h1 {
  margin: 2px 0 4px;
  font-size: 16px;
  line-height: 1.25;
}

body[data-ui-version="ops-dashboard"] .brand-block p {
  margin: 0;
  font-size: 12px;
  color: var(--ops-muted);
}

body[data-ui-version="ops-dashboard"] .nav-group-label {
  padding: 14px 10px 6px;
  color: var(--ops-muted-2);
  font-size: 10.5px;
  font-weight: 700;
  letter-spacing: 0.08em;
}

body[data-ui-version="ops-dashboard"] .nav-item,
body[data-ui-version="ops-dashboard"] .nav-subitem {
  min-height: 38px;
  border-radius: var(--ops-radius-sm);
  color: var(--ops-text-soft);
  font-size: 13.5px;
}

body[data-ui-version="ops-dashboard"] .nav-item:hover,
body[data-ui-version="ops-dashboard"] .nav-item.active,
body[data-ui-version="ops-dashboard"] .nav-subitem:hover,
body[data-ui-version="ops-dashboard"] .nav-subitem.active {
  background: var(--ops-primary-soft);
  color: var(--ops-primary-ink);
}

body[data-ui-version="ops-dashboard"] .content-shell {
  width: 100%;
  max-width: 1240px;
  padding: 22px;
}

body[data-ui-version="ops-dashboard"] .top-header,
body[data-ui-version="ops-dashboard"].scan-view-active .top-header {
  position: sticky;
  top: 0;
  z-index: 20;
  grid-template-columns: minmax(0, 1fr) auto minmax(280px, auto);
  align-items: center;
  gap: 14px;
  margin: -22px -22px 18px;
  padding: 11px 22px;
  border-width: 0 0 1px;
  border-radius: 0;
  background: rgba(255, 255, 255, 0.9);
  backdrop-filter: saturate(160%) blur(8px);
  box-shadow: none;
}

body[data-ui-version="ops-dashboard"] .top-status-strip {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 7px;
  margin-bottom: 4px;
}

body[data-ui-version="ops-dashboard"] .top-header h2 {
  margin: 0;
  font-size: 16px;
  font-weight: 700;
}

body[data-ui-version="ops-dashboard"] #view-subtitle {
  margin: 1px 0 0;
  color: var(--ops-muted);
  font-size: 11.5px;
}

body[data-ui-version="ops-dashboard"] .status-pill {
  min-height: 24px;
  border-radius: 999px;
  padding: 3px 10px;
  font-size: 11.5px;
  font-weight: 700;
  white-space: nowrap;
}

body[data-ui-version="ops-dashboard"] .status-entry,
body[data-ui-version="ops-dashboard"] .status-hold,
body[data-ui-version="ops-dashboard"] .status-add_watch {
  background: var(--ops-green-bg);
  color: var(--ops-green-ink);
}

body[data-ui-version="ops-dashboard"] .status-watch,
body[data-ui-version="ops-dashboard"] .status-warning,
body[data-ui-version="ops-dashboard"] .status-insufficient_data,
body[data-ui-version="ops-dashboard"] .status-partial_data,
body[data-ui-version="ops-dashboard"] .status-partial_holding {
  background: var(--ops-primary-soft);
  color: var(--ops-primary-ink);
}

body[data-ui-version="ops-dashboard"] .status-exit,
body[data-ui-version="ops-dashboard"] .status-excluded {
  background: var(--ops-red-bg);
  color: var(--ops-red-ink);
}

body[data-ui-version="ops-dashboard"] .neutral {
  background: var(--ops-slate-bg);
  color: var(--ops-slate-ink);
}

body[data-ui-version="ops-dashboard"] .primary-btn {
  background: var(--ops-primary);
  border-color: var(--ops-primary);
}

body[data-ui-version="ops-dashboard"] .secondary-btn {
  background: var(--ops-primary-soft);
  color: var(--ops-primary-ink);
}

body[data-ui-version="ops-dashboard"] .ghost-btn,
body[data-ui-version="ops-dashboard"] .mini-btn {
  background: var(--ops-surface);
}

@media (max-width: 680px) {
  body[data-ui-version="ops-dashboard"] .app-shell {
    grid-template-columns: 1fr;
  }

  body[data-ui-version="ops-dashboard"] .content-shell {
    max-width: none;
    padding: 16px 14px;
  }

  body[data-ui-version="ops-dashboard"] .top-header,
  body[data-ui-version="ops-dashboard"].scan-view-active .top-header {
    position: static;
    grid-template-columns: 1fr;
    margin: 0 0 12px;
    padding: 0 0 12px;
    border-radius: 0;
    background: transparent;
  }

  body[data-ui-version="ops-dashboard"] .top-status-strip {
    display: none;
  }

  body[data-ui-version="ops-dashboard"] .app-sidebar {
    position: fixed;
    width: 280px;
    max-width: calc(100vw - 32px);
    transform: translateX(-100%);
    transition: transform 0.22s ease;
  }

  body[data-ui-version="ops-dashboard"].mobile-menu-open .app-sidebar {
    transform: translateX(0);
  }
}
```

- [ ] **Step 5: Run the guardrail test and confirm remaining expected failures**

Run:

```bash
npx playwright test tests/e2e/visual-layout.spec.ts --project=chromium
```

Expected: FAIL only on missing view-specific elements `.ops-status-strip`, `.ops-next-actions`, and `.ops-console-grid`. The body marker and top status strip should now pass.

- [ ] **Step 6: Commit the shell foundation**

```bash
git add frontend/index.html frontend/ops_dashboard.css
git commit -m "style: add operational dashboard shell"
```

## Task 3: Implement Overview Operations Dashboard

**Files:**
- Modify: `frontend/index.html`
- Modify: `frontend/app.js`
- Create: `frontend/ops_status.js`
- Modify: `frontend/ops_dashboard.css`
- Test: `tests/e2e/visual-layout.spec.ts`

**Interfaces:**
- Consumes: `state.marketScan`, `state.schedulerStatus`, `state.schedulerAutoScan`, `state.holdingsScan`, existing overview count IDs.
- Produces: `.ops-status-strip`, `.ops-next-actions`, and text updates through `renderOverviewStats()`.

- [ ] **Step 1: Add overview operational sections**

In `frontend/index.html`, inside `[data-view-panel="overview"]`, keep the existing `.summary-grid.kpi-strip` and IDs. Replace the current `.overview-copy-grid` with:

```html
<section class="panel ops-status-strip" aria-label="營運狀態">
  <div class="ops-strip-head">
    <h2>今日掃描總覽</h2>
    <p class="section-subtitle">本輪掃描、資料來源、快取、申報窗與持股風險。</p>
  </div>
  <div class="ops-strip-grid">
    <article class="ops-status-cell">
      <span class="ops-status-label">資料源</span>
      <strong id="overview-source-state">讀取中</strong>
      <small id="overview-source-note">等待資料狀態</small>
    </article>
    <article class="ops-status-cell">
      <span class="ops-status-label">快取</span>
      <strong id="overview-cache-state">待讀取</strong>
      <small id="overview-cache-note">等待掃描</small>
    </article>
    <article class="ops-status-cell">
      <span class="ops-status-label">申報窗</span>
      <strong id="overview-filing-state">待讀取</strong>
      <small id="overview-filing-note">等待掃描</small>
    </article>
    <article class="ops-status-cell">
      <span class="ops-status-label">持股風險</span>
      <strong id="overview-holding-risk-state">待掃描</strong>
      <small id="overview-holding-risk-note">套用 X1-X5 後更新</small>
    </article>
  </div>
</section>

<section class="panel ops-next-actions" aria-label="建議的下一步">
  <div class="section-heading">
    <div>
      <h2>建議的下一步</h2>
      <p class="section-subtitle">依風險與掃描結果排序。</p>
    </div>
  </div>
  <div class="ops-action-list">
    <button class="ops-action-row" type="button" data-view="holdings">
      <span class="status-dot status-warning" aria-hidden="true"></span>
      <span><strong>覆核持股警示</strong><small id="overview-action-holdings">套用 X1-X5 後檢查出場風險</small></span>
      <span class="ops-action-arrow">前往</span>
    </button>
    <button class="ops-action-row" type="button" data-view="scan">
      <span class="status-dot status-entry" aria-hidden="true"></span>
      <span><strong>檢視進場候選</strong><small id="overview-action-scan">查看 E1-E6 與 A1-A7 結果</small></span>
      <span class="ops-action-arrow">前往</span>
    </button>
    <button class="ops-action-row" type="button" data-view="data">
      <span class="status-dot neutral" aria-hidden="true"></span>
      <span><strong>確認資料缺口</strong><small id="overview-action-data">檢查 INSUFFICIENT_DATA 與 PARTIAL_DATA</small></span>
      <span class="ops-action-arrow">前往</span>
    </button>
  </div>
</section>
```

- [ ] **Step 2: Create the presentation status helper module**

Create `frontend/ops_status.js`:

```js
(function exposeOpsStatusHelpers(root, factory) {
  const helpers = factory();
  if (typeof module !== "undefined" && module.exports) {
    module.exports = helpers;
  }
  if (root) {
    root.StockScannerOpsStatus = helpers;
  }
})(typeof globalThis !== "undefined" ? globalThis : null, function createOpsStatusModule() {
  function createOpsStatus({ query, getState, holdingExitAlerts }) {
    const $ = query;

    function setText(selector, value) {
      const target = $(selector);
      if (target) target.textContent = value;
    }

    function renderOverviewOpsStatus(scan = null) {
      const state = getState();
      const marketScan = scan || state.marketScan;
      const provider = state.dataSourceStatus?.activeProvider || marketScan?.dataSource || "待讀取";
      const providerFlags = state.dataSourceStatus
        ? `${state.dataSourceStatus.activeProviderIsFullMarket ? "全市場" : "樣本"} · ${state.dataSourceStatus.activeProviderHasCompleteFundamentals ? "完整財報因子" : "部分財報因子"}`
        : "等待資料來源狀態";
      const cache = marketScan?.cacheStatus;
      const cacheState = cache ? (cache.cacheHit ? "命中快取" : "同步建立") : "待讀取";
      const cacheNote = cache ? `${cache.refreshStatus || "未請求更新"}${cache.isStale ? " · 已過期" : ""}` : "等待市場掃描";
      const filing = marketScan?.filingContext?.activeFinancialReport;
      const filingState = filing ? filing.label : "月營收窗口";
      const filingNote = filing
        ? `一般期限 ${filing.generalDeadline}${filing.financialDeadline ? ` · 金控 ${filing.financialDeadline}` : ""}`
        : `期間 ${marketScan?.filingContext?.monthlyRevenuePeriod || "待讀取"}`;
      const exitAlerts = holdingExitAlerts();
      const warningCount = exitAlerts.filter((item) => item.signal.status === "EXIT" || item.signal.status === "WARNING").length;
      const holdingState = state.holdings.length ? `${warningCount} 檔警示` : "尚無持股";
      const holdingNote = state.holdings.length ? `${state.holdings.length} 檔持股 · X1-X5 追蹤` : "新增持股後可掃描";

      setText("#overview-source-state", provider);
      setText("#overview-source-note", providerFlags);
      setText("#overview-cache-state", cacheState);
      setText("#overview-cache-note", cacheNote);
      setText("#overview-filing-state", filingState);
      setText("#overview-filing-note", filingNote);
      setText("#overview-holding-risk-state", holdingState);
      setText("#overview-holding-risk-note", holdingNote);
      setText("#top-source-status", `資料源 ${provider}`);
      setText("#top-cache-status", `快取 ${cacheState}`);
      setText("#top-filing-status", filingState);
      setText("#overview-action-holdings", warningCount ? `${warningCount} 檔需覆核` : "目前無高優先出場警示");
      setText("#overview-action-scan", marketScan ? "查看已公告/待公告分組" : "等待市場掃描完成");
      setText("#overview-action-data", state.dataSourceStatus ? "查看資料源與排程主控台" : "等待資料狀態讀取");
    }

    return { renderOverviewOpsStatus };
  }

  return { createOpsStatus };
});
```

- [ ] **Step 3: Load and wire `ops_status.js`**

In `frontend/index.html`, add this script before `app.js`:

```html
<script src="/ops_status.js?v=20260622-ops-dashboard" defer></script>
```

In `frontend/app.js`, add this helper reference near the other `_mod(...)` constants:

```js
const OPS_STATUS_HELPERS = _mod("StockScannerOpsStatus", "./ops_status.js");
```

After `holdingExitAlerts()` is defined, create the status helper:

```js
const { renderOverviewOpsStatus } = OPS_STATUS_HELPERS.createOpsStatus({
  query: $,
  getState: () => state,
  holdingExitAlerts,
});
```

At the end of `renderOverviewStats()`, add this call:

```js
renderOverviewOpsStatus(scan);
```

At the end of `loadDataStatus()`, after `renderDataAndScheduler();`, add:

```js
renderOverviewOpsStatus(state.marketScan);
```

- [ ] **Step 4: Wire overview action rows through existing navigation**

In `bindEvents()`, after the `.side-nav` listener, add this listener:

```js
const overviewActions = document.querySelector(".ops-next-actions");
if (overviewActions) {
  overviewActions.addEventListener("click", (event) => {
    const button = event.target.closest("[data-view]");
    if (!button || !overviewActions.contains(button)) return;
    showView(button.dataset.view);
  });
}
```

- [ ] **Step 5: Add overview CSS**

Append to `frontend/ops_dashboard.css`:

```css
body[data-ui-version="ops-dashboard"] .summary-grid.kpi-strip {
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 14px;
  margin-bottom: 14px;
}

body[data-ui-version="ops-dashboard"] .kpi-card {
  min-height: 130px;
  padding: 16px 18px;
}

body[data-ui-version="ops-dashboard"] .kpi-value strong {
  font-size: 30px;
}

body[data-ui-version="ops-dashboard"] .ops-status-strip {
  padding: 0;
  overflow: hidden;
  margin-bottom: 14px;
}

body[data-ui-version="ops-dashboard"] .ops-strip-head {
  display: flex;
  align-items: baseline;
  gap: 12px;
  padding: 13px 16px;
  border-bottom: 1px solid var(--ops-line);
}

body[data-ui-version="ops-dashboard"] .ops-strip-head h2 {
  margin: 0;
  font-size: 14px;
}

body[data-ui-version="ops-dashboard"] .ops-strip-grid {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
}

body[data-ui-version="ops-dashboard"] .ops-status-cell {
  padding: 14px 16px;
  border-right: 1px solid var(--ops-line);
}

body[data-ui-version="ops-dashboard"] .ops-status-cell:last-child {
  border-right: 0;
}

body[data-ui-version="ops-dashboard"] .ops-status-label {
  display: block;
  margin-bottom: 6px;
  color: var(--ops-muted);
  font-size: 11.5px;
}

body[data-ui-version="ops-dashboard"] .ops-status-cell strong {
  display: block;
  font-size: 14px;
}

body[data-ui-version="ops-dashboard"] .ops-status-cell small {
  display: block;
  margin-top: 3px;
  color: var(--ops-muted-2);
  font-size: 11px;
}

body[data-ui-version="ops-dashboard"] .ops-next-actions {
  padding: 0;
  overflow: hidden;
}

body[data-ui-version="ops-dashboard"] .ops-action-list {
  display: grid;
}

body[data-ui-version="ops-dashboard"] .ops-action-row {
  display: grid;
  grid-template-columns: 28px minmax(0, 1fr) auto;
  align-items: center;
  gap: 12px;
  width: 100%;
  border: 0;
  border-top: 1px solid var(--ops-line);
  background: transparent;
  padding: 14px 16px;
  color: inherit;
  text-align: left;
}

body[data-ui-version="ops-dashboard"] .ops-action-row:hover {
  background: var(--ops-surface-soft);
}

body[data-ui-version="ops-dashboard"] .ops-action-row strong,
body[data-ui-version="ops-dashboard"] .ops-action-row small {
  display: block;
}

body[data-ui-version="ops-dashboard"] .ops-action-row small {
  color: var(--ops-muted);
}

body[data-ui-version="ops-dashboard"] .ops-action-arrow {
  color: var(--ops-primary-ink);
  font-size: 12px;
  font-weight: 700;
}

@media (max-width: 1020px) {
  body[data-ui-version="ops-dashboard"] .summary-grid.kpi-strip,
  body[data-ui-version="ops-dashboard"] .ops-strip-grid {
    grid-template-columns: 1fr;
  }

  body[data-ui-version="ops-dashboard"] .ops-status-cell {
    border-right: 0;
    border-bottom: 1px solid var(--ops-line);
  }

  body[data-ui-version="ops-dashboard"] .ops-status-cell:last-child {
    border-bottom: 0;
  }
}

@media (max-width: 420px) {
  body[data-ui-version="ops-dashboard"] .ops-action-row {
    grid-template-columns: 24px minmax(0, 1fr);
  }

  body[data-ui-version="ops-dashboard"] .ops-action-arrow {
    grid-column: 2;
    width: fit-content;
  }
}
```

- [ ] **Step 6: Run the visual layout test**

Run:

```bash
npx playwright test tests/e2e/visual-layout.spec.ts --project=chromium
```

Expected: FAIL only on `.ops-console-grid` until Task 5. No overflow failures.

- [ ] **Step 7: Run the existing overview parser test**

Run:

```bash
python -m pytest tests\test_frontend_parser.py::test_overview_counts_follow_active_disclosure_tab -q
```

Expected: PASS. This verifies overview count behavior still follows the active disclosure tab.

- [ ] **Step 8: Commit overview dashboard changes**

```bash
git add frontend/index.html frontend/app.js frontend/ops_status.js frontend/ops_dashboard.css
git commit -m "style: add operational overview dashboard"
```

## Task 4: Restyle Market Scan Results Without Changing Scan Workflow

**Files:**
- Modify: `frontend/index.html`
- Modify: `frontend/market_render.js`
- Modify: `frontend/ops_dashboard.css`
- Test: `tests/e2e/smoke.spec.ts`
- Test: `tests/e2e/visual-layout.spec.ts`

**Interfaces:**
- Consumes: `renderMarketResultRow(result, options)`, `renderMarketResultDetails(result, options)`, `renderScanCacheStatus(scan)`.
- Produces: denser result rows with code/name, market/industry, status pill, rule chips, expandable evidence, and cache strip.

- [ ] **Step 1: Add scan action row class hooks**

In `frontend/index.html`, inside `[data-view-panel="scan"]`, change:

```html
<div class="report-actions" aria-label="匯出報告">
```

to:

```html
<div class="report-actions scan-action-row" aria-label="掃描操作與匯出報告">
```

Keep all four export button IDs and `#refresh-market-scan-btn` unchanged.

- [ ] **Step 2: Replace `renderMarketResultDetails()` presentation**

In `frontend/market_render.js`, replace the body of `renderMarketResultDetails` with:

```js
function renderMarketResultDetails(result, options = {}) {
  const reasons = sortRulesForDisplay(Array.isArray(result.reasons) ? result.reasons.filter(Boolean) : []);
  const { status: displayStatus, summary } = displayResultStatus(result, options.disclosureGroup);
  const passed = reasons.filter((rule) => rule?.passed && rule?.severity !== "INSUFFICIENT_DATA");
  const warnings = reasons.filter((rule) => !rule?.passed || rule?.severity === "INSUFFICIENT_DATA");
  const renderRuleGroup = (items, emptyText) =>
    items.length ? items.map(renderRule).join("") : `<p class="muted">${escapeHtml(emptyText)}</p>`;
  return `
    <div class="market-result-details">
      ${result.detailLoading ? `<p class="muted">正在載入完整細項...</p>` : ""}
      ${result.detailError ? `<p class="form-error">${escapeHtml(result.detailError)}</p>` : ""}
      <div class="detail-summary">
        <span class="status-pill ${statusClass(displayStatus)}">${escapeHtml(statusLabel(displayStatus))}</span>
        <p class="muted">${escapeHtml(summary)}</p>
      </div>
      <div class="market-evidence-grid">
        <section>
          <h4>通過規則</h4>
          <div class="rules">${renderRuleGroup(passed, "無通過項目")}</div>
        </section>
        <section>
          <h4>未達 / 警示</h4>
          <div class="rules">${renderRuleGroup(warnings, "無未達或警示項目")}</div>
        </section>
      </div>
      ${resultActionButtons(result, options)}
    </div>
  `;
}
```

- [ ] **Step 3: Replace `renderMarketResultRow()` presentation**

In `frontend/market_render.js`, replace the body of `renderMarketResultRow` with:

```js
function renderMarketResultRow(result, options = {}) {
  const state = getState();
  const companyName = result.companyName || safeCompanyName(result);
  const stockCode = safeText(result.stockCode, "未知代碼");
  const resultId = marketResultId(result, options.disclosureGroup, options.columnKey);
  const expanded = state.expandedMarketResultIds.has(resultId);
  const reasons = sortRulesForDisplay(Array.isArray(result.reasons) ? result.reasons.filter(Boolean) : []);
  const { status: displayStatus } = displayResultStatus(result, options.disclosureGroup);
  const industry = safeText(result.industryName || result.industry, "產業未填");
  const market = safeText(result.market, "市場未填");
  const rulePreview = reasons
    .slice(0, 4)
    .map((rule) => `<span class="rule-chip">${escapeHtml(rule.code || "")}</span>`)
    .join("");
  return `
    <article class="market-result-item ${expanded ? "expanded" : ""}" data-market-result-status="${escapeHtml(displayStatus)}">
      <button class="market-result-summary" type="button" data-market-result-toggle="${escapeHtml(resultId)}" aria-expanded="${expanded ? "true" : "false"}">
        <span class="market-result-code">
          <strong>${escapeHtml(stockCode)}</strong>
          <small>${escapeHtml(market)}</small>
        </span>
        <span class="market-result-body">
          <span class="market-result-name">${escapeHtml(companyName)}</span>
          <span class="market-result-meta">${escapeHtml(industry)}</span>
          <span class="market-rule-preview">${rulePreview}</span>
        </span>
        <span class="status-pill ${statusClass(displayStatus)}">${escapeHtml(statusLabel(displayStatus))}</span>
        <span class="market-expand-icon" aria-hidden="true">${expanded ? "−" : "+"}</span>
      </button>
      ${expanded ? renderMarketResultDetails(result, options) : ""}
    </article>
  `;
}
```

- [ ] **Step 4: Replace cache status markup with compact strip**

In `frontend/market_render.js`, replace the returned template inside `renderScanCacheStatus(scan = {})` with:

```js
return `
  <div class="cache-status-note ops-cache-strip">
    <span><strong>快取狀態</strong> ${cache.cacheHit ? "命中" : "新建"}</span>
    <span><strong>刷新</strong> ${escapeHtml(cacheRefreshLabel(cache.refreshStatus))}</span>
    <span><strong>資料時間</strong> ${escapeHtml(formatCacheTime(cache.storedAt))}</span>
    <span><strong>下次檢查</strong> ${escapeHtml(formatCacheTime(cache.nextRefreshAfter))}</span>
    <span>${escapeHtml(staleText)}</span>
  </div>
`;
```

- [ ] **Step 5: Add scan CSS**

Append to `frontend/ops_dashboard.css`:

```css
body[data-ui-version="ops-dashboard"] .scan-action-row,
body[data-ui-version="ops-dashboard"] .results-heading-actions {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: flex-end;
  gap: 8px;
}

body[data-ui-version="ops-dashboard"] .ops-cache-strip {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 12px;
  border-color: var(--ops-line);
  background: var(--ops-surface-soft);
  color: var(--ops-text-soft);
}

body[data-ui-version="ops-dashboard"] .market-disclosure-tabs {
  border-bottom: 1px solid var(--ops-line);
  padding: 0 8px;
}

body[data-ui-version="ops-dashboard"] .market-disclosure-tabs .tab {
  min-height: 40px;
  border-radius: 0;
  border-bottom: 2px solid transparent;
  background: transparent;
  color: var(--ops-muted);
}

body[data-ui-version="ops-dashboard"] .market-disclosure-tabs .tab.active {
  border-bottom-color: var(--ops-primary);
  background: transparent;
  color: var(--ops-primary-ink);
}

body[data-ui-version="ops-dashboard"] .result-column {
  overflow: hidden;
}

body[data-ui-version="ops-dashboard"] .result-column-head {
  border-bottom: 1px solid var(--ops-line);
}

body[data-ui-version="ops-dashboard"] .market-result-list {
  gap: 0;
}

body[data-ui-version="ops-dashboard"] .market-result-item {
  border-radius: 0;
  border-width: 0 0 1px;
  box-shadow: none;
}

body[data-ui-version="ops-dashboard"] .market-result-summary {
  display: grid;
  grid-template-columns: 126px minmax(0, 1fr) auto 34px;
  gap: 14px;
  align-items: center;
  width: 100%;
  min-height: 72px;
  padding: 13px 16px;
}

body[data-ui-version="ops-dashboard"] .market-result-code strong,
body[data-ui-version="ops-dashboard"] .market-result-code small,
body[data-ui-version="ops-dashboard"] .market-result-name,
body[data-ui-version="ops-dashboard"] .market-result-meta {
  display: block;
}

body[data-ui-version="ops-dashboard"] .market-result-code strong {
  font-family: var(--ops-font-mono);
  font-size: 14px;
}

body[data-ui-version="ops-dashboard"] .market-result-code small,
body[data-ui-version="ops-dashboard"] .market-result-meta {
  color: var(--ops-muted);
  font-size: 11.5px;
}

body[data-ui-version="ops-dashboard"] .market-result-name {
  font-size: 14px;
  font-weight: 700;
}

body[data-ui-version="ops-dashboard"] .market-rule-preview {
  display: flex;
  flex-wrap: wrap;
  gap: 5px;
  margin-top: 5px;
}

body[data-ui-version="ops-dashboard"] .rule-chip {
  border: 1px solid #d6e3f5;
  border-radius: 5px;
  background: var(--ops-primary-soft);
  color: var(--ops-primary-ink);
  padding: 2px 7px;
  font-family: var(--ops-font-mono);
  font-size: 11.5px;
}

body[data-ui-version="ops-dashboard"] .market-result-details {
  border-top: 1px solid var(--ops-line);
  background: var(--ops-surface-soft);
  padding: 14px 16px 16px;
}

body[data-ui-version="ops-dashboard"] .market-evidence-grid {
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
  gap: 12px;
}

body[data-ui-version="ops-dashboard"] .market-evidence-grid h4 {
  margin: 0 0 8px;
  color: var(--ops-muted-2);
  font-size: 11px;
  letter-spacing: 0.06em;
}

@media (max-width: 680px) {
  body[data-ui-version="ops-dashboard"] .scan-action-row,
  body[data-ui-version="ops-dashboard"] .results-heading-actions {
    justify-content: flex-start;
  }

  body[data-ui-version="ops-dashboard"] .market-result-summary {
    grid-template-columns: minmax(0, 1fr) 34px;
    grid-template-areas:
      "code icon"
      "body icon"
      "status icon";
    gap: 8px 12px;
  }

  body[data-ui-version="ops-dashboard"] .market-result-code {
    grid-area: code;
    display: flex;
    gap: 8px;
    align-items: baseline;
  }

  body[data-ui-version="ops-dashboard"] .market-result-body {
    grid-area: body;
  }

  body[data-ui-version="ops-dashboard"] .market-result-summary > .status-pill {
    grid-area: status;
    width: fit-content;
  }

  body[data-ui-version="ops-dashboard"] .market-expand-icon {
    grid-area: icon;
  }

  body[data-ui-version="ops-dashboard"] .market-evidence-grid {
    grid-template-columns: 1fr;
  }
}
```

- [ ] **Step 6: Verify scan behavior still uses POST only for manual refresh**

Run:

```bash
npx playwright test tests/e2e/smoke.spec.ts --grep "manual refresh button forces a POST scan"
```

Expected: PASS.

- [ ] **Step 7: Verify result evidence still expands**

Run:

```bash
npx playwright test tests/e2e/smoke.spec.ts --grep "market results expose evidence"
```

Expected: PASS.

- [ ] **Step 8: Run visual guardrail test**

Run:

```bash
npx playwright test tests/e2e/visual-layout.spec.ts
```

Expected: FAIL only on `.ops-console-grid` until Task 5. No overflow failures.

- [ ] **Step 9: Commit market scan presentation**

```bash
git add frontend/index.html frontend/market_render.js frontend/ops_dashboard.css
git commit -m "style: redesign market scan results"
```

## Task 5: Redesign Holdings, Data Console, Settings, And Strategy Reference

**Files:**
- Modify: `frontend/app.js`
- Create: `frontend/ops_view_renderers.js`
- Modify: `frontend/index.html`
- Modify: `frontend/ops_dashboard.css`
- Test: `tests/e2e/smoke.spec.ts`
- Test: `tests/e2e/visual-layout.spec.ts`

**Interfaces:**
- Consumes: existing holdings state, settings state, app status payload, scheduler payload, strategy content.
- Produces: denser holdings rows, `.ops-console-grid`, `.settings-permission-note`, read-only/admin visibility, and compact strategy reference cards.

- [ ] **Step 1: Add settings permission note and data console class hooks**

In `frontend/index.html`, inside `[data-view-panel="settings"]`, after the `.section-heading`, add:

```html
<div class="settings-permission-note" aria-live="polite">
  <strong>權限狀態</strong>
  <span>非管理員登入時，策略、排程、資料源與快取設定維持唯讀。</span>
</div>
```

Change the data status grid opener from:

```html
<div id="data-source-status" class="data-status-grid"></div>
```

to:

```html
<div id="data-source-status" class="data-status-grid ops-console-grid"></div>
```

- [ ] **Step 2: Create view renderer module for holdings and data console**

Create `frontend/ops_view_renderers.js`:

```js
(function exposeOpsViewRenderers(root, factory) {
  const helpers = factory();
  if (typeof module !== "undefined" && module.exports) {
    module.exports = helpers;
  }
  if (root) {
    root.StockScannerOpsViewRenderers = helpers;
  }
})(typeof globalThis !== "undefined" ? globalThis : null, function createOpsViewRenderersModule() {
  function createOpsViewRenderers({ escapeHtml, safeCompanyName, renderHoldingSignal, displayResultStatus }) {
    function normalizedShares(holding = {}) {
      const shares = Number(holding.shares);
      return Number.isFinite(shares) ? Math.max(0, Math.floor(shares)) : 0;
    }

    function normalizedCost(holding = {}) {
      if (holding.averageCost === "" || holding.averageCost === null || holding.averageCost === undefined) return null;
      const cost = Number(holding.averageCost);
      return Number.isFinite(cost) ? Math.max(0, cost) : null;
    }

    function renderHoldingCard({ holding, isEditing, analysis, missing }) {
      const name = safeCompanyName(holding);
      const stockCode = holding.stockCode || "未知代碼";
      const shares = normalizedShares(holding);
      const averageCost = normalizedCost(holding);
      const xStatus = analysis ? displayResultStatus(analysis, "holding").status : missing ? "待補" : "未掃描";
      return `
        <article class="card holding-card" data-holding-code="${escapeHtml(stockCode)}">
          <div class="holding-row-main">
            <div class="holding-id">
              <h3 class="stock-title">${escapeHtml(stockCode)}</h3>
              <p class="muted">${escapeHtml(name)}</p>
              ${renderHoldingSignal(analysis, missing)}
            </div>
            <dl class="holding-metrics">
              <div><dt>持有股數</dt><dd>${escapeHtml(shares)}</dd></div>
              <div><dt>平均成本</dt><dd>${escapeHtml(averageCost ?? "未填")}</dd></div>
              <div><dt>X1-X5</dt><dd>${escapeHtml(xStatus)}</dd></div>
            </dl>
            <div class="button-row compact-actions">
              ${
                isEditing
                  ? `<button class="ghost-btn" type="button" data-action="cancel-edit">取消</button>`
                  : `<button class="secondary-btn" type="button" data-action="edit">編輯</button>`
              }
              <button class="danger-btn" type="button" data-action="delete">刪除</button>
            </div>
          </div>
          ${
            isEditing
              ? `
          <div class="holding-edit">
            <div class="field">
              <label>目前股數</label>
              <input type="number" min="0" step="1" data-field="shares" value="${escapeHtml(shares)}" aria-label="目前股數" />
              <span class="field-help">修改後按「儲存修改」。</span>
            </div>
            <div class="field">
              <label>平均成本</label>
              <input type="number" min="0" step="0.01" data-field="averageCost" value="${escapeHtml(averageCost ?? "")}" aria-label="平均成本" />
              <span class="field-help">選填，可留空。</span>
            </div>
            <div class="field">
              <label>減碼股數</label>
              <input type="number" min="0" step="1" data-field="reduce" placeholder="例如：500" aria-label="減碼股數" />
              <span class="field-help">只在按「減碼」時使用。</span>
            </div>
          </div>
          <div class="button-row">
            <button class="primary-btn" type="button" data-action="save">儲存修改</button>
            <button class="secondary-btn" type="button" data-action="reduce">減碼</button>
          </div>
        `
              : ""
          }
        </article>
      `;
    }

    function renderDataConsole({ status, integrationStatus, backtestStatus }) {
      const configuredNotifications = (integrationStatus?.notifications || []).filter((item) => item.configured).length;
      return `
        <article class="card ops-console-card">
          <div class="ops-card-head"><h3>資料源健康</h3><span class="status-pill status-entry">全部正常</span></div>
          <dl class="ops-kv">
            <div><dt>啟用資料源</dt><dd>${escapeHtml(status.activeProvider)}</dd></div>
            <div><dt>Realtime</dt><dd>${status.activeProviderIsRealtime ? "是" : "否"}</dd></div>
            <div><dt>全市場 universe</dt><dd>${status.activeProviderIsFullMarket ? "是" : "否"}</dd></div>
            <div><dt>完整財報因子</dt><dd>${status.activeProviderHasCompleteFundamentals ? "是" : "否"}</dd></div>
          </dl>
        </article>
        <article class="card ops-console-card">
          <div class="ops-card-head"><h3>官方股票池</h3><span class="muted">universe 同步</span></div>
          <dl class="ops-kv">
            <div><dt>Mock Universe</dt><dd>${escapeHtml(status.mockUniverseSize)}</dd></div>
            <div><dt>官方 Universe</dt><dd>${escapeHtml(status.officialUniverseSize ?? "尚未啟用")}</dd></div>
            <div><dt>月營收快照</dt><dd>${escapeHtml(status.officialMonthlySnapshotSize ?? "尚未啟用")}</dd></div>
            <div><dt>官方歷史快取</dt><dd>${escapeHtml(status.officialHistoryRows ?? 0)}</dd></div>
          </dl>
        </article>
        <article class="card ops-console-card">
          <div class="ops-card-head"><h3>歷史與回補</h3><span class="status-pill neutral">待缺口回補</span></div>
          <dl class="ops-kv">
            <div><dt>最新季損益</dt><dd>${escapeHtml(status.officialIncomeStatementSize ?? "尚未啟用")}</dd></div>
            <div><dt>資產負債</dt><dd>${escapeHtml(status.officialBalanceSheetSize ?? "尚未啟用")}</dd></div>
            <div><dt>估值</dt><dd>${escapeHtml(status.officialValuationSize ?? "尚未啟用")}</dd></div>
            <div><dt>財報匯入</dt><dd>${escapeHtml(status.fundamentalsImportRows ?? 0)}</dd></div>
          </dl>
          <p class="muted">${escapeHtml(status.officialHistoricalFundamentals?.note || "缺口會列為待補，不誤判進出場。")}</p>
        </article>
        <article class="card ops-console-card">
          <div class="ops-card-head"><h3>整合與回測</h3><span class="muted">唯讀</span></div>
          <dl class="ops-kv">
            <div><dt>通知整合</dt><dd>${escapeHtml(configuredNotifications)} 個已設定</dd></div>
            <div><dt>券商同步</dt><dd>${integrationStatus?.broker?.configured ? "已設定" : "未設定"}</dd></div>
            <div><dt>AI 摘要</dt><dd>${integrationStatus?.aiSummary?.configured ? "已設定" : "未設定"}</dd></div>
            <div><dt>回測交易數</dt><dd>${escapeHtml(backtestStatus?.metrics?.tradeCount ?? 0)}</dd></div>
          </dl>
          <p class="muted">${escapeHtml(backtestStatus?.note || "匯入 data/backtest_history.csv 後可模擬進出場與績效。")}</p>
        </article>
      `;
    }

    function renderSchedulerStatus({ schedulerStatus, autoAction }) {
      return `
        <div class="ops-scheduler-strip">
          <strong>排程狀態：${escapeHtml(schedulerStatus.status)}</strong>
          <span>事件：${escapeHtml((schedulerStatus.events || []).join("、") || "無")}</span>
          <span>下一交易日：${escapeHtml(schedulerStatus.nextTradingDay)}</span>
          <span>自動掃描：${escapeHtml(autoAction)}</span>
        </div>
      `;
    }

    return { renderDataConsole, renderHoldingCard, renderSchedulerStatus };
  }

  return { createOpsViewRenderers };
});
```

- [ ] **Step 3: Load and wire `ops_view_renderers.js`**

In `frontend/index.html`, add this script before `app.js` and after `ops_status.js`:

```html
<script src="/ops_view_renderers.js?v=20260622-ops-dashboard" defer></script>
```

In `frontend/app.js`, add this helper reference near the other `_mod(...)` constants:

```js
const OPS_VIEW_RENDERER_HELPERS = _mod("StockScannerOpsViewRenderers", "./ops_view_renderers.js");
```

After renderer dependencies are initialized, create the view renderers:

```js
const { renderDataConsole, renderHoldingCard, renderSchedulerStatus } = OPS_VIEW_RENDERER_HELPERS.createOpsViewRenderers({
  escapeHtml,
  safeCompanyName,
  renderHoldingSignal,
  displayResultStatus,
});
```

In `renderHoldings()`, replace the `.map((holding) => { ... })` template body with:

```js
.map((holding) => {
  const stockCode = safeText(holding.stockCode, "未知代碼");
  return renderHoldingCard({
    holding: { ...holding, stockCode },
    isEditing: state.editingHoldingCode === stockCode,
    analysis: holdingScanResultByCode(stockCode),
    missing: holdingScanMissingByCode(stockCode),
  });
})
```

- [ ] **Step 4: Replace data/scheduler presentation with renderer calls**

In `frontend/app.js`, inside `renderDataAndScheduler()`, replace the `setSafeHtml(dataTarget, ...)` body for `state.dataSourceStatus` with:

```js
setSafeHtml(dataTarget, renderDataConsole({
  status,
  integrationStatus: state.integrationStatus,
  backtestStatus: state.backtestStatus,
}));
```

Keep the `schedulerTarget` update in the same function, but change its success template to:

```js
setSafeHtml(schedulerTarget, renderSchedulerStatus({
  schedulerStatus: state.schedulerStatus,
  autoAction,
}));
```

- [ ] **Step 5: Add holdings/data/settings/strategy CSS**

Append to `frontend/ops_dashboard.css`:

```css
body[data-ui-version="ops-dashboard"] .holding-form {
  border: 1px solid var(--ops-line);
  border-radius: var(--ops-radius);
  background: var(--ops-surface-soft);
}

body[data-ui-version="ops-dashboard"] .holding-card {
  padding: 0;
  overflow: hidden;
}

body[data-ui-version="ops-dashboard"] .holding-row-main {
  display: grid;
  grid-template-columns: 150px minmax(0, 1fr) auto;
  align-items: center;
  gap: 14px;
  padding: 14px 16px;
}

body[data-ui-version="ops-dashboard"] .holding-id .stock-title {
  margin: 0;
}

body[data-ui-version="ops-dashboard"] .holding-metrics {
  display: flex;
  flex-wrap: wrap;
  gap: 18px;
  margin: 0;
}

body[data-ui-version="ops-dashboard"] .holding-metrics div,
body[data-ui-version="ops-dashboard"] .ops-kv div {
  display: grid;
  gap: 2px;
}

body[data-ui-version="ops-dashboard"] .holding-metrics dt,
body[data-ui-version="ops-dashboard"] .ops-kv dt {
  color: var(--ops-muted);
  font-size: 11px;
}

body[data-ui-version="ops-dashboard"] .holding-metrics dd,
body[data-ui-version="ops-dashboard"] .ops-kv dd {
  margin: 0;
  font-family: var(--ops-font-mono);
  font-weight: 700;
}

body[data-ui-version="ops-dashboard"] .ops-console-grid {
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 14px;
}

body[data-ui-version="ops-dashboard"] .ops-console-card {
  padding: 0;
  overflow: hidden;
}

body[data-ui-version="ops-dashboard"] .ops-card-head {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 13px 16px;
  border-bottom: 1px solid var(--ops-line);
}

body[data-ui-version="ops-dashboard"] .ops-card-head h3 {
  margin: 0;
  font-size: 13.5px;
}

body[data-ui-version="ops-dashboard"] .ops-card-head > :last-child {
  margin-left: auto;
}

body[data-ui-version="ops-dashboard"] .ops-kv {
  display: grid;
  gap: 0;
  margin: 0;
  padding: 14px 16px;
}

body[data-ui-version="ops-dashboard"] .ops-kv div {
  grid-template-columns: minmax(0, 1fr) auto;
  align-items: center;
  padding: 10px 0;
  border-bottom: 1px solid var(--ops-line);
}

body[data-ui-version="ops-dashboard"] .ops-kv div:last-child {
  border-bottom: 0;
}

body[data-ui-version="ops-dashboard"] .ops-scheduler-strip,
body[data-ui-version="ops-dashboard"] .settings-permission-note {
  display: flex;
  flex-wrap: wrap;
  gap: 10px 14px;
  align-items: center;
  border: 1px solid var(--ops-line);
  border-radius: var(--ops-radius);
  background: var(--ops-surface);
  padding: 12px 14px;
}

body[data-ui-version="ops-dashboard"] .settings-permission-note {
  margin-bottom: 14px;
  background: var(--ops-primary-soft);
  color: var(--ops-primary-ink);
}

body[data-ui-version="ops-dashboard"] .settings-grid {
  grid-template-columns: 1fr;
  gap: 0;
  border: 1px solid var(--ops-line);
  border-radius: var(--ops-radius);
  background: var(--ops-surface);
  overflow: hidden;
}

body[data-ui-version="ops-dashboard"] .settings-grid label {
  border: 0;
  border-bottom: 1px solid var(--ops-line);
  border-radius: 0;
  background: var(--ops-surface);
}

body[data-ui-version="ops-dashboard"] .settings-grid label:last-child {
  border-bottom: 0;
}

body[data-ui-version="ops-dashboard"] .strategy-rule-grid {
  grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));
  gap: 14px;
}

body[data-ui-version="ops-dashboard"] .strategy-rule-card {
  padding: 0;
  overflow: hidden;
}

body[data-ui-version="ops-dashboard"] .strategy-rule-card > div:first-child {
  padding: 14px 16px;
  border-bottom: 1px solid var(--ops-line);
}

body[data-ui-version="ops-dashboard"] .strategy-rule-list {
  padding: 0;
}

body[data-ui-version="ops-dashboard"] .strategy-rule-item {
  padding: 12px 16px;
}

@media (max-width: 900px) {
  body[data-ui-version="ops-dashboard"] .holding-row-main,
  body[data-ui-version="ops-dashboard"] .ops-console-grid {
    grid-template-columns: 1fr;
  }
}

@media (max-width: 420px) {
  body[data-ui-version="ops-dashboard"] .holding-metrics,
  body[data-ui-version="ops-dashboard"] .compact-actions {
    gap: 10px;
  }

  body[data-ui-version="ops-dashboard"] .ops-kv div {
    grid-template-columns: 1fr;
  }
}
```

- [ ] **Step 6: Run visual layout test**

Run:

```bash
npx playwright test tests/e2e/visual-layout.spec.ts
```

Expected: PASS for desktop and mobile.

- [ ] **Step 7: Run holdings/settings/data e2e checks**

Run:

```bash
npx playwright test tests/e2e/smoke.spec.ts --grep "holdings and mobile navigation render|saved holdings surface|settings stay read-only|super user can save settings"
```

Expected: PASS. This confirms holdings persistence, X1-X5 alert surfacing, settings permissions, and data status still work.

- [ ] **Step 8: Run frontend parser tests**

Run:

```bash
python -m pytest tests\test_frontend_parser.py -q
```

Expected: PASS. This confirms render helpers remain safe and overview counts still match disclosure grouping.

- [ ] **Step 9: Commit secondary views**

```bash
git add frontend/index.html frontend/app.js frontend/ops_view_renderers.js frontend/ops_dashboard.css
git commit -m "style: redesign holdings data settings and strategy views"
```

## Task 6: Full Verification And Visual QA

**Files:**
- Modify: `frontend/ops_dashboard.css` only for final responsive polish found during verification.
- Test: all verification commands below.

**Interfaces:**
- Consumes: all previous task outputs.
- Produces: verified UI redesign ready for final review.

- [ ] **Step 1: Run frontend hygiene check**

Run:

```bash
python scripts\check_frontend_hygiene.py
```

Expected: exit code `0`; `innerHTMLAssignments` remains `0`; no dangerous HTML sinks.

- [ ] **Step 2: Run code size budget check**

Run:

```bash
python scripts\check_code_size_budgets.py
```

Expected: exit code `0`. Existing budgeted files stay within limits.

- [ ] **Step 3: Run lint**

Run:

```bash
npm run lint
```

Expected: exit code `0`.

- [ ] **Step 4: Run business/API contract tests**

Run:

```bash
python -m pytest tests\test_rules.py tests\test_frontend_parser.py tests\test_api_worker_contracts.py tests\test_cloudflare_worker.py -q
```

Expected: exit code `0`. These protect the strategy, parser, API/Worker contract, and Cloudflare behavior.

- [ ] **Step 5: Run all e2e tests**

Run:

```bash
npm run test:e2e
```

Expected: exit code `0` for desktop Chromium and mobile Chrome projects.

- [ ] **Step 6: Capture manual visual screenshots for review**

Run this Node script from the repo root:

```bash
node - <<'NODE'
const { chromium } = require('@playwright/test');
const fs = require('fs');
const path = require('path');

const outDir = path.resolve('.tmp/implementation-visual-scan');
fs.mkdirSync(outDir, { recursive: true });
const url = 'http://127.0.0.1:8010/';
const viewports = [
  { name: 'desktop', width: 1366, height: 900 },
  { name: 'mobile390', width: 390, height: 844 },
  { name: 'mobile360', width: 360, height: 740 },
];
const views = ['overview', 'scan', 'holdings', 'data', 'strategy', 'settings'];

async function audit(page) {
  return page.evaluate(() => {
    const root = document.scrollingElement || document.documentElement;
    const viewportWidth = document.documentElement.clientWidth;
    const boundary = [];
    const scrollOverflow = [];
    for (const el of Array.from(document.querySelectorAll('body *'))) {
      if (!(el instanceof HTMLElement || el instanceof SVGElement)) continue;
      if (viewportWidth <= 680 && el.closest('.app-sidebar') && !document.body.classList.contains('mobile-menu-open')) continue;
      const style = getComputedStyle(el);
      if (style.display === 'none' || style.visibility === 'hidden' || Number(style.opacity) === 0) continue;
      const rect = el.getBoundingClientRect();
      if (rect.width < 1 || rect.height < 1) continue;
      if (rect.left < -1 || rect.right > viewportWidth + 1) {
        boundary.push({ tag: el.tagName, cls: String(el.className || ''), left: Math.round(rect.left), right: Math.round(rect.right) });
      }
      if (el instanceof HTMLElement && el.clientWidth > 0 && el.scrollWidth > el.clientWidth + 2 && style.overflowX !== 'visible') {
        scrollOverflow.push({ tag: el.tagName, cls: String(el.className || ''), clientWidth: el.clientWidth, scrollWidth: el.scrollWidth });
      }
    }
    return { overflow: root.scrollWidth - root.clientWidth, boundary, scrollOverflow };
  });
}

(async () => {
  const browser = await chromium.launch();
  const results = [];
  for (const vp of viewports) {
    const page = await browser.newPage({ viewport: { width: vp.width, height: vp.height } });
    await page.goto(url, { waitUntil: 'networkidle' });
    await page.locator('#auth-modal:not(.hidden)').waitFor({ state: 'attached', timeout: 3000 }).catch(() => {});
    if (await page.locator('#close-auth-modal-btn').isVisible()) await page.locator('#close-auth-modal-btn').click();
    if (await page.locator('#defer-onboarding-btn').isVisible()) await page.locator('#defer-onboarding-btn').click();
    for (const view of views) {
      await page.evaluate((viewName) => window.showView(viewName), view);
      await page.waitForTimeout(200);
      results.push({ viewport: vp.name, view, ...(await audit(page)) });
      if (['overview', 'scan', 'holdings', 'data'].includes(view)) {
        await page.screenshot({ path: path.join(outDir, `${vp.name}-${view}.png`), fullPage: true });
      }
    }
    await page.close();
  }
  await browser.close();
  fs.writeFileSync(path.join(outDir, 'audit.json'), JSON.stringify(results, null, 2));
  const failures = results.filter((item) => item.overflow !== 0 || item.boundary.length || item.scrollOverflow.length);
  console.table(results.map((item) => ({
    viewport: item.viewport,
    view: item.view,
    overflow: item.overflow,
    boundary: item.boundary.length,
    scrollOverflow: item.scrollOverflow.length,
  })));
  if (failures.length) {
    console.error(JSON.stringify(failures, null, 2));
    process.exit(1);
  }
})();
NODE
```

Expected: `overflow`, `boundary`, and `scrollOverflow` are `0` for every view/viewport, and screenshots are saved under `.tmp/implementation-visual-scan/`.

- [ ] **Step 7: Inspect representative screenshots**

Open these local images:

```text
.tmp/implementation-visual-scan/desktop-overview.png
.tmp/implementation-visual-scan/desktop-scan.png
.tmp/implementation-visual-scan/mobile390-overview.png
.tmp/implementation-visual-scan/mobile390-scan.png
.tmp/implementation-visual-scan/mobile390-holdings.png
.tmp/implementation-visual-scan/mobile390-data.png
```

Expected: visual hierarchy matches the approved Open Design direction; no text, button, chip, card border, or status pill exceeds the viewport.

- [ ] **Step 8: Commit verification polish**

If Step 6 or Step 7 required CSS corrections, commit them:

```bash
git add frontend/ops_dashboard.css
git commit -m "style: polish dashboard responsive layout"
```

If no corrections were required, do not create an empty commit.
