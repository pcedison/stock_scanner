(function exposeDomHelpers(root, factory) {
  const helpers = factory();
  if (typeof module !== "undefined" && module.exports) {
    module.exports = helpers;
  }
  if (root) {
    root.StockScannerDom = helpers;
  }
})(typeof globalThis !== "undefined" ? globalThis : null, function createDomHelpers() {
  function escapeHtml(value) {
    return String(value ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#039;");
  }

  function emptyStateHtml(message) {
    return `<div class="empty-state">${escapeHtml(message)}</div>`;
  }

  function setEmptyState(target, message) {
    setSafeHtml(target, emptyStateHtml(message));
  }

  function setSafeHtml(target, html) {
    if (target) target.innerHTML = String(html ?? "");
  }

  function clearElement(target) {
    if (target) target.replaceChildren();
  }

  return {
    clearElement,
    emptyStateHtml,
    escapeHtml,
    setEmptyState,
    setSafeHtml,
  };
});
