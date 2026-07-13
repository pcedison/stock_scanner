import { expect, test } from "@playwright/test";

test.beforeEach(async ({ page }) => {
  await page.addInitScript(() => {
    const config = globalThis.StockScannerConfig || {};
    globalThis.StockScannerConfig = { ...config, apiMode: "fallback" };
  });
});

async function closeBlockingModals(page) {
  await page
    .locator("#auth-modal:not(.hidden)")
    .waitFor({ state: "attached", timeout: 3000 })
    .catch(() => {});
  const authCloseButton = page.locator("#close-auth-modal-btn");
  if (await authCloseButton.isVisible()) await authCloseButton.click();
  await page
    .locator("#onboarding-modal:not(.hidden)")
    .waitFor({ state: "attached", timeout: 500 })
    .catch(() => {});
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

async function showMarketColumn(page, isMobile: boolean, column: string) {
  await openMobileNavIfNeeded(page, isMobile);
  await page.locator(`[data-market-column-nav="${column}"]`).click();
}

async function registerViaApi(page, username: string, password = "test-password-123") {
  await page.evaluate(
    async ({ username, password }) => {
      const response = await fetch("/api/auth/register", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "same-origin",
        body: JSON.stringify({ username, password }),
      });
      if (response.ok) return;
      if (response.status === 400) {
        const login = await fetch("/api/auth/login", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          credentials: "same-origin",
          body: JSON.stringify({ username, password }),
        });
        if (login.ok) return;
        throw new Error(`login fallback failed: ${login.status}`);
      }
      throw new Error(`register failed: ${response.status}`);
    },
    { username, password },
  );
}

function withActiveOfficialQuarter(item: any, activePeriod: string, overrides: Record<string, unknown> = {}) {
  const reasons = (item.reasons || []).filter((reason: any) => reason?.code !== "OFFICIAL_Q");
  reasons.push({
    code: "OFFICIAL_Q",
    passed: true,
    severity: "INFO",
    message: `${activePeriod} 官方財報已公告`,
  });
  return { ...item, ...overrides, reasons };
}

test("overview auto-refreshes market counts on load and after login", async ({ page }) => {
  const scanRequests: { method: string; body: any }[] = [];
  await page.route("**/api/scan/market", async (route) => {
    const request = route.request();
    let body: any = null;
    if (request.method() === "POST") {
      try {
        body = request.postDataJSON();
      } catch {
        body = {};
      }
    }
    scanRequests.push({ method: request.method(), body });
    await route.continue();
  });

  await page.goto("/");
  await closeBlockingModals(page);
  await expect(page.locator("#overview-entry-count")).not.toHaveText("--", { timeout: 15_000 });
  await expect(page.locator("#overview-watch-count")).not.toHaveText("--", { timeout: 15_000 });
  await expect(page.locator("#overview-excluded-count")).not.toHaveText("--", { timeout: 15_000 });
  // Passive/auto loads use the cacheable GET; only an explicit force refresh POSTs.
  const autoCountBeforeLogin = scanRequests.filter((r) => r.method === "GET").length;
  expect(autoCountBeforeLogin).toBeGreaterThan(0);
  expect(scanRequests.some((r) => r.method === "POST" && r.body?.refreshMode === "force")).toBeFalsy();

  const overviewCounts = await page
    .locator("#overview-entry-count, #overview-watch-count, #overview-excluded-count")
    .allTextContents();
  const navCounts = await page
    .locator("[data-market-column-nav]")
    .evaluateAll((buttons) => buttons.map((button) => (button.textContent || "").match(/\((\d+)\)/)?.[1] || ""));
  expect(overviewCounts).toEqual(navCounts);

  await page.locator("#open-onboarding-btn").click();
  await expect(page.locator("#auth-modal")).toBeVisible();
  await expect(page.locator("#auth-modal-username")).toBeFocused();
  await page.locator("#auth-modal-username").fill(`overview-${Date.now()}@example.com`);
  await page.locator("#auth-modal-password").fill("test-password-123");
  await page.locator("#auth-modal-form").evaluate((form: HTMLFormElement) => form.requestSubmit());
  await expect.poll(() => scanRequests.filter((r) => r.method === "GET").length).toBeGreaterThan(autoCountBeforeLogin);
  expect(scanRequests.some((r) => r.method === "POST" && r.body?.refreshMode === "force")).toBeFalsy();
});

