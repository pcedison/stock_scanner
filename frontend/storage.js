(function exposeStorageHelpers(root, factory) {
  const helpers = factory();
  if (typeof module !== "undefined" && module.exports) {
    module.exports = helpers;
  }
  if (root) {
    root.StockScannerStorage = helpers;
  }
})(typeof globalThis !== "undefined" ? globalThis : null, function createStorageHelpers() {
  const HOLDINGS_KEY = "tw_stock_scanner.holdings.v1";
  const ONBOARDING_KEY = "tw_stock_scanner.onboarding_done.v1";

  function loadHoldingsFromStorage(storage, companies, normalizeHoldingRecords) {
    try {
      const parsed = JSON.parse(storage.getItem(HOLDINGS_KEY) || "[]");
      if (!Array.isArray(parsed)) throw new Error("Holdings must be an array");
      return normalizeHoldingRecords(parsed, companies);
    } catch {
      storage.setItem(HOLDINGS_KEY, "[]");
      return [];
    }
  }

  function saveHoldingsLocalOnly(holdings, storage, companies, normalizeHoldingRecords) {
    storage.setItem(HOLDINGS_KEY, JSON.stringify(normalizeHoldingRecords(holdings, companies)));
  }

  function onboardingStorageKey(user) {
    const identity = user?.id || user?.username || "guest";
    return `${ONBOARDING_KEY}.${identity}`;
  }

  function hasCompletedOnboarding(storage, user) {
    return storage.getItem(onboardingStorageKey(user)) === "true";
  }

  function markOnboardingDone(storage, user) {
    storage.setItem(onboardingStorageKey(user), "true");
  }

  return {
    HOLDINGS_KEY,
    ONBOARDING_KEY,
    hasCompletedOnboarding,
    loadHoldingsFromStorage,
    markOnboardingDone,
    onboardingStorageKey,
    saveHoldingsLocalOnly,
  };
});
