const DEFAULT_WORKER_API_ORIGIN = "https://stock-scanner-beta-api.pcedison.workers.dev";
const RETRYABLE_API_STATUSES = new Set([500, 502, 503, 504]);
const SAME_ENDPOINT_RETRYABLE_STATUSES = new Set([502, 503, 504]);
const IDEMPOTENT_API_METHODS = new Set(["GET", "HEAD"]);
const DEFAULT_GET_RETRY_DELAYS_MS = [150, 450];
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
  /^GET \/api\/scan\/market\/(?:index|results)$/,
  /^GET \/api\/scan\/market\/refresh\/[0-9a-f]{32}$/,
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
  const normalized = String(explicitMode || "")
    .trim()
    .toLowerCase();
  if (["direct", "fallback", "same-origin"].includes(normalized)) return normalized;

  const runtimeMode = String(globalThis.StockScannerConfig?.apiMode || "")
    .trim()
    .toLowerCase();
  if (["direct", "fallback", "same-origin"].includes(runtimeMode)) return runtimeMode;

  if (typeof document !== "undefined") {
    const meta = document.querySelector('meta[name="stock-scanner-api-mode"]');
    const metaMode = String(meta?.getAttribute("content") || "")
      .trim()
      .toLowerCase();
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
  const normalizedMethod = String(method || "GET").toUpperCase();
  const routeMethod = normalizedMethod === "HEAD" ? "GET" : normalizedMethod;
  const route = `${routeMethod} ${normalizedApiPath(url)}`;
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

function abortError(signal) {
  if (signal?.reason instanceof Error) return signal.reason;
  if (typeof DOMException === "function") return new DOMException("The operation was aborted", "AbortError");
  const error = new Error("The operation was aborted");
  error.name = "AbortError";
  return error;
}

function isAbortError(error, signal) {
  return Boolean(signal?.aborted || error?.name === "AbortError");
}

function abortableDelay(delayMs, signal) {
  if (signal?.aborted) return Promise.reject(abortError(signal));
  const milliseconds = Math.max(0, Number(delayMs) || 0);
  if (!milliseconds) return Promise.resolve();
  return new Promise((resolve, reject) => {
    const onAbort = () => {
      clearTimeout(timer);
      reject(abortError(signal));
    };
    const timer = setTimeout(() => {
      signal?.removeEventListener("abort", onAbort);
      resolve();
    }, milliseconds);
    signal?.addEventListener("abort", onAbort, { once: true });
  });
}

function createApiClient(options = {}) {
  const fallbackOrigin = configuredApiOrigin(options.fallbackOrigin);
  const apiMode = configuredApiMode(options.apiMode);
  const getRetryCount = Number.isInteger(options.getRetryCount) ? Math.max(0, options.getRetryCount) : 2;
  const retryDelaysMs = Array.isArray(options.retryDelaysMs) ? options.retryDelaysMs : DEFAULT_GET_RETRY_DELAYS_MS;
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

  function rememberResponseOrigin(response, candidate) {
    activeOrigin = response.ok && candidate.origin ? candidate.origin : "";
    return response;
  }

  async function request(url, requestOptions = {}) {
    const { headers = {}, ...rest } = requestOptions;
    const method = String(rest.method || "GET").toUpperCase();
    const safeMethod = IDEMPOTENT_API_METHODS.has(method);
    const list = candidates(url, method);
    const fetchCandidate = async (candidate) => {
      if (rest.signal?.aborted) throw abortError(rest.signal);
      return fetch(candidate.url, {
        ...rest,
        method,
        headers: cloneHeaders(headers, options.csrfHeaderName, options.csrfHeaderValue),
        credentials: candidate.origin ? "include" : rest.credentials || "same-origin",
      });
    };

    if (!safeMethod) {
      const primary = list[0];
      return rememberResponseOrigin(await fetchCandidate(primary), primary);
    }

    let lastResult = null;
    for (let round = 0; round <= getRetryCount; round += 1) {
      let roundCanRetry = true;
      for (let index = 0; index < list.length; index += 1) {
        const candidate = list[index];
        const hasNextCandidate = index < list.length - 1;
        try {
          const response = await fetchCandidate(candidate);
          const status = Number(response?.status) || 0;
          lastResult = response;
          if (RETRYABLE_API_STATUSES.has(status) && hasNextCandidate) {
            if (status === 500) roundCanRetry = false;
            continue;
          }
          if (SAME_ENDPOINT_RETRYABLE_STATUSES.has(status)) {
            if (hasNextCandidate) continue;
            break;
          }
          return rememberResponseOrigin(response, candidate);
        } catch (error) {
          if (isAbortError(error, rest.signal)) throw abortError(rest.signal);
          lastResult = error;
          if (hasNextCandidate) continue;
          break;
        }
      }

      if (!roundCanRetry || round >= getRetryCount) {
        if (lastResult instanceof Error) throw lastResult;
        activeOrigin = "";
        return lastResult;
      }
      const delayMs = retryDelaysMs[round] ?? retryDelaysMs.at(-1) ?? 0;
      try {
        await abortableDelay(delayMs, rest.signal);
      } catch (error) {
        if (isAbortError(error, rest.signal)) throw abortError(rest.signal);
        throw error;
      }
    }

    return lastResult;
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