test("manual refresh button forces a POST scan", async ({ page, isMobile }) => {
  const scanRequests: { method: string; body: any }[] = [];
  await page.route("**/api/scan/market", async (route) => {
    const request = route.request();
    let body: any = null;
    if (request.method() === "POST") {
      try {
        body = request.postDataJSON();
      } catch {
        body = {};
      }
    }
    scanRequests.push({ method: request.method(), body });
    await route.continue();
  });

  await page.goto("/");
  await closeBlockingModals(page);
  await showView(page, isMobile, "scan");
  await page.locator("#refresh-market-scan-btn").click();
  await expect
    .poll(() => scanRequests.some((r) => r.method === "POST" && r.body?.refreshMode === "force"))
    .toBeTruthy();
});

test("manual refresh failure preserves last successful market scan", async ({ page, isMobile }) => {
  await page.goto("/");
  await closeBlockingModals(page);
  await showView(page, isMobile, "scan");

  const successfulScan = await page.evaluate(async () => (await fetch("/api/scan/market")).json());
  const visiblePeriod =
    successfulScan.filingContext?.freshnessFinancialReport?.period ||
    successfulScan.filingContext?.activeFinancialReport?.period;
  expect(visiblePeriod).toBeTruthy();
  expect(successfulScan.entry?.length).toBeGreaterThan(0);
  const template = successfulScan.entry[0];
  const lastGoodScan = {
    ...successfulScan,
    generatedAt: "2026-07-12T01:00:00+00:00",
    entry: Array.from({ length: 7 }, (_, index) =>
      withActiveOfficialQuarter(template, visiblePeriod, {
        stockCode: String(9100 + index),
        companyName: `保留測試公司 ${index + 1}`,
        detailsAvailable: false,
        hasFullDetails: true,
      }),
    ),
  };

  let fail = false;
  await page.route("**/api/scan/market", async (route) => {
    if (route.request().method() !== "POST") {
      await route.continue();
      return;
    }
    if (fail) {
      await route.fulfill({
        status: 503,
        contentType: "application/json",
        body: JSON.stringify({
          detail: "不應顯示的上游機密錯誤",
          code: "DEPENDENCY_UNAVAILABLE",
          requestId: "test-request-id",
          retryable: true,
          stage: "r2_read",
        }),
      });
      return;
    }
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(lastGoodScan),
    });
  });

  await page.locator("#refresh-market-scan-btn").click();
  await expect(page.locator(".market-result-code strong").filter({ hasText: "9100" })).toHaveCount(1);
  await page
    .locator('[data-market-page-tab="announced"][data-market-page-column="entry"][data-market-page-dir="1"]')
    .click();
  await expect(page.locator(".market-pagination")).toContainText("第 2 / 2 頁");
  const toggle = page.locator("[data-market-result-toggle]").first();
  await toggle.click();
  await expect(page.locator("[data-market-result-toggle]").first()).toHaveAttribute("aria-expanded", "true");
  await expect(page.locator(".market-result-details").first()).toBeVisible();

  const oldTime = await page.locator("#scan-time").textContent();
  const oldFirstRow = await page.locator("#market-results .market-result-item").first().textContent();
  const oldPage = await page.locator(".market-pagination").textContent();
  const oldExpandedId = await page
    .locator("[data-market-result-toggle]")
    .first()
    .getAttribute("data-market-result-toggle");
  const oldExpandedContent = await page.locator(".market-result-details").first().textContent();

  fail = true;
  await page.locator("#refresh-market-scan-btn").click();
  const warning = page.locator(".market-scan-warning");
  await expect(warning).toBeVisible();
  await expect(warning).toHaveAttribute("role", "status");
  await expect(warning).toContainText("伺服器暫時無法處理請求");
  await expect(warning).toContainText("test-request-id");
  await expect(warning).not.toContainText("上游機密錯誤");
  await expect(page.locator("#scan-time")).toHaveText(oldTime || "");
  await expect(page.locator("#market-results .market-result-item").first()).toContainText(oldFirstRow || "");
  await expect(page.locator(".market-pagination")).toHaveText(oldPage || "");
  await expect(page.locator("[data-market-result-toggle]").first()).toHaveAttribute(
    "data-market-result-toggle",
    oldExpandedId || "",
  );
  await expect(page.locator("[data-market-result-toggle]").first()).toHaveAttribute("aria-expanded", "true");
  await expect(page.locator(".market-result-details").first()).toContainText(oldExpandedContent || "");
  await expect(page.locator("#market-results > .form-error")).toHaveCount(0);

  await showMarketColumn(page, isMobile, "watch");
  await showMarketColumn(page, isMobile, "entry");
  await expect(page.locator(".market-pagination")).toContainText("第 2 / 2 頁");
  await expect(warning).toBeVisible();
  expect(await warning.evaluate((element) => element.parentElement?.firstElementChild === element)).toBe(true);
  if (isMobile) {
    expect(
      await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth),
    ).toBe(true);
    await expect(warning).toBeInViewport();
  }
});

