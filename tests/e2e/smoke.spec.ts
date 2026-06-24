import { expect, test } from "@playwright/test";

test.beforeEach(async ({ page }) => {
  await page.addInitScript(() => {
    const config = globalThis.StockScannerConfig || {};
    globalThis.StockScannerConfig = { ...config, apiMode: "fallback" };
  });
});

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

async function showMarketColumn(page, isMobile: boolean, column: string) {
  await openMobileNavIfNeeded(page, isMobile);
  await page.locator(`[data-market-column-nav="${column}"]`).click();
}

async function registerViaApi(page, username: string, password = "test-password-123") {
  await page.evaluate(async ({ username, password }) => {
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
  }, { username, password });
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
  const navCounts = await page.locator("[data-market-column-nav]").evaluateAll((buttons) =>
    buttons.map((button) => (button.textContent || "").match(/\((\d+)\)/)?.[1] || ""),
  );
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
