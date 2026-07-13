(function exposeMarketRefresh(root, factory) {
  const helpers = factory();
  if (typeof module !== "undefined" && module.exports) module.exports = helpers;
  if (root) root.StockScannerMarketRefresh = helpers;
})(typeof globalThis !== "undefined" ? globalThis : null, function createMarketRefreshModule() {
  const COMMAND_URL = "/api/scan/market/refresh";
  const POLL_DELAYS_MS = [1000, 2000, 4000, 8000, 15000];
  const DEFAULT_TIMEOUT_MS = 60_000;
  const JOB_ID_PATTERN = /^[0-9a-f]{32}$/;
  const SAFE_ID_PATTERN = /^[A-Za-z0-9._:-]{1,80}$/;
  const ACTIVE_STATUSES = new Set(["queued", "running"]);
  const TERMINAL_STATUSES = new Set(["success", "failed"]);
  const ALL_STATUSES = new Set([...ACTIVE_STATUSES, ...TERMINAL_STATUSES]);
  const REFRESH_REASONS = new Set(["financial_report_window", "monthly_revenue_window", "routine_refresh"]);
  const COMMAND_FIELDS = new Set(["jobId", "status", "requestId", "statusUrl"]);
  const STATUS_FIELDS = new Set([
    "jobId",
    "status",
    "reason",
    "queuedAt",
    "startedAt",
    "finishedAt",
    "updatedAt",
    "ownerRunId",
    "ownerRunUrl",
    "hasError",
    "dispatchStatus",
    "dispatchErrorCode",
  ]);
  const DISPATCH_STATUSES = new Set(["pending", "dispatching", "dispatched", "failed", "unknown", "workflow_claimed"]);

  function requireValue(condition, message) {
    if (!condition) throw new Error(message);
  }
  function isObject(value) {
    return value !== null && typeof value === "object" && !Array.isArray(value);
  }
  function exactFields(value, allowed, required) {
    requireValue(isObject(value), "invalid refresh payload");
    const keys = Object.keys(value);
    requireValue(
      keys.every((key) => allowed.has(key)),
      "unexpected refresh field",
    );
    requireValue(
      required.every((key) => keys.includes(key)),
      "missing refresh field",
    );
  }
  function boundedText(value, maximum) {
    return (
      typeof value === "string" &&
      value.length > 0 &&
      value.length <= maximum &&
      [...value].every((character) => character.charCodeAt(0) >= 32 && character.charCodeAt(0) !== 127)
    );
  }
  function validTimestamp(value) {
    return (
      value === null || (boundedText(value, 128) && /^[0-9T: +.Z-]+$/.test(value) && !Number.isNaN(Date.parse(value)))
    );
  }
  function validateRefreshCommand(value) {
    exactFields(value, COMMAND_FIELDS, ["jobId", "status", "requestId", "statusUrl"]);
    requireValue(JOB_ID_PATTERN.test(value.jobId), "invalid refresh jobId");
    requireValue(ALL_STATUSES.has(value.status), "invalid refresh status");
    requireValue(SAFE_ID_PATTERN.test(value.requestId), "invalid refresh requestId");
    requireValue(value.statusUrl === `${COMMAND_URL}/${value.jobId}`, "invalid refresh statusUrl");
    return { ...value };
  }
  function validateRefreshStatus(value, expectedJobId) {
    exactFields(value, STATUS_FIELDS, ["jobId", "status"]);
    requireValue(JOB_ID_PATTERN.test(expectedJobId) && value.jobId === expectedJobId, "refresh jobId mismatch");
    requireValue(ALL_STATUSES.has(value.status), "invalid refresh status");
    if ("reason" in value) requireValue(REFRESH_REASONS.has(value.reason), "invalid refresh reason");
    for (const field of ["queuedAt", "startedAt", "finishedAt", "updatedAt"])
      if (field in value) requireValue(validTimestamp(value[field]), `invalid ${field}`);
    const hasOwnerId = "ownerRunId" in value;
    const hasOwnerUrl = "ownerRunUrl" in value;
    requireValue(hasOwnerId === hasOwnerUrl, "incomplete owner run fields");
    if (hasOwnerId) {
      requireValue(/^[0-9]{1,20}$/.test(value.ownerRunId), "invalid ownerRunId");
      const validUrl =
        value.ownerRunUrl === null ||
        (typeof value.ownerRunUrl === "string" &&
          /^https:\/\/github\.com\/[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+\/actions\/runs\/[0-9]{1,20}$/.test(
            value.ownerRunUrl,
          ) &&
          value.ownerRunUrl.endsWith(`/actions/runs/${value.ownerRunId}`));
      requireValue(validUrl, "invalid ownerRunUrl");
    }
    if ("hasError" in value) requireValue(typeof value.hasError === "boolean", "invalid hasError");
    if ("dispatchStatus" in value) requireValue(DISPATCH_STATUSES.has(value.dispatchStatus), "invalid dispatchStatus");
    if ("dispatchErrorCode" in value)
      requireValue(/^[A-Z0-9_:-]{1,80}$/.test(value.dispatchErrorCode), "invalid dispatchErrorCode");
    return JSON.parse(JSON.stringify(value));
  }
  function refreshError(message, code, name = "Error") {
    const error = new Error(message);
    error.code = code;
    error.name = name;
    return error;
  }
  function defaultKey() {
    const uuid = globalThis.crypto?.randomUUID?.();
    if (uuid && SAFE_ID_PATTERN.test(uuid)) return uuid;
    return `refresh-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 14)}`;
  }
  function defaultDelay(milliseconds, signal) {
    if (signal.aborted) return Promise.reject(signal.reason);
    return new Promise((resolve, reject) => {
      const onAbort = () => {
        clearTimeout(timer);
        reject(signal.reason);
      };
      const timer = setTimeout(() => {
        signal.removeEventListener("abort", onAbort);
        resolve();
      }, milliseconds);
      signal.addEventListener("abort", onAbort, { once: true });
    });
  }
  function createMarketRefreshClient({
    apiJson,
    createKey = defaultKey,
    delay = defaultDelay,
    timeoutMs = DEFAULT_TIMEOUT_MS,
    pageLifecycle = typeof globalThis !== "undefined" ? globalThis : null,
    onSuccess = null,
  }) {
    requireValue(typeof apiJson === "function", "apiJson is required");
    requireValue(onSuccess === null || typeof onSuccess === "function", "invalid success callback");
    requireValue(Number.isFinite(timeoutMs) && timeoutMs > 0 && timeoutMs <= 60_000, "invalid refresh timeout");
    let activePromise = null;
    let activeController = null;
    let pendingKey = null;
    const abortForPagehide = () =>
      activeController?.abort(refreshError("refresh polling aborted", "REFRESH_ABORTED", "AbortError"));
    pageLifecycle?.addEventListener?.("pagehide", abortForPagehide);

    async function checkedRequest(url, options, signal) {
      try {
        return await apiJson(url, { ...options, signal });
      } catch (error) {
        if (signal.aborted) throw signal.reason;
        throw error;
      }
    }
    async function terminalResult(payload) {
      if (payload.status === "failed") throw refreshError("market refresh failed", "REFRESH_FAILED");
      const result = onSuccess ? await onSuccess(payload) : null;
      return result ?? payload;
    }
    async function run(signal) {
      const clientKey = pendingKey || createKey();
      requireValue(typeof clientKey === "string" && SAFE_ID_PATTERN.test(clientKey), "invalid client refresh key");
      pendingKey = clientKey;
      const rawCommand = await checkedRequest(
        COMMAND_URL,
        { method: "POST", headers: { "Idempotency-Key": clientKey }, body: "{}" },
        signal,
      );
      const command = validateRefreshCommand(rawCommand);
      pendingKey = null;
      if (TERMINAL_STATUSES.has(command.status)) return terminalResult(command);
      let pollIndex = 0;
      while (ACTIVE_STATUSES.has(command.status)) {
        const wait = POLL_DELAYS_MS[Math.min(pollIndex, POLL_DELAYS_MS.length - 1)];
        await delay(wait, signal);
        if (signal.aborted) throw signal.reason;
        const status = validateRefreshStatus(
          await checkedRequest(command.statusUrl, { method: "GET" }, signal),
          command.jobId,
        );
        if (TERMINAL_STATUSES.has(status.status)) return terminalResult(status);
        pollIndex += 1;
      }
      throw refreshError("invalid refresh state", "REFRESH_INVALID_STATE");
    }
    function refresh({ signal } = {}) {
      if (activePromise) return activePromise;
      const controller = new AbortController();
      activeController = controller;
      const forwardAbort = () =>
        controller.abort(signal.reason || refreshError("refresh polling aborted", "REFRESH_ABORTED", "AbortError"));
      if (signal?.aborted) forwardAbort();
      else signal?.addEventListener("abort", forwardAbort, { once: true });
      const timer = setTimeout(
        () => controller.abort(refreshError("refresh polling timed out", "REFRESH_TIMEOUT")),
        timeoutMs,
      );
      activePromise = run(controller.signal).finally(() => {
        clearTimeout(timer);
        signal?.removeEventListener("abort", forwardAbort);
        activeController = null;
        activePromise = null;
      });
      return activePromise;
    }
    function abort() {
      activeController?.abort(refreshError("refresh polling aborted", "REFRESH_ABORTED", "AbortError"));
    }
    function destroy() {
      abort();
      pageLifecycle?.removeEventListener?.("pagehide", abortForPagehide);
    }
    return { refresh, abort, destroy };
  }
  return {
    COMMAND_URL,
    DEFAULT_TIMEOUT_MS,
    POLL_DELAYS_MS,
    createMarketRefreshClient,
    validateRefreshCommand,
    validateRefreshStatus,
  };
});