test("successful market refresh clears last-good warning", async ({ page, isMobile }) => {
  await page.goto("/");
  await closeBlockingModals(page);
  await showView(page, isMobile, "scan");
  const successfulScan = await page.evaluate(async () => (await fetch("/api/scan/market")).json());
  const visiblePeriod =
    successfulScan.filingContext?.freshnessFinancialReport?.period ||
    successfulScan.filingContext?.activeFinancialReport?.period;
  expect(visiblePeriod).toBeTruthy();
  expect(successfulScan.entry?.length).toBeGreaterThan(0);
  successfulScan.entry = successfulScan.entry.map((item: any, index: number) =>
    index === 0 ? withActiveOfficialQuarter(item, visiblePeriod, { stockCode: "2454", companyName: "聯發科" }) : item,
  );
  successfulScan.generatedAt = "2026-07-12T02:00:00+00:00";

  let fail = false;
  await page.route("**/api/scan/market", async (route) => {
    if (route.request().method() !== "POST") {
      await route.continue();
      return;
    }
    if (fail) {
      await route.fulfill({
        status: 503,
        contentType: "application/json",
        body: JSON.stringify({ detail: "不應顯示的上游錯誤", requestId: "retry-id" }),
      });
      return;
    }
    await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(successfulScan) });
  });

  await page.locator("#refresh-market-scan-btn").click();
  await expect(page.locator(".market-result-code strong").filter({ hasText: "2454" })).toHaveCount(1);
  fail = true;
  await page.locator("#refresh-market-scan-btn").click();
  await expect(page.locator(".market-scan-warning")).toHaveCount(1);
  await expect(page.locator(".market-scan-warning")).toContainText("retry-id");
  fail = false;
  await page.locator("#refresh-market-scan-btn").click();
  await expect(page.locator(".market-scan-warning")).toHaveCount(0);
  await expect(page.locator(".market-result-code strong").filter({ hasText: "2454" })).toHaveCount(1);
});

test("settings stay read-only for non-admin users and CSP is strict", async ({ page, isMobile }) => {
  const response = await page.goto("/");
  expect(response?.headers()["content-security-policy"]).not.toContain("unsafe-inline");
  await closeBlockingModals(page);

  await showView(page, isMobile, "settings");
  await expect(page.locator('[data-view-panel="settings"]')).toBeVisible();
  await expect(page.locator("#settings-status")).toContainText(/需要登入管理員|需要管理員/);
  await expect(page.locator('[data-setting="manual_scan_enabled"]')).toBeDisabled();

  const username = `qa-${Date.now()}@example.com`;
  await registerViaApi(page, username);
  await page.reload();
  await closeBlockingModals(page);
  await showView(page, isMobile, "settings");
  await expect(page.locator("#settings-status")).toContainText("需要管理員");
  await expect(page.locator('[data-setting="manual_scan_enabled"]')).toBeDisabled();
});

test("holdings and mobile navigation render without layout blockers", async ({ page, isMobile }) => {
  await page.goto("/");
  await closeBlockingModals(page);

  await showView(page, isMobile, "holdings");
  await page.locator("#manual-stock-input").fill("2357 100 500");
  await page.locator("#manual-add-form").evaluate((form: HTMLFormElement) => form.requestSubmit());
  await expect(page.locator("#holdings-list")).toContainText("2357");

  await showView(page, isMobile, "data");
  await expect(page.locator("#data-source-status")).toBeVisible();
});

