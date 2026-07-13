(function exposeRenderers(root, factory) {
  const helpers = factory();
  if (typeof module !== "undefined" && module.exports) {
    module.exports = helpers;
  }
  if (root) {
    root.StockScannerRenderers = helpers;
  }
})(typeof globalThis !== "undefined" ? globalThis : null, function createRendererHelpers() {
  function createRenderers(dependencies = {}) {
    const {
      escapeHtml,
      isHoldingTracked,
      isPartialPublishedResult,
      normalizeText,
      safeCompanyName,
      safeText,
      strategyStatusDetails = [],
      strategyRuleThresholds = {},
    } = dependencies;

    function statusLabel(status) {
      const labels = {
        ENTRY: "適合進場",
        WATCH: "觀察",
        HOLD: "續抱",
        ADD_WATCH: "加碼觀察",
        WARNING: "警戒",
        EXIT: "建議出清",
        EXCLUDED: "排除",
        INSUFFICIENT_DATA: "待補資料",
        PARTIAL_DATA: "可初篩",
        PARTIAL_HOLDING: "可追蹤",
      };
      return labels[status] || "未知";
    }

    function statusClass(status) {
      return `status-${String(status || "neutral").toLowerCase()}`;
    }

    function ruleDisplayOrder(rule = {}) {
      const code = String(rule.code || "").toUpperCase();
      let match = code.match(/^E([1-6])$/);
      if (match) return 100 + Number(match[1]);
      if (code === "OFFICIAL_Q") return 170;
      if (code === "OFFICIAL_VALUATION") return 180;
      match = code.match(/^X([1-5])$/);
      if (match) return 200 + Number(match[1]);
      if (code === "SPRING_FESTIVAL_WATCH") return 230;
      match = code.match(/^T(\d+)$/);
      if (match) return 240 + Number(match[1]);
      match = code.match(/^A([1-7])$/);
      if (match) return 300 + Number(match[1]);
      if (code === "HOLDING") return 400;
      match = code.match(/^FIN(\d+)$/);
      if (match) return 110 + Number(match[1]);
      return 900;
    }

    function sortRulesForDisplay(reasons = []) {
      return reasons
        .map((reason, index) => ({ reason, index }))
        .sort(
          (left, right) => ruleDisplayOrder(left.reason) - ruleDisplayOrder(right.reason) || left.index - right.index,
        )
        .map((item) => item.reason);
    }

    function formatEvidenceValue(item = {}) {
      const value = Number(item.value);
      if (!Number.isFinite(value)) return "待補";
      if (item.unit === "thousand_twd") {
        const absValue = Math.abs(value);
        const divisor = absValue >= 100000 ? 100000 : 10;
        const unit = absValue >= 100000 ? "億" : "萬";
        const amount = value / divisor;
        return `${amount.toLocaleString("zh-TW", {
          minimumFractionDigits: absValue >= 100000 ? 2 : 0,
          maximumFractionDigits: 2,
        })} ${unit}`;
      }
      return value.toLocaleString("zh-TW", { maximumFractionDigits: 2 });
    }

    function evidenceTone(item = {}) {
      const value = Number(item.value);
      if (!Number.isFinite(value)) return { className: "missing", label: "待補" };
      if (value > 0) return { className: "passed", label: "獲利" };
      if (value < 0) return { className: "failed", label: "虧損" };
      return { className: "missing", label: "損益兩平" };
    }

    function renderRuleEvidence(rule = {}) {
      const evidence = Array.isArray(rule.evidence) ? rule.evidence.filter(Boolean) : [];
      if (!evidence.length) return "";
      const numericValues = evidence
        .map((item) => Math.abs(Number(item.value)))
        .filter((value) => Number.isFinite(value));
      const maxAbs = numericValues.length ? Math.max(...numericValues) : 0;
      const bars = evidence
        .map((item) => {
          const value = Number(item.value);
          const tone = evidenceTone(item);
          const width =
            Number.isFinite(value) && maxAbs > 0 ? Math.max(8, Math.round((Math.abs(value) / maxAbs) * 100)) : 0;
          return `
          <div class="evidence-bar-row">
            <span class="evidence-label">${escapeHtml(item.label || "")}</span>
            <span class="evidence-track"><span class="evidence-bar ${tone.className}" data-evidence-width="${escapeHtml(width)}"></span></span>
            <span class="evidence-value ${tone.className}">${escapeHtml(formatEvidenceValue(item))}</span>
          </div>
        `;
        })
        .join("");
      const rows = evidence
        .map((item) => {
          const tone = evidenceTone(item);
          return `
          <tr>
            <th scope="row">${escapeHtml(item.label || "")}</th>
            <td class="${tone.className}">${escapeHtml(formatEvidenceValue(item))}</td>
            <td class="${tone.className}">${escapeHtml(tone.label)}</td>
          </tr>
        `;
        })
        .join("");
      return `
        <div class="rule-evidence" aria-label="${escapeHtml(rule.code || "")} 年度數據">
          <div class="evidence-bars">${bars}</div>
          <table class="evidence-table">
            <thead>
              <tr><th scope="col">年度</th><th scope="col">淨利</th><th scope="col">判讀</th></tr>
            </thead>
            <tbody>${rows}</tbody>
          </table>
        </div>
      `;
    }

    function applyEvidenceBarWidths(root = null) {
      if (typeof document === "undefined" && !root) return;
      const scope = root || document;
      if (!scope.querySelectorAll) return;
      scope.querySelectorAll("[data-evidence-width]").forEach((bar) => {
        const value = Number(bar.dataset.evidenceWidth);
        const width = Number.isFinite(value) ? Math.max(0, Math.min(100, value)) : 0;
        bar.style.setProperty("--bar-width", `${width}%`);
      });
    }

    function renderRule(rule = {}) {
      const isMissing = rule.severity === "INSUFFICIENT_DATA";
      const stateClass = isMissing ? "missing" : rule.passed ? "passed" : "failed";
      const stateText = isMissing ? "待補" : rule.passed ? "通過" : "未通過";
      return `
        <div class="rule">
          <div class="rule-code ${stateClass}">${escapeHtml(rule.code)} ${stateText}</div>
          <div>
            <strong>${escapeHtml(rule.title)}</strong>
            <div class="muted">${escapeHtml(rule.message)}</div>
            ${renderRuleEvidence(rule)}
          </div>
        </div>
      `;
    }

    function displayResultStatus(result = {}, disclosureGroup = "") {
      const partialPublished = disclosureGroup === "announced" && isPartialPublishedResult(result);
      const partialHolding = disclosureGroup === "holding" && isPartialPublishedResult(result);
      return {
        status: partialHolding ? "PARTIAL_HOLDING" : partialPublished ? "PARTIAL_DATA" : result.status,
        summary: partialHolding
          ? "已有公開揭露資料可追蹤；完整續抱或出場結論仍待補歷史財報、季 YoY 或週轉率等欄位。"
          : partialPublished
            ? "已有公開揭露資料可初步掃描；完整進場結論仍待補歷史財報、PER 或週轉率等欄位。"
            : result.summary || "已完成規則分析。",
      };
    }

    function resultActionButtons(result, options = {}) {
      const companyName = result.companyName || safeCompanyName(result);
      const tracked = isHoldingTracked(result.stockCode);
      const stockCode = safeText(result.stockCode, "未知代碼");
      const exitButton =
        options.allowExitAction && result.status === "EXIT"
          ? `<button class="danger-btn" type="button" data-clear-from-result="${escapeHtml(stockCode)}">採用出場建議</button>`
          : "";
      const addButton =
        options.allowAddAction && result.status !== "EXCLUDED"
          ? tracked
            ? `<button class="secondary-btn holding-state-btn" type="button" disabled>已在持股</button>`
            : `<button class="secondary-btn" type="button" data-add-from-result="${escapeHtml(stockCode)}" data-add-name="${escapeHtml(companyName)}">加入持股</button>`
          : "";
      return exitButton || addButton ? `<div class="button-row">${addButton}${exitButton}</div>` : "";
    }

    function renderAnalysisCard(result, options = {}) {
      result = result || {};
      const companyName = result.companyName || safeCompanyName(result);
      const stockCode = safeText(result.stockCode, "未知代碼");
      const reasons = sortRulesForDisplay(Array.isArray(result.reasons) ? result.reasons.filter(Boolean) : []);
      const { status: displayStatus, summary: displaySummary } = displayResultStatus(result, options.disclosureGroup);
      return `
        <article class="card analysis-card" data-result-status="${escapeHtml(displayStatus)}">
          <div class="card-head">
            <div>
              <h3 class="stock-title">${escapeHtml(stockCode)} ${escapeHtml(companyName)}</h3>
              <p class="muted">${escapeHtml(displaySummary)}</p>
            </div>
            <span class="status-pill ${statusClass(displayStatus)}">${escapeHtml(statusLabel(displayStatus))}</span>
          </div>
          <div class="rules">${reasons.map(renderRule).join("")}</div>
          ${resultActionButtons(result, options)}
        </article>
      `;
    }

    function strategyThresholdsFor(code, title) {
      const direct = strategyRuleThresholds[code];
      if (direct) return direct;
      const compactTitle = normalizeText(title);
      const matches = compactTitle.match(/(>=|>|<|≤|小於|大於)\s*\d+(?:\.\d+)?%?/g);
      return matches?.length ? matches : ["條件檢查"];
    }

    function renderStrategyRuleCards(details = strategyStatusDetails) {
      return details
        .map(
          (detail) => `
            <article class="strategy-rule-card">
              <div>
                <h3>${escapeHtml(detail.label)}</h3>
                <p class="strategy-rule-summary">${escapeHtml(detail.summary)}</p>
              </div>
              <ul class="strategy-rule-list">
                ${detail.items
                  .map(([code, title]) => {
                    const thresholds = strategyThresholdsFor(code, title);
                    return `
                      <li class="strategy-rule-item">
                        <span class="strategy-rule-code">${escapeHtml(code)}</span>
                        <p class="strategy-rule-title">${escapeHtml(title)}</p>
                        <div class="strategy-rule-thresholds">
                          ${thresholds.map((threshold) => `<span class="rule-threshold-pill">${escapeHtml(threshold)}</span>`).join("")}
                        </div>
                      </li>
                    `;
                  })
                  .join("")}
              </ul>
            </article>
          `,
        )
        .join("");
    }

    return {
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
      strategyThresholdsFor,
    };
  }

  return { createRenderers };
});
