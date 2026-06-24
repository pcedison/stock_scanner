const DEFAULT_WORKER_API_ORIGIN = "https://stock-scanner-beta-api.pcedison.workers.dev";
const RETRYABLE_API_STATUSES = new Set([500, 502, 503, 504]);
const PUBLIC_DIRECT_FALLBACK_ROUTES = [
  /^GET \/api\/health$/,
  /^GET \/api\/app-status$/,
  /^GET \/api\/settings$/,
  /^GET \/api\/integrations\/status$/,
  /^GET \/api\/backtest$/,
  /^GET \/api\/cache\/status$/,
  /^GET \/api\/scheduler\//,
  /^GET \/api\/data-sources\//,
  /^GET \/api\/calendar\//,
  /^GET \/api\/companies(?:$|[/?])/,
  /^GET \/api\/scan\/market$/,
  /^POST \/api\/scan\/market$/,
  /^POST \/api\/scan\/holdings$/,
  /^POST \/api\/analyze\//,
  /^POST \/api\/reports\/market$/,
  /^POST \/api\/reports\/holdings$/,
];

function normalizeApiOrigin(value) {
  const raw = String(value || "").trim();
  if (!raw) return "";
  try {
    const url = new URL(raw);
    return url.protocol === "https:" ? url.origin : "";
  } catch {
    return "";
  }
}

function configuredApiOrigin(explicitOrigin) {
  const explicit = normalizeApiOrigin(explicitOrigin);
  if (explicit) return explicit;

  if (typeof document !== "undefined") {
    const meta = document.querySelector('meta[name="stock-scanner-api-origin"]');
    const metaOrigin = normalizeApiOrigin(meta?.getAttribute("content"));
    if (metaOrigin) return metaOrigin;
  }

  const runtimeOrigin = normalizeApiOrigin(globalThis.StockScannerConfig?.apiOrigin);
  if (runtimeOrigin) return runtimeOrigin;

  return DEFAULT_WORKER_API_ORIGIN;
}

function configuredApiMode(explicitMode) {
  const normalized = String(explicitMode || "").trim().toLowerCase();
  if (["direct", "fallback", "same-origin"].includes(normalized)) return normalized;

  const runtimeMode = String(globalThis.StockScannerConfig?.apiMode || "").trim().toLowerCase();
  if (["direct", "fallback", "same-origin"].includes(runtimeMode)) return runtimeMode;

  if (typeof document !== "undefined") {
    const meta = document.querySelector('meta[name="stock-scanner-api-mode"]');
    const metaMode = String(meta?.getAttribute("content") || "").trim().toLowerCase();
    if (["direct", "fallback", "same-origin"].includes(metaMode)) return metaMode;
  }

  if (typeof location !== "undefined" && String(location.hostname || "").endsWith(".pages.dev")) {
    return "same-origin";
  }

  return "fallback";
}

function isApiPath(url) {
  return String(url || "").startsWith("/api/");
}

function apiUrlForOrigin(path, origin) {
  return origin ? new URL(path, origin).toString() : path;
}

function normalizedApiPath(url) {
  const text = String(url || "");
  try {
    return new URL(text, "https://stock-scanner.local").pathname;
  } catch {
    return text.split("?", 1)[0] || "";
  }
}

function allowsDirectFallback(url, method = "GET") {
  const route = `${String(method || "GET").toUpperCase()} ${normalizedApiPath(url)}`;
  return PUBLIC_DIRECT_FALLBACK_ROUTES.some((pattern) => pattern.test(route));
}

function cloneHeaders(headers = {}, csrfHeaderName, csrfHeaderValue) {
  const cloned = new Headers(headers);
  if (!cloned.has("Content-Type")) cloned.set("Content-Type", "application/json");
  if (csrfHeaderName && csrfHeaderValue && !cloned.has(csrfHeaderName)) {
    cloned.set(csrfHeaderName, csrfHeaderValue);
  }
  return cloned;
}

function createApiClient(options = {}) {
  const fallbackOrigin = configuredApiOrigin(options.fallbackOrigin);
  const apiMode = configuredApiMode(options.apiMode);
  let activeOrigin = "";

  function candidates(url, method) {
    if (!isApiPath(url) || !fallbackOrigin || apiMode === "same-origin") {
      return [{ url, origin: "" }];
    }
    if (apiMode === "direct") {
      return [{ url: apiUrlForOrigin(url, fallbackOrigin), origin: fallbackOrigin }];
    }
    if (!allowsDirectFallback(url, method)) {
      return [{ url, origin: "" }];
    }
    const sameOrigin = { url, origin: "" };
    const directWorker = { url: apiUrlForOrigin(url, fallbackOrigin), origin: fallbackOrigin };
    return activeOrigin === fallbackOrigin ? [directWorker, sameOrigin] : [sameOrigin, directWorker];
  }

  function shouldRetry(response, index, list) {
    return index < list.length - 1 && RETRYABLE_API_STATUSES.has(Number(response?.status) || 0);
  }

  async function request(url, requestOptions = {}) {
    const { headers = {}, ...rest } = requestOptions;
    const list = candidates(url, rest.method || "GET");
    let lastError = null;

    for (let index = 0; index < list.length; index += 1) {
      const candidate = list[index];
      try {
        const response = await fetch(candidate.url, {
          ...rest,
          headers: cloneHeaders(headers, options.csrfHeaderName, options.csrfHeaderValue),
          credentials: candidate.origin ? "include" : rest.credentials || "same-origin",
        });
        if (shouldRetry(response, index, list)) {
          lastError = response;
          continue;
        }
        activeOrigin = response.ok && candidate.origin ? candidate.origin : "";
        return response;
      } catch (error) {
        lastError = error;
        if (index >= list.length - 1) throw error;
      }
    }

    return lastError;
  }

  return {
    request,
    activeOrigin: () => activeOrigin,
    fallbackOrigin: () => fallbackOrigin,
    apiMode: () => apiMode,
  };
}

const StockScannerApiClient = {
  createApiClient,
  configuredApiMode,
  configuredApiOrigin,
  allowsDirectFallback,
  normalizeApiOrigin,
};

if (typeof module !== "undefined") {
  module.exports = StockScannerApiClient;
}

if (typeof globalThis !== "undefined") {
  globalThis.StockScannerApiClient = StockScannerApiClient;
}