test("localStorage SecurityError stays fail-soft through browser initialization", async ({ page }) => {
  const pageErrors: string[] = [];
  page.on("pageerror", (error) => pageErrors.push(`${error.name}:${error.message}`));
  await page.addInitScript(() => {
    Object.defineProperty(globalThis, "localStorage", {
      configurable: true,
      get() {
        throw new DOMException("blocked localStorage", "SecurityError");
      },
    });
  });
  await page.goto("/");
  await closeBlockingModals(page);
  await expect(page.locator("#overview-entry-count")).not.toHaveText("--", { timeout: 15_000 });
  expect(pageErrors.filter((message) => /SecurityError|localStorage/.test(message))).toEqual([]);
});

test("localStorage quota failures do not block initialization or holding saves", async ({ page, isMobile }) => {
  const pageErrors: string[] = [];
  page.on("pageerror", (error) => pageErrors.push(`${error.name}:${error.message}`));
  await page.addInitScript(() => {
    Storage.prototype.setItem = function setItem() {
      throw new DOMException("quota", "QuotaExceededError");
    };
  });
  await page.goto("/");
  await closeBlockingModals(page);
  await expect(page.locator("#overview-entry-count")).not.toHaveText("--", { timeout: 15_000 });
  await showView(page, isMobile, "holdings");
  await page.locator("#manual-stock-input").fill("2357 100 500");
  await page.locator("#manual-add-form").evaluate((form: HTMLFormElement) => form.requestSubmit());
  await expect(page.locator("#holdings-list")).toContainText("2357");
  expect(pageErrors.filter((message) => /QuotaExceededError|quota/.test(message))).toEqual([]);
});

test("saved holdings surface X1-X5 exit alerts", async ({ page, isMobile }) => {
  await page.goto("/");
  await closeBlockingModals(page);

  await showView(page, isMobile, "holdings");
  await page.locator("#manual-stock-input").fill("3008 100 2000");
  await page.locator("#manual-add-form").evaluate((form: HTMLFormElement) => form.requestSubmit());

  const alerts = page.locator("#holding-exit-alerts");
  await expect(alerts).toBeVisible({ timeout: 10_000 });
  await expect(alerts).toContainText("3008");
  await expect(alerts).toContainText(/X4|X5|出場/);

  await page.locator("[data-open-holding-alert-details]").click();
  await expect(page.locator("#holding-results")).toBeVisible();
  await expect(page.locator("#holding-results")).toContainText("3008");
  await expect(page.locator("#holding-results")).toContainText(/X4|X5|出場/);
});

test("super user can save settings and cache status is visible", async ({ page, isMobile }) => {
  await page.goto("/");
  await closeBlockingModals(page);
  await registerViaApi(page, "test-admin@example.com");
  await page.reload();
  await closeBlockingModals(page);

  await showView(page, isMobile, "settings");
  const manualScan = page.locator('[data-setting="manual_scan_enabled"]');
  await expect(manualScan).toBeEnabled();
  const before = await manualScan.isChecked();
  await manualScan.setChecked(!before);
  await expect(page.locator("#settings-status")).toContainText(/已儲存|已同步/);
  await manualScan.setChecked(before);
  await expect(page.locator("#settings-status")).toContainText(/已儲存|已同步/);

  await showView(page, isMobile, "data");
  await expect(page.locator("#data-source-status")).toContainText(/MockDataProvider|資料來源|Mock/);
  await expect(page.locator("#scheduler-status")).toBeVisible();
});

test("login failures are throttled", async ({ page }) => {
  await page.goto("/");
  await closeBlockingModals(page);
  const username = `locked-${Date.now()}@example.com`;
  await registerViaApi(page, username);

  const status = await page.evaluate(async (username) => {
    await fetch("/api/auth/logout", { method: "POST", credentials: "same-origin" });
    const attempts = [];
    for (let index = 0; index < 5; index += 1) {
      const response = await fetch("/api/auth/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "same-origin",
        body: JSON.stringify({ username, password: "wrong-password" }),
      });
      attempts.push(response.status);
    }
    const locked = await fetch("/api/auth/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      credentials: "same-origin",
      body: JSON.stringify({ username, password: "test-password-123" }),
    });
    return { attempts, lockedStatus: locked.status, retryAfter: locked.headers.get("retry-after") };
  }, username);

  expect(status.attempts).toEqual([401, 401, 401, 401, 401]);
  expect(status.lockedStatus).toBe(429);
  expect(Number(status.retryAfter)).toBeGreaterThan(0);
});

