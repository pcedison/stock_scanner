(function exposeHoldingSignals(root, factory) {
  const helpers = factory(
    typeof require === "function" && typeof module !== "undefined" && module.exports
      ? require("./dom.js")
      : root?.StockScannerDom,
  );
  if (typeof module !== "undefined" && module.exports) {
    module.exports = helpers;
  }
  if (root) {
    root.StockScannerHoldingSignals = helpers;
  }
})(typeof globalThis !== "undefined" ? globalThis : null, function createHoldingSignals(domHelpers = {}) {
  const escapeHtml = domHelpers.escapeHtml || ((value) => String(value ?? ""));

  function statusClass(status) {
    return `status-${String(status || "neutral").toLowerCase()}`;
  }

  function holdingExitCodes(result = {}) {
    const reasons = Array.isArray(result.reasons) ? result.reasons : [];
    return reasons
      .filter((reason) => /^X[1-5]$/.test(String(reason?.code || "").toUpperCase()) && reason.passed === false)
      .map((reason) => String(reason.code).toUpperCase());
  }

  function holdingSignal(result = null, missing = null, options = {}) {
    if (missing) {
      return {
        status: "INSUFFICIENT_DATA",
        label: "找不到資料",
        summary: missing.reason || "找不到可套用 X1-X5 的資料。",
      };
    }
    if (!result) {
      return {
        status: options.isScanning ? "WATCH" : "NEUTRAL",
        label: options.isScanning ? "分析中" : "待分析",
        summary: options.isScanning ? "正在套用 X1-X5 出場規則。" : "儲存後會套用 X1-X5 出場規則。",
      };
    }

    const status = result.status;
    const exitCodes = holdingExitCodes(result);
    const exitText = exitCodes.length ? exitCodes.join("、") : "X1-X5";
    const labels = {
      EXIT: `出場 ${exitText}`,
      WARNING: `警戒 ${exitText}`,
      HOLD: "續抱",
      ADD_WATCH: "加碼觀察",
      INSUFFICIENT_DATA: "資料待補",
      EXCLUDED: "策略排除",
      PARTIAL_HOLDING: "持股待補",
    };
    return {
      status,
      label: labels[status] || "未知",
      summary: exitCodes.length ? `觸發 ${exitText}，請查看持股掃描明細。` : result.summary || "X1-X5 未觸發出場條件。",
    };
  }

  function renderHoldingSignal(result = null, missing = null, options = {}) {
    const signal = holdingSignal(result, missing, options);
    const className = signal.status === "NEUTRAL" ? "neutral" : statusClass(signal.status);
    return `
      <div class="holding-signal">
        <span class="status-pill ${className}">${escapeHtml(signal.label)}</span>
        <span>${escapeHtml(signal.summary)}</span>
      </div>
    `;
  }

  return {
    holdingExitCodes,
    holdingSignal,
    renderHoldingSignal,
  };
});
