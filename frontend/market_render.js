(function exposeMarketRenderHelpers(root, factory) {
  const helpers = factory();
  if (typeof module !== "undefined" && module.exports) module.exports = helpers;
  if (root) root.StockScannerMarketRender = helpers;
})(typeof globalThis !== "undefined" ? globalThis : null, function createMarketRenderModule() {
  function createMarketRender({
    getState,
    escapeHtml,
    safeText,
    safeCompanyName,
    displayResultStatus,
    statusClass,
    statusLabel,
    renderRule,
    resultActionButtons,
    sortRulesForDisplay,
    marketColumnNote,
    marketResultId,
    MARKET_LIST_PAGE_SIZE,
  }) {
    function getMarketPage(groupKey, columnKey) {
      return Math.max(0, Number(getState().marketListPages?.[groupKey]?.[columnKey]) || 0);
    }
    function resetMarketListUi() {
      const state = getState();
      state.marketListPages = {
        announced: { entry: 0, watch: 0, excluded: 0 },
        pending: { entry: 0, watch: 0, excluded: 0 },
      };
      state.expandedMarketResultIds = new Set();
    }
    function renderMarketPagination(groupKey, columnKey, total) {
      const page = getMarketPage(groupKey, columnKey);
      const totalPages = Math.max(1, Math.ceil(total / MARKET_LIST_PAGE_SIZE));
      const start = total ? page * MARKET_LIST_PAGE_SIZE + 1 : 0;
      const end = Math.min(total, (page + 1) * MARKET_LIST_PAGE_SIZE);
      if (totalPages <= 1)
        return total
          ? `<div class="market-pagination"><span>顯示 ${escapeHtml(start)}-${escapeHtml(end)} / ${escapeHtml(total)}</span></div>`
          : "";
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
      const passed = reasons.filter((rule) => rule?.passed && rule?.severity !== "INSUFFICIENT_DATA");
      const warnings = reasons.filter((rule) => !rule?.passed || rule?.severity === "INSUFFICIENT_DATA");
      const renderRuleGroup = (items, emptyText) =>
        items.length ? items.map(renderRule).join("") : `<p class="muted">${escapeHtml(emptyText)}</p>`;
      return `
        <div class="market-result-details">
          ${result.detailLoading ? `<p class="muted">資料載入中...</p>` : ""}
          ${result.detailError ? `<p class="form-error">${escapeHtml(result.detailError)}</p>` : ""}
          <div class="detail-summary">
            <span class="status-pill ${statusClass(displayStatus)}">${escapeHtml(statusLabel(displayStatus))}</span>
            <p class="muted">${escapeHtml(summary)}</p>
          </div>
          <div class="market-evidence-grid">
            <section>
              <h4>通過條件</h4>
              <div class="rules">${renderRuleGroup(passed, "目前沒有通過的規則")}</div>
            </section>
            <section>
              <h4>警示 / 例外</h4>
              <div class="rules">${renderRuleGroup(warnings, "目前沒有警示或例外")}</div>
            </section>
          </div>
          ${resultActionButtons(result, options)}
        </div>
      `;
    }
    function renderMarketResultRow(result, options = {}) {
      const state = getState();
      const companyName = result.companyName || safeCompanyName(result);
      const stockCode = safeText(result.stockCode, "未知代號");
      const resultId = marketResultId(result, options.disclosureGroup, options.columnKey);
      const expanded = state.expandedMarketResultIds.has(resultId);
      const reasons = sortRulesForDisplay(Array.isArray(result.reasons) ? result.reasons.filter(Boolean) : []);
      const { status: displayStatus } = displayResultStatus(result, options.disclosureGroup);
      const industry = safeText(result.industryName || result.industry, "產業資訊");
      const market = safeText(result.market, "市場別");
      const rulePreview = reasons
        .slice(0, 4)
        .map((rule) => `<span class="rule-chip">${escapeHtml(rule.code || "")}</span>`)
        .join("");
      return `
        <article class="market-result-item ${expanded ? "expanded" : ""}" data-market-result-status="${escapeHtml(displayStatus)}">
          <button class="market-result-summary" type="button" data-market-result-toggle="${escapeHtml(resultId)}" aria-expanded="${expanded ? "true" : "false"}">
            <span class="market-result-code"><strong>${escapeHtml(stockCode)}</strong><small>${escapeHtml(market)}</small></span>
            <span class="market-result-body">
              <span class="sr-only">${escapeHtml(`${stockCode} ${companyName}`)}</span>
              <span class="market-result-name">${escapeHtml(companyName)}</span>
              <span class="market-result-meta">${escapeHtml(industry)}</span>
              <span class="market-rule-preview">${rulePreview}</span>
            </span>
            <span class="status-pill ${statusClass(displayStatus)}">${escapeHtml(statusLabel(displayStatus))}</span>
            <span class="market-expand-icon" aria-hidden="true">${expanded ? "-" : "+"}</span>
          </button>
          ${expanded ? renderMarketResultDetails(result, options) : ""}
        </article>
      `;
    }
    function renderMarketColumn(groupKey, columnKey, title, results) {
      // `results` is always a server window: the API returns the page already
      // ordered and sliced, so the renderer never sorts or slices again.
      const visibleResults = results?.items || [];
      const total = Math.max(0, Number(results?.total) || 0);
      const note = marketColumnNote(columnKey);
      return `
        <div class="result-column">
          <div class="result-column-head">
            <h3>${escapeHtml(title)} (${escapeHtml(total)})</h3>
            ${note ? `<span class="result-column-note">${escapeHtml(note)}</span>` : ""}
          </div>
          ${visibleResults.length ? `<div class="market-result-list">${visibleResults.map((result) => renderMarketResultRow(result, { allowAddAction: true, disclosureGroup: groupKey, columnKey })).join("")}</div>` : !results?.loading ? `<div class="empty-state">沒有結果</div>` : ""}
          ${results?.loading ? `<p class="muted" role="status">載入掃描結果中…</p>` : ""}
          ${results?.error ? `<p class="form-error" role="status">${escapeHtml(results.error)}</p>` : ""}
          ${renderMarketPagination(groupKey, columnKey, total)}
        </div>
      `;
    }
    function formatCacheTime(value) {
      const date = new Date(value || Number.NaN);
      return Number.isNaN(date.getTime()) ? "未記錄" : date.toLocaleString();
    }
    function cacheRefreshLabel(status = "") {
      const labels = {
        completed_sync: "同步完成",
        fresh: "最新",
        queued: "排隊中",
        running: "更新中",
        success: "更新成功",
        failed: "更新失敗",
        cache_only: "僅快取",
        unavailable: "更新暫時不可用",
      };
      return labels[status] || status || "未知";
    }
    function renderScanCacheStatus(scan = {}) {
      const cache = scan.cacheStatus;
      if (!cache) return "";
      const staleText = cache.isStale ? "快取已過期，將在下次更新時重新整理" : "目前使用最新快取";
      return `
        <div class="cache-status-note ops-cache-strip">
          <span><strong>快取命中</strong> ${cache.cacheHit ? "是" : "否"}</span>
          <span><strong>狀態</strong> ${escapeHtml(cacheRefreshLabel(cache.refreshStatus))}</span>
          <span><strong>建立時間</strong> ${escapeHtml(formatCacheTime(cache.storedAt))}</span>
          <span><strong>下次更新</strong> ${escapeHtml(formatCacheTime(cache.nextRefreshAfter))}</span>
          <span>${escapeHtml(staleText)}</span>
        </div>
      `;
    }
    return {
      getMarketPage,
      resetMarketListUi,
      renderMarketPagination,
      renderMarketResultDetails,
      renderMarketResultRow,
      renderMarketColumn,
      formatCacheTime,
      cacheRefreshLabel,
      renderScanCacheStatus,
    };
  }
  return { createMarketRender };
});
