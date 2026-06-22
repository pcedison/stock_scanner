(function exposeOpsViewRenderers(root, factory) {
  const helpers = factory();
  if (typeof module !== "undefined" && module.exports) {
    module.exports = helpers;
  }
  if (root) {
    root.StockScannerOpsViewRenderers = helpers;
  }
})(typeof globalThis !== "undefined" ? globalThis : null, function createOpsViewRenderersModule() {
  function createOpsViewRenderers({ escapeHtml, safeCompanyName, renderHoldingSignal, displayResultStatus }) {
    function normalizedShares(holding = {}) {
      const shares = Number(holding.shares);
      return Number.isFinite(shares) ? Math.max(0, Math.floor(shares)) : 0;
    }

    function normalizedCost(holding = {}) {
      if (holding.averageCost === "" || holding.averageCost === null || holding.averageCost === undefined) return null;
      const cost = Number(holding.averageCost);
      return Number.isFinite(cost) ? Math.max(0, cost) : null;
    }

    function boolLabel(value) {
      return value ? "是" : "否";
    }

    function configuredLabel(value) {
      return value ? "已設定" : "未設定";
    }

    function freshnessLabel(status) {
      const labels = {
        ok: "最新",
        warning: "覆蓋不足",
        stale: "落後",
        mock: "示範資料",
        pending: "待確認",
      };
      return labels[status] || "待確認";
    }

    function freshnessTone(status) {
      if (status === "ok") return "status-entry";
      if (status === "stale") return "status-exit";
      if (status === "warning") return "status-watch";
      return "neutral";
    }

    function renderFinancialFreshnessCard(status = {}) {
      const freshness = status.financialFreshness || {};
      return `
        <article class="card ops-console-card">
          <div class="ops-card-head"><h3>財報新鮮度</h3><span class="status-pill ${freshnessTone(freshness.status)}">${escapeHtml(freshnessLabel(freshness.status))}</span></div>
          <dl class="ops-kv">
            <div><dt>應覆蓋期別</dt><dd>${escapeHtml(freshness.expectedFinancialPeriod ?? "--")}</dd></div>
            <div><dt>快取最新期別</dt><dd>${escapeHtml(freshness.latestCachedFinancialPeriod ?? "--")}</dd></div>
            <div><dt>當期覆蓋數</dt><dd>${escapeHtml(freshness.expectedPeriodCoverage ?? 0)}</dd></div>
            <div><dt>阻擋部署</dt><dd>${escapeHtml(boolLabel(Boolean(freshness.blocksDeployment)))}</dd></div>
          </dl>
          <p class="muted">${escapeHtml(freshness.message || "等待財報快取新鮮度檢查。")}</p>
        </article>
      `;
    }

    function renderHoldingCard({ holding, isEditing, analysis, missing }) {
      const name = safeCompanyName(holding);
      const stockCode = holding.stockCode || "未知代碼";
      const shares = normalizedShares(holding);
      const averageCost = normalizedCost(holding);
      const xStatus = analysis ? displayResultStatus(analysis, "holding").status : missing ? "資料不足" : "狀態未提供";
      return `
        <article class="card holding-card" data-holding-code="${escapeHtml(stockCode)}">
          <div class="holding-row-main">
            <div class="holding-id">
              <h3 class="stock-title">${escapeHtml(stockCode)}</h3>
              <p class="muted">${escapeHtml(name)}</p>
              ${renderHoldingSignal(analysis, missing)}
            </div>
            <dl class="holding-metrics">
              <div><dt>持股股數</dt><dd>${escapeHtml(shares)}</dd></div>
              <div><dt>平均成本</dt><dd>${escapeHtml(averageCost ?? "--")}</dd></div>
              <div><dt>X1-X5</dt><dd>${escapeHtml(xStatus)}</dd></div>
            </dl>
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
              <label>持股股數</label>
              <input type="number" min="0" step="1" data-field="shares" value="${escapeHtml(shares)}" aria-label="持股股數" />
              <span class="field-help">儲存後會覆蓋目前的持股股數。</span>
            </div>
            <div class="field">
              <label>平均成本</label>
              <input type="number" min="0" step="0.01" data-field="averageCost" value="${escapeHtml(averageCost ?? "")}" aria-label="平均成本" />
              <span class="field-help">留空可表示尚未設定平均成本。</span>
            </div>
            <div class="field">
              <label>減碼股數</label>
              <input type="number" min="0" step="1" data-field="reduce" placeholder="例如 100" aria-label="減碼股數" />
              <span class="field-help">送出後會從目前持股股數中扣除。</span>
            </div>
          </div>
          <div class="button-row">
            <button class="primary-btn" type="button" data-action="save">儲存</button>
            <button class="secondary-btn" type="button" data-action="reduce">減碼</button>
          </div>
        `
              : ""
          }
        </article>
      `;
    }

    function renderDataConsole({ status = {}, integrationStatus = {}, backtestStatus = {} }) {
      const configuredNotifications = (integrationStatus.notifications || []).filter((item) => item?.configured).length;
      return `
        <article class="card ops-console-card">
          <div class="ops-card-head"><h3>資料來源狀態</h3><span class="status-pill status-entry">資料源</span></div>
          <dl class="ops-kv">
            <div><dt>資料源</dt><dd>${escapeHtml(status.activeProvider ?? "--")}</dd></div>
            <div><dt>即時資料</dt><dd>${escapeHtml(boolLabel(status.activeProviderIsRealtime))}</dd></div>
            <div><dt>股票池</dt><dd>${escapeHtml(boolLabel(status.activeProviderIsFullMarket))}</dd></div>
            <div><dt>基本面</dt><dd>${escapeHtml(boolLabel(status.activeProviderHasCompleteFundamentals))}</dd></div>
          </dl>
        </article>
        <article class="card ops-console-card">
          <div class="ops-card-head"><h3>股票池</h3><span class="muted">覆蓋範圍</span></div>
          <dl class="ops-kv">
            <div><dt>Mock Data</dt><dd>${escapeHtml(status.mockUniverseSize ?? 0)}</dd></div>
            <div><dt>官方股票池</dt><dd>${escapeHtml(status.officialUniverseSize ?? "--")}</dd></div>
            <div><dt>月營收快照</dt><dd>${escapeHtml(status.officialMonthlySnapshotSize ?? "--")}</dd></div>
            <div><dt>歷史列數</dt><dd>${escapeHtml(status.officialHistoryRows ?? 0)}</dd></div>
          </dl>
        </article>
        <article class="card ops-console-card">
          <div class="ops-card-head"><h3>基本面</h3><span class="status-pill neutral">匯入</span></div>
          <dl class="ops-kv">
            <div><dt>損益表</dt><dd>${escapeHtml(status.officialIncomeStatementSize ?? "--")}</dd></div>
            <div><dt>資產負債表</dt><dd>${escapeHtml(status.officialBalanceSheetSize ?? "--")}</dd></div>
            <div><dt>估值資料</dt><dd>${escapeHtml(status.officialValuationSize ?? "--")}</dd></div>
            <div><dt>匯入列數</dt><dd>${escapeHtml(status.fundamentalsImportRows ?? 0)}</dd></div>
          </dl>
          <p class="muted">${escapeHtml(status.officialHistoricalFundamentals?.note || "官方基本面資料會整理 EPS、月營收與 PER/PBR 匯入結果。")}</p>
        </article>
        ${renderFinancialFreshnessCard(status)}
        <article class="card ops-console-card">
          <div class="ops-card-head"><h3>整合與通知</h3><span class="muted">提醒</span></div>
          <dl class="ops-kv">
            <div><dt>通知項目</dt><dd>${escapeHtml(configuredNotifications)} 個已設定</dd></div>
            <div><dt>券商</dt><dd>${escapeHtml(configuredLabel(integrationStatus.broker?.configured))}</dd></div>
            <div><dt>AI 摘要</dt><dd>${escapeHtml(configuredLabel(integrationStatus.aiSummary?.configured))}</dd></div>
            <div><dt>交易筆數</dt><dd>${escapeHtml(backtestStatus.metrics?.tradeCount ?? 0)}</dd></div>
          </dl>
          <p class="muted">${escapeHtml(backtestStatus.note || "回測交易摘要會從 data/backtest_history.csv 讀取。")}</p>
        </article>
      `;
    }

    function renderSchedulerStatus({ schedulerStatus = {}, autoAction = "" }) {
      return `
        <div class="ops-scheduler-strip">
          <strong>排程狀態：${escapeHtml(schedulerStatus.status ?? "--")}</strong>
          <span>事件：${escapeHtml((schedulerStatus.events || []).join(" / ") || "--")}</span>
          <span>下個交易日：${escapeHtml(schedulerStatus.nextTradingDay ?? "--")}</span>
          <span>自動掃描：${escapeHtml(autoAction || "--")}</span>
        </div>
      `;
    }

    return { renderDataConsole, renderHoldingCard, renderSchedulerStatus };
  }

  return { createOpsViewRenderers };
});
