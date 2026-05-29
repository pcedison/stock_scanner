const COMPANIES_PAGE_LIMIT = 500;
const MAX_COMPANY_PAGES = 10;

const _isCjs =
  typeof require === "function" && typeof module !== "undefined" && module.exports;
const _mod = (globalKey, requirePath) =>
  _isCjs ? require(requirePath) : globalThis[globalKey];

const REFERENCE_DATA = _mod("StockScannerReferenceData", "./reference_data.js");
const STRATEGY_CONTENT = _mod("StockScannerStrategyContent", "./strategy_content.js");
const STORAGE_HELPERS = _mod("StockScannerStorage", "./storage.js");
const RENDERER_HELPERS = _mod("StockScannerRenderers", "./renderers.js");
const DOM_HELPERS = _mod("StockScannerDom", "./dom.js");
const HOLDING_SIGNAL_HELPERS = _mod("StockScannerHoldingSignals", "./holding_signals.js");
const AUTH_HELPERS = _mod("StockScannerAuth", "./auth.js");
const API_CLIENT_HELPERS = _mod("StockScannerApiClient", "./api_client.js");
const CSRF_HEADER_NAME = "X-Stock-Scanner-CSRF";
const CSRF_HEADER_VALUE = "1";
const { authValidationMessage, isSuperUserIdentity, normalizeAuthUser, normalizeAuthUsername } = AUTH_HELPERS;
const { DEFAULT_COMPANIES } = REFERENCE_DATA;
const { STRATEGY_STATUS_DETAILS, STRATEGY_RULE_THRESHOLDS } = STRATEGY_CONTENT;
const API_CLIENT = API_CLIENT_HELPERS.createApiClient({
  csrfHeaderName: CSRF_HEADER_NAME,
  csrfHeaderValue: CSRF_HEADER_VALUE,
});

const DEFAULT_SETTINGS = {
  auto_scan_full_market: true,
  manual_scan_enabled: true,
  exclude_financial_industry: true,
  use_mock_data: false,
  scan_twse: true,
  scan_tpex: true,
  spring_festival_guard: true,
  revenue_growth_mode: "cumulative_ytd",
};

const state = {
  companies: [...DEFAULT_COMPANIES],
  holdings: [],
  settings: { ...DEFAULT_SETTINGS },
  selectedCompany: null,
  marketScan: null,
  holdingsScan: null,
  onboardingDraft: [],
  editingHoldingCode: null,
  activeView: "overview",
  dataSourceStatus: null,
  schedulerStatus: null,
  schedulerAutoScan: null,
  integrationStatus: null,
  backtestStatus: null,
  isScanningHoldings: false,
  activeMarketDisclosureTab: "announced",
  activeMarketColumn: "entry",
  marketListPages: {
    announced: { entry: 0, watch: 0, excluded: 0 },
    pending: { entry: 0, watch: 0, excluded: 0 },
  },
  expandedMarketResultIds: new Set(),
  activeStrategyStatusKey: null,
  auth: {
    checked: false,
    available: true,
    authenticated: false,
    user: null,
    message: "",
  },
  adminUsers: [],
  adminMessage: "",
  adminError: "",
  adminIsLoading: false,
  isSyncingHoldings: false,
  pendingHoldingsSync: false,
};

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];
const MOBILE_NAV_BREAKPOINT = 680;
let holdingsScanRefreshTimer = null;
let marketScanRefreshPromise = null;

const NAVIGATION_HELPERS = _mod("StockScannerNavigation", "./navigation.js");
const {
  syncAccountPanelPlacement,
  isMobileNavigation,
  setScanNavExpanded,
  normalizeResponsiveNavigation,
  closeMobileMenu,
  toggleMobileMenu,
} = NAVIGATION_HELPERS.createNavigation({ query: $, breakpoint: MOBILE_NAV_BREAKPOINT });

function normalizeText(value) {
  return String(value || "").trim().replace(/\s+/g, " ");
}

function safeText(value, fallback = "") {
  const normalized = normalizeText(value);
  return normalized || fallback;
}

function normalizeCompany(company) {
  if (!company || typeof company !== "object") return null;
  const stockCode = safeText(company.stockCode);
  if (!/^\d{4,6}$/.test(stockCode)) return null;
  return {
    stockCode,
    name: safeText(company.name || company.companyName, "未知公司"),
    market: safeText(company.market, "未標示市場"),
    industryName: safeText(company.industryName, "未標示產業"),
    isFinancial: Boolean(company.isFinancial),
  };
}

function normalizeCompanies(companies) {
  if (!Array.isArray(companies)) return [];
  return companies.map(normalizeCompany).filter(Boolean);
}

function companySource(companies = state.companies) {
  const normalized = normalizeCompanies(companies);
  return normalized.length ? normalized : normalizeCompanies(DEFAULT_COMPANIES);
}

function escapeHtml(value) {
  return DOM_HELPERS.escapeHtml(value);
}

function emptyStateHtml(message) {
  return DOM_HELPERS.emptyStateHtml(message);
}

function setEmptyState(target, message) {
  DOM_HELPERS.setEmptyState(target, message);
}

function setFormError(target, message) {
  DOM_HELPERS.setFormError(target, message);
}

function setSafeHtml(target, html) {
  DOM_HELPERS.setSafeHtml(target, html);
}

function clearElement(target) {
  DOM_HELPERS.clearElement(target);
}

function safeCompanyName(companyOrHolding) {
  if (!companyOrHolding) return "未知公司";
  const directName = safeText(companyOrHolding.name || companyOrHolding.companyName);
  if (directName) return directName;
  const company = findCompanyByCodeOrName(companyOrHolding.stockCode, state.companies);
  return company ? company.name : "未知公司";
}

function optionalNumber(value) {
  if (value === null || value === undefined || value === "") return null;
  const number = Number(value);
  return Number.isFinite(number) ? Math.max(0, number) : null;
}

function findCompanyByCodeOrName(query, companies = state.companies) {
  const normalized = normalizeText(query).toLowerCase();
  if (!normalized) return null;
  const source = companySource(companies);
  return (
    source.find((company) => company.stockCode.toLowerCase() === normalized) ||
    source.find((company) => company.name.toLowerCase() === normalized) ||
    source.find((company) => `${company.stockCode} ${company.name} ${company.market} ${company.industryName}`.toLowerCase().includes(normalized)) ||
    null
  );
}

function parseStockInput(input, companies = state.companies) {
  const normalized = normalizeText(input);
  if (!normalized) {
    return { ok: false, error: "請輸入股票代碼或公司名稱" };
  }

  const tokens = normalized.split(" ");
  const codeToken = tokens.find((token) => /^\d{4,6}$/.test(token));
  const numericTokens = [];
  const nameTokens = [];
  let consumedCode = false;

  for (const token of tokens) {
    if (codeToken && token === codeToken && !consumedCode) {
      consumedCode = true;
      continue;
    }
    if (/^\d+(\.\d+)?$/.test(token)) {
      numericTokens.push(Number(token));
      continue;
    }
    nameTokens.push(token);
  }

  const nameQuery = nameTokens.join(" ");
  const company =
    findCompanyByCodeOrName(codeToken || "", companies) ||
    findCompanyByCodeOrName(nameQuery, companies) ||
    findCompanyByCodeOrName(normalized, companies);

  if (!company) {
    return { ok: false, error: "找不到資料" };
  }

  const shares = Number.isFinite(numericTokens[0]) ? Math.max(0, Math.floor(numericTokens[0])) : 0;
  const averageCost = Number.isFinite(numericTokens[1]) ? Math.max(0, numericTokens[1]) : null;
  return {
    ok: true,
    stockCode: company.stockCode,
    name: company.name,
    company,
    shares,
    averageCost,
  };
}

function normalizeHoldingRecord(item, companies = state.companies) {
  if (!item || typeof item !== "object") return null;
  const company = findCompanyByCodeOrName(item.stockCode || item.name, companies);
  if (!company) return null;
  const shares = Number(item.shares);
  const averageCost = Number(item.averageCost);
  return {
    stockCode: company.stockCode,
    name: company.name,
    shares: Number.isFinite(shares) ? Math.max(0, Math.floor(shares)) : 0,
    averageCost: item.averageCost === null || item.averageCost === undefined || item.averageCost === "" || !Number.isFinite(averageCost) ? null : Math.max(0, averageCost),
  };
}

function normalizeHoldingRecords(records, companies = state.companies) {
  if (!Array.isArray(records)) return [];
  const deduped = new Map();
  records.map((item) => normalizeHoldingRecord(item, companies)).filter(Boolean).forEach((holding) => deduped.set(holding.stockCode, holding));
  return [...deduped.values()].sort((a, b) => a.stockCode.localeCompare(b.stockCode));
}

function loadHoldingsFromStorage(storage = localStorage, companies = state.companies) {
  return STORAGE_HELPERS.loadHoldingsFromStorage(storage, companies, normalizeHoldingRecords);
}

function loadHoldings() {
  const normalized = loadHoldingsFromStorage(localStorage, state.companies);
  saveHoldingsLocalOnly(normalized);
  return normalized;
}

function saveHoldingsLocalOnly(holdings = state.holdings, storage = localStorage) {
  STORAGE_HELPERS.saveHoldingsLocalOnly(holdings, storage, state.companies, normalizeHoldingRecords);
}

function saveHoldings(holdings = state.holdings, storage = localStorage) {
  const normalized = normalizeHoldingRecords(holdings, state.companies);
  state.holdings = normalized;
  saveHoldingsLocalOnly(normalized, storage);
  const isBrowserLocalStorage = typeof localStorage !== "undefined" && storage === localStorage;
  if (isBrowserLocalStorage && state.auth?.authenticated) {
    state.pendingHoldingsSync = true;
    syncHoldingsToServer(normalized);
  }
  if (isBrowserLocalStorage) requestHoldingsScanRefresh();
}

async function syncHoldingsToServer(holdings = state.holdings) {
  if (!state.auth?.authenticated) return false;
  if (state.isSyncingHoldings) {
    state.pendingHoldingsSync = true;
    return false;
  }
  state.pendingHoldingsSync = false;
  state.isSyncingHoldings = true;
  renderAccountPanel();
  try {
    const submittedHoldings = normalizeHoldingRecords(holdings, state.companies);
    const payload = await apiJson("/api/me/holdings", {
      method: "PUT",
      body: JSON.stringify({ holdings: submittedHoldings }),
    });
    if (!state.pendingHoldingsSync) {
      state.holdings = normalizeHoldingRecords(payload.holdings, state.companies);
      saveHoldingsLocalOnly(state.holdings);
      refreshHoldingsDependentViews();
    }
    state.auth.message = "持股已同步到帳號。";
    return true;
  } catch {
    state.auth.available = false;
    state.pendingHoldingsSync = false;
    state.auth.message = "伺服器同步失敗，已保留本機持股。";
    return false;
  } finally {
    state.isSyncingHoldings = false;
    renderAccountPanel();
    if (state.pendingHoldingsSync) {
      syncHoldingsToServer(state.holdings);
    }
  }
}