test("authenticated holdings survive reload and market results expose evidence", async ({ page, isMobile }) => {
  await page.goto("/");
  await closeBlockingModals(page);
  await registerViaApi(page, `holdings-${Date.now()}@example.com`);
  await page.evaluate(async () => {
    const response = await fetch("/api/me/holdings", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      credentials: "same-origin",
      body: JSON.stringify({ holdings: [{ stockCode: "2357", name: "ASUS", shares: 100, averageCost: 500 }] }),
    });
    if (!response.ok) throw new Error(`holdings failed: ${response.status}`);
  });
  await page.reload();
  await closeBlockingModals(page);
  await showView(page, isMobile, "holdings");
  await expect(page.locator("#holdings-list")).toContainText("2357");

  await showView(page, isMobile, "scan");
  await page.locator('[data-tab="market"]').click();
  await expect(page.locator("#market-results")).toContainText(/2357|市場|掃描|Mock/);
  // The announced/pending split tracks the live filing-deadline calendar, so the
  // bucket that actually holds results shifts with the run date (e.g. nothing is
  // "pending" once a quarter's filing window has closed). Find the first
  // expandable result across the disclosure tabs and columns instead of assuming
  // a fixed bucket, then verify its rule evidence renders.
  let expanded = false;
  for (const tab of ["announced", "pending"]) {
    await page.locator(`[data-market-disclosure-tab="${tab}"]`).click();
    for (const column of ["entry", "watch", "excluded"]) {
      await showMarketColumn(page, isMobile, column);
      const toggle = page.locator("[data-market-result-toggle]").first();
      if ((await toggle.count()) > 0 && (await toggle.isVisible())) {
        await toggle.click();
        expanded = true;
        break;
      }
    }
    if (expanded) break;
  }
  expect(expanded).toBe(true);
  await expect(page.locator(".rule-evidence").first()).toBeVisible();
  await expect(page.locator("[data-evidence-width]").first()).toBeVisible();
});

const MARKET_V2_GENERATION = "a".repeat(24);

function marketV2Index(entry = 205, watch = 3, generationId = MARKET_V2_GENERATION) {
  const pages = (category: string, count: number) =>
    Array.from({ length: Math.ceil(count / 100) }, (_, page) => {
      const cursor = page * 100;
      return {
        key: `public/market_scan/v2/${generationId}/announced/${category}/${cursor}.json`,
        cursor,
        count: Math.min(100, count - cursor),
        bytes: 256,
        sha256: "0".repeat(64),
      };
    });
  const bucket = (category: string, count: number, disclosure = "announced") => ({
    count,
    pages: disclosure === "announced" ? pages(category, count) : [],
  });
  return {
    schemaVersion: 2,
    generationId,
    generatedAt: "2026-07-13T01:02:03+00:00",
    pageSize: 100,
    detailMode: "summary",
    disclosurePeriod: "2026Q1",
    filingContext: { freshnessFinancialReport: { period: "2026Q1" } },
    financialFreshness: { latestCachedFinancialPeriod: "2026Q1" },
    cacheStatusInputs: { latestFinancialPeriod: "2026Q1" },
    counts: {
      universeSize: entry + watch,
      announced: entry + watch,
      pending: 0,
      categories: { entry, watch, excluded: 0 },
    },
    disclosures: {
      announced: {
        count: entry + watch,
        entry: bucket("entry", entry),
        watch: bucket("watch", watch),
        excluded: bucket("excluded", 0),
      },
      pending: {
        count: 0,
        entry: bucket("entry", 0, "pending"),
        watch: bucket("watch", 0, "pending"),
        excluded: bucket("excluded", 0, "pending"),
      },
    },
    cacheStatus: { cacheHit: true, isStale: false, storedAt: "2026-07-13T01:02:03+00:00", refreshStatus: "fresh" },
  };
}

