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

  function setSafeHtml(target, html) {
    if (target) target.innerHTML = String(html ?? "");
  }

  function clearElement(target) {
    if (!target) return;
    if (typeof target.replaceChildren === "function") {
      target.replaceChildren();
      return;
    }
    if ("textContent" in target) {
      target.textContent = "";
      return;
    }
    setSafeHtml(target, "");
  }

  function setClassedText(target, className, message, tagName = "div") {
    if (!target) return;
    const safeTagName = /^[a-z][a-z0-9-]*$/i.test(String(tagName)) ? String(tagName) : "div";
    const doc = target.ownerDocument || (typeof document !== "undefined" ? document : null);
    const element = doc?.createElement?.(safeTagName);
    if (!element || typeof target.appendChild !== "function") {
      setSafeHtml(target, `<${safeTagName} class="${escapeHtml(className)}">${escapeHtml(message)}</${safeTagName}>`);
      return;
    }
    clearElement(target);
    element.className = className;
    element.textContent = String(message ?? "");
    target.appendChild(element);
  }

  function setEmptyState(target, message) {
    setClassedText(target, "empty-state", message);
  }

  function setFormError(target, message) {
    setClassedText(target, "form-error", message, "p");
  }

  return {
    clearElement,
    emptyStateHtml,
    escapeHtml,
    setEmptyState,
    setFormError,
    setSafeHtml,
  };
});
