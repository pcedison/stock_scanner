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
