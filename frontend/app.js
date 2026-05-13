const HOLDINGS_KEY = "tw_stock_scanner.holdings.v1";
const ONBOARDING_KEY = "tw_stock_scanner.onboarding_done.v1";

const DEFAULT_COMPANIES = [
  { stockCode: "2330", name: "台積電", market: "TWSE", industryName: "半導體業", isFinancial: false },
  { stockCode: "2357", name: "華碩", market: "TWSE", industryName: "電腦及週邊設備業", isFinancial: false },
  { stockCode: "2454", name: "聯發科", market: "TWSE", industryName: "半導體業", isFinancial: false },
  { stockCode: "3008", name: "大立光", market: "TWSE", industryName: "光電業", isFinancial: false },
  { stockCode: "5274", name: "信驊", market: "TPEX", industryName: "半導體業", isFinancial: false },
  { stockCode: "2881", name: "富邦金", market: "TWSE", industryName: "金融保險業", isFinancial: true },
];

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
};

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];

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
  return String(value ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#039;");
}

function safeCompanyName(companyOrHolding) {
  if (!companyOrHolding) return "未知公司";
  const directName = safeText(companyOrHolding.name || companyOrHolding.companyName);
  if (directName) return directName;
  const company = findCompanyByCodeOrName(companyOrHolding.stockCode, state.companies);
  return company ? company.name : "未知公司";
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

function loadHoldings() {
  try {
    const parsed = JSON.parse(localStorage.getItem(HOLDINGS_KEY) || "[]");
    if (!Array.isArray(parsed)) throw new Error("Holdings must be an array");
    const normalized = parsed
      .map((item) => {
        const company = findCompanyByCodeOrName(item.stockCode || item.name, state.companies);
        if (!company) return null;
        return {
          stockCode: company.stockCode,
          name: company.name,
          shares: Number.isFinite(Number(item.shares)) ? Math.max(0, Math.floor(Number(item.shares))) : 0,
          averageCost:
            item.averageCost === null || item.averageCost === undefined || item.averageCost === ""
              ? null
              : Math.max(0, Number(item.averageCost)),
        };
      })
      .filter(Boolean);
    saveHoldings(normalized);
    return normalized;
  } catch {
    localStorage.setItem(HOLDINGS_KEY, "[]");
    return [];
  }
}

function saveHoldings(holdings = state.holdings) {
  localStorage.setItem(HOLDINGS_KEY, JSON.stringify(holdings));
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
  renderHoldings();
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
  renderHoldings();
}

function clearHolding(stockCode) {
  state.holdings = state.holdings.filter((item) => item.stockCode !== stockCode);
  if (state.editingHoldingCode === stockCode) state.editingHoldingCode = null;
  saveHoldings();
  renderHoldings();
}

async function apiJson(url, options = {}) {
  const response = await fetch(url, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  if (!response.ok) {
    const detail = await response.text();
    throw new Error(detail || `HTTP ${response.status}`);
  }
  return response.json();
}

function statusLabel(status) {
  const labels = {
    ENTRY: "適合進場",
    WATCH: "觀察",
    HOLD: "續抱",
    ADD_WATCH: "加碼觀察",
    WARNING: "警戒",
    EXIT: "建議出清",
    EXCLUDED: "排除",
    INSUFFICIENT_DATA: "資料不足",
  };
  return labels[status] || "未知";
}

function statusClass(status) {
  return `status-${String(status || "neutral").toLowerCase()}`;
}

function renderRule(rule = {}) {
  const stateClass = rule.passed ? "passed" : "failed";
  const stateText = rule.passed ? "通過" : "未通過";
  return `
    <div class="rule">
      <div class="rule-code ${stateClass}">${escapeHtml(rule.code)} ${stateText}</div>
      <div>
        <strong>${escapeHtml(rule.title)}</strong>
        <div class="muted">${escapeHtml(rule.message)}</div>
      </div>
    </div>
  `;
}

function renderAnalysisCard(result, options = {}) {
  result = result || {};
  const companyName = result.companyName || safeCompanyName(result);
  const stockCode = safeText(result.stockCode, "未知代碼");
  const reasons = Array.isArray(result.reasons) ? result.reasons.filter(Boolean) : [];
  const exitButton =
    options.allowExitAction && result.status === "EXIT"
      ? `<button class="danger-btn" type="button" data-clear-from-result="${escapeHtml(stockCode)}">採用出場建議</button>`
      : "";
  return `
    <article class="card">
      <div class="card-head">
        <div>
          <h3 class="stock-title">${escapeHtml(stockCode)} ${escapeHtml(companyName)}</h3>
          <p class="muted">${escapeHtml(result.summary || "已完成規則分析。")}</p>
        </div>
        <span class="status-pill ${statusClass(result.status)}">${escapeHtml(statusLabel(result.status))}</span>
      </div>
      <div class="rules">${reasons.map(renderRule).join("")}</div>
      ${exitButton ? `<div class="button-row">${exitButton}</div>` : ""}
    </article>
  `;
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
  target.className = "selected-company card";
  target.innerHTML = `
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
  `;
}

function renderHoldings() {
  const target = $("#holdings-list");
  if (!state.holdings.length) {
    target.innerHTML = `<div class="empty-state">目前沒有持股</div>`;
    return;
  }

  target.innerHTML = state.holdings
    .map((holding) => {
      const name = safeCompanyName(holding);
      const stockCode = safeText(holding.stockCode, "未知代碼");
      const shares = Number.isFinite(Number(holding.shares)) ? Math.max(0, Math.floor(Number(holding.shares))) : 0;
      const averageCost = Number.isFinite(Number(holding.averageCost)) ? Math.max(0, Number(holding.averageCost)) : null;
      const isEditing = state.editingHoldingCode === stockCode;
      return `
        <article class="card" data-holding-code="${escapeHtml(stockCode)}">
          <div class="card-head">
            <div>
              <h3 class="stock-title">${escapeHtml(stockCode)} ${escapeHtml(name)}</h3>
              <p class="muted">目前 ${escapeHtml(shares)} 股，平均成本 ${escapeHtml(averageCost ?? "未填")}</p>
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
    .join("");
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
  list.innerHTML = state.onboardingDraft
    .map(
      (holding) => {
        const stockCode = safeText(holding.stockCode, "未知代碼");
        const name = safeCompanyName(holding);
        const shares = Number.isFinite(Number(holding.shares)) ? Math.max(0, Math.floor(Number(holding.shares))) : 0;
        const averageCost = Number.isFinite(Number(holding.averageCost)) ? Math.max(0, Number(holding.averageCost)) : null;
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
    .join("");
}

function renderSettings() {
  $$("[data-setting]").forEach((input) => {
    input.checked = Boolean(state.settings[input.dataset.setting]);
  });
  $("#scan-market-btn").disabled = !state.settings.manual_scan_enabled;
  $("#scan-holdings-btn").disabled = !state.settings.manual_scan_enabled;
}

function renderMarketResults() {
  const target = $("#market-results");
  if (!state.marketScan) {
    target.innerHTML = `<div class="empty-state">尚未掃描市場</div>`;
    return;
  }
  $("#scan-time").textContent = `更新 ${new Date(state.marketScan.generatedAt).toLocaleString()}`;
  const columns = [
    ["entry", "適合進場"],
    ["watch", "接近觀察"],
    ["excluded", "排除清單"],
  ];
  const visibleLimit = 30;
  target.innerHTML = `
    <div class="data-source-note">
      <strong>資料來源：${escapeHtml(state.marketScan.dataSource || "mock")}</strong>
      <span>${escapeHtml(state.marketScan.note || "目前為示範樣本，不代表真實全台股即時掃描。")}</span>
    </div>
    <div class="result-columns">
      ${columns
        .map(([key, title]) => {
          const results = state.marketScan[key] || [];
          const visibleResults = results.slice(0, visibleLimit);
          const hiddenCount = Math.max(0, results.length - visibleResults.length);
          return `
            <div class="result-column">
              <h3>${title} (${results.length})</h3>
              ${visibleResults.length ? visibleResults.map((result) => renderAnalysisCard(result)).join("") : `<div class="empty-state">無資料</div>`}
              ${hiddenCount ? `<div class="empty-state">尚有 ${escapeHtml(hiddenCount)} 筆結果未展開，避免主畫面過載。</div>` : ""}
            </div>
          `;
        })
        .join("")}
    </div>
  `;
}

function renderDataAndScheduler() {
  const dataTarget = $("#data-source-status");
  const schedulerTarget = $("#scheduler-status");
  if (dataTarget) {
    if (!state.dataSourceStatus) {
      dataTarget.innerHTML = `<div class="empty-state">尚未讀取資料來源狀態</div>`;
    } else {
      const status = state.dataSourceStatus;
      dataTarget.innerHTML = `
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
          <p class="muted">官方月營收已可掃描；季報、年報、PER、存貨週轉與毛利率仍會標示資料不足。</p>
        </article>
      `;
    }
  }

  if (schedulerTarget) {
    if (!state.schedulerStatus) {
      schedulerTarget.innerHTML = `<strong>排程狀態：</strong><span>尚未讀取</span>`;
    } else {
      schedulerTarget.innerHTML = `
        <strong>排程狀態：${escapeHtml(state.schedulerStatus.status)}</strong>
        <span>事件：${escapeHtml((state.schedulerStatus.events || []).join("、") || "無")}；下一交易日：${escapeHtml(state.schedulerStatus.nextTradingDay)}</span>
      `;
    }
  }
}

function renderHoldingResults() {
  const target = $("#holding-results");
  if (!state.holdingsScan) {
    target.innerHTML = `<div class="empty-state">尚未掃描持股</div>`;
    return;
  }
  $("#scan-time").textContent = `更新 ${new Date(state.holdingsScan.generatedAt).toLocaleString()}`;
  const missing = state.holdingsScan.missing || [];
  target.innerHTML = `
    <div class="stack">
      ${(state.holdingsScan.results || []).map((result) => renderAnalysisCard(result, { allowExitAction: true })).join("")}
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
  `;
}

function showTab(tab) {
  $$(".tab").forEach((button) => button.classList.toggle("active", button.dataset.tab === tab));
  $("#market-results").classList.toggle("hidden", tab !== "market");
  $("#holding-results").classList.toggle("hidden", tab !== "holdings");
}

function showView(view) {
  state.activeView = view;
  const titles = {
    overview: ["總覽", "快速查看資料來源、持股狀態與策略完成度。"],
    search: ["搜尋與單檔分析", "查詢股票並查看單檔規則原因。"],
    holdings: ["我的持股", "管理本機持股，掃描續抱、加碼、警戒與出場。"],
    scan: ["掃描結果", "查看目前資料源與持股掃描結果。"],
    settings: ["設定", "調整掃描範圍與策略開關。"],
    strategy: ["策略規則", "對照長輩懶人包的進出場規則。"],
    data: ["資料與排程", "查看資料來源、官方 adapter 與事件驅動排程狀態。"],
  };
  $$(".nav-item").forEach((button) => button.classList.toggle("active", button.dataset.view === view));
  $$("[data-view-panel]").forEach((panel) => panel.classList.toggle("hidden", panel.dataset.viewPanel !== view));
  const [title, subtitle] = titles[view] || titles.overview;
  $("#view-title").textContent = title;
  $("#view-subtitle").textContent = subtitle;
  if (view === "data") renderDataAndScheduler();
}

async function loadCompanies() {
  try {
    const payload = await apiJson("/api/companies");
    const normalized = normalizeCompanies(payload.items);
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
    state.dataSourceStatus = await apiJson("/api/data-sources/status");
    state.schedulerStatus = await apiJson("/api/scheduler/wakeup");
  } catch {
    state.dataSourceStatus = null;
    state.schedulerStatus = null;
  }
  renderDataAndScheduler();
}

async function saveSettings() {
  try {
    state.settings = await apiJson("/api/settings", {
      method: "PUT",
      body: JSON.stringify(state.settings),
    });
    await loadCompanies();
    await loadDataStatus();
    $("#settings-status").textContent = "已儲存";
  } catch {
    $("#settings-status").textContent = "儲存失敗";
  }
  renderSelectedCompany();
  renderHoldings();
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
    box.innerHTML = "";
    return;
  }
  box.innerHTML = companies
    .map(
      (company) => `
      <button class="suggestion-item" type="button" data-select-code="${escapeHtml(company.stockCode)}">
        <span>${escapeHtml(company.stockCode)} ${escapeHtml(company.name)}</span>
        <span class="muted">${escapeHtml(company.market)}</span>
      </button>
    `,
    )
    .join("");
  box.classList.add("open");
}

async function analyzeSelectedCompany() {
  if (!state.selectedCompany) return;
  const target = $("#single-analysis");
  target.innerHTML = `<div class="empty-state">分析中</div>`;
  try {
    const result = await apiJson(`/api/analyze/${state.selectedCompany.stockCode}`, {
      method: "POST",
      body: JSON.stringify({ settings: state.settings }),
    });
    target.innerHTML = renderAnalysisCard(result);
  } catch (error) {
    target.innerHTML = `<p class="form-error">${escapeHtml(error.message || "分析失敗")}</p>`;
  }
}

async function scanMarket() {
  const target = $("#market-results");
  target.innerHTML = `<div class="empty-state">掃描中</div>`;
  showView("scan");
  showTab("market");
  try {
    state.marketScan = await apiJson("/api/scan/market", {
      method: "POST",
      body: JSON.stringify({ settings: state.settings }),
    });
  } catch (error) {
    target.innerHTML = `<p class="form-error">${escapeHtml(error.message || "掃描失敗")}</p>`;
    return;
  }
  renderMarketResults();
}

async function scanHoldings() {
  const target = $("#holding-results");
  target.innerHTML = `<div class="empty-state">掃描中</div>`;
  showView("scan");
  showTab("holdings");
  try {
    state.holdingsScan = await apiJson("/api/scan/holdings", {
      method: "POST",
      body: JSON.stringify({ holdings: state.holdings, settings: state.settings }),
    });
  } catch (error) {
    target.innerHTML = `<p class="form-error">${escapeHtml(error.message || "掃描失敗")}</p>`;
    return;
  }
  renderHoldingResults();
}

function openOnboarding() {
  renderOnboardingDraft();
  $("#onboarding-modal").classList.remove("hidden");
  $("#onboarding-error").textContent = "";
  $("#onboarding-stock-input").focus();
}

function closeOnboarding() {
  $("#onboarding-modal").classList.add("hidden");
  localStorage.setItem(ONBOARDING_KEY, "true");
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
      renderHoldings();
    }
    if (action === "reduce") {
      reduceHolding(code, card.querySelector('[data-field="reduce"]').value);
    }
  });

  $("#scan-market-btn").addEventListener("click", scanMarket);
  $("#scan-holdings-btn").addEventListener("click", scanHoldings);
  $("#open-onboarding-btn").addEventListener("click", openOnboarding);
  $("#skip-onboarding-btn").addEventListener("click", () => {
    state.onboardingDraft = [];
    renderOnboardingDraft();
    closeOnboarding();
  });

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
    input.addEventListener("change", () => {
      state.settings[input.dataset.setting] = input.checked;
      saveSettings();
    });
  });

  $$(".tab").forEach((button) => {
    button.addEventListener("click", () => showTab(button.dataset.tab));
  });

  $("#holding-results").addEventListener("click", (event) => {
    const button = event.target.closest("[data-clear-from-result]");
    if (!button) return;
    clearHolding(button.dataset.clearFromResult);
    scanHoldings();
    scanMarket();
  });

  $$(".nav-item").forEach((button) => {
    button.addEventListener("click", () => showView(button.dataset.view));
  });
}

async function init() {
  bindEvents();
  await loadSettings();
  await loadCompanies();
  state.holdings = loadHoldings();
  await loadDataStatus();
  renderSelectedCompany();
  renderHoldings();
  renderMarketResults();
  renderHoldingResults();
  showView("overview");
  if (localStorage.getItem(ONBOARDING_KEY) !== "true") {
    openOnboarding();
  }
}

if (typeof document !== "undefined") {
  document.addEventListener("DOMContentLoaded", init);
}

if (typeof module !== "undefined") {
  module.exports = {
    DEFAULT_COMPANIES,
    findCompanyByCodeOrName,
    normalizeCompany,
    normalizeCompanies,
    parseStockInput,
    normalizeText,
    renderAnalysisCard,
    safeText,
  };
}