function marketV2Page(index: any, category: "entry" | "watch" | "excluded", cursor: number) {
  const total = index.disclosures.announced[category].count;
  const count = Math.max(0, Math.min(100, total - cursor));
  const base = category === "entry" ? 1000 : 2000;
  return {
    schemaVersion: 2,
    generationId: index.generationId,
    disclosure: "announced",
    category,
    cursor,
    limit: 100,
    total,
    nextCursor: cursor + count < total ? cursor + count : null,
    items: Array.from({ length: count }, (_, offset) => ({
      stockCode: category === "entry" && cursor + offset === 101 ? "2357" : String(base + cursor + offset),
      companyName: `V2 ${category} ${cursor + offset}`,
      status: category === "entry" ? "ENTRY" : "WATCH",
      summary: "paged result",
      reasons: [],
      detailsAvailable: false,
      hasFullDetails: true,
    })),
  };
}

async function useMarketV2(page) {
  await page.addInitScript(() => {
    const config = globalThis.StockScannerConfig || {};
    globalThis.StockScannerConfig = { ...config, marketApiVersion: "v2" };
  });
}

test("paged market and market index lazy load bounded windows", async ({ page, isMobile }) => {
  await useMarketV2(page);
  const index = marketV2Index();
  const resultRequests: { category: string; cursor: number }[] = [];
  let legacyCalls = 0;
  let reportCalls = 0;
  await page.route("**/api/scan/market/index", (route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(index) }),
  );
  await page.route("**/api/scan/market/results?*", (route) => {
    const url = new URL(route.request().url());
    const category = url.searchParams.get("category") as "entry" | "watch" | "excluded";
    const cursor = Number(url.searchParams.get("cursor"));
    resultRequests.push({ category, cursor });
    return route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(marketV2Page(index, category, cursor)),
    });
  });
  await page.route("**/api/scan/market", (route) => {
    legacyCalls += 1;
    return route.fulfill({ status: 500, contentType: "application/json", body: "{}" });
  });
  await page.route("**/api/reports/market?*", (route) => {
    reportCalls += 1;
    return route.fulfill({ status: 200, contentType: "text/csv", body: "stockCode\n1000\n" });
  });

  await page.goto("/");
  await closeBlockingModals(page);
  await showView(page, isMobile, "scan");
  await expect(page.locator(".market-result-code strong").first()).toHaveText("1000");
  await expect(page.locator('[data-market-column-nav="entry"]')).toContainText("205");
  expect(resultRequests).toEqual([{ category: "entry", cursor: 0 }]);
  expect(legacyCalls).toBe(0);

  const next = page.locator('[data-market-page-column="entry"][data-market-page-dir="1"]');
  for (let pageNumber = 0; pageNumber < 16; pageNumber += 1) await next.click();
  await expect(page.locator(".market-result-code strong").first()).toHaveText("1096");
  await expect(page.locator(".market-result-code strong").last()).toHaveText("2357");
  expect(resultRequests.filter((request) => request.category === "entry").map((request) => request.cursor)).toEqual([
    0, 100,
  ]);

  await showMarketColumn(page, isMobile, "watch");
  await expect(page.locator(".market-result-code strong").first()).toHaveText("2000");
  await showMarketColumn(page, isMobile, "excluded");
  await expect(page.locator("#market-results .empty-state")).toBeVisible();
  expect(resultRequests.some((request) => request.category === "excluded")).toBe(false);

  const beforeExport = resultRequests.length;
  await page.locator("#export-market-csv-btn").click();
  await expect.poll(() => reportCalls).toBe(1);
  expect(resultRequests).toHaveLength(beforeExport);

  await showMarketColumn(page, isMobile, "entry");
  await page.locator('[data-market-result-toggle*="2357"]').click();
  const add = page.locator('[data-add-from-result="2357"]');
  await expect(add).toHaveCount(1);
  await add.click();
  await expect(page.locator('[data-add-from-result="2357"]')).toHaveCount(0);
});

test("last-known-good shell keeps validated market index offline", async ({ page, isMobile }) => {
  await useMarketV2(page);
  const index = marketV2Index(12, 0);
  await page.addInitScript((storedIndex) => {
    localStorage.setItem("tw_stock_scanner.market_index.v2", JSON.stringify(storedIndex));
  }, index);
  await page.route("**/api/scan/market/index", (route) =>
    route.fulfill({ status: 503, contentType: "application/json", body: JSON.stringify({ detail: "offline" }) }),
  );
  await page.route("**/api/scan/market/results?*", (route) =>
    route.fulfill({ status: 503, contentType: "application/json", body: JSON.stringify({ detail: "offline page" }) }),
  );

  await page.goto("/");
  await closeBlockingModals(page);
  await showView(page, isMobile, "scan");
  await expect(page.locator('[data-market-column-nav="entry"]')).toContainText("12");
  await expect(page.locator("#market-results")).toContainText("2026Q1");
  await expect(page.locator(".market-scan-warning")).toBeVisible();
  await expect(page.locator("#market-results .form-error")).toBeVisible();
});

