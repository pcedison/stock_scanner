(function exposeMarketRenderHelpers(root, factory) {
  const helpers = factory();
  if (typeof module !== "undefined" && module.exports) {
    module.exports = helpers;
  }
  if (root) {
    root.StockScannerMarketRender = helpers;
  }
})(typeof globalThis !== "undefined" ? globalThis : null, function createMarketRenderModule() {
  // Market-scan result presentation: list pagination state, result row/column/
  // detail rendering, and the cache-status note. The only app-state coupling
  // (per-column page offsets and the set of expanded result ids) is injected via
  // `getState()`; rendering primitives and classification helpers are borrowed
  // from the renderers / market-scan modules. Bodies are otherwise unchanged
  // from app.js.
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
    sortMarketResultsForDisplay,
    marketColumnNote,
    marketResultId,
    MARKET_LIST_PAGE_SIZE,
    MARKET_RESULT_COLUMNS,
    MARKET_DISCLOSURE_TABS,
  }) {
    function getMarketPage(groupKey, columnKey) {
      const state = getState();
      return Math.max(0, Number(state.marketListPages?.[groupKey]?.[columnKey]) || 0);
    }

    function resetMarketListUi() {
      const state = getState();
      state.marketListPages = {
        announced: { entry: 0, watch: 0, excluded: 0 },
        pending: { entry: 0, watch: 0, excluded: 0 },
      };
      state.expandedMarketResultIds = new Set();
    }

    function clampMarketListPages(grouped) {
      const state = getState();
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
      const state = getState();
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

    return {
      getMarketPage,
      resetMarketListUi,
      clampMarketListPages,
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
