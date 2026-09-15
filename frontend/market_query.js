(function exposeMarketQuery(root, factory) {
  const helpers = factory();
  if (typeof module !== "undefined" && module.exports) module.exports = helpers;
  if (root) root.StockScannerMarketQuery = helpers;
})(typeof globalThis !== "undefined" ? globalThis : null, function createMarketQueryModule() {
  const INDEX_STORAGE_KEY = "tw_stock_scanner.market_index.v2";
  const API_PAGE_SIZE = 100;
  const UI_PAGE_SIZE = 6;
  const MAX_INDEX_BYTES = 50 * 1024;
  const MAX_PAGE_BYTES = 500 * 1024;
  const DISCLOSURES = ["announced", "pending"];
  const CATEGORIES = ["entry", "watch", "excluded"];
  const INDEX_FIELDS =
    "schemaVersion generationId generatedAt pageSize detailMode disclosurePeriod filingContext financialFreshness cacheStatusInputs counts disclosures".split(
      " ",
    );
  const CACHE_STATUS_FIELDS = new Set(
    "strategy source cacheKey cacheHit storedAt servedAt isStale refreshStatus refreshReason nextRefreshAfter latestRevenuePeriod latestFinancialPeriod quality requestId stage retryable".split(
      " ",
    ),
  );
  const CACHE_BOOLEAN_FIELDS = new Set(["cacheHit", "isStale", "retryable"]);
  const ITEM_FIELDS = new Set(
    "stockCode companyName status summary reasons detailsAvailable hasFullDetails".split(" "),
  );
  const REASON_FIELDS = new Set(["code", "title", "passed", "severity", "message"]);
  const REASON_CODES = new Set(["E4", "OFFICIAL_Q", "OFFICIAL_VALUATION", "X1", "X2", "X3", "X4", "X5"]);

  function requireValue(condition, message) {
    if (!condition) throw new Error(message);
  }
  function isObject(value) {
    return value !== null && typeof value === "object" && !Array.isArray(value);
  }
  function exactKeys(value, required, optional = []) {
    requireValue(isObject(value), "expected object");
    const keys = Object.keys(value);
    const allowed = new Set([...required, ...optional]);
    const hasRequired = required.every((key) => keys.includes(key));
    const hasOnlyAllowed = keys.every((key) => allowed.has(key));
    requireValue(hasRequired, "missing field");
    requireValue(hasOnlyAllowed, "unexpected field");
  }
  function nonnegativeInt(value) {
    return Number.isInteger(value) && value >= 0 && value <= 2_000_000;
  }
  function boundedJson(value, depth = 0) {
    requireValue(depth <= 6, "metadata is too deep");
    if (value === null || typeof value === "boolean") return;
    if (typeof value === "number") {
      requireValue(Number.isFinite(value), "metadata number is invalid");
      return;
    }
    if (typeof value === "string") {
      requireValue(value.length <= 4096, "metadata string is too long");
      return;
    }
    if (Array.isArray(value)) {
      requireValue(value.length <= 100, "metadata array is too long");
      value.forEach((item) => boundedJson(item, depth + 1));
      return;
    }
    requireValue(isObject(value), "metadata value is invalid");
    const entries = Object.entries(value);
    requireValue(entries.length <= 50, "metadata object is too large");
    for (const [key, item] of entries) {
      requireValue(key.length <= 80, "metadata key is too long");
      boundedJson(item, depth + 1);
    }
  }
  function validateCacheStatus(value) {
    requireValue(isObject(value), "invalid cacheStatus");
    for (const [key, item] of Object.entries(value)) {
      requireValue(CACHE_STATUS_FIELDS.has(key), "invalid cacheStatus field");
      if (key === "quality") {
        requireValue(isObject(item), "invalid cacheStatus quality");
        boundedJson(item);
      } else if (CACHE_BOOLEAN_FIELDS.has(key)) {
        requireValue(typeof item === "boolean", "invalid cacheStatus boolean");
      } else {
        requireValue(item === null || (typeof item === "string" && item.length <= 4096), "invalid cacheStatus string");
      }
    }
  }
  function validateBucket(bucket, generationId, disclosure, category) {
    exactKeys(bucket, ["count", "pages"]);
    requireValue(nonnegativeInt(bucket.count) && Array.isArray(bucket.pages), "invalid bucket");
    requireValue(bucket.pages.length === Math.ceil(bucket.count / API_PAGE_SIZE), "invalid page count");
    let covered = 0;
    bucket.pages.forEach((reference, pageIndex) => {
      exactKeys(reference, ["key", "cursor", "count", "bytes", "sha256"]);
      const cursor = pageIndex * API_PAGE_SIZE;
      const count = Math.min(API_PAGE_SIZE, bucket.count - cursor);
      const expectedKey = `public/market_scan/v2/${generationId}/${disclosure}/${category}/${cursor}.json`;
      requireValue(reference.key === expectedKey, "invalid page key");
      requireValue(reference.cursor === cursor && reference.count === count, "invalid page range");
      requireValue(
        Number.isInteger(reference.bytes) && reference.bytes > 0 && reference.bytes < MAX_PAGE_BYTES,
        "invalid page bytes",
      );
      requireValue(
        typeof reference.sha256 === "string" && /^[0-9a-f]{64}$/.test(reference.sha256),
        "invalid page hash",
      );
      covered += count;
    });
    requireValue(covered === bucket.count, "incomplete page coverage");
    return bucket.count;
  }
  function validIsoTimestamp(value) {
    const match =
      typeof value === "string" &&
      /^((?!0000)\d{4})-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])T(?:[01]\d|2[0-3]):[0-5]\d:[0-5]\d(?:\.\d+)?(?:Z|[+-](?:[01]\d|2[0-3]):[0-5]\d)$/.exec(
        value,
      );
    if (!match || Number.isNaN(Date.parse(value))) return false;
    const [year, month, day] = match.slice(1, 4).map(Number);
    return day <= new Date(Date.UTC(year, month, 0)).getUTCDate();
  }
  function validateMarketIndex(value) {
    exactKeys(value, INDEX_FIELDS, ["cacheStatus"]);
    requireValue(value.schemaVersion === 2, "invalid schemaVersion");
    requireValue(
      typeof value.generationId === "string" && /^[0-9a-f]{24}$/.test(value.generationId),
      "invalid generationId",
    );
    requireValue(
      typeof value.generatedAt === "string" && value.generatedAt.length <= 128 && validIsoTimestamp(value.generatedAt),
      "invalid generatedAt",
    );
    requireValue(value.pageSize === API_PAGE_SIZE && value.detailMode === "summary", "invalid page contract");
    requireValue(
      value.disclosurePeriod === null ||
        (typeof value.disclosurePeriod === "string" &&
          value.disclosurePeriod.trim().length > 0 &&
          value.disclosurePeriod.length <= 64),
      "invalid disclosurePeriod",
    );
    for (const field of ["filingContext", "financialFreshness", "cacheStatusInputs"]) {
      requireValue(isObject(value[field]), `invalid ${field}`);
      boundedJson(value[field]);
    }
    if ("cacheStatus" in value) validateCacheStatus(value.cacheStatus);
    exactKeys(value.counts, ["universeSize", "announced", "pending", "categories"]);
    exactKeys(value.counts.categories, CATEGORIES);
    exactKeys(value.disclosures, DISCLOSURES);
    const categoryTotals = Object.fromEntries(CATEGORIES.map((category) => [category, 0]));
    let universe = 0;
    for (const disclosure of DISCLOSURES) {
      const group = value.disclosures[disclosure];
      exactKeys(group, ["count", ...CATEGORIES]);
      requireValue(nonnegativeInt(group.count), "invalid disclosure count");
      let groupTotal = 0;
      for (const category of CATEGORIES) {
        const total = validateBucket(group[category], value.generationId, disclosure, category);
        groupTotal += total;
        categoryTotals[category] += total;
      }
      requireValue(groupTotal === group.count && value.counts[disclosure] === groupTotal, "disclosure count mismatch");
      universe += groupTotal;
    }
    requireValue(
      nonnegativeInt(value.counts.universeSize) && value.counts.universeSize === universe,
      "universe mismatch",
    );
    for (const category of CATEGORIES) {
      requireValue(value.counts.categories[category] === categoryTotals[category], "category count mismatch");
    }
    const serialized = JSON.stringify(value);
    requireValue(new TextEncoder().encode(serialized).length < MAX_INDEX_BYTES, "index is too large");
    return JSON.parse(serialized);
  }
  function validateItem(item) {
    requireValue(isObject(item) && Object.keys(item).every((key) => ITEM_FIELDS.has(key)), "invalid market item");
    requireValue(
      ["stockCode", "detailsAvailable", "hasFullDetails"].every((field) => field in item),
      "missing market item field",
    );
    requireValue(
      typeof item.stockCode === "string" && item.stockCode.length > 0 && item.stockCode.length <= 32,
      "invalid stockCode",
    );
    for (const field of ["companyName", "status", "summary"]) {
      if (field in item)
        requireValue(typeof item[field] === "string" && item[field].length <= 4096, `invalid ${field}`);
    }
    for (const field of ["detailsAvailable", "hasFullDetails"])
      requireValue(typeof item[field] === "boolean", `invalid ${field}`);
    if ("reasons" in item) {
      requireValue(Array.isArray(item.reasons) && item.reasons.length <= 100, "invalid reasons");
      item.reasons.forEach((reason) => {
        requireValue(
          isObject(reason) &&
            Object.keys(reason).every((key) => REASON_FIELDS.has(key)) &&
            REASON_CODES.has(reason.code),
          "invalid reason",
        );
        for (const field of ["title", "severity", "message"])
          if (field in reason)
            requireValue(typeof reason[field] === "string" && reason[field].length <= 4096, `invalid reason ${field}`);
        if ("passed" in reason) requireValue(typeof reason.passed === "boolean", "invalid reason passed");
      });
    }
  }
  function validatePage(value, index, disclosure, category, cursor) {
    const fields = [
      "schemaVersion",
      "generationId",
      "disclosure",
      "category",
      "cursor",
      "limit",
      "total",
      "nextCursor",
      "items",
    ];
    exactKeys(value, fields);
    const total = index.disclosures[disclosure][category].count;
    const count = Math.max(0, Math.min(API_PAGE_SIZE, total - cursor));
    const nextCursor = cursor + count < total ? cursor + count : null;
    requireValue(value.schemaVersion === 2 && value.generationId === index.generationId, "page generation mismatch");
    requireValue(value.disclosure === disclosure && value.category === category, "page identity mismatch");
    requireValue(
      value.cursor === cursor && value.limit === API_PAGE_SIZE && value.total === total,
      "page range mismatch",
    );
    requireValue(
      value.nextCursor === nextCursor && Array.isArray(value.items) && value.items.length === count,
      "page count mismatch",
    );
    value.items.forEach(validateItem);
    const serialized = JSON.stringify(value);
    requireValue(new TextEncoder().encode(serialized).length < MAX_PAGE_BYTES, "page is too large");
    return JSON.parse(serialized);
  }
  function requiredApiCursors(uiPage, apiPageSize = API_PAGE_SIZE, uiPageSize = UI_PAGE_SIZE) {
    requireValue(Number.isInteger(uiPage) && uiPage >= 0, "invalid UI page");
    requireValue(Number.isInteger(apiPageSize) && apiPageSize > 0, "invalid API page size");
    requireValue(Number.isInteger(uiPageSize) && uiPageSize > 0, "invalid UI page size");
    const start = uiPage * uiPageSize;
    const first = Math.floor(start / apiPageSize) * apiPageSize;
    const last = Math.floor((start + uiPageSize - 1) / apiPageSize) * apiPageSize;
    return first === last ? [first] : [first, last];
  }
  function pageCacheKey(generationId, disclosure, category, cursor) {
    return `${generationId}:${disclosure}:${category}:${cursor}`;
  }
  function staleGenerationError() {
    const error = new Error("stale market generation");
    error.code = "STALE_MARKET_GENERATION";
    return error;
  }
  function marketApiVersion() {
    // The whole-scan v1 read is retired; paginated v2 is the only market API.
    return "v2";
  }
  function createMarketQueryClient({ apiJson, storage, apiPageSize = API_PAGE_SIZE, uiPageSize = UI_PAGE_SIZE }) {
    requireValue(typeof apiJson === "function", "apiJson is required");
    requireValue(apiPageSize === API_PAGE_SIZE && uiPageSize === UI_PAGE_SIZE, "unsupported page size");
    let currentIndex = null;
    let indexPromise = null;
    let indexEpoch = 0;
    let epoch = 0;
    let warning = "";
    let source = "";
    const pages = new Map();
    const pending = new Map();
    const lookup = new Map();
    function resetPages() {
      epoch += 1;
      pages.clear();
      pending.clear();
      lookup.clear();
    }
    function storageCall(method, ...args) {
      try {
        return storage?.[method]?.(...args) ?? null;
      } catch {
        return null;
      }
    }
    function storedIndex() {
      const raw = storageCall("getItem", INDEX_STORAGE_KEY);
      if (!raw) return null;
      if (typeof raw !== "string" || raw.length >= MAX_INDEX_BYTES) {
        storageCall("removeItem", INDEX_STORAGE_KEY);
        return null;
      }
      try {
        return validateMarketIndex(JSON.parse(raw));
      } catch {
        storageCall("removeItem", INDEX_STORAGE_KEY);
        return null;
      }
    }
    function acceptIndex(index, nextSource) {
      if (!currentIndex || currentIndex.generationId !== index.generationId) resetPages();
      currentIndex = index;
      source = nextSource;
      return index;
    }
    async function requestIndex(requestEpoch) {
      try {
        const index = validateMarketIndex(await apiJson("/api/scan/market/index", { method: "GET" }));
        if (requestEpoch !== indexEpoch) throw staleGenerationError();
        storageCall("setItem", INDEX_STORAGE_KEY, JSON.stringify(index));
        warning = "";
        return acceptIndex(index, "network");
      } catch (error) {
        if (requestEpoch !== indexEpoch || error?.code === "STALE_MARKET_GENERATION") throw staleGenerationError();
        const memory = currentIndex;
        const fallback = memory || storedIndex();
        if (!fallback) throw error;
        warning = error?.message || "market index unavailable";
        return acceptIndex(fallback, memory ? "memory" : "storage");
      }
    }
    async function loadIndex({ force = false } = {}) {
      if (currentIndex && !force) return currentIndex;
      if (indexPromise && !force) return indexPromise;
      if (indexPromise) {
        indexEpoch += 1;
        indexPromise = null;
      }
      const request = requestIndex(indexEpoch).finally(() => {
        if (indexPromise === request) indexPromise = null;
      });
      indexPromise = request;
      return request;
    }
    function fetchPage(index, disclosure, category, cursor, requestEpoch) {
      const key = pageCacheKey(index.generationId, disclosure, category, cursor);
      if (pages.has(key)) return Promise.resolve(pages.get(key));
      if (pending.has(key)) return pending.get(key);
      const query = new URLSearchParams({
        disclosure,
        category,
        cursor: String(cursor),
        limit: String(API_PAGE_SIZE),
        generationId: index.generationId,
      });
      const request = Promise.resolve(apiJson(`/api/scan/market/results?${query}`, { method: "GET" }))
        .then((payload) => {
          if (epoch !== requestEpoch) throw staleGenerationError();
          const page = validatePage(payload, index, disclosure, category, cursor);
          pages.set(key, page);
          page.items.forEach((item) => lookup.set(String(item.stockCode), item));
          return page;
        })
        .finally(() => {
          if (pending.get(key) === request) pending.delete(key);
        });
      pending.set(key, request);
      return request;
    }
    async function loadWindow(disclosure, category, uiPage) {
      requireValue(DISCLOSURES.includes(disclosure), "invalid disclosure");
      requireValue(CATEGORIES.includes(category), "invalid category");
      requireValue(Number.isInteger(uiPage) && uiPage >= 0, "invalid UI page");
      const index = currentIndex || (await loadIndex());
      const requestEpoch = epoch;
      const total = index.disclosures[disclosure][category].count;
      const windowStart = uiPage * UI_PAGE_SIZE;
      if (total === 0 || windowStart >= total) return { items: [], total, windowStart, loading: false, error: null };
      const cursors = requiredApiCursors(uiPage).filter((cursor) => cursor < total);
      await Promise.all(cursors.map((cursor) => fetchPage(index, disclosure, category, cursor, requestEpoch)));
      if (epoch !== requestEpoch || currentIndex?.generationId !== index.generationId) throw staleGenerationError();
      const items = [];
      const end = Math.min(total, windowStart + UI_PAGE_SIZE);
      for (let row = windowStart; row < end; row += 1) {
        const cursor = Math.floor(row / API_PAGE_SIZE) * API_PAGE_SIZE;
        const page = pages.get(pageCacheKey(index.generationId, disclosure, category, cursor));
        requireValue(page, "market window is incomplete");
        items.push(page.items[row - cursor]);
      }
      return { items, total, windowStart, loading: false, error: null };
    }
    function findLoadedResult(stockCode) {
      return lookup.get(String(stockCode || "")) || null;
    }
    function clearGeneration() {
      indexEpoch += 1;
      indexPromise = null;
      currentIndex = null;
      warning = "";
      source = "";
      resetPages();
    }
    return {
      loadIndex,
      loadWindow,
      findLoadedResult,
      clearGeneration,
      get index() {
        return currentIndex;
      },
      get warning() {
        return warning;
      },
      get source() {
        return source;
      },
    };
  }
  function createMarketQueryCoordinator({ client, state, getSelection, resetUi, render }) {
    let requestEpoch = 0;
    let committedWindow = state.marketWindow?.loading ? null : state.marketWindow;
    async function loadWindow() {
      if (!state.marketIndex) return null;
      const { disclosure, category, uiPage } = getSelection();
      const epoch = ++requestEpoch;
      const previous = state.marketWindow || { items: [] };
      const same = previous.disclosure === disclosure && previous.category === category && previous.uiPage === uiPage;
      state.marketWindow = {
        ...(same ? previous : { items: [] }),
        total: state.marketIndex.disclosures[disclosure][category].count,
        windowStart: uiPage * UI_PAGE_SIZE,
        disclosure,
        category,
        uiPage,
        loading: true,
        error: null,
      };
      render();
      try {
        const window = await client.loadWindow(disclosure, category, uiPage);
        if (epoch !== requestEpoch) return null;
        committedWindow = { ...window, disclosure, category, uiPage };
        state.marketWindow = committedWindow;
        state.marketQueryWarning = client.warning || null;
      } catch (error) {
        if (epoch !== requestEpoch || error?.code === "STALE_MARKET_GENERATION") return null;
        const message = error?.message || "分頁資料暫時無法載入";
        const rollback = committedWindow?.disclosure === disclosure && committedWindow?.category === category;
        state.marketWindow = { ...(rollback ? committedWindow : state.marketWindow), loading: false, error: message };
        if (rollback && Number.isInteger(committedWindow.uiPage) && state.marketListPages?.[disclosure]) {
          state.marketListPages[disclosure][category] = committedWindow.uiPage;
        }
        state.marketQueryWarning = message;
      }
      render();
      return state.marketWindow;
    }
    async function refresh({ force = false } = {}) {
      const previousGeneration = state.marketIndex?.generationId;
      const index = await client.loadIndex({ force });
      if (previousGeneration !== index.generationId) {
        resetUi();
        committedWindow = null;
        state.marketWindow = { items: [], total: 0, windowStart: 0, loading: false, error: null };
      }
      state.marketIndex = index;
      state.marketQueryWarning = client.warning || null;
      await loadWindow();
      return index;
    }
    return { loadWindow, refresh, forceRefresh: () => refresh({ force: true }) };
  }
  function createMarketCountUi(options) {
    const { state, apiVersion, queryAll, labels, activeTab, activeColumn, groupScan, renderOps } = options;
    const getApiVersion = options.getApiVersion || (() => apiVersion);
    function renderOverview(scan = state.marketScan, selectedTab = state.activeMarketDisclosureTab) {
      if (typeof document === "undefined") return;
      const keys = CATEGORIES;
      const values = Object.fromEntries(keys.map((key) => [key, "--"]));
      const apiVersion = getApiVersion() === "v2" ? "v2" : "v1";
      const source = apiVersion === "v2" ? state.marketIndex : scan;
      const note = source?.generatedAt ? `${new Date(source.generatedAt).toLocaleDateString()} 更新` : "等待掃描";
      const tab = activeTab(selectedTab);
      if (apiVersion === "v2" && state.marketIndex) {
        keys.forEach((key) => (values[key] = state.marketIndex.disclosures[tab][key].count));
      } else if (scan) {
        const grouped = groupScan(scan);
        keys.forEach((key) => (values[key] = grouped?.[tab]?.[key]?.length ?? 0));
      }
      keys.forEach((key) => {
        const count = document.querySelector(`#overview-${key}-count`);
        const noteElement = document.querySelector(`#overview-${key}-note`);
        if (count) count.textContent = String(values[key]);
        if (noteElement) noteElement.textContent = note;
      });
      renderOps(source);
    }
    function updateNavigation(grouped = null, selectedTab = state.activeMarketDisclosureTab) {
      const apiVersion = getApiVersion() === "v2" ? "v2" : "v1";
      const tab = activeTab(selectedTab);
      queryAll("[data-market-column-nav]").forEach((button) => {
        const category = button.dataset.marketColumnNav;
        const count =
          apiVersion === "v2" && state.marketIndex
            ? state.marketIndex.disclosures[tab][category].count
            : grouped
              ? (grouped?.[tab]?.[category]?.length ?? 0)
              : null;
        button.classList.toggle("active", category === activeColumn());
        button.textContent =
          count === null ? labels[category] || category : `${labels[category] || category} (${count})`;
      });
    }
    return { renderOverview, updateNavigation };
  }
  return {
    API_PAGE_SIZE,
    CATEGORIES,
    DISCLOSURES,
    INDEX_STORAGE_KEY,
    UI_PAGE_SIZE,
    createMarketQueryClient,
    createMarketQueryCoordinator,
    createMarketCountUi,
    pageCacheKey,
    marketApiVersion,
    requiredApiCursors,
    validateMarketIndex,
  };
});