test("legacy market fallback uses v1 route only", async ({ page }) => {
  let legacyCalls = 0;
  let indexCalls = 0;
  await page.route("**/api/scan/market/index", (route) => {
    indexCalls += 1;
    return route.fulfill({ status: 500, contentType: "application/json", body: "{}" });
  });
  await page.route("**/api/scan/market", async (route) => {
    legacyCalls += 1;
    return route.continue();
  });
  await page.goto("/");
  await closeBlockingModals(page);
  await expect(page.locator("#overview-entry-count")).not.toHaveText("--", { timeout: 15_000 });
  expect(legacyCalls).toBeGreaterThan(0);
  expect(indexCalls).toBe(0);
});

test("refresh command posts once and refresh polling preserves rows until generation changes", async ({
  page,
  isMobile,
}) => {
  await useMarketV2(page);
  const firstIndex = marketV2Index(205, 0, "a".repeat(24));
  const nextIndex = marketV2Index(12, 0, "b".repeat(24));
  let activeIndex = firstIndex;
  const jobId = "c".repeat(32);
  const statusUrl = `/api/scan/market/refresh/${jobId}`;
  const commandRequests: { method: string; key: string | null }[] = [];
  const statusRequests: string[] = [];
  let legacyCalls = 0;
  let polls = 0;

  await page.route("**/api/scan/market/index", (route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(activeIndex) }),
  );
  await page.route("**/api/scan/market/results?*", (route) => {
    const url = new URL(route.request().url());
    const category = url.searchParams.get("category") as "entry" | "watch" | "excluded";
    const cursor = Number(url.searchParams.get("cursor"));
    return route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(marketV2Page(activeIndex, category, cursor)),
    });
  });
  await page.route("**/api/scan/market/refresh", (route) => {
    commandRequests.push({
      method: route.request().method(),
      key: route.request().headers()["idempotency-key"] || null,
    });
    return route.fulfill({
      status: 202,
      contentType: "application/json",
      headers: { Location: statusUrl },
      body: JSON.stringify({ jobId, status: "queued", requestId: "e2e-refresh-1", statusUrl }),
    });
  });
  await page.route("**/api/scan/market", (route) => {
    legacyCalls += 1;
    return route.fulfill({ status: 500, contentType: "application/json", body: "{}" });
  });
  await page.route(`**${statusUrl}`, (route) => {
    statusRequests.push(new URL(route.request().url()).pathname);
    polls += 1;
    if (polls >= 2) activeIndex = nextIndex;
    return route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ jobId, status: polls >= 2 ? "success" : "running", hasError: false }),
    });
  });

  await page.goto("/");
  await closeBlockingModals(page);
  await showView(page, isMobile, "scan");
  await expect(page.locator(".market-result-code strong").first()).toHaveText("1000");
  await page.locator('[data-market-page-column="entry"][data-market-page-dir="1"]').click();
  await expect(page.locator(".market-result-code strong").first()).toHaveText("1006");
  await page.locator("[data-market-result-toggle]").first().click();
  await expect(page.locator("[data-market-result-toggle]").first()).toHaveAttribute("aria-expanded", "true");

  await page.locator("#refresh-market-scan-btn").click();
  await page.locator("#refresh-market-scan-btn").click();
  await expect.poll(() => commandRequests.length).toBe(1);
  await expect(page.locator(".market-result-code strong").first()).toHaveText("1006");
  await expect(page.locator("[data-market-result-toggle]").first()).toHaveAttribute("aria-expanded", "true");
  await expect.poll(() => polls, { timeout: 8_000 }).toBe(2);
  await expect(page.locator('[data-market-column-nav="entry"]')).toContainText("12");
  await expect(page.locator(".market-result-code strong").first()).toHaveText("1000");

  expect(commandRequests[0].method).toBe("POST");
  expect(commandRequests[0].key).toMatch(/^[A-Za-z0-9._:-]{1,80}$/);
  expect(statusRequests).toEqual([statusUrl, statusUrl]);
  expect(legacyCalls).toBe(0);
});