function isHoldingTracked(stockCode) {
  const normalized = safeText(stockCode);
  return Boolean(normalized && state.holdings.some((holding) => holding.stockCode === normalized));
}

const {
  applyEvidenceBarWidths,
  displayResultStatus,
  formatEvidenceValue,
  renderAnalysisCard,
  renderRule,
  renderRuleEvidence,
  renderStrategyRuleCards,
  resultActionButtons,
  ruleDisplayOrder,
  sortRulesForDisplay,
  statusClass,
  statusLabel,
} = RENDERER_HELPERS.createRenderers({
  escapeHtml,
  isHoldingTracked,
  isPartialPublishedResult,
  normalizeText,
  safeCompanyName,
  safeText,
  strategyStatusDetails: STRATEGY_STATUS_DETAILS,
  strategyRuleThresholds: STRATEGY_RULE_THRESHOLDS,
});

function holdingScanResultByCode(stockCode, scan = state.holdingsScan) {
  const normalized = safeText(stockCode);
  if (!normalized || !scan || !Array.isArray(scan.results)) return null;
  return scan.results.find((result) => safeText(result?.stockCode) === normalized) || null;
}

function holdingScanMissingByCode(stockCode, scan = state.holdingsScan) {
  const normalized = safeText(stockCode);
  if (!normalized || !scan || !Array.isArray(scan.missing)) return null;
  return scan.missing.find((item) => safeText(item?.stockCode) === normalized) || null;
}

function holdingExitCodes(result = {}) {
  return HOLDING_SIGNAL_HELPERS.holdingExitCodes(result);
}

function holdingSignal(result = null, missing = null) {
  const display = result ? displayResultStatus(result, "holding") : null;
  return HOLDING_SIGNAL_HELPERS.holdingSignal(display ? { ...result, status: display.status, summary: display.summary } : result, missing, {
    isScanning: state.isScanningHoldings,
  });
}

function renderHoldingSignal(result = null, missing = null) {
  const display = result ? displayResultStatus(result, "holding") : null;
  return HOLDING_SIGNAL_HELPERS.renderHoldingSignal(display ? { ...result, status: display.status, summary: display.summary } : result, missing, {
    isScanning: state.isScanningHoldings,
  });
}

function holdingExitAlerts(scan = state.holdingsScan) {
  const results = Array.isArray(scan?.results) ? scan.results : [];
  const savedHoldingCodes = new Set(state.holdings.map((holding) => String(holding.stockCode || "").trim()).filter(Boolean));
  return results
    .map((result) => {
      const signal = holdingSignal(result);
      if (!["EXIT", "WARNING"].includes(signal.status)) return null;
      const stockCode = safeText(result?.stockCode, "未知代碼");
      if (savedHoldingCodes.size && !savedHoldingCodes.has(stockCode)) return null;
      return {
        stockCode,
        companyName: safeText(result?.companyName || safeCompanyName(result), "未知公司"),
        status: signal.status,
        label: signal.label,
        summary: signal.summary,
        exitCodes: holdingExitCodes(result),
      };
    })
    .filter(Boolean)
    .sort((left, right) => {
      if (left.status !== right.status) return left.status === "EXIT" ? -1 : 1;
      return left.stockCode.localeCompare(right.stockCode);
    });
}

function renderHoldingExitAlertBanner(alerts = holdingExitAlerts()) {
  if (!alerts.length) return "";
  const exitCount = alerts.filter((item) => item.status === "EXIT").length;
  const warningCount = alerts.filter((item) => item.status === "WARNING").length;
  const title = exitCount ? `${exitCount} 檔持股命中高優先出場條件` : `${warningCount} 檔持股進入出場警戒`;
  const tone = exitCount ? "critical" : "warning";
  const lead = exitCount
    ? "X4/X5 高優先出場已匹配，建議立即檢視部位，避免已累積獲利明顯回吐。"
    : "X1-X3 出場警戒已匹配，請提高追蹤頻率，必要時先降低持股曝險。";
  const extraCount = Math.max(0, alerts.length - 4);
  return `
    <section class="holding-exit-alert-banner ${tone}" role="alert" aria-live="polite">
      <div>
        <p class="eyebrow">持股出場提醒</p>
        <h3>${escapeHtml(title)}</h3>
        <p>${escapeHtml(lead)}</p>
      </div>
      <ul class="holding-exit-alert-list">
        ${alerts
          .slice(0, 4)
          .map(
            (item) => `
              <li class="holding-exit-alert-row ${item.status === "EXIT" ? "exit" : "warning"}">
                <strong>${escapeHtml(item.stockCode)} ${escapeHtml(item.companyName)}</strong>
                <span class="status-pill ${statusClass(item.status)}">${escapeHtml(item.label)}</span>
                <span>${escapeHtml(item.summary)}</span>
              </li>
            `,
          )
          .join("")}
        ${extraCount ? `<li class="holding-exit-alert-more"><span>另有 ${escapeHtml(extraCount)} 檔需檢視</span></li>` : ""}
      </ul>
      <div class="button-row">
        <button class="danger-btn" type="button" data-open-holding-alert-details>查看出場明細</button>
      </div>
    </section>
  `;
}

function renderHoldingExitAlerts() {
  if (typeof document === "undefined") return;
  const target = $("#holding-exit-alerts");
  if (!target) return;
  const html = renderHoldingExitAlertBanner();
  if (!html) {
    target.replaceChildren();
    target.classList.add("hidden");
    return;
  }
  const parsed = new DOMParser().parseFromString(html, "text/html");
  target.replaceChildren(...Array.from(parsed.body.childNodes));
  target.classList.remove("hidden");
}

function refreshHoldingsDependentViews() {
  renderHoldings();
  if (state.holdingsScan) renderHoldingResults();
  if (state.marketScan) renderMarketResults();
}

function upsertHolding(holding) {
  const normalizedCompany = normalizeCompany(holding);
  if (!normalizedCompany) return false;
  const normalizedHolding = {
    stockCode: normalizedCompany.stockCode,
    name: normalizedCompany.name,
    shares: Number.isFinite(holding.shares) ? Math.max(0, Math.floor(holding.shares)) : 0,
    averageCost: Number.isFinite(holding.averageCost) ? Math.max(0, holding.averageCost) : null,
  };
  const existing = state.holdings.find((item) => item.stockCode === normalizedHolding.stockCode);
  if (existing) {
    existing.name = normalizedHolding.name;
    if (normalizedHolding.shares > 0) {
      existing.shares = normalizedHolding.shares;
    }
    if (Number.isFinite(normalizedHolding.averageCost)) {
      existing.averageCost = normalizedHolding.averageCost;
    }
  } else {
    state.holdings.push(normalizedHolding);
  }
  state.holdings.sort((a, b) => a.stockCode.localeCompare(b.stockCode));
  saveHoldings();
  refreshHoldingsDependentViews();
  return true;
}

function reduceHolding(stockCode, amount) {
  const holding = state.holdings.find((item) => item.stockCode === stockCode);
  if (!holding) return;
  const reduction = Math.max(0, Math.floor(Number(amount)));
  if (!Number.isFinite(reduction) || reduction <= 0) return;
  holding.shares = Math.max(0, holding.shares - reduction);
  if (holding.shares === 0) {
    clearHolding(stockCode);
    return;
  }
  saveHoldings();
  refreshHoldingsDependentViews();
}

function clearHolding(stockCode) {
  state.holdings = state.holdings.filter((item) => item.stockCode !== stockCode);
  if (state.editingHoldingCode === stockCode) state.editingHoldingCode = null;
  saveHoldings();
  refreshHoldingsDependentViews();
}

function holdingResultsVisible() {
  if (typeof document === "undefined") return false;
  const target = $("#holding-results");
  return Boolean(target && !target.classList.contains("hidden"));
}

function requestHoldingsScanRefresh() {
  if (typeof window === "undefined") return;
  window.clearTimeout(holdingsScanRefreshTimer);
  if (!state.holdings.length) {
    state.holdingsScan = { generatedAt: new Date().toISOString(), dataSource: "local", results: [], missing: [] };
    refreshHoldingsDependentViews();
    return;
  }
  if (!state.settings.manual_scan_enabled) {
    state.holdingsScan = null;
    refreshHoldingsDependentViews();
    return;
  }
  holdingsScanRefreshTimer = window.setTimeout(() => {
    refreshStoredHoldingAnalysis({ renderResults: holdingResultsVisible(), quiet: true });
  }, 250);
}

async function refreshStoredHoldingAnalysis({ renderResults = false, quiet = false } = {}) {
  const target = typeof document !== "undefined" ? $("#holding-results") : null;
  if (!state.holdings.length) {
    state.holdingsScan = { generatedAt: new Date().toISOString(), dataSource: "local", results: [], missing: [] };
    refreshHoldingsDependentViews();
    if (renderResults) renderHoldingResults();
    return true;
  }
  if (!state.settings.manual_scan_enabled) {
    state.holdingsScan = null;
    refreshHoldingsDependentViews();
    if (renderResults && target) setEmptyState(target, "手動掃描已停用");
    return false;
  }

  state.isScanningHoldings = true;
  renderHoldings();
  if (renderResults && target && !quiet) setEmptyState(target, "正在套用 X1-X5 出場規則");
  try {
    state.holdingsScan = await apiJson("/api/scan/holdings", {
      method: "POST",
      body: JSON.stringify({ holdings: state.holdings, settings: state.settings }),
    });
  } catch (error) {
    if (renderResults && target) {
      setFormError(target, error.message || "持股掃描失敗");
    }
    return false;
  } finally {
    state.isScanningHoldings = false;
    renderHoldings();
  }
  if (renderResults) renderHoldingResults();
  return true;
}

async function apiJson(url, options = {}) {
  const response = await apiFetch(url, options);
  const text = await response.text();
  if (!response.ok) {
    throw new Error(apiErrorMessage(response, text));
  }
  return text ? JSON.parse(text) : {};
}

async function apiFetch(url, options = {}) {
  try {
    return await API_CLIENT.request(url, options);
  } catch {
    throw new Error(apiErrorMessage({ status: 503, headers: { get: () => "" } }, ""));
  }
}

function apiErrorMessage(response, text = "") {
  const status = Number(response?.status) || 0;
  const contentType = String(response?.headers?.get?.("content-type") || response?.headers?.get?.("Content-Type") || "").toLowerCase();
  const fallback =
    status >= 500
      ? "伺服器暫時無法處理請求，請稍後再試。"
      : status === 401
        ? "請重新登入後再試。"
        : status === 403
          ? "目前帳號沒有執行此操作的權限。"
          : `請求失敗（HTTP ${status || "未知"}）`;

  if (!text) return fallback;
  if (contentType.includes("application/json")) {
    try {
      const payload = JSON.parse(text);
      const detail = typeof payload.detail === "string" ? payload.detail.trim() : "";
      return status >= 500 || detail.startsWith("Cloudflare Worker API error:") ? fallback : detail || fallback;
    } catch {
      return fallback;
    }
  }

  const normalized = text.trim();
  if (!normalized || /<(!doctype|html|head|body|script|style)\b/i.test(normalized)) return fallback;
  return normalized.length <= 240 ? normalized : fallback;
}

