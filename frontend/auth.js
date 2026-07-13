(function exposeAuthHelpers(root, factory) {
  const helpers = factory();
  if (typeof module !== "undefined" && module.exports) {
    module.exports = helpers;
  }
  if (root) {
    root.StockScannerAuth = helpers;
  }
})(typeof globalThis !== "undefined" ? globalThis : null, function createAuthHelpers() {
  function normalizeText(value) {
    return String(value || "")
      .trim()
      .replace(/\s+/g, " ");
  }

  function normalizeAuthUsername(username) {
    return normalizeText(username).toLowerCase();
  }

  function isSuperUserIdentity(user) {
    return Boolean(user?.isSuperUser);
  }

  function normalizeAuthUser(user) {
    if (!user || typeof user !== "object") return null;
    const username = normalizeText(user.username);
    if (!username) return null;
    return {
      ...user,
      username,
      displayName: normalizeText(user.displayName) || username,
      isSuperUser: isSuperUserIdentity(user),
    };
  }

  function authValidationMessage(username, password) {
    const normalized = normalizeAuthUsername(username);
    if (!normalized) return "請輸入電子信箱。";
    if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(normalized)) return "請輸入有效電子信箱。";
    if ((password || "").length < 8) return "密碼至少需要 8 個字元。";
    if ((password || "").length > 128) return "密碼不可超過 128 個字元。";
    return "";
  }

  return {
    authValidationMessage,
    isSuperUserIdentity,
    normalizeAuthUser,
    normalizeAuthUsername,
  };
});
