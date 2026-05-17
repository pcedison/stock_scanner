import { expect, test } from "@playwright/test";

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
  await page.locator(`[data-view="${view}"]`).click();
}

test("settings stay read-only for non-admin users and CSP is strict", async ({ page, isMobile }) => {
  const response = await page.goto("/");
  expect(response?.headers()["content-security-policy"]).not.toContain("unsafe-inline");
  await closeBlockingModals(page);

  await showView(page, isMobile, "settings");
  await expect(page.locator('[data-view-panel="settings"]')).toBeVisible();
  await expect(page.locator("#settings-status")).toContainText(/需要登入管理員|需要管理員/);
  await expect(page.locator('[data-setting="manual_scan_enabled"]')).toBeDisabled();

  const username = `qa-${Date.now()}@example.com`;
  await page.evaluate(async (email) => {
    const response = await fetch("/api/auth/register", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      credentials: "same-origin",
      body: JSON.stringify({ username: email, password: "test-password-123" }),
    });
    if (!response.ok) throw new Error(`register failed: ${response.status}`);
  }, username);
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