function hasCompletedOnboarding(storage = localStorage, user = state.auth?.user || null) {
  return STORAGE_HELPERS.hasCompletedOnboarding(storage, user);
}

function markOnboardingDone(storage = localStorage, user = state.auth?.user || null) {
  STORAGE_HELPERS.markOnboardingDone(storage, user);
}


function shouldPromptOnboarding() {
  return Boolean(state.auth?.authenticated && !hasCompletedOnboarding() && !state.holdings.length);
}

function renderAuthGate() {
  if (typeof document === "undefined") return;
  const modal = $("#auth-modal");
  if (!modal) return;
  const message = $("#auth-modal-message");
  if (message) message.textContent = state.auth?.message || "";
}

function openAuthGate(message = "") {
  const modal = $("#auth-modal");
  if (!modal) return;
  if (message) state.auth.message = message;
  renderAuthGate();
  modal.classList.remove("hidden");
  setTimeout(() => $("#auth-modal-username")?.focus(), 0);
}

function closeAuthGate(options = {}) {
  $("#auth-modal")?.classList.add("hidden");
  if (options.clearMessage && state.auth) state.auth.message = "";
  const message = $("#auth-modal-message");
  if (message) message.textContent = "";
}

function maybeStartFirstRunFlow() {
  if (!state.auth?.checked) return;
  if (!state.auth.authenticated) {
    openAuthGate(state.auth.available ? "" : state.auth.message);
    return;
  }
  closeAuthGate();
  if (shouldPromptOnboarding()) openOnboarding();
}

function renderAccountPanel() {
  if (typeof document === "undefined") return;
  syncAccountPanelPlacement();
  const panel = $("#account-panel");
  if (!panel) return;
  const status = $("#account-status");
  const form = $("#auth-form");
  const userPanel = $("#auth-user-panel");
  const userLabel = $("#auth-user-label");
  const message = $("#auth-message");
  const storageNote = $("#holdings-storage-note");
  const isLoggedIn = Boolean(state.auth?.authenticated);
  const displayName = state.auth?.user?.displayName || state.auth?.user?.username || "";

  status.className = `status-pill ${isLoggedIn ? "status-entry" : state.auth?.available ? "neutral" : "status-watch"}`;
  status.textContent = state.isSyncingHoldings ? "同步中" : isLoggedIn ? "帳號同步" : "本機持股";
  form.classList.toggle("hidden", isLoggedIn);
  userPanel.classList.toggle("hidden", !isLoggedIn);
  userLabel.textContent = isLoggedIn ? `已登入：${displayName}` : "";
  message.textContent = state.auth?.message || "";
  const mobileSummary = $("#mobile-account-summary");
  if (mobileSummary) {
    mobileSummary.textContent = isLoggedIn ? `${displayName}，可匯入或登出` : "登入、註冊與同步持股";
  }
  if (storageNote) {
    storageNote.textContent = isLoggedIn
      ? "持股會同步儲存在伺服器端帳號；瀏覽器也會保留一份本機備援。"
      : "目前使用本機持股；登入後可同步到伺服器，換瀏覽器或重新登入仍可讀回。";
  }
  renderAuthGate();
  renderAdminVisibility();
  renderSettings();
}

function isSuperUser() {
  return Boolean(state.auth?.authenticated && isSuperUserIdentity(state.auth?.user));
}

function renderAdminVisibility() {
  if (typeof document === "undefined") return;
  const adminNavItem = $("#admin-nav-item");
  const canManageUsers = isSuperUser();
  if (adminNavItem) adminNavItem.classList.toggle("hidden", !canManageUsers);
  if (!canManageUsers && state.activeView === "admin") {
    showView("overview");
  }
}

async function loadAccountState() {
  const localHoldings = loadHoldings();
  try {
    const me = await apiJson("/api/auth/me");
    const authUser = normalizeAuthUser(me.user);
    state.auth = {
      checked: true,
      available: true,
      authenticated: Boolean(me.authenticated && authUser),
      user: authUser,
      message: "",
    };
    if (!state.auth.authenticated) {
      state.holdings = localHoldings;
      renderAccountPanel();
      return;
    }
    const serverHoldings = normalizeHoldingRecords(me.holdings || [], state.companies);
    if (!serverHoldings.length && localHoldings.length) {
      state.holdings = localHoldings;
      await syncHoldingsToServer(localHoldings);
      state.auth.message = "已將本機持股匯入帳號。";
    } else {
      state.holdings = serverHoldings;
      saveHoldingsLocalOnly(state.holdings);
    }
  } catch {
    state.auth = {
      checked: true,
      available: false,
      authenticated: false,
      user: null,
      message: "帳號服務暫時不可用，已切回本機持股。",
    };
    state.holdings = localHoldings;
  }
  renderAccountPanel();
}

async function authenticateFromForm(mode, source = "header") {
  const isModal = source === "modal";
  const usernameInput = isModal ? $("#auth-modal-username") : $("#auth-username");
  const passwordInput = isModal ? $("#auth-modal-password") : $("#auth-password");
  const username = usernameInput.value.trim();
  const password = passwordInput.value;
  const validationMessage = authValidationMessage(username, password);
  if (validationMessage) {
    state.auth.message = validationMessage;
    renderAccountPanel();
    usernameInput.focus();
    return;
  }
  state.auth.message = "";
  renderAccountPanel();
  try {
    const localHoldings = loadHoldingsFromStorage(localStorage, state.companies);
    const result = await apiJson(mode === "register" ? "/api/auth/register" : "/api/auth/login", {
      method: "POST",
      body: JSON.stringify({ username, password }),
    });
    const authUser = normalizeAuthUser(result.user);
    state.auth = {
      checked: true,
      available: true,
      authenticated: Boolean(result.authenticated && authUser),
      user: authUser,
      message: mode === "register" ? "帳號已建立。" : "已登入。",
    };
    const serverHoldings = normalizeHoldingRecords(result.holdings || [], state.companies);
    if (!serverHoldings.length && localHoldings.length) {
      state.holdings = localHoldings;
      await syncHoldingsToServer(localHoldings);
      state.auth.message = "已登入，並將本機持股匯入帳號。";
    } else {
      state.holdings = serverHoldings;
      saveHoldingsLocalOnly(state.holdings);
    }
    passwordInput.value = "";
    renderHoldings();
    requestHoldingsScanRefresh();
    refreshOverviewMarketScan();
    if (isModal) {
      $("#auth-username").value = username;
      closeAuthGate();
    }
    maybeStartFirstRunFlow();
  } catch (error) {
    state.auth.available = true;
    state.auth.authenticated = false;
    state.auth.user = null;
    state.auth.message = error.message || "帳號操作失敗。";
  }
  renderAccountPanel();
}

async function logoutAccount() {
  try {
    await apiJson("/api/auth/logout", { method: "POST", body: JSON.stringify({}) });
  } catch {
    // Logging out should still return the UI to local mode if the server is unreachable.
  }
  state.auth = {
    checked: true,
    available: true,
    authenticated: false,
    user: null,
    message: "已登出，畫面保留目前本機持股。",
  };
  state.adminUsers = [];
  state.adminMessage = "";
  state.adminError = "";
  saveHoldingsLocalOnly(state.holdings);
  closeOnboarding(false);
  renderAccountPanel();
  maybeStartFirstRunFlow();
}

async function importLocalHoldingsToAccount() {
  if (!state.auth?.authenticated) return;
  const localHoldings = loadHoldingsFromStorage(localStorage, state.companies);
  const merged = normalizeHoldingRecords([...state.holdings, ...localHoldings], state.companies);
  state.holdings = merged;
  const ok = await syncHoldingsToServer(merged);
  state.auth.message = ok ? "已將本機持股合併同步到帳號。" : state.auth.message;
  renderHoldings();
  requestHoldingsScanRefresh();
  renderAccountPanel();
}

function renderAdminUsers() {
  if (typeof document === "undefined") return;
  const target = $("#admin-users-list");
  const message = $("#admin-users-message");
  if (!target || !message) return;
  if (!isSuperUser()) {
    message.textContent = "此頁面僅限 super user 使用。";
    clearElement(target);
    return;
  }
  message.textContent = state.adminIsLoading ? "讀取使用者清單中..." : state.adminError || state.adminMessage || "";
  if (state.adminIsLoading) {
    setEmptyState(target, "讀取中");
    return;
  }
  if (state.adminError) {
    setEmptyState(target, state.adminError);
    return;
  }
  if (!state.adminUsers.length) {
    setEmptyState(target, "目前沒有其他使用者");
    return;
  }
  setSafeHtml(target, state.adminUsers
    .map((user) => {
      const createdAt = user.createdAt ? new Date(user.createdAt).toLocaleString() : "未知";
      const badge = user.isSuperUser ? `<span class="status-pill status-entry">super user</span>` : `<span class="status-pill neutral">一般使用者</span>`;
      const deleteButton = user.canDelete
        ? `<button class="small-danger-btn" type="button" data-delete-user="${escapeHtml(user.id)}" data-delete-username="${escapeHtml(user.username)}">刪除</button>`
        : `<button class="ghost-btn" type="button" disabled>不可刪除</button>`;
      return `
        <article class="admin-user-card">
          <div>
            <div class="admin-user-title">
              <strong>${escapeHtml(user.displayName || user.username)}</strong>
              ${badge}
            </div>
            <p>${escapeHtml(user.username)}</p>
            <dl class="admin-user-meta">
              <div><dt>建立時間</dt><dd>${escapeHtml(createdAt)}</dd></div>
              <div><dt>持股數</dt><dd>${escapeHtml(user.holdingsCount ?? 0)}</dd></div>
              <div><dt>有效登入</dt><dd>${escapeHtml(user.activeSessionCount ?? 0)}</dd></div>
            </dl>
          </div>
          ${deleteButton}
        </article>
      `;
    })
    .join(""));
}

function adminUsersErrorMessage(message) {
  const normalized = normalizeText(message || "使用者清單讀取失敗。");
  if (normalized === "Not found" || normalized.includes("404")) {
    return "使用者管理 API 尚未部署，或 API_ORIGIN 仍指向舊版後端。請重新部署 Cloudflare Worker / Pages 後再整理。";
  }
  return `使用者清單讀取失敗：${normalized}`;
}

async function loadAdminUsers() {
  if (!isSuperUser()) return;
  state.adminIsLoading = true;
  state.adminMessage = "";
  state.adminError = "";
  renderAdminUsers();
  try {
    const payload = await apiJson("/api/admin/users");
    state.adminUsers = Array.isArray(payload.users) ? payload.users : [];
    state.adminMessage = `已載入 ${state.adminUsers.length} 個帳號。`;
  } catch (error) {
    state.adminUsers = [];
    state.adminError = adminUsersErrorMessage(error.message);
  } finally {
    state.adminIsLoading = false;
    renderAdminUsers();
  }
}

