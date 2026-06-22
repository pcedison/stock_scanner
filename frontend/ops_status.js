(function exposeOpsStatusHelpers(root, factory) {
  const helpers = factory();
  if (typeof module !== "undefined" && module.exports) {
    module.exports = helpers;
  }
  if (root) {
    root.StockScannerOpsStatus = helpers;
  }
})(typeof globalThis !== "undefined" ? globalThis : null, function createOpsStatusModule() {
  function createOpsStatus({ query, getState, holdingExitAlerts }) {
    const $ = query;

    function setText(selector, value) {
      const target = $(selector);
      if (target) target.textContent = value;
    }

    function formatProviderState(state, marketScan) {
      const provider = state.dataSourceStatus?.activeProvider || marketScan?.dataSource || "未載入";
      if (!state.dataSourceStatus) {
        return {
          label: provider,
          note: "等待資料來源狀態",
        };
      }
      const scope = state.dataSourceStatus.activeProviderIsFullMarket ? "全市場" : "部分市場";
      const fundamentals = state.dataSourceStatus.activeProviderHasCompleteFundamentals ? "完整基本面" : "部分基本面";
      const realtime = state.dataSourceStatus.activeProviderIsRealtime ? "即時" : "非即時";
      return {
        label: provider,
        note: `${scope}、${fundamentals}、${realtime}`,
      };
    }

    function formatCacheState(marketScan) {
      const cache = marketScan?.cacheStatus;
      if (!cache) {
        return {
          label: "未知",
          note: "等待快取資訊",
        };
      }
      const label = cache.cacheHit ? "已命中快取" : "重新整理資料";
      const freshness = cache.isStale ? "，資料偏舊" : "";
      const refresh = cache.refreshStatus ? `，${cache.refreshStatus}` : "";
      return {
        label,
        note: `${cache.from || "市場掃描"}${refresh}${freshness}`,
      };
    }

    function formatFilingState(marketScan) {
      const filing = marketScan?.filingContext?.activeFinancialReport;
      if (filing) {
        const deadlines = [filing.generalDeadline, filing.financialDeadline].filter(Boolean).join(" / ");
        return {
          label: filing.label || "申報脈絡",
          note: deadlines ? `申報期限 ${deadlines}` : "申報期限待確認",
        };
      }
      const monthlyRevenuePeriod = marketScan?.filingContext?.monthlyRevenuePeriod;
      return {
        label: "申報資訊待定",
        note: monthlyRevenuePeriod ? `月營收期間 ${monthlyRevenuePeriod}` : "等待申報資訊",
      };
    }

    function formatRiskState(alerts, holdingsCount) {
      if (!holdingsCount) {
        return {
          label: "尚未建立持股",
          note: "建立持股後會依 X1-X5 更新風險訊號",
        };
      }
      const exitCount = alerts.filter((item) => item.status === "EXIT").length;
      const warningCount = alerts.filter((item) => item.status === "WARNING").length;
      if (exitCount) {
        return {
          label: `${exitCount} 筆需減碼`,
          note: "先處理退出訊號，再看其他警示",
        };
      }
      if (warningCount) {
        return {
          label: `${warningCount} 筆警示`,
          note: "持股已進入觀察區，請先檢查原因",
        };
      }
      return {
        label: "目前穩定",
        note: "持股沒有明顯退出或警示訊號",
      };
    }

    function formatHoldingAction(alerts, holdingsCount) {
      if (!holdingsCount) return "先建立持股，再看 X1-X5 風險。";
      const exitCount = alerts.filter((item) => item.status === "EXIT").length;
      const warningCount = alerts.filter((item) => item.status === "WARNING").length;
      if (exitCount) return `有 ${exitCount} 筆持股需要先處理。`;
      if (warningCount) return `有 ${warningCount} 筆持股進入警戒。`;
      return "目前持股狀態穩定，可再複查一次。";
    }

    function formatScanAction(marketScan) {
      if (!marketScan) return "先更新市場掃描，再看篩選結果。";
      const generatedAt = marketScan.generatedAt ? new Date(marketScan.generatedAt).toLocaleString() : "";
      return generatedAt ? `市場掃描已更新於 ${generatedAt}。` : "市場掃描已就緒。";
    }

    function formatDataAction(state, marketScan) {
      if (!state.schedulerStatus) return "資料來源與排程狀態尚未載入。";
      const status = state.schedulerStatus.status || "未知";
      const nextDay = state.schedulerStatus.nextTradingDay || "待確認";
      const autoAction = state.schedulerAutoScan?.action || "未排程";
      const cacheHint = marketScan?.cacheStatus?.cacheHit ? "快取已命中" : "等待重新整理";
      return `排程 ${status}，下一交易日 ${nextDay}，自動掃描 ${autoAction}，${cacheHint}。`;
    }

    function renderOverviewOpsStatus(scan = null) {
      const state = getState();
      const marketScan = scan || state.marketScan;
      const alerts = holdingExitAlerts(state.holdingsScan);
      const holdingsCount = Array.isArray(state.holdings) ? state.holdings.length : 0;
      const provider = formatProviderState(state, marketScan);
      const cache = formatCacheState(marketScan);
      const filing = formatFilingState(marketScan);
      const risk = formatRiskState(alerts, holdingsCount);

      setText("#overview-source-state", provider.label);
      setText("#overview-source-note", provider.note);
      setText("#overview-cache-state", cache.label);
      setText("#overview-cache-note", cache.note);
      setText("#overview-filing-state", filing.label);
      setText("#overview-filing-note", filing.note);
      setText("#overview-holding-risk-state", risk.label);
      setText("#overview-holding-risk-note", risk.note);

      setText("#overview-action-holdings", formatHoldingAction(alerts, holdingsCount));
      setText("#overview-action-scan", formatScanAction(marketScan));
      setText("#overview-action-data", formatDataAction(state, marketScan));

      setText("#top-source-status", `資料來源：${provider.label}`);
      setText("#top-cache-status", `快取：${cache.label}`);
      setText("#top-filing-status", filing.label);
    }

    return { renderOverviewOpsStatus };
  }

  return { createOpsStatus };
});
