(function exposeMarketScanHelpers(root, factory) {
  const helpers = factory();
  if (typeof module !== "undefined" && module.exports) {
    module.exports = helpers;
  }
  if (root) {
    root.StockScannerMarketScan = helpers;
  }
})(typeof globalThis !== "undefined" ? globalThis : null, function createMarketScanModule() {
  // Market-scan result classification and lookup helpers.
  // The only app-state coupling (active disclosure tab / column) is injected via
  // `getState()`; `safeText` is borrowed from the normalization module. Result
  // lookup goes through the v2 loaded-page index via `findLoadedResult`.
  function createMarketScan({ getState, safeText, findLoadedResult = () => null }) {
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
      return (
        result.status === "INSUFFICIENT_DATA" || reasons.some((reason) => reason?.severity === "INSUFFICIENT_DATA")
      );
    }

    function expectedFinancialPeriod(filingContext = {}) {
      for (const field of ["freshnessFinancialReport", "activeFinancialReport"]) {
        const report = filingContext?.[field];
        if (report && typeof report.period === "string" && report.period) return report.period;
      }
      return null;
    }

    function hasFinancialReportForContext(result = {}, filingContext = {}) {
      const targetPeriod = expectedFinancialPeriod(filingContext);
      if (!targetPeriod) return true;
      const reasons = Array.isArray(result.reasons) ? result.reasons : [];
      return reasons.some(
        (reason) =>
          reason?.code === "OFFICIAL_Q" &&
          reason?.severity !== "INSUFFICIENT_DATA" &&
          String(reason?.message || "").includes(targetPeriod),
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

    function activeMarketDisclosureKey(tab = getState().activeMarketDisclosureTab) {
      return MARKET_DISCLOSURE_TABS.some((item) => item.key === tab) ? tab : "announced";
    }

    function activeMarketColumnKey() {
      return MARKET_COLUMN_LABELS[getState().activeMarketColumn] ? getState().activeMarketColumn : "entry";
    }

    function marketColumnNote(columnKey) {
      if (columnKey === "entry") return "排序：E4 PER 低 → 高";
      if (columnKey === "watch") return "依股票代號排序；展開可看未通過或待補原因";
      if (columnKey === "excluded") return "依股票代號排序；展開可看排除原因";
      return "";
    }

    function marketResultId(result = {}, groupKey = "", columnKey = "") {
      return [groupKey, columnKey, safeText(result.stockCode, "unknown"), safeText(result.status, "unknown")].join(":");
    }

    function findMarketResultById(resultId) {
      const [groupKey, columnKey, stockCode] = String(resultId || "").split(":");
      const loaded = findLoadedResult(stockCode);
      return loaded && marketResultId(loaded, groupKey, columnKey) === resultId ? loaded : null;
    }

    return {
      MARKET_LIST_PAGE_SIZE,
      MARKET_COLUMN_LABELS,
      MARKET_DISCLOSURE_TABS,
      hasInsufficientData,
      hasFinancialReportForContext,
      hasPublishedScanData,
      isPartialPublishedResult,
      activeMarketDisclosureKey,
      activeMarketColumnKey,
      marketColumnNote,
      marketResultId,
      findMarketResultById,
    };
  }

  return { createMarketScan };
});