async function deleteAdminUser(userId, username) {
  if (!isSuperUser() || !userId) return;
  const confirmed = window.confirm(`確定要刪除 ${username}？此動作會移除該帳號的持股與登入 session。`);
  if (!confirmed) return;
  state.adminIsLoading = true;
  state.adminMessage = "";
  state.adminError = "";
  renderAdminUsers();
  try {
    const payload = await apiJson(`/api/admin/users/${encodeURIComponent(userId)}`, { method: "DELETE" });
    state.adminUsers = Array.isArray(payload.users) ? payload.users : [];
    state.adminMessage = `已刪除 ${username}。`;
  } catch (error) {
    state.adminError = `刪除使用者失敗：${error.message || "未知錯誤"}`;
  } finally {
    state.adminIsLoading = false;
    renderAdminUsers();
  }
}

function renderSelectedCompany() {
  const target = $("#selected-company");
  const company = normalizeCompany(state.selectedCompany);
  if (!company) {
    target.className = "selected-company empty-state";
    target.textContent = "尚未選擇股票";
    return;
  }

  state.selectedCompany = company;
  target.className = "selected-company card selected-company-card";
  setSafeHtml(target, `
    <div class="card-head">
      <div>
        <h3 class="stock-title">${escapeHtml(company.stockCode)} ${escapeHtml(company.name)}</h3>
        <p class="muted">${escapeHtml(company.market)} · ${escapeHtml(company.industryName)}</p>
      </div>
      <span class="status-pill neutral">${company.isFinancial ? "金融業" : "一般產業"}</span>
    </div>
    <div class="button-row">
      <button class="secondary-btn" type="button" id="analyze-selected-btn">單檔分析</button>
      <button class="primary-btn" type="button" id="add-selected-btn">加入持股</button>
    </div>
  `);
}

function renderHoldings() {
  renderHoldingExitAlerts();
  const target = $("#holdings-list");
  if (!state.holdings.length) {
    setEmptyState(target, "目前沒有持股");
    return;
  }

  setSafeHtml(target, state.holdings
    .map((holding) => {
      const name = safeCompanyName(holding);
      const stockCode = safeText(holding.stockCode, "未知代碼");
      const shares = Number.isFinite(Number(holding.shares)) ? Math.max(0, Math.floor(Number(holding.shares))) : 0;
      const averageCost = optionalNumber(holding.averageCost);
      const isEditing = state.editingHoldingCode === stockCode;
      const analysis = holdingScanResultByCode(stockCode);
      const missing = holdingScanMissingByCode(stockCode);
      return `
        <article class="card holding-card" data-holding-code="${escapeHtml(stockCode)}">
          <div class="card-head">
            <div>
              <h3 class="stock-title">${escapeHtml(stockCode)} ${escapeHtml(name)}</h3>
              <p class="muted">目前 ${escapeHtml(shares)} 股，平均成本 ${escapeHtml(averageCost ?? "未填")}</p>
              ${renderHoldingSignal(analysis, missing)}
            </div>
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
    })
    .join(""));
}

function renderOnboardingDraft() {
  const list = $("#onboarding-draft-list");
  const count = $("#onboarding-draft-count");
  if (!list || !count) return;

  count.textContent = `${state.onboardingDraft.length} 檔`;
  if (!state.onboardingDraft.length) {
    list.className = "draft-list empty-state";
    list.textContent = "尚未加入任何持股";
    return;
  }

  list.className = "draft-list";
  setSafeHtml(list, state.onboardingDraft
    .map(
      (holding) => {
        const stockCode = safeText(holding.stockCode, "未知代碼");
        const name = safeCompanyName(holding);
        const shares = Number.isFinite(Number(holding.shares)) ? Math.max(0, Math.floor(Number(holding.shares))) : 0;
        const averageCost = optionalNumber(holding.averageCost);
        return `
        <article class="draft-item" data-draft-code="${escapeHtml(stockCode)}">
          <div>
            <p class="draft-title">${escapeHtml(stockCode)} ${escapeHtml(name)}</p>
            <div class="draft-meta">
              <span>持有股數：${escapeHtml(shares)}</span>
              <span>平均成本：${escapeHtml(averageCost ?? "未填")}</span>
            </div>
          </div>
          <button class="small-danger-btn" type="button" data-remove-draft="${escapeHtml(stockCode)}">移除</button>
        </article>
      `;
      },
    )
    .join(""));
}

function renderSettings() {
  const canEditSettings = isSuperUser();
  const status = $("#settings-status");
  $$("[data-setting]").forEach((input) => {
    input.checked = Boolean(state.settings[input.dataset.setting]);
    input.disabled = !canEditSettings;
    input.closest("label")?.classList.toggle("disabled", !canEditSettings);
  });
  if (status && !canEditSettings) status.textContent = settingsPermissionMessage();
}

function settingsPermissionMessage() {
  if (isSuperUser()) return "";
  if (state.auth?.authenticated) return "需要管理員";
  if (state.auth?.checked) return "需要登入管理員";
  return "確認權限中";
}

function findStrategyStatusDetail(key) {
  return STRATEGY_STATUS_DETAILS.find((item) => item.key === key) || null;
}

function renderStrategyStatusDetail() {
  const target = $("#strategy-status-detail");
  if (!target) return;
  const detail = findStrategyStatusDetail(state.activeStrategyStatusKey);
  $$("[data-strategy-status]").forEach((button) => {
    const active = button.dataset.strategyStatus === state.activeStrategyStatusKey;
    button.classList.toggle("active", active);
    button.setAttribute("aria-expanded", active ? "true" : "false");
  });
  if (!detail) {
    target.classList.add("hidden");
    clearElement(target);
    return;
  }
  target.classList.remove("hidden");
  setSafeHtml(target, `
    <div class="strategy-detail-head">
      <h3>${escapeHtml(detail.label)}</h3>
      <p>${escapeHtml(detail.summary)}</p>
    </div>
    <div class="strategy-detail-list">
      ${detail.items
        .map(
          ([code, title, description]) => `
            <div class="strategy-detail-row">
              <span class="strategy-detail-code">${escapeHtml(code)}</span>
              <div>
                <strong>${escapeHtml(title)}</strong>
                <p>${escapeHtml(description)}</p>
              </div>
            </div>
          `,
        )
        .join("")}
    </div>
  `);
  applyEvidenceBarWidths(target);
}

function renderStrategyRulesGrid() {
  const target = $("#strategy-rules-grid");
  if (!target) return;
  setSafeHtml(target, renderStrategyRuleCards());
}

const MARKET_RESULT_COLUMNS = [
  ["entry", "適合進場"],
  ["watch", "接近觀察"],
  ["excluded", "排除清單"],
];
const MARKET_LIST_PAGE_SIZE = 6;
const MARKET_COLUMN_LABELS = Object.fromEntries(MARKET_RESULT_COLUMNS);

const MARKET_DISCLOSURE_TABS = [
  {
    key: "announced",
    title: "當期已公告，可掃描",
    note: "已抓到目前申報窗口所需的公開資訊，可做初步掃描；若缺歷史年報或週轉率，會在展開細節中標示。",
  },
  {
    key: "pending",
    title: "當期尚未公告，暫不判讀",
    note: "目前申報窗口所需的月報、季報或年報尚未抓到，不應視為進場、觀察或排除結論。",
  },
];

function hasInsufficientData(result = {}) {
  const reasons = Array.isArray(result.reasons) ? result.reasons : [];
  return result.status === "INSUFFICIENT_DATA" || reasons.some((reason) => reason?.severity === "INSUFFICIENT_DATA");
}

function hasFinancialReportForContext(result = {}, filingContext = {}) {
  const targetPeriod = filingContext?.activeFinancialReport?.period;
  if (!targetPeriod) return true;
  const reasons = Array.isArray(result.reasons) ? result.reasons : [];
  return reasons.some(
    (reason) =>
      reason?.code === "OFFICIAL_Q" &&
      reason?.severity !== "INSUFFICIENT_DATA" &&
      String(reason?.message || "").includes(targetPeriod)
  );
}

function hasPublishedScanData(result = {}, filingContext = {}) {
  const reasons = Array.isArray(result.reasons) ? result.reasons : [];
  const usableRuleCodes = new Set(["E3", "E4", "E6", "OFFICIAL_Q", "OFFICIAL_VALUATION"]);
  if (!hasFinancialReportForContext(result, filingContext)) return false;
  if (result.status && result.status !== "INSUFFICIENT_DATA") return true;
  return reasons.some((reason) => usableRuleCodes.has(reason?.code) && reason?.severity !== "INSUFFICIENT_DATA");
}

function isPartialPublishedResult(result = {}, filingContext = {}) {
  return result.status === "INSUFFICIENT_DATA" && hasPublishedScanData(result, filingContext);
}

function groupMarketScanResults(scan) {
  const grouped = {
    announced: { entry: [], watch: [], excluded: [] },
    pending: { entry: [], watch: [], excluded: [] },
  };
  for (const [key] of MARKET_RESULT_COLUMNS) {
    for (const result of scan?.[key] || []) {
      const groupKey = hasPublishedScanData(result, scan?.filingContext) ? "announced" : "pending";
      grouped[groupKey][key].push(result);
    }
  }
  return grouped;
}

function countMarketGroup(group) {
  return MARKET_RESULT_COLUMNS.reduce((total, [key]) => total + (group?.[key]?.length || 0), 0);
}

function activeMarketDisclosureKey(tab = state.activeMarketDisclosureTab) {
  return MARKET_DISCLOSURE_TABS.some((item) => item.key === tab) ? tab : "announced";
}

function renderOverviewStats(scan = state.marketScan, activeTab = state.activeMarketDisclosureTab) {
  if (typeof document === "undefined") return;
  const metricKeys = ["entry", "watch", "excluded"];
  const values = Object.fromEntries(metricKeys.map((key) => [key, "--"]));
  const note = scan?.generatedAt ? `${new Date(scan.generatedAt).toLocaleDateString()} 更新` : "等待掃描";
  if (scan) {
    const grouped = groupMarketScanResults(scan);
    const group = grouped[activeMarketDisclosureKey(activeTab)];
    for (const key of metricKeys) {
      values[key] = group?.[key]?.length || 0;
    }
  }
  for (const key of metricKeys) {
    const count = document.querySelector(`#overview-${key}-count`);
    const noteEl = document.querySelector(`#overview-${key}-note`);
    if (count) count.textContent = String(values[key]);
    if (noteEl) noteEl.textContent = note;
  }
}

function activeMarketColumnKey() {
  return MARKET_COLUMN_LABELS[state.activeMarketColumn] ? state.activeMarketColumn : "entry";
}

function ruleByCode(result = {}, code = "") {
  const reasons = Array.isArray(result.reasons) ? result.reasons : [];
  return reasons.find((reason) => reason?.code === code) || null;
}

function numericFromText(value) {
  const match = String(value || "").replace(",", "").match(/-?\d+(?:\.\d+)?/);
  return match ? Number(match[0]) : null;
}

function e4PerValue(result = {}) {
  const e4 = ruleByCode(result, "E4");
  const fromMessage = numericFromText(e4?.message);
  if (Number.isFinite(fromMessage)) return fromMessage;
  return null;
}

function sortMarketResultsForDisplay(columnKey, results = []) {
  const normalized = Array.isArray(results) ? [...results] : [];
  if (columnKey !== "entry") return normalized;
  return normalized.sort((left, right) => {
    const leftPer = e4PerValue(left);
    const rightPer = e4PerValue(right);
    if (Number.isFinite(leftPer) && Number.isFinite(rightPer) && leftPer !== rightPer) return leftPer - rightPer;
    if (Number.isFinite(leftPer) !== Number.isFinite(rightPer)) return Number.isFinite(leftPer) ? -1 : 1;
    return safeText(left.stockCode).localeCompare(safeText(right.stockCode));
  });
}

function marketColumnNote(columnKey) {
  if (columnKey === "entry") return "排序：E4 PER 低 → 高";
  if (columnKey === "watch") return "依股票代號排序；展開可看未通過或待補原因";
  if (columnKey === "excluded") return "依股票代號排序；展開可看排除原因";
  return "";
}

function updateMarketColumnNav(grouped = null, activeTab = state.activeMarketDisclosureTab) {
  const tabKey = activeMarketDisclosureKey(activeTab);
  $$("[data-market-column-nav]").forEach((button) => {
    const columnKey = button.dataset.marketColumnNav;
    const label = MARKET_COLUMN_LABELS[columnKey] || columnKey;
    const count = grouped ? grouped?.[tabKey]?.[columnKey]?.length ?? 0 : null;
    button.classList.toggle("active", columnKey === activeMarketColumnKey());
    button.textContent = count === null ? label : `${label} (${count})`;
  });
}

function marketResultId(result = {}, groupKey = "", columnKey = "") {
  return [groupKey, columnKey, safeText(result.stockCode, "unknown"), safeText(result.status, "unknown")].join(":");
}

function findMarketResultById(resultId) {
  if (!state.marketScan) return null;
  const grouped = groupMarketScanResults(state.marketScan);
  for (const tab of MARKET_DISCLOSURE_TABS) {
    for (const [columnKey] of MARKET_RESULT_COLUMNS) {
      for (const result of grouped?.[tab.key]?.[columnKey] || []) {
        if (marketResultId(result, tab.key, columnKey) === resultId) return result;
      }
    }
  }
  return null;
}

async function loadMarketResultDetails(resultId) {
  const result = findMarketResultById(resultId);
  if (!result || result.hasFullDetails || result.detailLoading || !result.detailsAvailable) return;
  result.detailLoading = true;
  result.detailError = "";
  renderMarketResults();
  try {
    const detail = await apiJson(`/api/analyze/${result.stockCode}`, {
      method: "POST",
      body: JSON.stringify({ settings: state.settings }),
    });
    Object.assign(result, detail, {
      detailsAvailable: false,
      hasFullDetails: true,
      detailLoading: false,
      detailError: "",
    });
  } catch (error) {
    result.detailLoading = false;
    result.detailError = error.message || "細項載入失敗";
  }
  renderMarketResults();
}

function getMarketPage(groupKey, columnKey) {
  return Math.max(0, Number(state.marketListPages?.[groupKey]?.[columnKey]) || 0);
}

function resetMarketListUi() {
  state.marketListPages = {
    announced: { entry: 0, watch: 0, excluded: 0 },
    pending: { entry: 0, watch: 0, excluded: 0 },
  };
  state.expandedMarketResultIds = new Set();
}

function clampMarketListPages(grouped) {
  for (const tab of MARKET_DISCLOSURE_TABS) {
    for (const [columnKey] of MARKET_RESULT_COLUMNS) {
      const total = grouped?.[tab.key]?.[columnKey]?.length || 0;
      const maxPage = Math.max(0, Math.ceil(total / MARKET_LIST_PAGE_SIZE) - 1);
      state.marketListPages[tab.key][columnKey] = Math.min(getMarketPage(tab.key, columnKey), maxPage);
    }
  }
}

function renderMarketPagination(groupKey, columnKey, total) {
  const page = getMarketPage(groupKey, columnKey);
  const totalPages = Math.max(1, Math.ceil(total / MARKET_LIST_PAGE_SIZE));
  const start = total ? page * MARKET_LIST_PAGE_SIZE + 1 : 0;
  const end = Math.min(total, (page + 1) * MARKET_LIST_PAGE_SIZE);
  if (totalPages <= 1) {
    return total ? `<div class="market-pagination"><span>顯示 ${escapeHtml(start)}-${escapeHtml(end)} / ${escapeHtml(total)}</span></div>` : "";
  }
  return `
    <div class="market-pagination">
      <span>顯示 ${escapeHtml(start)}-${escapeHtml(end)} / ${escapeHtml(total)}，第 ${escapeHtml(page + 1)} / ${escapeHtml(totalPages)} 頁</span>
      <div class="market-page-buttons">
        <button class="mini-btn" type="button" data-market-page-tab="${escapeHtml(groupKey)}" data-market-page-column="${escapeHtml(columnKey)}" data-market-page-dir="-1" ${page <= 0 ? "disabled" : ""}>上一頁</button>
        <button class="mini-btn" type="button" data-market-page-tab="${escapeHtml(groupKey)}" data-market-page-column="${escapeHtml(columnKey)}" data-market-page-dir="1" ${page >= totalPages - 1 ? "disabled" : ""}>下一頁</button>
      </div>
    </div>
  `;
}

function renderMarketResultDetails(result, options = {}) {
  const reasons = sortRulesForDisplay(Array.isArray(result.reasons) ? result.reasons.filter(Boolean) : []);
  const { status: displayStatus, summary } = displayResultStatus(result, options.disclosureGroup);
  return `
    <div class="market-result-details">
      ${result.detailLoading ? `<p class="muted">正在載入完整細項...</p>` : ""}
      ${result.detailError ? `<p class="form-error">${escapeHtml(result.detailError)}</p>` : ""}
      <div class="detail-summary">
        <span class="status-pill ${statusClass(displayStatus)}">${escapeHtml(statusLabel(displayStatus))}</span>
        <p class="muted">${escapeHtml(summary)}</p>
      </div>
      <div class="rules">${reasons.map(renderRule).join("")}</div>
      ${resultActionButtons(result, options)}
    </div>
  `;
}

function renderMarketResultRow(result, options = {}) {
  const companyName = result.companyName || safeCompanyName(result);
  const stockCode = safeText(result.stockCode, "未知代碼");
  const resultId = marketResultId(result, options.disclosureGroup, options.columnKey);
  const expanded = state.expandedMarketResultIds.has(resultId);
  const reasons = sortRulesForDisplay(Array.isArray(result.reasons) ? result.reasons.filter(Boolean) : []);
  const { status: displayStatus } = displayResultStatus(result, options.disclosureGroup);
  const metaParts = [safeText(result.industryName || result.industry, ""), safeText(result.market, "")].filter(Boolean);
  const meta = metaParts.length ? metaParts.join(" · ") : statusLabel(displayStatus);
  const rulePreview = reasons
    .slice(0, 3)
    .map((rule) => `<span class="rule-chip">${escapeHtml(rule.code || "")}</span>`)
    .join("");
  return `
    <article class="market-result-item ${expanded ? "expanded" : ""}">
      <button class="market-result-summary" type="button" data-market-result-toggle="${escapeHtml(resultId)}" aria-expanded="${expanded ? "true" : "false"}">
        <span class="status-dot ${statusClass(displayStatus)}" aria-hidden="true"></span>
        <span class="market-result-body">
          <span class="market-result-name">${escapeHtml(stockCode)} ${escapeHtml(companyName)}</span>
          <span class="market-result-meta">${escapeHtml(meta)}</span>
        </span>
        <span class="market-rule-preview">${rulePreview}</span>
        <span class="market-expand-icon" aria-hidden="true">${expanded ? "−" : "+"}</span>
      </button>
      ${expanded ? renderMarketResultDetails(result, options) : ""}
    </article>
  `;
}

function renderMarketColumn(groupKey, columnKey, title, results) {
  const page = getMarketPage(groupKey, columnKey);
  const pageStart = page * MARKET_LIST_PAGE_SIZE;
  const sortedResults = sortMarketResultsForDisplay(columnKey, results);
  const visibleResults = sortedResults.slice(pageStart, pageStart + MARKET_LIST_PAGE_SIZE);
  const note = marketColumnNote(columnKey);
  return `
    <div class="result-column">
      <div class="result-column-head">
        <h3>${escapeHtml(title)} (${escapeHtml(sortedResults.length)})</h3>
        ${note ? `<span class="result-column-note">${escapeHtml(note)}</span>` : ""}
      </div>
      ${
        visibleResults.length
          ? `<div class="market-result-list">${visibleResults
              .map((result) =>
                renderMarketResultRow(result, {
                  allowAddAction: true,
                  disclosureGroup: groupKey,
                  columnKey,
                })
              )
              .join("")}</div>`
          : `<div class="empty-state">無資料</div>`
      }
      ${renderMarketPagination(groupKey, columnKey, sortedResults.length)}
    </div>
  `;
}

function formatCacheTime(value) {
  if (!value) return "尚無紀錄";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "尚無紀錄";
  return date.toLocaleString();
}

function cacheRefreshLabel(status = "") {
  const labels = {
    completed_sync: "已同步建立快取",
    fresh: "快取仍有效",
    queued: "已排入背景更新",
    running: "背景更新中",
    success: "背景更新完成",
    failed: "背景更新失敗",
    cache_only: "僅讀取快取",
  };
  return labels[status] || status || "未請求更新";
}

function renderScanCacheStatus(scan = {}) {
  const cache = scan.cacheStatus;
  if (!cache) return "";
  const staleText = cache.isStale ? "快取已過期，會低負載補資料" : "快取仍在有效期限內";
  return `
    <div class="cache-status-note">
      <span><strong>快取狀態</strong>：${cache.cacheHit ? "先讀已存快取" : "同步建立新快取"}</span>
      <span>刷新狀態：${escapeHtml(cacheRefreshLabel(cache.refreshStatus))}</span>
      <span>資料時間：${escapeHtml(formatCacheTime(cache.storedAt))}</span>
      <span>下次檢查：${escapeHtml(formatCacheTime(cache.nextRefreshAfter))}</span>
      <span>${escapeHtml(staleText)}</span>
    </div>
  `;
}

function renderMarketResults() {
  const target = $("#market-results");
  if (!state.marketScan) {
    setEmptyState(target, "尚未掃描市場");
    updateMarketColumnNav();
    renderOverviewStats();
    return;
  }
  $("#scan-time").textContent = `更新 ${new Date(state.marketScan.generatedAt).toLocaleString()}`;
  const grouped = groupMarketScanResults(state.marketScan);
  clampMarketListPages(grouped);
  const activeTab = activeMarketDisclosureKey();
  const activeMeta = MARKET_DISCLOSURE_TABS.find((tab) => tab.key === activeTab);
  const activeGroup = grouped[activeTab];
  const activeColumn = activeMarketColumnKey();
  const activeColumnTitle = MARKET_COLUMN_LABELS[activeColumn] || "掃描結果";
  updateMarketColumnNav(grouped, activeTab);
  const filingSummary = state.marketScan.filingContext?.activeFinancialReport
    ? `目前依 ${state.marketScan.filingContext.activeFinancialReport.label}（一般公司期限 ${state.marketScan.filingContext.activeFinancialReport.generalDeadline}${
        state.marketScan.filingContext.activeFinancialReport.financialDeadline
          ? `，金控期限 ${state.marketScan.filingContext.activeFinancialReport.financialDeadline}`
          : ""
      }）判斷當期已公告。`
    : `目前非季報/年報申報窗口，主要依 ${state.marketScan.filingContext?.monthlyRevenuePeriod || "最新"} 月營收公告判斷。`;
  setSafeHtml(target, `
    <div class="data-source-note">
      <strong>資料來源：${escapeHtml(state.marketScan.dataSource || "mock")}</strong>
      <span>${escapeHtml(state.marketScan.note || "目前為示範樣本，不代表真實全台股即時掃描。")}</span>
    </div>
    ${renderScanCacheStatus(state.marketScan)}
    <div class="market-disclosure-tabs" aria-label="公告狀態分組">
      ${MARKET_DISCLOSURE_TABS.map((tab) => {
        const total = countMarketGroup(grouped[tab.key]);
        return `
          <button class="tab ${tab.key === activeTab ? "active" : ""}" type="button" data-market-disclosure-tab="${escapeHtml(tab.key)}">
            ${escapeHtml(tab.title)} (${escapeHtml(total)})
          </button>
        `;
      }).join("")}
    </div>
    <div class="data-source-note disclosure-note">
      <strong>${escapeHtml(activeMeta.title)}</strong>
      <span>${escapeHtml(filingSummary)} ${escapeHtml(activeMeta.note)} 目前顯示「${escapeHtml(activeColumnTitle)}」；每頁最多顯示 ${escapeHtml(MARKET_LIST_PAGE_SIZE)} 家，按 + 展開條件細節，也可匯出完整清單。</span>
    </div>
    <div class="result-columns single-result-column">
      ${renderMarketColumn(activeTab, activeColumn, activeColumnTitle, activeGroup[activeColumn] || [])}
    </div>
  `);
  applyEvidenceBarWidths(target);
  renderOverviewStats(state.marketScan, activeTab);
}

function renderDataAndScheduler() {
  const dataTarget = $("#data-source-status");
  const schedulerTarget = $("#scheduler-status");
  if (dataTarget) {
    if (!state.dataSourceStatus) {
      setEmptyState(dataTarget, "尚未讀取資料來源狀態");
    } else {
      const status = state.dataSourceStatus;
      setSafeHtml(dataTarget, `
        <article class="card">
          <h3 class="stock-title">目前啟用資料源</h3>
          <p class="muted">${escapeHtml(status.activeProvider)}</p>
          <p class="muted">Realtime：${status.activeProviderIsRealtime ? "是" : "否"}；全市場 universe：${status.activeProviderIsFullMarket ? "是" : "否"}；完整財報因子：${status.activeProviderHasCompleteFundamentals ? "是" : "否"}</p>
        </article>
        <article class="card">
          <h3 class="stock-title">Mock Universe</h3>
          <p class="muted">目前示範樣本 ${escapeHtml(status.mockUniverseSize)} 檔。</p>
        </article>
        <article class="card">
          <h3 class="stock-title">官方 TWSE / TPEx</h3>
          <p class="muted">Universe：${escapeHtml(status.officialUniverseSize ?? "尚未啟用")}；月營收快照：${escapeHtml(status.officialMonthlySnapshotSize ?? "尚未啟用")}。</p>
          <p class="muted">最新季損益：${escapeHtml(status.officialIncomeStatementSize ?? "尚未啟用")}；資產負債：${escapeHtml(status.officialBalanceSheetSize ?? "尚未啟用")}；估值：${escapeHtml(status.officialValuationSize ?? "尚未啟用")}。</p>
          <p class="muted">官方歷史快取：${escapeHtml(status.officialHistoryRows ?? 0)} 筆；財報匯入：${escapeHtml(status.fundamentalsImportRows ?? 0)} 筆。</p>
          <p class="muted">${escapeHtml(status.officialHistoricalFundamentals?.note || "已整合官方最新季 EPS、淨利、毛利率與 PER/PBR；缺口會列為待補。")}</p>
        </article>
        <article class="card">
          <h3 class="stock-title">外部整合</h3>
          <p class="muted">通知：${escapeHtml((state.integrationStatus?.notifications || []).filter((item) => item.configured).length)} 個已設定；券商同步：${state.integrationStatus?.broker?.configured ? "已設定" : "未設定"}；AI 摘要：${state.integrationStatus?.aiSummary?.configured ? "已設定" : "未設定"}。</p>
          <p class="muted">未設定金鑰或授權前，系統不會對外發送訊息或讀取真實券商持股。</p>
        </article>
        <article class="card">
          <h3 class="stock-title">回測資料</h3>
          <p class="muted">狀態：${escapeHtml(state.backtestStatus?.status || "未讀取")}；交易數：${escapeHtml(state.backtestStatus?.metrics?.tradeCount ?? 0)}。</p>
          <p class="muted">${escapeHtml(state.backtestStatus?.note || "匯入 data/backtest_history.csv 後可模擬進出場與績效。")}</p>
        </article>
      `);
    }
  }

  if (schedulerTarget) {
    if (!state.schedulerStatus) {
      setSafeHtml(schedulerTarget, `<strong>排程狀態：</strong><span>尚未讀取</span>`);
    } else {
      const autoAction = state.schedulerAutoScan?.action || "未執行";
      setSafeHtml(schedulerTarget, `
        <strong>排程狀態：${escapeHtml(state.schedulerStatus.status)}</strong>
        <span>事件：${escapeHtml((state.schedulerStatus.events || []).join("、") || "無")}；下一交易日：${escapeHtml(state.schedulerStatus.nextTradingDay)}；自動掃描：${escapeHtml(autoAction)}</span>
      `);
    }
  }
}

function renderHoldingResults() {
  const target = $("#holding-results");
  if (!state.holdingsScan) {
    setEmptyState(target, "尚未掃描持股");
    return;
  }
  $("#scan-time").textContent = `更新 ${new Date(state.holdingsScan.generatedAt).toLocaleString()}`;
  const missing = state.holdingsScan.missing || [];
  setSafeHtml(target, `
    <div class="stack">
      ${(state.holdingsScan.results || []).map((result) => renderAnalysisCard(result, { allowExitAction: true, disclosureGroup: "holding" })).join("")}
      ${missing
        .map(
          (item) => {
            const stockCode = safeText(item.stockCode, "未知代碼");
            const name = safeText(item.name, "未知公司");
            return `
          <article class="card">
            <h3 class="stock-title">${escapeHtml(stockCode)} ${escapeHtml(name)}</h3>
            <p class="form-error">${escapeHtml(item.reason || "找不到財報示範資料")}</p>
          </article>
        `;
          },
        )
        .join("")}
      ${!(state.holdingsScan.results || []).length && !missing.length ? `<div class="empty-state">沒有可掃描持股</div>` : ""}
    </div>
  `);
  applyEvidenceBarWidths(target);
}

function showTab(tab) {
  $$(".tab").forEach((button) => button.classList.toggle("active", button.dataset.tab === tab));
  $("#market-results").classList.toggle("hidden", tab !== "market");
  $("#holding-results").classList.toggle("hidden", tab !== "holdings");
}

function showView(view) {
  if (view === "admin" && !isSuperUser()) {
    view = "overview";
  }
  state.activeView = view;
  document.body.classList.toggle("scan-view-active", view === "scan");
  const titles = {
    overview: ["總覽", "快速查看資料來源、持股狀態與策略完成度。"],
    search: ["搜尋與單檔分析", "查詢股票並查看單檔規則原因。"],
    holdings: ["我的持股", "管理本機持股，掃描續抱、加碼、警戒與出場。"],
    scan: ["掃描結果", "查看目前資料源與持股掃描結果。"],
    settings: ["設定", "調整掃描範圍與策略開關。"],
    strategy: ["策略規則", ""],
    data: ["資料與排程", "查看資料來源、官方 adapter 與事件驅動排程狀態。"],
  };
  titles.admin = ["使用者管理", "只允許管理員管理註冊帳號與刪除一般使用者。"];
  $$(".nav-item").forEach((button) => button.classList.toggle("active", button.dataset.view === view));
  $$("[data-view-panel]").forEach((panel) => panel.classList.toggle("hidden", panel.dataset.viewPanel !== view));
  const [title, subtitle] = titles[view] || titles.overview;
  $("#view-title").textContent = title;
  $("#view-subtitle").textContent = subtitle;
  $("#view-subtitle").classList.toggle("hidden", !subtitle);
  if (view === "admin" && isSuperUser() && !state.adminUsers.length && !state.adminIsLoading) loadAdminUsers();
  if (view === "admin") renderAdminUsers();
  if (view === "data") renderDataAndScheduler();
  updateMarketColumnNav(state.marketScan ? groupMarketScanResults(state.marketScan) : null, state.activeMarketDisclosureTab);
  renderOverviewStats(state.marketScan, state.activeMarketDisclosureTab);
}

async function loadCompanies() {
  try {
    const items = [];
    let page = 1;
    let hasMore = true;
    while (hasMore && page <= MAX_COMPANY_PAGES) {
      const payload = await apiJson(`/api/companies?page=${page}&limit=${COMPANIES_PAGE_LIMIT}`);
      items.push(...(Array.isArray(payload.items) ? payload.items : []));
      hasMore = payload.hasMore === true;
      page += 1;
    }
    const normalized = normalizeCompanies(items);
    state.companies = normalized.length ? normalized : normalizeCompanies(DEFAULT_COMPANIES);
    $("#api-status").textContent = "已連線";
  } catch {
    state.companies = normalizeCompanies(DEFAULT_COMPANIES);
    $("#api-status").textContent = "離線資料";
  }
}

async function loadSettings() {
  try {
    state.settings = await apiJson("/api/settings");
    $("#settings-status").textContent = "已同步";
  } catch {
    state.settings = { ...DEFAULT_SETTINGS };
    $("#settings-status").textContent = "本地預設";
  }
  renderSettings();
}

async function loadDataStatus() {
  try {
    const status = await apiJson("/api/app-status");
    state.dataSourceStatus = status.dataSourceStatus;
    state.schedulerStatus = status.schedulerStatus;
    state.schedulerAutoScan = status.schedulerAutoScan;
    state.integrationStatus = status.integrationStatus;
    state.backtestStatus = status.backtestStatus;
    if (state.schedulerAutoScan?.scan) {
      state.marketScan = state.schedulerAutoScan.scan;
      resetMarketListUi();
      renderMarketResults();
    }
  } catch {
    state.dataSourceStatus = null;
    state.schedulerStatus = null;
    state.schedulerAutoScan = null;
    state.integrationStatus = null;
    state.backtestStatus = null;
  }
  renderDataAndScheduler();
}

async function saveSettings() {
  if (!isSuperUser()) {
    $("#settings-status").textContent = settingsPermissionMessage();
    renderSettings();
    return;
  }
  try {
    state.settings = await apiJson("/api/settings", {
      method: "PUT",
      body: JSON.stringify(state.settings),
    });
    await loadCompanies();
    await loadDataStatus();
    $("#settings-status").textContent = "已儲存";
  } catch (error) {
    $("#settings-status").textContent = error.message || "儲存失敗";
  }
  renderSelectedCompany();
  renderHoldings();
  requestHoldingsScanRefresh();
  renderSettings();
}

function localSearch(query) {
  const normalized = normalizeText(query).toLowerCase();
  if (!normalized) return [];
  return companySource(state.companies)
    .filter((company) => `${company.stockCode} ${company.name} ${company.industryName}`.toLowerCase().includes(normalized))
    .slice(0, 20);
}

async function searchCompanies(query) {
  if (!normalizeText(query)) return [];
  const localResults = localSearch(query);
  if (localResults.length) return localResults;
  try {
    const payload = await apiJson(`/api/companies/search?q=${encodeURIComponent(query)}`);
    return normalizeCompanies(payload.items);
  } catch {
    return localSearch(query);
  }
}

function renderSuggestions(items) {
  const box = $("#search-suggestions");
  const companies = normalizeCompanies(items);
  if (!companies.length) {
    box.classList.remove("open");
    clearElement(box);
    return;
  }
  setSafeHtml(box, companies
    .map(
      (company) => `
      <button class="suggestion-item" type="button" data-select-code="${escapeHtml(company.stockCode)}">
        <span>${escapeHtml(company.stockCode)} ${escapeHtml(company.name)}</span>
        <span class="muted">${escapeHtml(company.market)}</span>
      </button>
    `,
    )
    .join(""));
  box.classList.add("open");
}

async function analyzeSelectedCompany() {
  if (!state.selectedCompany) return;
  const target = $("#single-analysis");
  setEmptyState(target, "分析中");
  try {
    const result = await apiJson(`/api/analyze/${state.selectedCompany.stockCode}`, {
      method: "POST",
      body: JSON.stringify({ settings: state.settings }),
    });
    setSafeHtml(target, renderAnalysisCard(result));
    applyEvidenceBarWidths(target);
  } catch (error) {
    setFormError(target, error.message || "分析失敗");
  }
}

async function scanMarket() {
  return refreshMarketScan({ revealResults: true });
}

function renderMarketScanError(target, error) {
  if (target) setFormError(target, error.message || "掃描失敗");
}

async function refreshMarketScan({ revealResults = false, refreshMode = "auto" } = {}) {
  const target = $("#market-results");
  if (revealResults) {
    setEmptyState(target, "掃描中");
    showView("scan");
    showTab("market");
  }
  if (marketScanRefreshPromise) {
    try {
      await marketScanRefreshPromise;
      renderMarketResults();
    } catch (error) {
      if (revealResults) renderMarketScanError(target, error);
      return null;
    }
    return state.marketScan;
  }
  marketScanRefreshPromise = apiJson("/api/scan/market", {
    method: "POST",
    body: JSON.stringify({ settings: state.settings, refreshMode }),
  })
    .then((scan) => {
      state.marketScan = scan;
      resetMarketListUi();
      return scan;
    })
    .finally(() => {
      marketScanRefreshPromise = null;
    });
  try {
    await marketScanRefreshPromise;
  } catch (error) {
    if (revealResults) renderMarketScanError(target, error);
    return null;
  }
  renderMarketResults();
  return state.marketScan;
}

function refreshOverviewMarketScan() {
  void refreshMarketScan({ refreshMode: "auto" }).catch(() => {});
}

async function scanHoldings() {
  const target = $("#holding-results");
  showView("scan");
  showTab("holdings");
  setEmptyState(target, "正在套用 X1-X5 出場規則");
  await refreshStoredHoldingAnalysis({ renderResults: true });
}

async function exportReport(kind, reportFormat) {
  const endpoint = kind === "holdings" ? "/api/reports/holdings" : "/api/reports/market";
  const payload =
    kind === "holdings"
      ? { holdings: state.holdings, settings: state.settings }
      : { settings: state.settings };
  const response = await apiFetch(`${endpoint}?report_format=${encodeURIComponent(reportFormat)}`, {
    method: "POST",
    body: JSON.stringify(payload),
  });
  if (!response.ok) throw new Error(apiErrorMessage(response, await response.text()));
  const blob = await response.blob();
  const extension = reportFormat === "csv" ? "csv" : "md";
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = `${kind}_scan_${new Date().toISOString().slice(0, 10)}.${extension}`;
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
}

function openOnboarding() {
  if (!state.auth?.authenticated) {
    openAuthGate();
    return;
  }
  renderOnboardingDraft();
  $("#onboarding-modal").classList.remove("hidden");
  $("#onboarding-error").textContent = "";
  $("#onboarding-stock-input").focus();
}

function closeOnboarding(markDone = true) {
  $("#onboarding-modal").classList.add("hidden");
  if (markDone) markOnboardingDone();
  $("#onboarding-error").textContent = "";
}

function addHoldingFromInput(rawInput, sharesInput, costInput) {
  const parsed = parseStockInput(rawInput, state.companies);
  if (!parsed.ok) return parsed;
  const shares = sharesInput === "" || sharesInput === undefined ? parsed.shares : Math.max(0, Math.floor(Number(sharesInput)));
  const averageCost =
    costInput === "" || costInput === undefined || costInput === null ? parsed.averageCost : Math.max(0, Number(costInput));
  upsertHolding({
    stockCode: parsed.stockCode,
    name: parsed.name,
    shares: Number.isFinite(shares) ? shares : 0,
    averageCost: Number.isFinite(averageCost) ? averageCost : null,
  });
  return { ok: true };
}

function addOnboardingDraftFromFields() {
  const parsed = parseStockInput($("#onboarding-stock-input").value, state.companies);
  if (!parsed.ok) return parsed;

  const sharesValue = $("#onboarding-shares-input").value;
  const costValue = $("#onboarding-cost-input").value;
  const shares = sharesValue === "" ? 0 : Math.max(0, Math.floor(Number(sharesValue)));
  const averageCost = costValue === "" ? null : Math.max(0, Number(costValue));

  const draftHolding = {
    stockCode: parsed.stockCode,
    name: parsed.name,
    shares: Number.isFinite(shares) ? shares : 0,
    averageCost: Number.isFinite(averageCost) ? averageCost : null,
  };
  const existing = state.onboardingDraft.find((item) => item.stockCode === draftHolding.stockCode);
  if (existing) {
    Object.assign(existing, draftHolding);
  } else {
    state.onboardingDraft.push(draftHolding);
  }
  state.onboardingDraft.sort((a, b) => a.stockCode.localeCompare(b.stockCode));

  $("#onboarding-add-form").reset();
  $("#onboarding-stock-input").focus();
  renderOnboardingDraft();
  return { ok: true };
}

function bindEvents() {
  normalizeResponsiveNavigation();
  const mobileMenuToggle = $("#mobile-menu-toggle");
  if (mobileMenuToggle) mobileMenuToggle.addEventListener("click", toggleMobileMenu);
  const mobileNavBackdrop = $("#mobile-nav-backdrop");
  if (mobileNavBackdrop) mobileNavBackdrop.addEventListener("click", closeMobileMenu);
  window.addEventListener("keydown", (event) => {
    if (event.key !== "Escape") return;
    if (!$("#onboarding-modal")?.classList.contains("hidden")) {
      closeOnboarding(false);
      return;
    }
    if (!$("#auth-modal")?.classList.contains("hidden")) {
      closeAuthGate({ clearMessage: true });
      return;
    }
    closeMobileMenu();
  });
  let _resizeTimer = null;
  window.addEventListener("resize", () => {
    clearTimeout(_resizeTimer);
    _resizeTimer = setTimeout(() => {
      normalizeResponsiveNavigation();
      if (window.innerWidth > MOBILE_NAV_BREAKPOINT) closeMobileMenu();
    }, 100);
  });

  const sideNav = document.querySelector(".side-nav");
  if (sideNav) {
    sideNav.addEventListener("click", (event) => {
      const marketColumnButton = event.target.closest("[data-market-column-nav]");
      if (marketColumnButton && sideNav.contains(marketColumnButton)) {
        state.activeMarketColumn = marketColumnButton.dataset.marketColumnNav;
        showView("scan");
        showTab("market");
        renderMarketResults();
        closeMobileMenu();
        return;
      }

      const navButton = event.target.closest(".nav-item[data-view]");
      if (!navButton || !sideNav.contains(navButton)) return;
      if (navButton.dataset.navGroupToggle === "scan" && isMobileNavigation()) {
        const nextExpanded = navButton.getAttribute("aria-expanded") !== "true";
        setScanNavExpanded(nextExpanded);
        showView("scan");
        return;
      }
      showView(navButton.dataset.view);
      closeMobileMenu();
    });
  }

  const authForm = $("#auth-form");
  if (authForm) {
    authForm.addEventListener("submit", (event) => {
      event.preventDefault();
      authenticateFromForm("login");
    });
  }
  const registerButton = $("#auth-register-btn");
  if (registerButton) registerButton.addEventListener("click", () => authenticateFromForm("register"));
  const authModalForm = $("#auth-modal-form");
  if (authModalForm) {
    authModalForm.addEventListener("submit", (event) => {
      event.preventDefault();
      authenticateFromForm("register", "modal");
    });
  }
  const authModalLoginButton = $("#auth-modal-login-btn");
  if (authModalLoginButton) authModalLoginButton.addEventListener("click", () => authenticateFromForm("login", "modal"));
  const closeAuthModalButton = $("#close-auth-modal-btn");
  if (closeAuthModalButton) closeAuthModalButton.addEventListener("click", () => closeAuthGate({ clearMessage: true }));
  const deferAuthModalButton = $("#defer-auth-modal-btn");
  if (deferAuthModalButton) deferAuthModalButton.addEventListener("click", () => closeAuthGate({ clearMessage: true }));
  const authModal = $("#auth-modal");
  if (authModal) {
    authModal.addEventListener("click", (event) => {
      if (event.target === authModal) closeAuthGate({ clearMessage: true });
    });
  }
  const logoutButton = $("#auth-logout-btn");
  if (logoutButton) logoutButton.addEventListener("click", logoutAccount);
  const importLocalButton = $("#import-local-holdings-btn");
  if (importLocalButton) importLocalButton.addEventListener("click", importLocalHoldingsToAccount);
  const refreshAdminUsersButton = $("#refresh-admin-users-btn");
  if (refreshAdminUsersButton) refreshAdminUsersButton.addEventListener("click", loadAdminUsers);
  const adminUsersList = $("#admin-users-list");
  if (adminUsersList) {
    adminUsersList.addEventListener("click", (event) => {
      const button = event.target.closest("[data-delete-user]");
      if (!button) return;
      deleteAdminUser(button.dataset.deleteUser, button.dataset.deleteUsername || "此使用者");
    });
  }

  let searchTimer = null;
  $("#stock-search").addEventListener("input", (event) => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(async () => {
      renderSuggestions(await searchCompanies(event.target.value));
    }, 140);
  });

  $("#search-suggestions").addEventListener("click", (event) => {
    const button = event.target.closest("[data-select-code]");
    if (!button) return;
    state.selectedCompany = findCompanyByCodeOrName(button.dataset.selectCode, state.companies);
    $("#search-suggestions").classList.remove("open");
    if (!state.selectedCompany) {
      $("#stock-search").value = "";
      renderSelectedCompany();
      return;
    }
    $("#stock-search").value = `${state.selectedCompany.stockCode} ${state.selectedCompany.name}`;
    renderSelectedCompany();
    analyzeSelectedCompany();
  });

  $("#selected-company").addEventListener("click", (event) => {
    if (event.target.id === "analyze-selected-btn") analyzeSelectedCompany();
    if (event.target.id === "add-selected-btn" && state.selectedCompany) {
      upsertHolding({ ...state.selectedCompany, shares: 0, averageCost: null });
    }
  });

  $("#manual-add-form").addEventListener("submit", (event) => {
    event.preventDefault();
    const result = addHoldingFromInput($("#manual-stock-input").value, $("#manual-shares-input").value, $("#manual-cost-input").value);
    $("#holding-error").textContent = result.ok ? "" : result.error;
    if (result.ok) event.target.reset();
  });

  const scanStoredHoldingsButton = $("#scan-stored-holdings-btn");
  if (scanStoredHoldingsButton) {
    scanStoredHoldingsButton.addEventListener("click", () => {
      refreshStoredHoldingAnalysis({ renderResults: false });
    });
  }

  const holdingExitAlertsTarget = $("#holding-exit-alerts");
  if (holdingExitAlertsTarget) {
    holdingExitAlertsTarget.addEventListener("click", (event) => {
      if (!event.target.closest("[data-open-holding-alert-details]")) return;
      showView("scan");
      showTab("holdings");
      renderHoldingResults();
    });
  }

  $("#holdings-list").addEventListener("click", (event) => {
    const card = event.target.closest("[data-holding-code]");
    const action = event.target.dataset.action;
    if (!card || !action) return;
    const code = card.dataset.holdingCode;
    const holding = state.holdings.find((item) => item.stockCode === code);
    if (!holding && action !== "delete") return;
    if (action === "edit") {
      state.editingHoldingCode = code;
      renderHoldings();
      return;
    }
    if (action === "cancel-edit") {
      state.editingHoldingCode = null;
      renderHoldings();
      return;
    }
    if (action === "delete") {
      clearHolding(code);
      return;
    }
    if (action === "save") {
      const shares = Number(card.querySelector('[data-field="shares"]').value);
      const averageCostValue = card.querySelector('[data-field="averageCost"]').value;
      holding.shares = Number.isFinite(shares) ? Math.max(0, Math.floor(shares)) : holding.shares;
      holding.averageCost = averageCostValue === "" ? null : Math.max(0, Number(averageCostValue));
      state.editingHoldingCode = null;
      saveHoldings();
      refreshHoldingsDependentViews();
    }
    if (action === "reduce") {
      reduceHolding(code, card.querySelector('[data-field="reduce"]').value);
    }
  });

  $("#export-market-md-btn").addEventListener("click", () => exportReport("market", "markdown"));
  $("#export-market-csv-btn").addEventListener("click", () => exportReport("market", "csv"));
  $("#export-holdings-md-btn").addEventListener("click", () => exportReport("holdings", "markdown"));
  $("#export-holdings-csv-btn").addEventListener("click", () => exportReport("holdings", "csv"));
  $("#open-onboarding-btn").addEventListener("click", () => {
    if (!state.auth?.authenticated) {
      openAuthGate();
      return;
    }
    openOnboarding();
  });
  $("#skip-onboarding-btn").addEventListener("click", () => {
    state.onboardingDraft = [];
    renderOnboardingDraft();
    closeOnboarding();
  });
  const closeOnboardingButton = $("#close-onboarding-btn");
  if (closeOnboardingButton) closeOnboardingButton.addEventListener("click", () => closeOnboarding(false));
  const deferOnboardingButton = $("#defer-onboarding-btn");
  if (deferOnboardingButton) deferOnboardingButton.addEventListener("click", () => closeOnboarding(false));
  const onboardingModal = $("#onboarding-modal");
  if (onboardingModal) {
    onboardingModal.addEventListener("click", (event) => {
      if (event.target === onboardingModal) closeOnboarding(false);
    });
  }

  $("#onboarding-add-form").addEventListener("submit", (event) => {
    event.preventDefault();
    const result = addOnboardingDraftFromFields();
    $("#onboarding-error").textContent = result.ok ? "" : result.error;
  });

  $("#onboarding-draft-list").addEventListener("click", (event) => {
    const button = event.target.closest("[data-remove-draft]");
    if (!button) return;
    state.onboardingDraft = state.onboardingDraft.filter((item) => item.stockCode !== button.dataset.removeDraft);
    renderOnboardingDraft();
  });

  $("#save-onboarding-btn").addEventListener("click", () => {
    if (!state.onboardingDraft.length) {
      $("#onboarding-error").textContent = "請先填寫上方欄位並按「加入清單」，或選擇「目前沒有持股」。";
      return;
    }
    state.onboardingDraft.forEach((holding) => upsertHolding(holding));
    state.onboardingDraft = [];
    renderOnboardingDraft();
    closeOnboarding();
  });

  $$(".settings-grid input").forEach((input) => {
    input.addEventListener("change", (event) => {
      if (!isSuperUser()) {
        event.preventDefault();
        $("#settings-status").textContent = settingsPermissionMessage();
        renderSettings();
        return;
      }
      state.settings[input.dataset.setting] = input.checked;
      saveSettings();
    });
  });

  const strategyStatusOptions = $("#strategy-status-options");
  if (strategyStatusOptions) {
    strategyStatusOptions.addEventListener("click", (event) => {
      const button = event.target.closest("[data-strategy-status]");
      if (!button) return;
      state.activeStrategyStatusKey = button.dataset.strategyStatus;
      renderStrategyStatusDetail();
    });
  }

  $$(".tab").forEach((button) => {
    button.addEventListener("click", () => {
      const tab = button.dataset.tab;
      if (tab === "market") {
        if (state.settings.manual_scan_enabled) scanMarket();
        else showTab("market");
        return;
      }
      if (tab === "holdings") {
        if (state.settings.manual_scan_enabled) scanHoldings();
        else showTab("holdings");
        return;
      }
      showTab(tab);
    });
  });

  $("#holding-results").addEventListener("click", (event) => {
    const button = event.target.closest("[data-clear-from-result]");
    if (!button) return;
    clearHolding(button.dataset.clearFromResult);
    scanHoldings();
    scanMarket();
  });

  $("#market-results").addEventListener("click", async (event) => {
    const tabButton = event.target.closest("[data-market-disclosure-tab]");
    if (tabButton) {
      state.activeMarketDisclosureTab = tabButton.dataset.marketDisclosureTab;
      renderMarketResults();
      return;
    }
    const pageButton = event.target.closest("[data-market-page-column]");
    if (pageButton) {
      const tabKey = pageButton.dataset.marketPageTab;
      const columnKey = pageButton.dataset.marketPageColumn;
      const direction = Number(pageButton.dataset.marketPageDir) || 0;
      if (state.marketListPages?.[tabKey] && columnKey in state.marketListPages[tabKey]) {
        state.marketListPages[tabKey][columnKey] = Math.max(0, getMarketPage(tabKey, columnKey) + direction);
        renderMarketResults();
      }
      return;
    }
    const toggleButton = event.target.closest("[data-market-result-toggle]");
    if (toggleButton) {
      const resultId = toggleButton.dataset.marketResultToggle;
      if (state.expandedMarketResultIds.has(resultId)) {
        state.expandedMarketResultIds.delete(resultId);
      } else {
        state.expandedMarketResultIds.add(resultId);
        loadMarketResultDetails(resultId);
      }
      renderMarketResults();
      return;
    }
    const button = event.target.closest("[data-add-from-result]");
    if (!button) return;
    upsertHolding({
      stockCode: button.dataset.addFromResult,
      name: button.dataset.addName,
      shares: 0,
      averageCost: null,
    });
    showView("holdings");
  });

}

async function init() {
  bindEvents();
  await Promise.all([loadSettings(), loadCompanies()]);
  await loadAccountState();
  await loadDataStatus();
  renderSelectedCompany();
  renderHoldings();
  renderStrategyStatusDetail();
  renderStrategyRulesGrid();
  renderMarketResults();
  renderHoldingResults();
  showView("overview");
  requestHoldingsScanRefresh();
  refreshOverviewMarketScan();
  maybeStartFirstRunFlow();
}

if (typeof document !== "undefined") {
  document.addEventListener("DOMContentLoaded", init);
}

if (typeof module !== "undefined") {
  module.exports = {
    COMPANIES_PAGE_LIMIT,
    DEFAULT_COMPANIES,
    STRATEGY_STATUS_DETAILS,
    STRATEGY_RULE_THRESHOLDS,
    state,
    findCompanyByCodeOrName,
    findStrategyStatusDetail,
    normalizeCompany,
    normalizeCompanies,
    normalizeHoldingRecord,
    normalizeHoldingRecords,
    parseStockInput,
    loadHoldingsFromStorage,
    normalizeText,
    apiErrorMessage,
    apiFetch,
    apiJson,
    API_CLIENT,
    normalizeAuthUsername,
    normalizeAuthUser,
    isSuperUserIdentity,
    isSuperUser,
    authValidationMessage,
    adminUsersErrorMessage,
    formatEvidenceValue,
    renderRuleEvidence,
    applyEvidenceBarWidths,
    renderRule,
    holdingScanResultByCode,
    holdingScanMissingByCode,
    holdingExitCodes,
    holdingSignal,
    renderHoldingSignal,
    holdingExitAlerts,
    renderHoldingExitAlertBanner,
    renderStrategyRuleCards,
    ruleDisplayOrder,
    sortRulesForDisplay,
    renderAnalysisCard,
    renderStrategyStatusDetail,
    renderMarketResultRow,
    renderMarketPagination,
    groupMarketScanResults,
    activeMarketDisclosureKey,
    renderOverviewStats,
    sortMarketResultsForDisplay,
    e4PerValue,
    isHoldingTracked,
    hasInsufficientData,
    hasFinancialReportForContext,
    hasPublishedScanData,
    isPartialPublishedResult,
    settingsPermissionMessage,
    safeText,
    escapeHtml,
    emptyStateHtml,
    setEmptyState,
  };
}
