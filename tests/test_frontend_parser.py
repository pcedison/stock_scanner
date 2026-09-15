import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MOJIBAKE_CONTROL_RE = re.compile(r"[\u0080-\u009f\ue000-\uf8ff]")


@pytest.fixture(autouse=True)
def _require_node() -> None:
    # These tests exercise the real frontend JS via node. A missing node must be a
    # hard failure, not a silent skip, so CI/local runs can't quietly lose this
    # coverage.
    if shutil.which("node") is None:
        pytest.fail("Node.js is required for the frontend parser tests; install Node to run them.", pytrace=False)


def _run_node_json(script: str) -> dict:
    completed = subprocess.run(
        ["node", "-e", script],
        cwd=ROOT,
        check=True,
        text=True,
        encoding="utf-8",
        capture_output=True,
    )
    return json.loads(completed.stdout)


MARKET_QUERY_NODE_FIXTURE = r"""
const {
  createMarketQueryClient,
  createMarketQueryCoordinator,
  requiredApiCursors,
  validateMarketIndex,
} = require("./frontend/market_query.js");

const generationA = "a".repeat(24);
const generationB = "b".repeat(24);
function pageRefs(generationId, disclosure, category, count) {
  const refs = [];
  for (let cursor = 0; cursor < count; cursor += 100) {
    const pageCount = Math.min(100, count - cursor);
    refs.push({
      key: `public/market_scan/v2/${generationId}/${disclosure}/${category}/${cursor}.json`,
      cursor,
      count: pageCount,
      bytes: 256,
      sha256: "0".repeat(64),
    });
  }
  return refs;
}
function marketIndex({ generationId = generationA, watch = 205, entry = 0, excluded = 0 } = {}) {
  const announcedCount = watch + entry + excluded;
  const bucket = (disclosure, category, count) => ({
    count,
    pages: pageRefs(generationId, disclosure, category, count),
  });
  return {
    schemaVersion: 2,
    generationId,
    generatedAt: "2026-07-13T01:02:03+00:00",
    pageSize: 100,
    detailMode: "summary",
    disclosurePeriod: "2026Q1",
    filingContext: { freshnessFinancialReport: { period: "2026Q1" } },
    financialFreshness: { latestCachedFinancialPeriod: "2026Q1" },
    cacheStatusInputs: { latestFinancialPeriod: "2026Q1" },
    counts: {
      universeSize: announcedCount,
      announced: announcedCount,
      pending: 0,
      categories: { entry, watch, excluded },
    },
    disclosures: {
      announced: {
        count: announcedCount,
        entry: bucket("announced", "entry", entry),
        watch: bucket("announced", "watch", watch),
        excluded: bucket("announced", "excluded", excluded),
      },
      pending: {
        count: 0,
        entry: bucket("pending", "entry", 0),
        watch: bucket("pending", "watch", 0),
        excluded: bucket("pending", "excluded", 0),
      },
    },
    cacheStatus: {
      cacheHit: true,
      isStale: false,
      storedAt: "2026-07-13T01:02:03+00:00",
      refreshStatus: "fresh",
    },
  };
}
function pagePayload(index, disclosure, category, cursor) {
  const total = index.disclosures[disclosure][category].count;
  const count = Math.max(0, Math.min(100, total - cursor));
  return {
    schemaVersion: 2,
    generationId: index.generationId,
    disclosure,
    category,
    cursor,
    limit: 100,
    total,
    nextCursor: cursor + count < total ? cursor + count : null,
    items: Array.from({ length: count }, (_, offset) => ({
      stockCode: String(1000 + cursor + offset),
      companyName: `Company ${cursor + offset}`,
      status: "WATCH",
      summary: "summary",
      reasons: [],
      detailsAvailable: true,
      hasFullDetails: false,
    })),
  };
}
function memoryStorage(initial = {}) {
  const values = new Map(Object.entries(initial));
  const calls = [];
  return {
    calls,
    getItem(key) { calls.push(["get", key]); return values.has(key) ? values.get(key) : null; },
    setItem(key, value) { calls.push(["set", key, value]); values.set(key, value); },
    removeItem(key) { calls.push(["remove", key]); values.delete(key); },
    value(key) { return values.get(key); },
  };
}
"""


def test_frontend_dom_helpers_escape_empty_state_html():
    script = r"""
const { emptyStateHtml, escapeHtml, setEmptyState } = require("./frontend/dom.js");
const target = {};
Object.defineProperty(target, "innerHTML", { set(value) { this.value = value; } });
setEmptyState(target, '<img src=x onerror="alert(1)">');
console.log(JSON.stringify({
  escaped: escapeHtml('<img src=x onerror="alert(1)">'),
  empty: emptyStateHtml('<script>alert(1)</script>'),
  setEmpty: target.value,
}));
"""
    completed = subprocess.run(
        ["node", "-e", script],
        cwd=ROOT,
        check=True,
        text=True,
        encoding="utf-8",
        capture_output=True,
    )
    payload = json.loads(completed.stdout)

    assert "<img" not in payload["escaped"]
    assert "&lt;img" in payload["escaped"]
    assert "<script" not in payload["empty"].lower()
    assert "&lt;script" in payload["empty"].lower()
    assert "<img" not in payload["setEmpty"].lower()
    assert "&lt;img" in payload["setEmpty"].lower()


def test_market_render_column_includes_sr_only_stock_identity():
    script = r"""
const { createMarketRender } = require("./frontend/market_render.js");
const renderer = createMarketRender({
  getState: () => ({ marketListPages: { announced: { watch: 0 } }, expandedMarketResultIds: new Set() }),
  escapeHtml(value) {
    return String(value ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  },
  safeText(value, fallback = "") {
    const text = value == null ? "" : String(value).trim();
    return text || fallback;
  },
  safeCompanyName(result) {
    return result?.companyName || "";
  },
  displayResultStatus(result) {
    return { status: result?.status || "WATCH", summary: result?.summary || "" };
  },
  statusClass(status) {
    return String(status || "").toLowerCase();
  },
  statusLabel(status) {
    return status || "";
  },
  renderRule() {
    return "";
  },
  resultActionButtons() {
    return "";
  },
  sortRulesForDisplay(rules) {
    return Array.isArray(rules) ? rules : [];
  },
  sortMarketResultsForDisplay(_columnKey, results) {
    return Array.isArray(results) ? results : [];
  },
  marketColumnNote() {
    return "";
  },
  marketResultId(result, disclosureGroup, columnKey) {
    return `${disclosureGroup}:${columnKey}:${result.stockCode}`;
  },
  MARKET_LIST_PAGE_SIZE: 12,
  MARKET_RESULT_COLUMNS: [["watch", "Watch"]],
  MARKET_DISCLOSURE_TABS: [{ key: "announced" }],
});
const html = renderer.renderMarketColumn("announced", "watch", "Watch", [
  { stockCode: "1101", companyName: "台泥", status: "WATCH", reasons: [] },
]);
console.log(JSON.stringify({
  html,
  formatCacheTime: typeof renderer.formatCacheTime,
  cacheRefreshLabel: typeof renderer.cacheRefreshLabel,
}));
"""
    payload = _run_node_json(script)

    assert '<span class="sr-only">1101 台泥</span>' in payload["html"]
    assert 'data-market-result-toggle="announced:watch:1101"' in payload["html"]
    assert payload["formatCacheTime"] == "function"
    assert payload["cacheRefreshLabel"] == "function"


def test_overview_counts_follow_active_disclosure_tab():
    script = r"""
const { state, activeMarketDisclosureKey, renderOverviewStats } = require("./frontend/app.js");
const nodes = new Map();
global.document = {
  querySelector(selector) {
    if (!nodes.has(selector)) nodes.set(selector, { textContent: "" });
    return nodes.get(selector);
  },
};
const officialQ = { code: "OFFICIAL_Q", severity: "INFO", message: "2026Q1 EPS 1.23" };
const oldQ = { code: "OFFICIAL_Q", severity: "INFO", message: "2025Q4 EPS 1.23" };
const scan = {
  generatedAt: "2026-05-20T00:00:00.000Z",
  filingContext: { activeFinancialReport: { period: "2026Q1" } },
  entry: [
    { stockCode: "1001", status: "ENTRY", reasons: [officialQ] },
    { stockCode: "1002", status: "ENTRY", reasons: [oldQ] },
  ],
  watch: [
    { stockCode: "2001", status: "INSUFFICIENT_DATA", reasons: [officialQ, { code: "E3", severity: "WATCH" }] },
    { stockCode: "2002", status: "INSUFFICIENT_DATA", reasons: [{ code: "E3", severity: "INSUFFICIENT_DATA" }] },
  ],
  excluded: [
    { stockCode: "3001", status: "EXCLUDED", reasons: [officialQ] },
  ],
};
function counts() {
  return {
    entry: nodes.get("#overview-entry-count").textContent,
    watch: nodes.get("#overview-watch-count").textContent,
    excluded: nodes.get("#overview-excluded-count").textContent,
  };
}
state.activeMarketDisclosureTab = "announced";
renderOverviewStats(scan);
const announced = counts();
state.activeMarketDisclosureTab = "pending";
renderOverviewStats(scan);
const pending = counts();
console.log(JSON.stringify({ announced, pending, fallback: activeMarketDisclosureKey("bogus") }));
"""
    completed = subprocess.run(
        ["node", "-e", script],
        cwd=ROOT,
        check=True,
        text=True,
        encoding="utf-8",
        capture_output=True,
    )
    payload = json.loads(completed.stdout)

    assert payload["announced"] == {"entry": "1", "watch": "1", "excluded": "1"}
    assert payload["pending"] == {"entry": "1", "watch": "1", "excluded": "0"}
    assert payload["fallback"] == "announced"


def test_market_scan_groups_by_freshness_financial_period_before_active_period():
    script = r"""
const { createMarketScan } = require("./frontend/market_scan.js");
const { groupMarketScanResults } = createMarketScan({
  getState: () => ({}),
  safeText: (value, fallback = "") => String(value ?? "").trim() || fallback,
});
const officialQ = (stockCode, period, status = "INSUFFICIENT_DATA") => ({
  stockCode,
  status,
  reasons: [
    { code: "E3", severity: "WATCH" },
    { code: "OFFICIAL_Q", severity: "INFO", message: `${period} EPS 1.23` },
  ],
});
const stockCodes = (group) => Object.fromEntries(
  Object.entries(group).map(([column, results]) => [column, results.map((result) => result.stockCode)])
);

const mixedContext = groupMarketScanResults({
  filingContext: {
    activeFinancialReport: { period: "2026Q2" },
    freshnessFinancialReport: { period: "2026Q1" },
  },
  entry: [officialQ("1001", "2026Q1", "ENTRY"), officialQ("1002", "2026Q2", "ENTRY")],
  watch: [officialQ("2001", "2026Q1"), officialQ("2002", "2025Q4")],
  excluded: [],
});
const activeFallback = groupMarketScanResults({
  filingContext: { activeFinancialReport: { period: "2026Q2" } },
  entry: [officialQ("3001", "2026Q2", "ENTRY"), officialQ("3002", "2026Q1", "ENTRY")],
  watch: [],
  excluded: [],
});
const noPeriod = groupMarketScanResults({
  filingContext: {},
  entry: [],
  watch: [{ stockCode: "4001", status: "INSUFFICIENT_DATA", reasons: [{ code: "E3", severity: "WATCH" }] }],
  excluded: [],
});
console.log(JSON.stringify({
  mixed: { announced: stockCodes(mixedContext.announced), pending: stockCodes(mixedContext.pending) },
  fallback: { announced: stockCodes(activeFallback.announced), pending: stockCodes(activeFallback.pending) },
  noPeriod: { announced: stockCodes(noPeriod.announced), pending: stockCodes(noPeriod.pending) },
}));
"""
    payload = _run_node_json(script)

    assert payload["mixed"] == {
        "announced": {"entry": ["1001"], "watch": ["2001"], "excluded": []},
        "pending": {"entry": ["1002"], "watch": ["2002"], "excluded": []},
    }
    assert payload["fallback"] == {
        "announced": {"entry": ["3001"], "watch": [], "excluded": []},
        "pending": {"entry": ["3002"], "watch": [], "excluded": []},
    }
    assert payload["noPeriod"] == {
        "announced": {"entry": [], "watch": ["4001"], "excluded": []},
        "pending": {"entry": [], "watch": [], "excluded": []},
    }


def test_api_client_replays_only_safe_reads_after_pages_proxy_5xx():
    script = r"""
const { createApiClient } = require("./frontend/api_client.js");
const calls = [];
global.fetch = async (url, options) => {
  calls.push({ url, credentials: options.credentials, csrf: options.headers.get("X-Stock-Scanner-CSRF") });
  if (String(url).startsWith("/api/")) {
    return { ok: false, status: 503, text: async () => "proxy down", headers: { get: () => "text/plain" } };
  }
  return { ok: true, status: 200, text: async () => '{"ok":true}', headers: { get: () => "application/json" } };
};
const client = createApiClient({
  csrfHeaderName: "X-Stock-Scanner-CSRF",
  csrfHeaderValue: "1",
  fallbackOrigin: "https://worker.example",
});
(async () => {
  const first = await client.request("/api/scan/market", { method: "POST", body: "{}" });
  const second = await client.request("/api/settings");
  console.log(JSON.stringify({ firstStatus: first.status, secondStatus: second.status, calls, activeOrigin: client.activeOrigin() }));
})();
"""
    completed = subprocess.run(
        ["node", "-e", script],
        cwd=ROOT,
        check=True,
        text=True,
        encoding="utf-8",
        capture_output=True,
    )
    payload = json.loads(completed.stdout)

    assert payload["firstStatus"] == 503
    assert payload["secondStatus"] == 200
    assert [item["url"] for item in payload["calls"]] == [
        "/api/scan/market",
        "/api/settings",
        "https://worker.example/api/settings",
    ]
    assert payload["calls"][2]["credentials"] == "include"
    assert payload["calls"][2]["csrf"] == "1"
    assert payload["activeOrigin"] == "https://worker.example"


def test_api_client_allows_direct_fallback_for_v2_market_get_routes_only():
    payload = _run_node_json(
        r"""
const { allowsDirectFallback } = require("./frontend/api_client.js");
console.log(JSON.stringify({
  indexGet: allowsDirectFallback("/api/scan/market/index", "GET"),
  resultsGet: allowsDirectFallback("/api/scan/market/results?disclosure=announced&category=watch&cursor=0&limit=100", "GET"),
  resultsHead: allowsDirectFallback("/api/scan/market/results?disclosure=announced&category=watch&cursor=0&limit=100", "HEAD"),
  refreshStatusGet: allowsDirectFallback(`/api/scan/market/refresh/${"a".repeat(32)}`, "GET"),
  refreshStatusUpper: allowsDirectFallback(`/api/scan/market/refresh/${"A".repeat(32)}`, "GET"),
  indexPost: allowsDirectFallback("/api/scan/market/index", "POST"),
  suffix: allowsDirectFallback("/api/scan/market/results/extra", "GET"),
}));
"""
    )

    assert payload == {
        "indexGet": True,
        "resultsGet": True,
        "resultsHead": True,
        "refreshStatusGet": True,
        "refreshStatusUpper": False,
        "indexPost": False,
        "suffix": False,
    }


def test_api_client_safe_reads_use_rounds_and_bounded_status_retries():
    payload = _run_node_json(
        r"""
const { createApiClient } = require("./frontend/api_client.js");

function response(status) {
  return {
    ok: status >= 200 && status < 300,
    status,
    text: async () => "",
    headers: { get: () => "application/json" },
  };
}

async function run({ apiMode, outcomes, method, includeMethod = true, retryCount }) {
  const calls = [];
  let attempt = 0;
  global.fetch = async (url, options) => {
    calls.push({ url: String(url), method: options.method || "GET" });
    const outcome = outcomes[Math.min(attempt, outcomes.length - 1)];
    attempt += 1;
    if (outcome === "network") throw new TypeError("transport unavailable");
    return response(outcome);
  };
  const options = {
    apiMode,
    fallbackOrigin: "https://worker.example",
    retryDelaysMs: [0, 0],
  };
  if (retryCount !== undefined) options.getRetryCount = retryCount;
  const client = createApiClient(options);
  const requestOptions = includeMethod ? { method } : {};
  try {
    const finalResponse = await client.request("/api/scan/market", requestOptions);
    return { status: finalResponse.status, calls };
  } catch (error) {
    return { error: error.name, calls };
  }
}

(async () => {
  const results = {
    direct: await run({ apiMode: "direct", outcomes: [503, 502, 200], method: "GET", retryCount: 2 }),
    sameDefault: await run({ apiMode: "same-origin", outcomes: [503, 502, 200], includeMethod: false }),
    fallbackRounds: await run({ apiMode: "fallback", outcomes: [503, 503, 200], method: "GET" }),
    fallback500ThenOk: await run({ apiMode: "fallback", outcomes: [500, 200], method: "GET" }),
    fallback503Then500: await run({ apiMode: "fallback", outcomes: [503, 500], method: "GET" }),
    directExhausted: await run({ apiMode: "direct", outcomes: [504], method: "GET" }),
    fallbackExhausted: await run({ apiMode: "fallback", outcomes: [502], method: "GET" }),
    client400: await run({ apiMode: "fallback", outcomes: [400, 200], method: "GET" }),
    client429: await run({ apiMode: "fallback", outcomes: [429, 200], method: "GET" }),
    direct500: await run({ apiMode: "direct", outcomes: [500, 200], method: "GET" }),
    head: await run({ apiMode: "direct", outcomes: [504, 200], method: "HEAD" }),
    explicitGet: await run({ apiMode: "same-origin", outcomes: [503, 200], method: "GET" }),
  };
  console.log(JSON.stringify(results));
})();
"""
    )

    worker_url = "https://worker.example/api/scan/market"
    assert payload["direct"] == {
        "status": 200,
        "calls": [{"url": worker_url, "method": "GET"}] * 3,
    }
    assert payload["sameDefault"] == {
        "status": 200,
        "calls": [{"url": "/api/scan/market", "method": "GET"}] * 3,
    }
    assert payload["fallbackRounds"] == {
        "status": 200,
        "calls": [
            {"url": "/api/scan/market", "method": "GET"},
            {"url": worker_url, "method": "GET"},
            {"url": "/api/scan/market", "method": "GET"},
        ],
    }
    assert payload["fallback500ThenOk"]["status"] == 200
    assert [call["url"] for call in payload["fallback500ThenOk"]["calls"]] == [
        "/api/scan/market",
        worker_url,
    ]
    assert payload["fallback503Then500"]["status"] == 500
    assert [call["url"] for call in payload["fallback503Then500"]["calls"]] == [
        "/api/scan/market",
        worker_url,
    ]
    assert payload["directExhausted"]["status"] == 504
    assert len(payload["directExhausted"]["calls"]) == 3
    assert payload["fallbackExhausted"]["status"] == 502
    assert len(payload["fallbackExhausted"]["calls"]) == 6
    assert len(payload["client400"]["calls"]) == 1
    assert len(payload["client429"]["calls"]) == 1
    assert len(payload["direct500"]["calls"]) == 1
    assert payload["head"] == {
        "status": 200,
        "calls": [{"url": worker_url, "method": "HEAD"}] * 2,
    }
    assert payload["explicitGet"]["status"] == 200
    assert len(payload["explicitGet"]["calls"]) == 2


def test_api_client_clears_worker_preference_after_http_exhaustion_but_throws_network_errors():
    payload = _run_node_json(
        r"""
const { createApiClient } = require("./frontend/api_client.js");
const response = (status) => ({
  ok: status >= 200 && status < 300,
  status,
  text: async () => "",
  headers: { get: () => "application/json" },
});

(async () => {
  const calls = [];
  const statuses = [503, 200, 503, 503, 503, 503, 503, 503, 200];
  global.fetch = async (url, options) => {
    calls.push({ url: String(url), method: options.method });
    return response(statuses.shift());
  };
  const client = createApiClient({
    apiMode: "fallback",
    fallbackOrigin: "https://worker.example",
    retryDelaysMs: [0, 0],
  });
  await client.request("/api/scan/market");
  const primedOrigin = client.activeOrigin();
  const exhausted = await client.request("/api/scan/market");
  const originAfterExhaustion = client.activeOrigin();
  const recoveryStart = calls.length;
  await client.request("/api/scan/market");

  let networkCalls = 0;
  global.fetch = async () => {
    networkCalls += 1;
    throw new TypeError("network unavailable");
  };
  const networkClient = createApiClient({
    apiMode: "fallback",
    fallbackOrigin: "https://worker.example",
    retryDelaysMs: [0, 0],
  });
  let networkError = "";
  try {
    await networkClient.request("/api/scan/market");
  } catch (error) {
    networkError = error.name;
  }

  console.log(JSON.stringify({
    calls,
    primedOrigin,
    exhaustedStatus: exhausted.status,
    originAfterExhaustion,
    recoveryCalls: calls.slice(recoveryStart),
    networkCalls,
    networkError,
  }));
})();
"""
    )

    assert payload["primedOrigin"] == "https://worker.example"
    assert payload["exhaustedStatus"] == 503
    assert payload["originAfterExhaustion"] == ""
    assert payload["recoveryCalls"] == [{"url": "/api/scan/market", "method": "GET"}]
    assert payload["networkCalls"] == 6
    assert payload["networkError"] == "TypeError"


def test_api_client_mutations_fetch_one_primary_candidate_and_consume_bodies_once():
    payload = _run_node_json(
        r"""
const { createApiClient } = require("./frontend/api_client.js");

function response(status = 503) {
  return { ok: false, status, text: async () => "", headers: { get: () => "application/json" } };
}

async function runMutation(method, apiMode = "fallback", body = `${method}-body`, throwNetwork = false) {
  const calls = [];
  global.fetch = async (url, options) => {
    calls.push({ url: String(url), method: options.method, body: options.body });
    if (throwNetwork) throw new TypeError("unknown commit outcome");
    return response();
  };
  const client = createApiClient({
    apiMode,
    fallbackOrigin: "https://worker.example",
    getRetryCount: 2,
    retryDelaysMs: [0, 0],
  });
  try {
    const result = await client.request("/api/scan/market", { method, body });
    return { status: result.status, calls };
  } catch (error) {
    return { error: error.name, calls };
  }
}

(async () => {
  const methods = {};
  for (const method of ["POST", "PUT", "PATCH", "DELETE"]) {
    methods[method] = await runMutation(method);
  }
  const direct = await runMutation("POST", "direct", '{"refreshMode":"force"}');
  const network = await runMutation("POST", "fallback", "payload", true);

  const stream = new ReadableStream({
    start(controller) {
      controller.enqueue(new TextEncoder().encode("one-shot"));
      controller.close();
    },
  });
  let streamReads = 0;
  const streamCalls = [];
  global.fetch = async (url, options) => {
    streamCalls.push({ url: String(url), method: options.method, sameBody: options.body === stream });
    const reader = options.body.getReader();
    await reader.read();
    streamReads += 1;
    return response();
  };
  const streamClient = createApiClient({ apiMode: "fallback", fallbackOrigin: "https://worker.example" });
  let streamResponse = null;
  let streamError = "";
  try {
    streamResponse = await streamClient.request("/api/scan/market", {
      method: "POST",
      body: stream,
      duplex: "half",
    });
  } catch (error) {
    streamError = error.name;
  }

  console.log(JSON.stringify({ methods, direct, network, streamStatus: streamResponse?.status || null, streamError, streamCalls, streamReads }));
})();
"""
    )

    for method in ("POST", "PUT", "PATCH", "DELETE"):
        result = payload["methods"][method]
        assert result["status"] == 503
        assert result["calls"] == [
            {"url": "/api/scan/market", "method": method, "body": f"{method}-body"}
        ]
    assert payload["direct"]["calls"] == [
        {
            "url": "https://worker.example/api/scan/market",
            "method": "POST",
            "body": '{"refreshMode":"force"}',
        }
    ]
    assert payload["network"]["error"] == "TypeError"
    assert len(payload["network"]["calls"]) == 1
    assert payload["streamStatus"] == 503
    assert payload["streamError"] == ""
    assert payload["streamCalls"] == [
        {"url": "/api/scan/market", "method": "POST", "sameBody": True}
    ]
    assert payload["streamReads"] == 1


def test_api_client_transport_retry_and_abort_are_terminal_and_abortable():
    payload = _run_node_json(
        r"""
const { createApiClient } = require("./frontend/api_client.js");
const ok = { ok: true, status: 200, text: async () => "", headers: { get: () => "application/json" } };
const unavailable = { ok: false, status: 503, text: async () => "", headers: { get: () => "application/json" } };

(async () => {
  let transportCalls = 0;
  global.fetch = async () => {
    transportCalls += 1;
    if (transportCalls === 1) throw new TypeError("temporary network failure");
    return ok;
  };
  const transportClient = createApiClient({
    apiMode: "direct",
    fallbackOrigin: "https://worker.example",
    retryDelaysMs: [0, 0],
  });
  let transportResponse = null;
  let transportError = "";
  try {
    transportResponse = await transportClient.request("/api/scan/market");
  } catch (error) {
    transportError = error.name;
  }

  let abortCalls = 0;
  global.fetch = async () => {
    abortCalls += 1;
    throw new DOMException("caller cancelled", "AbortError");
  };
  const abortClient = createApiClient({
    apiMode: "direct",
    fallbackOrigin: "https://worker.example",
    retryDelaysMs: [0, 0],
  });
  let abortName = "";
  try {
    await abortClient.request("/api/scan/market");
  } catch (error) {
    abortName = error.name;
  }

  let backoffCalls = 0;
  const controller = new AbortController();
  global.fetch = async () => {
    backoffCalls += 1;
    return unavailable;
  };
  const backoffClient = createApiClient({
    apiMode: "direct",
    fallbackOrigin: "https://worker.example",
    retryDelaysMs: [100, 0],
  });
  const pending = backoffClient.request("/api/scan/market", { signal: controller.signal });
  setTimeout(() => controller.abort(), 5);
  let backoffAbortName = "";
  try {
    await pending;
  } catch (error) {
    backoffAbortName = error.name;
  }

  console.log(JSON.stringify({
    transportStatus: transportResponse?.status || null,
    transportError,
    transportCalls,
    abortName,
    abortCalls,
    backoffAbortName,
    backoffCalls,
  }));
})();
"""
    )

    assert payload == {
        "transportStatus": 200,
        "transportError": "",
        "transportCalls": 2,
        "abortName": "AbortError",
        "abortCalls": 1,
        "backoffAbortName": "AbortError",
        "backoffCalls": 1,
    }


def test_api_error_message_exposes_only_strict_request_ids_for_server_errors():
    payload = _run_node_json(
        r"""
const { apiErrorMessage } = require("./frontend/app.js");

function message(body, contentType = "application/json") {
  return apiErrorMessage(
    { status: 503, headers: { get: () => contentType } },
    typeof body === "string" ? body : JSON.stringify(body),
  );
}

const validIds = ["a", "request-123._:ok", "x".repeat(80)];
const invalidIds = [
  "",
  " ",
  "x".repeat(81),
  "line\nbreak",
  "line\rbreak",
  "<b>html</b>",
  "追蹤編號",
  123,
  { nested: true },
];
const valid = validIds.map((requestId) => ({ requestId, text: message({ detail: "secret detail", requestId }) }));
const invalid = invalidIds.map((requestId) => message({ detail: "secret detail", requestId }));
console.log(JSON.stringify({
  valid,
  invalid,
  malformed: message('{"detail":', "application/json"),
  nonJson: message("secret upstream body request-123", "text/plain"),
}));
"""
    )

    generic = "伺服器暫時無法處理請求，請稍後再試。"
    for item in payload["valid"]:
        assert item["text"].startswith(generic)
        assert f"追蹤編號：{item['requestId']}" in item["text"]
        assert "secret detail" not in item["text"]
    assert payload["invalid"] == [generic] * 9
    assert payload["malformed"] == generic
    assert payload["nonJson"] == generic


def test_market_scan_acceptance_helper_owns_its_warning_lifecycle():
    source = (ROOT / "frontend" / "app.js").read_text(encoding="utf-8")
    market_render_source = (ROOT / "frontend" / "market_render.js").read_text(encoding="utf-8")
    assert "state.marketScan = scan" not in source
    assert market_render_source.count("state.marketScan = scan") == 1

    payload = _run_node_json(
        r"""
const app = require("./frontend/app.js");
const available = typeof app.acceptMarketScan === "function";
if (!available) {
  console.log(JSON.stringify({ available }));
} else {
  const base = {
    generatedAt: "2026-07-12T00:00:00Z",
    entry: [],
    watch: [],
    excluded: [],
  };
  app.state.marketScan = base;
  app.state.marketScanWarning = null;
  app.acceptMarketScan({
    ...base,
    cacheStatus: { refreshStatus: "unavailable", requestId: "task2-id" },
  }, { resetUi: false });
  const unavailableWarning = app.state.marketScanWarning;
  app.acceptMarketScan({ ...base, cacheStatus: { refreshStatus: "fresh" } }, { resetUi: false });
  console.log(JSON.stringify({
    available,
    unavailableWarning,
    cleared: app.state.marketScanWarning === null,
  }));
}
"""
    )

    assert payload["available"] is True
    assert "暫時" in payload["unavailableWarning"]
    assert "task2-id" in payload["unavailableWarning"]
    assert payload["cleared"] is True


def test_api_client_does_not_direct_fallback_for_account_mutations():
    script = r"""
const { createApiClient } = require("./frontend/api_client.js");
const calls = [];
global.fetch = async (url, options) => {
  calls.push({ url, method: options.method });
  return { ok: false, status: 503, text: async () => "proxy down", headers: { get: () => "text/plain" } };
};
const client = createApiClient({
  csrfHeaderName: "X-Stock-Scanner-CSRF",
  csrfHeaderValue: "1",
  fallbackOrigin: "https://worker.example",
});
(async () => {
  const response = await client.request("/api/auth/register", { method: "POST", body: "{}" });
  console.log(JSON.stringify({ status: response.status, calls, activeOrigin: client.activeOrigin() }));
})();
"""
    completed = subprocess.run(
        ["node", "-e", script],
        cwd=ROOT,
        check=True,
        text=True,
        encoding="utf-8",
        capture_output=True,
    )
    payload = json.loads(completed.stdout)

    assert payload["status"] == 503
    assert payload["calls"] == [{"url": "/api/auth/register", "method": "POST"}]
    assert payload["activeOrigin"] == ""


def test_api_client_direct_mode_uses_worker_for_account_mutations():
    script = r"""
const { createApiClient } = require("./frontend/api_client.js");
const calls = [];
global.fetch = async (url, options) => {
  calls.push({ url, method: options.method, credentials: options.credentials, csrf: options.headers.get("X-Stock-Scanner-CSRF") });
  return { ok: true, status: 200, text: async () => '{"authenticated":true}', headers: { get: () => "application/json" } };
};
const client = createApiClient({
  apiMode: "direct",
  csrfHeaderName: "X-Stock-Scanner-CSRF",
  csrfHeaderValue: "1",
  fallbackOrigin: "https://worker.example",
});
(async () => {
  const response = await client.request("/api/auth/register", { method: "POST", body: "{}" });
  console.log(JSON.stringify({ status: response.status, calls, activeOrigin: client.activeOrigin(), mode: client.apiMode() }));
})();
"""
    completed = subprocess.run(
        ["node", "-e", script],
        cwd=ROOT,
        check=True,
        text=True,
        encoding="utf-8",
        capture_output=True,
    )
    payload = json.loads(completed.stdout)

    assert payload["status"] == 200
    assert payload["calls"] == [
        {
            "url": "https://worker.example/api/auth/register",
            "method": "POST",
            "credentials": "include",
            "csrf": "1",
        }
    ]
    assert payload["activeOrigin"] == "https://worker.example"
    assert payload["mode"] == "direct"


def test_api_client_runtime_mode_overrides_meta_mode():
    script = r"""
const { createApiClient } = require("./frontend/api_client.js");
global.location = { hostname: "127.0.0.1" };
global.document = {
  querySelector(selector) {
    if (selector === 'meta[name="stock-scanner-api-mode"]') {
      return { getAttribute: () => "direct" };
    }
    return null;
  },
};
global.StockScannerConfig = { apiMode: "fallback" };
const calls = [];
global.fetch = async (url, options) => {
  calls.push({ url, method: options.method, credentials: options.credentials });
  return { ok: true, status: 200, text: async () => '{"authenticated":true}', headers: { get: () => "application/json" } };
};
const client = createApiClient({
  csrfHeaderName: "X-Stock-Scanner-CSRF",
  csrfHeaderValue: "1",
  fallbackOrigin: "https://worker.example",
});
(async () => {
  await client.request("/api/auth/login", { method: "POST", body: "{}" });
  console.log(JSON.stringify({ mode: client.apiMode(), calls, activeOrigin: client.activeOrigin() }));
})().catch((error) => {
  console.error(error);
  process.exit(1);
});
"""
    completed = subprocess.run(
        ["node", "-e", script],
        cwd=ROOT,
        check=True,
        text=True,
        encoding="utf-8",
        capture_output=True,
    )
    payload = json.loads(completed.stdout)

    assert payload["mode"] == "fallback"


def test_api_client_allows_runtime_config_direct_fallback():
    source = (ROOT / "frontend" / "api_client.js").read_text(encoding="utf-8")

    assert 'GET \\/api\\/runtime-config' in source


def test_pages_dev_api_client_defaults_to_same_origin_for_account_routes():
    script = r"""
const { createApiClient } = require("./frontend/api_client.js");
global.location = { hostname: "stock-scanner-beta.pages.dev" };
const calls = [];
global.fetch = async (url, options) => {
  calls.push({ url, method: options.method, credentials: options.credentials });
  return { ok: true, status: 200, text: async () => '{"authenticated":true}', headers: { get: () => "application/json" } };
};
const client = createApiClient({
  csrfHeaderName: "X-Stock-Scanner-CSRF",
  csrfHeaderValue: "1",
  fallbackOrigin: "https://worker.example",
});
(async () => {
  await client.request("/api/auth/login", { method: "POST", body: "{}" });
  console.log(JSON.stringify({ mode: client.apiMode(), calls, activeOrigin: client.activeOrigin() }));
})().catch((error) => {
  console.error(error);
  process.exit(1);
});
"""
    completed = subprocess.run(
        ["node", "-e", script],
        cwd=ROOT,
        check=True,
        text=True,
        encoding="utf-8",
        capture_output=True,
    )
    payload = json.loads(completed.stdout)

    assert payload["mode"] == "same-origin"
    assert payload["calls"] == [{"url": "/api/auth/login", "method": "POST", "credentials": "same-origin"}]
    assert payload["activeOrigin"] == ""


def test_parse_stock_input_cases_do_not_return_undefined():
    script = r"""
const { DEFAULT_COMPANIES, STRATEGY_STATUS_DETAILS, state, findStrategyStatusDetail, parseStockInput, normalizeCompanies, renderAnalysisCard, renderMarketResultRow, renderMarketPagination, renderStrategyRuleCards, adminUsersErrorMessage, loadHoldingsFromStorage, normalizeHoldingRecords, apiErrorMessage, normalizeAuthUsername, normalizeAuthUser, authValidationMessage, isSuperUserIdentity, isSuperUser, groupMarketScanResults, sortMarketResultsForDisplay, e4PerValue, hasInsufficientData, hasFinancialReportForContext, hasPublishedScanData, isPartialPublishedResult, formatEvidenceValue, renderRuleEvidence, renderRule, sortRulesForDisplay, settingsPermissionMessage, holdingExitCodes, holdingSignal, renderHoldingSignal, holdingExitAlerts, renderHoldingExitAlertBanner } = require("./frontend/app.js");
const cases = DEFAULT_COMPANIES.flatMap((company) => [
  [company.stockCode, company.stockCode, company.name],
  [company.name, company.stockCode, company.name],
  [`${company.stockCode} ${company.name}`, company.stockCode, company.name],
  [` ${company.stockCode}  ${company.name} `, company.stockCode, company.name],
  [`${company.name} 1000 300`, company.stockCode, company.name],
  [`${company.stockCode} ${company.name} 1000 300`, company.stockCode, company.name],
]);
const output = cases.map(([input, stockCode, name]) => {
  const parsed = parseStockInput(input, DEFAULT_COMPANIES);
  return { input, ok: parsed.ok, stockCode: parsed.stockCode, name: parsed.name, expectedCode: stockCode, expectedName: name };
});
const unknownInputs = ["9999 不存在", "不存在股票", "台灣神奇公司", "0000"];
const unknown = unknownInputs.map((input) => ({ input, ...parseStockInput(input, DEFAULT_COMPANIES) }));
const incompleteCompanies = normalizeCompanies([
  { stockCode: "9999" },
  { name: "缺代碼公司" },
  { stockCode: "1234", name: "測試公司", market: undefined, industryName: undefined },
  null,
]);
const incompleteParsed = [
  parseStockInput("9999", incompleteCompanies),
  parseStockInput("測試公司", incompleteCompanies),
];
const incompleteHtml = renderAnalysisCard({
  status: undefined,
  reasons: [{ passed: false }, null],
});
const partialPublishedResult = {
  stockCode: "1101",
  companyName: "台泥",
  status: "INSUFFICIENT_DATA",
  summary: "資料不足，不能硬給進場或排除結論。",
  reasons: [
    { code: "E1", title: "近 5 年沒有虧損", passed: false, severity: "INSUFFICIENT_DATA", message: "近 5 年年度淨利尚未自動補齊。" },
    { code: "E3", title: "今年累計營收年增率 >= 50%", passed: false, severity: "WATCH", message: "目前採用 cumulative_ytd，年增率為 -4.6%。" },
    { code: "OFFICIAL_Q", title: "最新季官方財報資料", passed: true, severity: "INFO", message: "2026Q1 EPS 0.1。" }
  ],
};
const partialHtml = renderAnalysisCard(partialPublishedResult, { disclosureGroup: "announced" });
const pendingHtml = renderAnalysisCard(partialPublishedResult, { disclosureGroup: "pending" });
const holdingPartialHtml = renderAnalysisCard(partialPublishedResult, { disclosureGroup: "holding" });
const compactMarketRow = renderMarketResultRow(partialPublishedResult, { disclosureGroup: "announced", columnKey: "watch" });
const marketPagination = renderMarketPagination("announced", "watch", 26);
state.holdings = [{ stockCode: "2357", name: "華碩", shares: 0, averageCost: null }];
const trackedAddHtml = renderAnalysisCard({ stockCode: "2357", companyName: "華碩", status: "ENTRY", summary: "ok", reasons: [] }, { allowAddAction: true });
state.holdings = [];
const untrackedAddHtml = renderAnalysisCard({ stockCode: "2357", companyName: "華碩", status: "ENTRY", summary: "ok", reasons: [] }, { allowAddAction: true });
const fakeStorage = {
  value: JSON.stringify([
    { stockCode: "2330", shares: "1000", averageCost: "600.5" },
    { name: "華碩", shares: "200", averageCost: "" },
    { stockCode: "9999", shares: "bad" }
  ]),
  getItem() { return this.value; },
  setItem(key, value) { this.value = value; },
};
const holdings = loadHoldingsFromStorage(fakeStorage, DEFAULT_COMPANIES);
fakeStorage.value = "{bad json";
const repaired = loadHoldingsFromStorage(fakeStorage, DEFAULT_COMPANIES);
const normalizedHoldings = normalizeHoldingRecords([{ stockCode: "2357", name: "x", shares: 1 }, { stockCode: "2357", shares: 2 }], DEFAULT_COMPANIES);
const marketGroups = groupMarketScanResults({
  entry: [{ stockCode: "2357", status: "ENTRY", reasons: [{ severity: "INFO" }] }],
  watch: [
    { stockCode: "1101", status: "INSUFFICIENT_DATA", reasons: [{ code: "E1", severity: "INSUFFICIENT_DATA" }, { code: "E3", severity: "WATCH" }] },
    { stockCode: "9999", status: "INSUFFICIENT_DATA", reasons: [{ code: "E3", severity: "INSUFFICIENT_DATA" }] }
  ],
  excluded: [{ stockCode: "2881", status: "EXCLUDED", reasons: [{ severity: "EXCLUDED" }] }],
});
const sortedEntryByPer = sortMarketResultsForDisplay("entry", [
  { stockCode: "3000", reasons: [{ code: "E4", message: "PER 為 18.5。" }] },
  { stockCode: "1000", reasons: [{ code: "E4", message: "PER 為 9.8。" }] },
  { stockCode: "2000", reasons: [{ code: "E4", message: "PER 為 15.2。" }] },
  { stockCode: "9999", reasons: [{ code: "E4", message: "PER 缺資料。" }] },
]).map((item) => item.stockCode);
const extractedPer = e4PerValue({ reasons: [{ code: "E4", message: "PER 為 12.34。" }] });
const q1Context = { activeFinancialReport: { period: "2026Q1" } };
const filingAwareGroups = groupMarketScanResults({
  filingContext: q1Context,
  entry: [],
  watch: [
    { stockCode: "1101", status: "INSUFFICIENT_DATA", reasons: [{ code: "E3", severity: "WATCH" }, { code: "OFFICIAL_Q", severity: "INFO", message: "2026Q1 EPS 0.1。" }] },
    { stockCode: "1102", status: "INSUFFICIENT_DATA", reasons: [{ code: "E3", severity: "WATCH" }, { code: "OFFICIAL_Q", severity: "INFO", message: "2025Q4 EPS 0.1。" }] },
    { stockCode: "1103", status: "INSUFFICIENT_DATA", reasons: [{ code: "E3", severity: "WATCH" }] }
  ],
  excluded: [],
});
const evidenceRule = {
  code: "E1",
  title: "近 5 年沒有虧損",
  passed: false,
  severity: "WATCH",
  message: "近 5 年年度淨利有 2 年虧損；詳見年度表格。",
  evidence: [
    { label: "2025", value: 11962952, unit: "thousand_twd", metric: "annual_net_income" },
    { label: "2024", value: 8969775, unit: "thousand_twd", metric: "annual_net_income" },
    { label: "2023", value: -1699593, unit: "thousand_twd", metric: "annual_net_income" }
  ],
};
const evidenceValue = formatEvidenceValue(evidenceRule.evidence[0]);
const evidenceHtml = renderRuleEvidence(evidenceRule);
const evidenceRuleHtml = renderRule(evidenceRule);
const orderedCodes = sortRulesForDisplay([
  { code: "HOLDING" },
  { code: "X2" },
  { code: "T3" },
  { code: "E1" },
  { code: "OFFICIAL_Q" },
  { code: "A1" },
  { code: "OFFICIAL_VALUATION" },
]).map((rule) => rule.code);
const orderedHtml = renderAnalysisCard({
  stockCode: "3135",
  companyName: "凌航",
  status: "HOLD",
  summary: "order test",
  reasons: [
    { code: "HOLDING", title: "目前持股", passed: true, severity: "INFO", message: "持股狀態" },
    { code: "X1", title: "當年度的累計營收年增率 >= 最新當月份營收年增率的 50%", passed: true, severity: "INFO", message: "X1" },
    { code: "T3", title: "毛利率追蹤", passed: true, severity: "INFO", message: "T3" },
    { code: "E1", title: "近 5 年沒有虧損", passed: true, severity: "INFO", message: "E1" },
    { code: "OFFICIAL_Q", title: "最新季官方財報資料", passed: true, severity: "INFO", message: "OFFICIAL_Q" },
    { code: "OFFICIAL_VALUATION", title: "官方估值資料", passed: true, severity: "INFO", message: "OFFICIAL_VALUATION" },
    { code: "A1", title: "原進場條件仍符合", passed: true, severity: "INFO", message: "A1" },
  ],
});
const exitHoldingResult = {
  stockCode: "3008",
  companyName: "大立光",
  status: "EXIT",
  summary: "已觸發高優先出場條件，建議出清或至少大幅降低部位。",
  reasons: [
    { code: "X1", title: "當年度的累計營收年增率 >= 最新當月份營收年增率的 50%", passed: false, severity: "WARNING", message: "X1" },
    { code: "X4", title: "季度 EPS 不可減少超過 10%", passed: false, severity: "EXIT", message: "X4" },
    { code: "HOLDING", title: "目前持股", passed: true, severity: "INFO", message: "目前 1000 股" },
  ],
};
const exitCodes = holdingExitCodes(exitHoldingResult);
const exitSignal = holdingSignal(exitHoldingResult);
const exitSignalHtml = renderHoldingSignal(exitHoldingResult);
state.holdings = [{ stockCode: "3008", name: "Largan", shares: 100, averageCost: 2000 }];
const exitAlerts = holdingExitAlerts({ results: [exitHoldingResult], missing: [] });
const exitAlertBanner = renderHoldingExitAlertBanner(exitAlerts);
state.holdings = [];
const pcedisonFromUsername = normalizeAuthUser({ username: " PCEDISON@GMAIL.COM ", displayName: "", isSuperUser: true });
const pcedisonMissingFlag = normalizeAuthUser({ username: "pcedison@gmail.com", isSuperUser: true });
const normalWithFlag = normalizeAuthUser({ username: "normal@example.com", isSuperUser: false });
state.auth = { authenticated: true, user: pcedisonFromUsername };
const superState = isSuperUser();
state.auth = { checked: true, authenticated: true, user: normalWithFlag };
const normalState = isSuperUser();
const normalSettingsPermission = settingsPermissionMessage();
state.auth = { checked: true, authenticated: false, user: null };
const anonymousSettingsPermission = settingsPermissionMessage();
const header = (contentType) => ({ get: () => contentType });
const apiErrors = {
  html500: apiErrorMessage({ status: 500, headers: header("text/html") }, '<!DOCTYPE html><html><head><title>Worker threw exception</title></head><body>raw</body></html>'),
  workerJson500: apiErrorMessage({ status: 500, headers: header("application/json") }, JSON.stringify({ detail: "Cloudflare Worker API error: internal detail" })),
  login401: apiErrorMessage({ status: 401, headers: header("application/json") }, JSON.stringify({ detail: "帳號或密碼錯誤" })),
};
const strategyCardsHtml = renderStrategyRuleCards();
const authValidation = {
  valid: authValidationMessage("qa@example.com", "test-password-123"),
  badEmail: authValidationMessage("qa", "test-password-123"),
  shortPassword: authValidationMessage("qa@example.com", "short"),
};
const adminErrors = {
  notFound: adminUsersErrorMessage("Not found"),
  normal: adminUsersErrorMessage("請先登入"),
};
console.log(JSON.stringify({ output, unknown, incompleteCompanies, incompleteParsed, incompleteHtml, partialHtml, pendingHtml, holdingPartialHtml, compactMarketRow, marketPagination, trackedAddHtml, untrackedAddHtml, holdings, repaired, repairedStorage: fakeStorage.value, normalizedHoldings, apiErrors, authValidation, adminErrors, auth: { normalizedSuperUsername: normalizeAuthUsername(" PCEDISON@GMAIL.COM "), pcedisonFromUsername, pcedisonMissingFlag, normalWithFlag, pcedisonIdentity: isSuperUserIdentity(pcedisonMissingFlag), normalIdentity: isSuperUserIdentity(normalWithFlag), superState, normalState, normalSettingsPermission, anonymousSettingsPermission }, marketGroups, sortedEntryByPer, extractedPer, filingAwareGroups, evidenceValue, evidenceHtml, evidenceRuleHtml, orderedCodes, orderedHtml, exitCodes, exitSignal, exitSignalHtml, exitAlerts, exitAlertBanner, strategyCardsHtml, strategyDetailLabels: STRATEGY_STATUS_DETAILS.map((item) => item.label), entryDetail: findStrategyStatusDetail("entry"), addWatchDetail: findStrategyStatusDetail("addWatch"), tSeriesDetail: findStrategyStatusDetail("grossMargin"), hasInsufficient: hasInsufficientData(marketGroups.announced.watch[0]), hasFinancialForContext: hasFinancialReportForContext(filingAwareGroups.announced.watch[0], q1Context), hasPublished: hasPublishedScanData(marketGroups.announced.watch[0]), isPartialPublished: isPartialPublishedResult(partialPublishedResult) }));
"""
    completed = subprocess.run(
        ["node", "-e", script],
        cwd=ROOT,
        check=True,
        text=True,
        encoding="utf-8",
        capture_output=True,
    )
    payload = json.loads(completed.stdout)

    for item in payload["output"]:
        assert item["ok"] is True
        assert item["stockCode"] == item["expectedCode"]
        assert item["name"] == item["expectedName"]
        assert "undefined" not in json.dumps(item, ensure_ascii=False)

    for item in payload["unknown"]:
        assert item["ok"] is False
        assert item["error"] == "找不到資料"
        generated = {key: value for key, value in item.items() if key != "input"}
        assert "undefined" not in json.dumps(generated, ensure_ascii=False)

    assert payload["incompleteParsed"][0]["ok"] is True
    assert payload["incompleteParsed"][0]["name"] == "未知公司"
    assert payload["incompleteParsed"][1]["ok"] is True
    assert payload["incompleteParsed"][1]["stockCode"] == "1234"
    assert "undefined" not in json.dumps(payload["incompleteCompanies"], ensure_ascii=False)
    assert "undefined" not in json.dumps(payload["incompleteParsed"], ensure_ascii=False)
    assert "undefined" not in payload["incompleteHtml"]
    assert "可初篩" in payload["partialHtml"]
    assert "待補" in payload["partialHtml"]
    assert "資料不足，不能硬給進場或排除結論。" not in payload["partialHtml"]
    assert "待補資料" in payload["pendingHtml"]
    assert "可追蹤" in payload["holdingPartialHtml"]
    assert "完整續抱或出場結論仍待補" in payload["holdingPartialHtml"]
    assert "1101 台泥" in payload["compactMarketRow"]
    assert "data-market-result-toggle" in payload["compactMarketRow"]
    assert "E1 待補" not in payload["compactMarketRow"]
    assert "第 1 / 5 頁" in payload["marketPagination"]
    assert "下一頁" in payload["marketPagination"]
    assert "已在持股" in payload["trackedAddHtml"]
    assert "data-add-from-result" not in payload["trackedAddHtml"]
    assert "data-add-from-result" in payload["untrackedAddHtml"]
    assert [item["stockCode"] for item in payload["holdings"]] == ["2330", "2357"]
    assert payload["holdings"][0]["shares"] == 1000
    assert payload["holdings"][1]["averageCost"] is None
    assert payload["repaired"] == []
    assert payload["repairedStorage"] == "[]"
    assert payload["normalizedHoldings"][0]["shares"] == 2
    assert payload["apiErrors"]["html500"] == "伺服器暫時無法處理請求，請稍後再試。"
    assert payload["apiErrors"]["workerJson500"] == "伺服器暫時無法處理請求，請稍後再試。"
    assert payload["apiErrors"]["login401"] == "帳號或密碼錯誤"
    assert payload["authValidation"]["valid"] == ""
    assert payload["authValidation"]["badEmail"] == "請輸入有效電子信箱。"
    assert payload["authValidation"]["shortPassword"] == "密碼至少需要 8 個字元。"
    assert "管理 API 尚未部署" in payload["adminErrors"]["notFound"]
    assert payload["adminErrors"]["normal"] == "使用者清單讀取失敗：請先登入"
    assert payload["auth"]["normalizedSuperUsername"] == "pcedison@gmail.com"
    assert payload["auth"]["pcedisonFromUsername"]["isSuperUser"] is True  # API flag trusted
    assert payload["auth"]["pcedisonMissingFlag"]["isSuperUser"] is True  # API flag trusted
    assert payload["auth"]["normalWithFlag"]["isSuperUser"] is False  # API flag trusted
    assert payload["auth"]["pcedisonIdentity"] is True
    assert payload["auth"]["normalIdentity"] is False
    assert payload["auth"]["superState"] is True
    assert payload["auth"]["normalState"] is False
    assert payload["auth"]["normalSettingsPermission"] == "需要管理員"
    assert payload["auth"]["anonymousSettingsPermission"] == "需要登入管理員"
    assert payload["marketGroups"]["announced"]["entry"][0]["stockCode"] == "2357"
    assert payload["marketGroups"]["announced"]["watch"][0]["stockCode"] == "1101"
    assert payload["marketGroups"]["announced"]["excluded"][0]["stockCode"] == "2881"
    assert payload["marketGroups"]["pending"]["watch"][0]["stockCode"] == "9999"
    assert payload["sortedEntryByPer"] == ["1000", "2000", "3000", "9999"]
    assert payload["extractedPer"] == 12.34
    assert payload["filingAwareGroups"]["announced"]["watch"][0]["stockCode"] == "1101"
    assert [item["stockCode"] for item in payload["filingAwareGroups"]["pending"]["watch"]] == ["1102", "1103"]
    assert payload["evidenceValue"] == "119.63 億"
    assert "evidence-table" in payload["evidenceHtml"]
    assert "data-evidence-width" in payload["evidenceHtml"]
    assert 'style="' not in payload["evidenceHtml"]
    assert "2025" in payload["evidenceHtml"]
    assert "虧損" in payload["evidenceRuleHtml"]
    assert payload["orderedCodes"] == ["E1", "OFFICIAL_Q", "OFFICIAL_VALUATION", "X2", "T3", "A1", "HOLDING"]
    assert payload["orderedHtml"].find("E1 通過") < payload["orderedHtml"].find("OFFICIAL_Q 通過")
    assert payload["orderedHtml"].find("OFFICIAL_VALUATION 通過") < payload["orderedHtml"].find("X1 通過")
    assert payload["orderedHtml"].find("T3 通過") < payload["orderedHtml"].find("A1 通過")
    assert payload["exitCodes"] == ["X1", "X4"]
    assert payload["exitSignal"]["status"] == "EXIT"
    assert payload["exitSignal"]["label"] == "出場 X1、X4"
    assert "holding-signal" in payload["exitSignalHtml"]
    assert payload["exitAlerts"][0]["status"] == "EXIT"
    assert payload["exitAlerts"][0]["stockCode"] == "3008"
    assert payload["exitAlerts"][0]["exitCodes"] == ["X1", "X4"]
    assert "holding-exit-alert-banner" in payload["exitAlertBanner"]
    assert "holding-exit-alert-row exit" in payload["exitAlertBanner"]
    assert "3008" in payload["exitAlertBanner"]
    assert "data-open-holding-alert-details" in payload["exitAlertBanner"]
    assert "出場 X1、X4" in payload["exitSignalHtml"]
    assert payload["strategyDetailLabels"] == [
        "進場 E1-E6",
        "加碼 A1-A7",
        "出場 X1-X5",
        "T 系列追蹤",
        "春節輔助營收",
        "金融業不套主策略",
        "待補資料不硬判斷",
    ]
    assert "strategy-rule-card" in payload["strategyCardsHtml"]
    assert "營收 YoY &gt;= 50%" in payload["strategyCardsHtml"]
    assert "PER &lt; 21.5" in payload["strategyCardsHtml"]
    assert "存貨週轉率 &gt; 2.5" in payload["strategyCardsHtml"]
    assert "本月 YoY &lt;= 200%" in payload["strategyCardsHtml"]
    assert [item[0] for item in payload["entryDetail"]["items"]] == ["E1", "E2", "E3", "E4", "E5", "E6"]
    assert [item[0] for item in payload["addWatchDetail"]["items"]] == ["A1", "A2", "A3", "A4", "A5", "A6", "A7"]
    assert [item[0] for item in payload["tSeriesDetail"]["items"]][:2] == ["T1/T2", "T3"]
    assert payload["hasInsufficient"] is True
    assert payload["hasFinancialForContext"] is True
    assert payload["hasPublished"] is True
    assert payload["isPartialPublished"] is True


def test_frontend_renderers_escape_untrusted_html_payloads():
    script = r"""
const { state, renderAnalysisCard, renderMarketResultRow, renderRuleEvidence, renderRule } = require("./frontend/app.js");

state.holdings = [];
const hostileRule = {
  code: '<img src=x onerror="alert(1)">',
  title: '<script>alert(2)</script>',
  passed: false,
  severity: "WATCH",
  message: 'message <img src=x onerror="alert(3)"> & <b>bold</b>',
  evidence: [
    { label: '<svg onload="alert(4)">', value: 1000, unit: "shares" },
  ],
};
const hostileResult = {
  stockCode: '2357" onclick="alert(5)',
  companyName: '<img src=x onerror="alert(6)">',
  status: "WATCH",
  summary: '<script>alert(7)</script>',
  reasons: [hostileRule],
};

console.log(JSON.stringify({
  analysisCard: renderAnalysisCard(hostileResult, { allowAddAction: true }),
  marketRow: renderMarketResultRow(hostileResult, { disclosureGroup: "announced", columnKey: "watch" }),
  rule: renderRule(hostileRule),
  evidence: renderRuleEvidence(hostileRule),
}));
"""
    completed = subprocess.run(
        ["node", "-e", script],
        cwd=ROOT,
        check=True,
        text=True,
        encoding="utf-8",
        capture_output=True,
    )
    payload = json.loads(completed.stdout)

    for rendered in payload.values():
        lowered = rendered.lower()
        assert "<script" not in lowered
        assert "<img" not in lowered
        assert "<svg" not in lowered
        assert "&lt;" in rendered


def test_task5_ops_copy_is_localized_without_mojibake():
    index_text = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    app_text = (ROOT / "frontend" / "app.js").read_text(encoding="utf-8")
    ops_text = (ROOT / "frontend" / "ops_view_renderers.js").read_text(encoding="utf-8")
    script = r"""
const { createOpsViewRenderers } = require("./frontend/ops_view_renderers.js");

function escapeHtml(value) {
  return String(value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

const renderers = createOpsViewRenderers({
  escapeHtml,
  safeCompanyName(holding = {}) {
    return String(holding.name || holding.companyName || "未知公司");
  },
  renderHoldingSignal() {
    return '<span class="holding-signal">持股訊號</span>';
  },
  displayResultStatus(result = {}) {
    return { status: result.status || "持有", summary: result.summary || "摘要" };
  },
});

console.log(JSON.stringify({
  holdingHtml: renderers.renderHoldingCard({
    holding: { stockCode: "", name: "", shares: 1000, averageCost: 623.5 },
    isEditing: true,
    analysis: null,
    missing: { stockCode: "2330" },
  }),
  dataHtml: renderers.renderDataConsole({
    status: {
      activeProvider: "TWSE",
      activeProviderIsRealtime: true,
      activeProviderIsFullMarket: false,
      activeProviderHasCompleteFundamentals: true,
      mockUniverseSize: 8,
      officialUniverseSize: 1200,
      officialMonthlySnapshotSize: 950,
      officialHistoryRows: 4321,
      officialIncomeStatementSize: 1200,
      officialBalanceSheetSize: 1200,
      officialValuationSize: 1180,
      fundamentalsImportRows: 3600,
      financialFreshness: {
        status: "ok",
        isFresh: true,
        blocksDeployment: false,
        expectedFinancialPeriod: "2026Q1",
        latestCachedFinancialPeriod: "2026Q1",
        expectedPeriodCoverage: 1188,
        historyUpdatedAt: "2026-06-22T01:00:00+00:00",
        message: "財報快取已覆蓋 2026Q1，目前有 1188 檔公司資料。",
      },
      officialHistoricalFundamentals: { note: "官方基本面資料已匯入。" },
    },
    integrationStatus: {
      notifications: [{ configured: true }, { configured: false }],
      broker: { configured: true },
      aiSummary: { configured: false },
    },
    backtestStatus: {
      metrics: { tradeCount: 42 },
      note: "回測交易摘要已同步。",
    },
  }),
  schedulerHtml: renderers.renderSchedulerStatus({
    schedulerStatus: {
      status: "執行中",
      events: ["daily-scan", "refresh"],
      nextTradingDay: "2026-06-23",
    },
    autoAction: "自動掃描",
  }),
}));
"""
    payload = _run_node_json(script)
    rendered_text = "\n".join(payload.values())

    assert "<strong>權限</strong>" in index_text
    assert "設定對所有人可見，但只有管理員與超級使用者可以修改並儲存。" in index_text
    assert "Access" not in index_text
    assert "Settings stay visible for everyone" not in index_text

    for expected in [
        "尚未儲存持股",
        "資料來源狀態暫時無法讀取",
        "排程狀態暫時無法讀取",
        "未知代碼",
        "未知公司",
    ]:
        assert expected in app_text

    for forbidden in [
        "No saved holdings yet",
        "Data source status unavailable",
        "Status unavailable",
        '"Unknown"',
        "'Unknown'",
    ]:
        assert forbidden not in app_text

    for expected in [
        "持股股數",
        "平均成本",
        "減碼股數",
        "取消",
        "刪除",
        "儲存",
        "減碼",
        "是",
        "否",
        "已設定",
        "未設定",
        "資料源",
        "即時資料",
        "股票池",
        "基本面",
        "官方股票池",
        "月營收快照",
        "歷史列數",
        "損益表",
        "資產負債表",
        "估值資料",
        "匯入列數",
        "財報新鮮度",
        "應覆蓋期別",
        "快取最新期別",
        "當期覆蓋數",
        "阻擋部署",
        "2026Q1",
        "1188",
        "整合與通知",
        "券商",
        "AI 摘要",
        "交易筆數",
        "排程狀態",
        "事件",
        "下個交易日",
        "自動掃描",
        "未知代碼",
        "未知公司",
    ]:
        assert expected in rendered_text

    for forbidden in [
        "Yes",
        "No",
        "Configured",
        "Missing",
        "Provider",
        "Realtime",
        "Universe",
        "Fundamentals",
        "Official Universe",
        "Monthly Snapshot",
        "History Rows",
        "Income Statement",
        "Balance Sheet",
        "Valuation",
        "Import Rows",
        "Integrations",
        "Broker",
        "Trades",
        "configured",
    ]:
        assert forbidden not in rendered_text

    for text in [ops_text, rendered_text]:
        assert not MOJIBAKE_CONTROL_RE.search(text), repr(MOJIBAKE_CONTROL_RE.search(text).group(0))


def test_market_query_loads_cross_boundary_window_and_deduplicates_physical_pages():
    payload = _run_node_json(
        MARKET_QUERY_NODE_FIXTURE
        + r"""
(async () => {
  const index = marketIndex();
  const calls = [];
  let releases = [];
  const apiJson = async (url) => {
    calls.push(url);
    if (url === "/api/scan/market/index") return index;
    const parsed = new URL(url, "https://example.test");
    const cursor = Number(parsed.searchParams.get("cursor"));
    await new Promise((resolve) => releases.push(resolve));
    return pagePayload(index, "announced", "watch", cursor);
  };
  const client = createMarketQueryClient({ apiJson, storage: memoryStorage(), apiPageSize: 100, uiPageSize: 6 });
  await client.loadIndex();
  const first = client.loadWindow("announced", "watch", 16);
  const second = client.loadWindow("announced", "watch", 16);
  await Promise.resolve();
  releases.splice(0).forEach((release) => release());
  const [left, right] = await Promise.all([first, second]);
  console.log(JSON.stringify({
    cursors: calls.filter((url) => url.includes("/results?")).map((url) => Number(new URL(url, "https://x").searchParams.get("cursor"))),
    rows: left.items.map((item) => Number(item.stockCode) - 1000),
    sameRows: right.items.map((item) => Number(item.stockCode) - 1000),
    total: left.total,
    required: requiredApiCursors(16, 100, 6),
    loaded: client.findLoadedResult("1100")?.stockCode,
  }));
})().catch((error) => { console.error(error); process.exit(1); });
"""
    )

    assert payload == {
        "cursors": [0, 100],
        "rows": [96, 97, 98, 99, 100, 101],
        "sameRows": [96, 97, 98, 99, 100, 101],
        "total": 205,
        "required": [0, 100],
        "loaded": "1100",
    }


def test_market_query_caches_each_settled_valid_page_during_cross_boundary_race():
    payload = _run_node_json(
        MARKET_QUERY_NODE_FIXTURE
        + r"""
(async () => {
  const index = marketIndex({ watch: 205 });
  const cursors = [];
  let releaseHundred;
  const client = createMarketQueryClient({
    storage: memoryStorage(),
    apiJson: async (url) => {
      if (url === "/api/scan/market/index") return index;
      const cursor = Number(new URL(url, "https://x").searchParams.get("cursor"));
      cursors.push(cursor);
      if (cursor === 100) await new Promise((resolve) => { releaseHundred = resolve; });
      return pagePayload(index, "announced", "watch", cursor);
    },
  });
  await client.loadIndex();
  const crossing = client.loadWindow("announced", "watch", 16);
  for (let turn = 0; turn < 5; turn += 1) await Promise.resolve();
  const loadedBeforeSecondWindow = client.findLoadedResult("1000")?.stockCode || null;
  const firstWindow = await client.loadWindow("announced", "watch", 0);
  const callsBeforeRelease = [...cursors];
  releaseHundred();
  const crossed = await crossing;

  const invalidIndex = marketIndex({ watch: 1 });
  let invalidCalls = 0;
  const invalidClient = createMarketQueryClient({
    storage: memoryStorage(),
    apiJson: async (url) => {
      if (url === "/api/scan/market/index") return invalidIndex;
      invalidCalls += 1;
      const page = pagePayload(invalidIndex, "announced", "watch", 0);
      if (invalidCalls === 1) delete page.items[0].detailsAvailable;
      return page;
    },
  });
  await invalidClient.loadIndex();
  let invalidError = "";
  try { await invalidClient.loadWindow("announced", "watch", 0); }
  catch (error) { invalidError = error.message; }
  const loadedAfterInvalid = invalidClient.findLoadedResult("1000");
  const retried = await invalidClient.loadWindow("announced", "watch", 0);
  console.log(JSON.stringify({
    loadedBeforeSecondWindow,
    firstRows: firstWindow.items.map((item) => item.stockCode),
    callsBeforeRelease,
    allCalls: cursors,
    crossedRows: crossed.items.map((item) => item.stockCode),
    invalidError,
    loadedAfterInvalid,
    invalidCalls,
    retried: retried.items[0].stockCode,
  }));
})().catch((error) => { console.error(error); process.exit(1); });
"""
    )

    assert payload["loadedBeforeSecondWindow"] == "1000"
    assert payload["firstRows"] == [str(code) for code in range(1000, 1006)]
    assert payload["callsBeforeRelease"] == [0, 100]
    assert payload["allCalls"] == [0, 100]
    assert payload["crossedRows"] == [str(code) for code in range(1096, 1102)]
    assert payload["invalidError"]
    assert payload["loadedAfterInvalid"] is None
    assert payload["invalidCalls"] == 2
    assert payload["retried"] == "1000"


def test_market_query_zero_count_skips_pages_and_persists_only_the_validated_index():
    payload = _run_node_json(
        MARKET_QUERY_NODE_FIXTURE
        + r"""
(async () => {
  const index = marketIndex({ watch: 0 });
  const storage = memoryStorage();
  const calls = [];
  const client = createMarketQueryClient({
    storage,
    apiJson: async (url) => { calls.push(url); return index; },
  });
  const loaded = await client.loadIndex();
  const window = await client.loadWindow("announced", "watch", 0);
  const stored = storage.value("tw_stock_scanner.market_index.v2");
  console.log(JSON.stringify({
    calls,
    window,
    generationId: loaded.generationId,
    stored: JSON.parse(stored),
    storedHasItems: stored.includes('"items"'),
  }));
})().catch((error) => { console.error(error); process.exit(1); });
"""
    )

    assert payload["calls"] == ["/api/scan/market/index"]
    assert payload["window"]["items"] == []
    assert payload["window"]["total"] == 0
    assert payload["generationId"] == "a" * 24
    assert payload["stored"]["schemaVersion"] == 2
    assert payload["storedHasItems"] is False


def test_market_query_uses_valid_lkg_and_storage_failures_are_soft():
    payload = _run_node_json(
        MARKET_QUERY_NODE_FIXTURE
        + r"""
(async () => {
  const index = marketIndex({ watch: 7 });
  const stored = memoryStorage({ "tw_stock_scanner.market_index.v2": JSON.stringify(index) });
  const offline = createMarketQueryClient({ storage: stored, apiJson: async () => { throw new Error("offline"); } });
  const lkg = await offline.loadIndex();

  const invalid = memoryStorage({ "tw_stock_scanner.market_index.v2": "{bad json" });
  let invalidMessage = "";
  try {
    await createMarketQueryClient({ storage: invalid, apiJson: async () => { throw new Error("offline"); } }).loadIndex();
  } catch (error) { invalidMessage = error.message; }

  const brokenStorage = {
    getItem() { throw new Error("blocked get"); },
    setItem() { throw new Error("quota"); },
    removeItem() { throw new Error("blocked remove"); },
  };
  const online = createMarketQueryClient({ storage: brokenStorage, apiJson: async () => index });
  const network = await online.loadIndex();
  const removeFailure = {
    getItem() { return "{bad json"; },
    removeItem() { throw new Error("blocked remove"); },
  };
  let removeFailureMessage = "";
  try {
    await createMarketQueryClient({ storage: removeFailure, apiJson: async () => { throw new Error("still offline"); } }).loadIndex();
  } catch (error) { removeFailureMessage = error.message; }
  console.log(JSON.stringify({
    lkgGeneration: lkg.generationId,
    lkgWarning: offline.warning,
    invalidMessage,
    invalidRemoved: invalid.calls.some(([kind]) => kind === "remove"),
    networkGeneration: network.generationId,
    removeFailureMessage,
  }));
})().catch((error) => { console.error(error); process.exit(1); });
"""
    )

    assert payload["lkgGeneration"] == "a" * 24
    assert payload["lkgWarning"]
    assert payload["invalidMessage"] == "offline"
    assert payload["invalidRemoved"] is True
    assert payload["networkGeneration"] == "a" * 24
    assert payload["removeFailureMessage"] == "still offline"


def test_market_query_removes_oversized_stored_index_before_json_parse():
    payload = _run_node_json(
        MARKET_QUERY_NODE_FIXTURE
        + r"""
(async () => {
  const key = "tw_stock_scanner.market_index.v2";
  const raw = '{"padding":"' + "x".repeat(60 * 1024) + '"}';
  const storage = memoryStorage({ [key]: raw });
  const originalParse = JSON.parse;
  let parseCalls = 0;
  JSON.parse = (value) => {
    parseCalls += 1;
    return originalParse(value);
  };
  let message = "";
  try {
    await createMarketQueryClient({
      storage,
      apiJson: async () => { throw new Error("offline"); },
    }).loadIndex();
  } catch (error) {
    message = error.message;
  } finally {
    JSON.parse = originalParse;
  }
  console.log(JSON.stringify({
    parseCalls,
    message,
    removed: storage.calls.some(([method]) => method === "remove"),
  }));
})().catch((error) => { console.error(error); process.exit(1); });
"""
    )

    assert payload == {"parseCalls": 0, "message": "offline", "removed": True}


def test_market_query_memory_lkg_never_rolls_back_to_older_stored_generation():
    payload = _run_node_json(
        MARKET_QUERY_NODE_FIXTURE
        + r"""
(async () => {
  const storedA = marketIndex({ generationId: generationA, watch: 7 });
  const networkB = marketIndex({ generationId: generationB, watch: 9 });
  let offline = false;
  const quotaStorage = {
    getItem() { return JSON.stringify(storedA); },
    setItem() { throw new Error("quota"); },
    removeItem() {},
  };
  const client = createMarketQueryClient({
    storage: quotaStorage,
    apiJson: async () => {
      if (offline) throw new Error("offline");
      return networkB;
    },
  });
  const online = await client.loadIndex();
  offline = true;
  const fallback = await client.loadIndex({ force: true });

  const firstOffline = createMarketQueryClient({
    storage: quotaStorage,
    apiJson: async () => { throw new Error("first offline"); },
  });
  const storedFallback = await firstOffline.loadIndex();
  console.log(JSON.stringify({
    online: online.generationId,
    fallback: fallback.generationId,
    current: client.index.generationId,
    warning: client.warning,
    source: client.source,
    storedFallback: storedFallback.generationId,
    storedSource: firstOffline.source,
  }));
})().catch((error) => { console.error(error); process.exit(1); });
"""
    )

    assert payload == {
        "online": "b" * 24,
        "fallback": "b" * 24,
        "current": "b" * 24,
        "warning": "offline",
        "source": "memory",
        "storedFallback": "a" * 24,
        "storedSource": "storage",
    }


def test_market_query_rejects_malformed_indexes_and_bounded_metadata():
    payload = _run_node_json(
        MARKET_QUERY_NODE_FIXTURE
        + r"""
const mutations = [
  (index) => { index.schemaVersion = 1; },
  (index) => { index.generationId = "A".repeat(24); },
  (index) => { index.pageSize = 99; },
  (index) => { index.counts.universeSize = -1; },
  (index) => { index.disclosures.announced.watch.count = 204; },
  (index) => { index.disclosures.announced.watch.pages[1].cursor = 99; },
  (index) => { index.generatedAt = "x".repeat(5000); },
  (index) => { index.cacheStatus.quality = Array.from({ length: 101 }, () => 1); },
  (index) => { index.unexpected = true; },
  (index) => { index.generatedAt = "1"; },
  (index) => { index.generatedAt = "2026-07-13T01:02:03"; },
  (index) => { index.generatedAt = "2026-02-29T00:00:00Z"; },
  (index) => { index.generatedAt = "2026-04-31T00:00:00+08:00"; },
  (index) => { index.generatedAt = "0000-01-01T00:00:00Z"; },
  (index) => { index.disclosurePeriod = ""; },
  (index) => { index.disclosurePeriod = "  "; },
  (index) => { index.cacheStatus.cacheHit = "yes"; },
  (index) => { index.cacheStatus.isStale = []; },
  (index) => { index.cacheStatus.refreshStatus = {}; },
  (index) => { index.cacheStatus.quality = []; },
  (index) => { index.cacheStatus.storedAt = 7; },
];
const rejected = mutations.map((mutate) => {
  const index = marketIndex();
  mutate(index);
  try { validateMarketIndex(index); return false; } catch { return true; }
});
const typedCache = marketIndex();
typedCache.cacheStatus = {
  cacheHit: true, isStale: false, storedAt: null, nextRefreshAfter: null,
  refreshStatus: "fresh", quality: { acceptedRows: 7 },
};
console.log(JSON.stringify({
  rejected,
  valid: validateMarketIndex(marketIndex()).generationId,
  typedCache: validateMarketIndex(typedCache).generationId,
}));
"""
    )

    assert payload["rejected"] == [True] * 21
    assert payload["valid"] == "a" * 24
    assert payload["typedCache"] == "a" * 24


def test_market_query_rejects_strict_page_item_and_reason_contract_mutations():
    payload = _run_node_json(
        MARKET_QUERY_NODE_FIXTURE
        + r"""
(async () => {
  async function rejects(mutate) {
    const index = marketIndex({ watch: 1 });
    const page = pagePayload(index, "announced", "watch", 0);
    mutate(page.items[0]);
    const client = createMarketQueryClient({
      storage: memoryStorage(),
      apiJson: async (url) => url === "/api/scan/market/index" ? index : page,
    });
    await client.loadIndex();
    try { await client.loadWindow("announced", "watch", 0); return false; }
    catch { return true; }
  }
  async function accepts(item) {
    const index = marketIndex({ watch: 1 });
    const page = pagePayload(index, "announced", "watch", 0);
    page.items = [item];
    const client = createMarketQueryClient({
      storage: memoryStorage(),
      apiJson: async (url) => url === "/api/scan/market/index" ? index : page,
    });
    await client.loadIndex();
    return (await client.loadWindow("announced", "watch", 0)).items[0].stockCode;
  }
  const mutations = [
    (item) => { delete item.detailsAvailable; },
    (item) => { delete item.hasFullDetails; },
    (item) => { item.detailsAvailable = 1; },
    (item) => { item.hasFullDetails = 0; },
    (item) => { item.companyName = []; },
    (item) => { item.status = true; },
    (item) => { item.summary = {}; },
    (item) => { item.reasons = "not-a-list"; },
    (item) => { item.reasons = [{}]; },
    (item) => { item.reasons = [{ code: "" }]; },
    (item) => { item.reasons = [{ code: "UNKNOWN" }]; },
    (item) => { item.reasons = [{ code: "E4", title: [] }]; },
    (item) => { item.reasons = [{ code: "E4", severity: false }]; },
    (item) => { item.reasons = [{ code: "E4", message: [] }]; },
    (item) => { item.reasons = [{ code: "E4", passed: 1 }]; },
    (item) => { item.reasons = [{ code: "E4", extra: true }]; },
    (item) => { item.extra = true; },
  ];
  const rejected = [];
  for (const mutate of mutations) rejected.push(await rejects(mutate));
  const minimal = await accepts({ stockCode: "1000", detailsAvailable: true, hasFullDetails: false });
  const minimalReason = await accepts({
    stockCode: "1000", detailsAvailable: true, hasFullDetails: false, reasons: [{ code: "E4" }],
  });
  console.log(JSON.stringify({ rejected, minimal, minimalReason }));
})().catch((error) => { console.error(error); process.exit(1); });
"""
    )

    assert payload["rejected"] == [True] * 17
    assert payload["minimal"] == "1000"
    assert payload["minimalReason"] == "1000"


def test_market_query_page_failure_retries_without_polluting_the_loaded_cache():
    payload = _run_node_json(
        MARKET_QUERY_NODE_FIXTURE
        + r"""
(async () => {
  const index = marketIndex({ watch: 7 });
  let pageCalls = 0;
  const client = createMarketQueryClient({
    storage: memoryStorage(),
    apiJson: async (url) => {
      if (url === "/api/scan/market/index") return index;
      pageCalls += 1;
      if (pageCalls === 1) throw new Error("page offline");
      return pagePayload(index, "announced", "watch", 0);
    },
  });
  await client.loadIndex();
  let firstError = "";
  try { await client.loadWindow("announced", "watch", 0); } catch (error) { firstError = error.message; }
  const afterFailure = client.findLoadedResult("1000");
  const retried = await client.loadWindow("announced", "watch", 0);
  console.log(JSON.stringify({ firstError, afterFailure, pageCalls, rows: retried.items.map((item) => Number(item.stockCode) - 1000) }));
})().catch((error) => { console.error(error); process.exit(1); });
"""
    )

    assert payload == {
        "firstError": "page offline",
        "afterFailure": None,
        "pageCalls": 2,
        "rows": list(range(6)),
    }


def test_market_query_generation_epoch_ignores_late_pages_and_clears_lookup():
    payload = _run_node_json(
        MARKET_QUERY_NODE_FIXTURE
        + r"""
(async () => {
  let index = marketIndex({ generationId: generationA, watch: 7 });
  let release;
  const apiJson = async (url) => {
    if (url === "/api/scan/market/index") return index;
    const requestGeneration = new URL(url, "https://x").searchParams.get("generationId");
    if (requestGeneration === generationA) {
      await new Promise((resolve) => { release = resolve; });
      return pagePayload(marketIndex({ generationId: generationA, watch: 7 }), "announced", "watch", 0);
    }
    return pagePayload(index, "announced", "watch", 0);
  };
  const client = createMarketQueryClient({ storage: memoryStorage(), apiJson });
  await client.loadIndex();
  const late = client.loadWindow("announced", "watch", 0);
  await Promise.resolve();
  index = marketIndex({ generationId: generationB, watch: 7 });
  await client.loadIndex({ force: true });
  release();
  let staleCode = "";
  try { await late; } catch (error) { staleCode = error.code; }
  const beforeNewPage = client.findLoadedResult("1000");
  await client.loadWindow("announced", "watch", 0);
  console.log(JSON.stringify({ staleCode, beforeNewPage, generationId: client.index.generationId, loaded: client.findLoadedResult("1000")?.stockCode }));
})().catch((error) => { console.error(error); process.exit(1); });
"""
    )

    assert payload == {
        "staleCode": "STALE_MARKET_GENERATION",
        "beforeNewPage": None,
        "generationId": "b" * 24,
        "loaded": "1000",
    }


def test_market_query_clear_generation_invalidates_pending_index_without_losing_dedup():
    payload = _run_node_json(
        MARKET_QUERY_NODE_FIXTURE
        + r"""
(async () => {
  let indexCalls = 0;
  let releaseOld;
  const client = createMarketQueryClient({
    storage: memoryStorage(),
    apiJson: async () => {
      indexCalls += 1;
      if (indexCalls === 1) {
        await new Promise((resolve) => { releaseOld = resolve; });
        return marketIndex({ generationId: generationA, watch: 7 });
      }
      return marketIndex({ generationId: generationB, watch: 9 });
    },
  });
  const old = client.loadIndex();
  const duplicate = client.loadIndex();
  await Promise.resolve();
  client.clearGeneration();
  const clearedIndex = client.index;
  const fresh = client.loadIndex();
  for (let turn = 0; turn < 3; turn += 1) await Promise.resolve();
  releaseOld();
  const oldResults = await Promise.allSettled([old, duplicate]);
  const freshIndex = await fresh;
  console.log(JSON.stringify({
    indexCalls,
    clearedIndex,
    oldStatuses: oldResults.map((result) => result.status),
    oldCodes: oldResults.map((result) => result.reason?.code || null),
    fresh: freshIndex.generationId,
    current: client.index?.generationId || null,
    source: client.source,
  }));
})().catch((error) => { console.error(error); process.exit(1); });
"""
    )

    assert payload == {
        "indexCalls": 2,
        "clearedIndex": None,
        "oldStatuses": ["rejected", "rejected"],
        "oldCodes": ["STALE_MARKET_GENERATION", "STALE_MARKET_GENERATION"],
        "fresh": "b" * 24,
        "current": "b" * 24,
        "source": "network",
    }


def test_market_renderer_uses_server_window_without_sorting_or_slicing_again():
    script = r"""
const { createMarketRender } = require("./frontend/market_render.js");
let sortCalls = 0;
const state = {
  marketListPages: { announced: { watch: 16 } },
  expandedMarketResultIds: new Set(),
};
const renderer = createMarketRender({
  getState: () => state,
  escapeHtml: (value) => String(value ?? ""),
  safeText: (value, fallback = "") => String(value ?? "").trim() || fallback,
  safeRequestId: () => "",
  appendRequestId: (message) => message,
  safeCompanyName: (result) => result.companyName || "",
  displayResultStatus: (result) => ({ status: result.status || "WATCH", summary: result.summary || "" }),
  statusClass: () => "watch",
  statusLabel: (status) => status,
  renderRule: () => "",
  resultActionButtons: () => "",
  sortRulesForDisplay: (rules) => rules,
  sortMarketResultsForDisplay: (_column, rows) => { sortCalls += 1; return [...rows].reverse(); },
  marketColumnNote: () => "",
  marketResultId: (result) => `announced:watch:${result.stockCode}`,
  MARKET_LIST_PAGE_SIZE: 6,
  MARKET_RESULT_COLUMNS: [["watch", "Watch"]],
  MARKET_DISCLOSURE_TABS: [{ key: "announced" }],
});
const rows = Array.from({ length: 6 }, (_, offset) => ({ stockCode: String(1096 + offset), companyName: `Row ${96 + offset}`, status: "WATCH", reasons: [] }));
const html = renderer.renderMarketColumn("announced", "watch", "Watch", {
  items: rows,
  total: 205,
  windowStart: 96,
  loading: true,
  error: "temporary",
});
console.log(JSON.stringify({ html, sortCalls }));
"""
    payload = _run_node_json(script)

    assert payload["sortCalls"] == 0
    assert "Row 96" in payload["html"]
    assert "Row 101" in payload["html"]
    assert "(205)" in payload["html"]
    assert "temporary" in payload["html"]


def test_market_scan_lookup_falls_back_to_loaded_v2_page_items():
    payload = _run_node_json(
        r"""
const { createMarketScan } = require("./frontend/market_scan.js");
const loaded = { stockCode: "2330", companyName: "TSMC", status: "WATCH", reasons: [] };
const helpers = createMarketScan({
  getState: () => ({ marketScan: null }),
  safeText: (value, fallback = "") => String(value ?? "").trim() || fallback,
  findLoadedResult: (stockCode) => stockCode === "2330" ? loaded : null,
});
const result = helpers.findMarketResultById("announced:watch:2330:WATCH");
console.log(JSON.stringify({ same: result === loaded, missing: helpers.findMarketResultById("announced:watch:9999:WATCH") }));
"""
    )

    assert payload == {"same": True, "missing": None}


def test_app_v2_integration_keeps_legacy_state_separate_and_exports_bounded():
    source = (ROOT / "frontend" / "app.js").read_text(encoding="utf-8")
    export_body = source[source.index("async function exportReport") : source.index("function openOnboarding")]

    assert "marketIndex: null" in source
    assert "marketWindow:" in source
    assert "marketQueryWarning:" in source
    assert 'MARKET_API_VERSION === "v2"' in source
    assert "state.schedulerAutoScan?.scan" not in source
    assert "loadWindow" not in export_body
    assert "state.marketIndex" in source[source.index("function refreshHoldingsDependentViews") : source.index("function upsertHolding")]


def test_app_loads_runtime_config_before_initial_data_and_is_v2_only():
    source = (ROOT / "frontend" / "app.js").read_text(encoding="utf-8")
    market_query_source = (ROOT / "frontend" / "market_query.js").read_text(encoding="utf-8")
    index_html = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    init_start = source.index("async function init")
    init_body = source[init_start : source.index('if (typeof document !== "undefined")', init_start)]

    assert "let MARKET_API_VERSION" in source
    assert "async function loadRuntimeConfig" in source
    assert 'apiJson("/api/runtime-config", { method: "GET" })' in source
    assert init_body.index("await loadRuntimeConfig();") < init_body.index("await Promise.all")

    # v1 is retired: app.js must carry no downgrade path at all (not even the literal),
    # and neither module may request the retired whole-scan route.
    assert '"v1"' not in source
    for js_source in (source, market_query_source):
        assert '"/api/scan/market"' not in js_source
        assert "'/api/scan/market'" not in js_source
        assert "`/api/scan/market`" not in js_source
    assert 'name="stock-scanner-market-api-version" content="v2"' in index_html


def test_app_market_query_storage_getter_security_error_is_fail_soft():
    payload = _run_node_json(
        MARKET_QUERY_NODE_FIXTURE
        + r"""
(async () => {
  Object.defineProperty(globalThis, "localStorage", {
    configurable: true,
    get() {
      const error = new Error("blocked localStorage");
      error.name = "SecurityError";
      throw error;
    },
  });
  let appError = "";
  let app = null;
  try { app = require("./frontend/app.js"); }
  catch (error) { appError = error.name + ":" + error.message; }
  let loadError = "";
  let defaultHoldings = null;
  try { defaultHoldings = app?.loadHoldingsFromStorage(); }
  catch (error) { loadError = error.name + ":" + error.message; }
  const appSource = require("fs").readFileSync("./frontend/app.js", "utf8");
  const directStorageRefs = (appSource.match(/\blocalStorage\b/g) || []).length;
  const storageHelpers = require("./frontend/storage.js");
  const quota = {
    getItem() { throw new Error("blocked get"); },
    setItem() { throw new Error("quota"); },
  };
  let storageSoft = false;
  try {
    const fromNull = storageHelpers.loadHoldingsFromStorage(null, [], (rows) => rows);
    const fromQuota = storageHelpers.loadHoldingsFromStorage(quota, [], (rows) => rows);
    storageHelpers.saveHoldingsLocalOnly([], null, [], (rows) => rows);
    storageHelpers.saveHoldingsLocalOnly([], quota, [], (rows) => rows);
    const completed = storageHelpers.hasCompletedOnboarding(quota, null);
    storageHelpers.markOnboardingDone(quota, null);
    storageSoft = fromNull.length === 0 && fromQuota.length === 0 && completed === false;
  } catch {}
  const helpers = require("./frontend/market_query.js");
  const queryHelperType = typeof helpers.safeStorage;
  const helperType = typeof storageHelpers.safeStorage;
  const safe = helperType === "function" ? storageHelpers.safeStorage(globalThis) : "missing";
  let nullStorageGeneration = null;
  if (safe === null) {
    const client = helpers.createMarketQueryClient({
      storage: null,
      apiJson: async () => marketIndex({ generationId: generationA, watch: 1 }),
    });
    nullStorageGeneration = (await client.loadIndex()).generationId;
  }
  console.log(JSON.stringify({
    appError,
    loadError,
    defaultHoldings,
    directStorageRefs,
    storageSoft,
    queryHelperType,
    helperType,
    safeIsNull: safe === null,
    nullStorageGeneration,
  }));
})().catch((error) => { console.error(error); process.exit(1); });
"""
    )

    assert payload == {
        "appError": "",
        "loadError": "",
        "defaultHoldings": [],
        "directStorageRefs": 0,
        "storageSoft": True,
        "queryHelperType": "undefined",
        "helperType": "function",
        "safeIsNull": True,
        "nullStorageGeneration": "a" * 24,
    }


def test_app_null_primary_storage_still_triggers_account_sync_and_holding_scan():
    payload = _run_node_json(
        r"""
Object.defineProperty(globalThis, "localStorage", {
  configurable: true,
  get() { throw new DOMException("blocked", "SecurityError"); },
});
let scanTimers = 0;
global.window = {
  clearTimeout() {},
  setTimeout() { scanTimers += 1; return scanTimers; },
};
global.document = {
  addEventListener() {},
  querySelector() { return null; },
  querySelectorAll() { return []; },
};
global.fetch = async () => new Promise(() => {});
const app = require("./frontend/app.js");
app.state.companies = [{
  stockCode: "2357", name: "華碩", companyName: "華碩",
  market: "TWSE", industryName: "電腦及週邊設備業", isFinancial: false,
}];
app.state.auth = { authenticated: true, available: true, user: { id: "user-1" } };
app.state.settings.manual_scan_enabled = true;
app.saveHoldings([{ stockCode: "2357", companyName: "華碩", shares: 100, averageCost: 500 }]);
console.log(JSON.stringify({
  syncing: app.state.isSyncingHoldings,
  scanTimers,
  holdings: app.state.holdings.map((holding) => holding.stockCode),
}));
"""
    )

    assert payload == {"syncing": True, "scanTimers": 1, "holdings": ["2357"]}


def test_market_query_coordinator_preserves_rows_page_expansion_and_detail_on_failure():
    payload = _run_node_json(
        MARKET_QUERY_NODE_FIXTURE
        + r"""
(async () => {
  const originalItem = { stockCode: "2330", detailLoading: false, detailError: "kept" };
  const state = {
    marketIndex: marketIndex({ watch: 7 }),
    marketWindow: {
      items: [originalItem], total: 7, windowStart: 0,
      disclosure: "announced", category: "watch", uiPage: 0,
      loading: false, error: null,
    },
    marketQueryWarning: null,
    marketListPages: { announced: { watch: 0 } },
    expandedMarketResultIds: new Set(["announced:watch:2330:WATCH"]),
  };
  let fail = true;
  const client = {
    warning: "",
    async loadWindow() {
      if (fail) throw new Error("page unavailable");
      return { items: [originalItem], total: 7, windowStart: 0, loading: false, error: null };
    },
  };
  let renders = 0;
  const coordinator = createMarketQueryCoordinator({
    client,
    state,
    getSelection: () => ({ disclosure: "announced", category: "watch", uiPage: 0 }),
    resetUi() {},
    render() { renders += 1; },
  });
  await coordinator.loadWindow();
  const failed = {
    sameItem: state.marketWindow.items[0] === originalItem,
    detailError: state.marketWindow.items[0].detailError,
    page: state.marketListPages.announced.watch,
    expanded: [...state.expandedMarketResultIds],
    loading: state.marketWindow.loading,
    error: state.marketWindow.error,
  };
  fail = false;
  await coordinator.loadWindow();
  console.log(JSON.stringify({ failed, retriedError: state.marketWindow.error, renders }));
})().catch((error) => { console.error(error); process.exit(1); });
"""
    )

    assert payload["failed"] == {
        "sameItem": True,
        "detailError": "kept",
        "page": 0,
        "expanded": ["announced:watch:2330:WATCH"],
        "loading": False,
        "error": "page unavailable",
    }
    assert payload["retriedError"] is None
    assert payload["renders"] == 4


def test_market_query_coordinator_rolls_back_same_bucket_page_after_failure_without_cross_bucket_rows():
    payload = _run_node_json(
        MARKET_QUERY_NODE_FIXTURE
        + r"""
(async () => {
  const originalItem = { stockCode: "2330", detailLoading: false, detailError: "kept" };
  const state = {
    marketIndex: marketIndex({ watch: 20, entry: 4 }),
    marketWindow: {
      items: [originalItem], total: 20, windowStart: 0,
      disclosure: "announced", category: "watch", uiPage: 0,
      loading: false, error: null,
    },
    marketQueryWarning: null,
    marketListPages: { announced: { watch: 1, entry: 0 } },
    expandedMarketResultIds: new Set(["announced:watch:2330:WATCH"]),
  };
  let category = "watch";
  const client = {
    warning: "",
    async loadWindow() { throw new Error("page unavailable"); },
  };
  const coordinator = createMarketQueryCoordinator({
    client,
    state,
    getSelection: () => ({
      disclosure: "announced",
      category,
      uiPage: state.marketListPages.announced[category],
    }),
    resetUi() {},
    render() {},
  });

  await coordinator.loadWindow();
  const sameBucketFailure = {
    sameItem: state.marketWindow.items[0] === originalItem,
    detailError: state.marketWindow.items[0]?.detailError,
    uiPage: state.marketWindow.uiPage,
    selectedPage: state.marketListPages.announced.watch,
    expanded: [...state.expandedMarketResultIds],
    error: state.marketWindow.error,
    warning: state.marketQueryWarning,
  };

  category = "entry";
  await coordinator.loadWindow();
  const crossBucketFailure = {
    category: state.marketWindow.category,
    uiPage: state.marketWindow.uiPage,
    itemCount: state.marketWindow.items.length,
    error: state.marketWindow.error,
  };
  console.log(JSON.stringify({ sameBucketFailure, crossBucketFailure }));
})().catch((error) => { console.error(error); process.exit(1); });
"""
    )

    assert payload["sameBucketFailure"] == {
        "sameItem": True,
        "detailError": "kept",
        "uiPage": 0,
        "selectedPage": 0,
        "expanded": ["announced:watch:2330:WATCH"],
        "error": "page unavailable",
        "warning": "page unavailable",
    }
    assert payload["crossBucketFailure"] == {
        "category": "entry",
        "uiPage": 0,
        "itemCount": 0,
        "error": "page unavailable",
    }


def test_market_query_coordinator_rapid_flip_rolls_back_to_last_committed_window():
    payload = _run_node_json(
        MARKET_QUERY_NODE_FIXTURE
        + r"""
(async () => {
  const committedItem = { stockCode: "2330", detailLoading: false, detailError: "kept" };
  const state = {
    marketIndex: marketIndex({ watch: 30 }),
    marketWindow: { items: [], total: 0, windowStart: 0, loading: false, error: null },
    marketQueryWarning: null,
    marketListPages: { announced: { watch: 0 } },
    expandedMarketResultIds: new Set(),
  };
  let releasePageOne;
  const client = {
    warning: "",
    async loadWindow(_disclosure, _category, uiPage) {
      if (uiPage === 0) {
        return { items: [committedItem], total: 30, windowStart: 0, loading: false, error: null };
      }
      if (uiPage === 1) {
        await new Promise((resolve) => { releasePageOne = resolve; });
        return { items: [{ stockCode: "late" }], total: 30, windowStart: 6, loading: false, error: null };
      }
      throw new Error("page two unavailable");
    },
  };
  const coordinator = createMarketQueryCoordinator({
    client,
    state,
    getSelection: () => ({
      disclosure: "announced",
      category: "watch",
      uiPage: state.marketListPages.announced.watch,
    }),
    resetUi() {},
    render() {},
  });

  await coordinator.loadWindow();
  state.expandedMarketResultIds.add("announced:watch:2330:WATCH");
  state.marketListPages.announced.watch = 1;
  const latePageOne = coordinator.loadWindow();
  await Promise.resolve();
  state.marketListPages.announced.watch = 2;
  await coordinator.loadWindow();
  releasePageOne();
  await latePageOne;
  console.log(JSON.stringify({
    sameItem: state.marketWindow.items[0] === committedItem,
    detailError: state.marketWindow.items[0]?.detailError,
    uiPage: state.marketWindow.uiPage,
    selectedPage: state.marketListPages.announced.watch,
    expanded: [...state.expandedMarketResultIds],
    error: state.marketWindow.error,
  }));
})().catch((error) => { console.error(error); process.exit(1); });
"""
    )

    assert payload == {
        "sameItem": True,
        "detailError": "kept",
        "uiPage": 0,
        "selectedPage": 0,
        "expanded": ["announced:watch:2330:WATCH"],
        "error": "page two unavailable",
    }


def test_market_query_coordinator_ignores_late_selection_and_resets_ui_on_generation_change():
    payload = _run_node_json(
        MARKET_QUERY_NODE_FIXTURE
        + r"""
(async () => {
  const state = {
    marketIndex: marketIndex({ generationId: generationA, watch: 205 }),
    marketWindow: { items: [], total: 205, windowStart: 0, loading: false, error: null },
    marketQueryWarning: null,
    marketListPages: { announced: { watch: 0 } },
    expandedMarketResultIds: new Set(["old"]),
  };
  let uiPage = 0;
  let releaseOld;
  let firstPageZero = true;
  const client = {
    warning: "",
    async loadIndex() { return marketIndex({ generationId: generationB, watch: 205 }); },
    async loadWindow(_disclosure, _category, requestedPage) {
      if (requestedPage === 0 && firstPageZero) {
        firstPageZero = false;
        await new Promise((resolve) => { releaseOld = resolve; });
        return { items: [{ stockCode: "old" }], total: 205, windowStart: 0, loading: false, error: null };
      }
      return { items: [{ stockCode: "new" }], total: 205, windowStart: requestedPage * 6, loading: false, error: null };
    },
  };
  let resets = 0;
  const coordinator = createMarketQueryCoordinator({
    client,
    state,
    getSelection: () => ({ disclosure: "announced", category: "watch", uiPage }),
    resetUi() {
      resets += 1;
      state.marketListPages.announced.watch = 0;
      state.expandedMarketResultIds.clear();
    },
    render() {},
  });
  const old = coordinator.loadWindow();
  await Promise.resolve();
  uiPage = 1;
  await coordinator.loadWindow();
  releaseOld();
  await old;
  const afterLate = { code: state.marketWindow.items[0].stockCode, uiPage: state.marketWindow.uiPage };
  uiPage = 0;
  await coordinator.refresh({ force: true });
  console.log(JSON.stringify({
    afterLate,
    generationId: state.marketIndex.generationId,
    resets,
    page: state.marketListPages.announced.watch,
    expanded: [...state.expandedMarketResultIds],
  }));
})().catch((error) => { console.error(error); process.exit(1); });
"""
    )

    assert payload == {
        "afterLate": {"code": "new", "uiPage": 1},
        "generationId": "b" * 24,
        "resets": 1,
        "page": 0,
        "expanded": [],
    }


def test_market_refresh_polling_same_generation_keeps_page_and_expanded_state():
    payload = _run_node_json(
        MARKET_QUERY_NODE_FIXTURE
        + r"""
(async () => {
  const sameIndex = marketIndex({ generationId: generationA, watch: 205 });
  const state = {
    marketIndex: sameIndex,
    marketWindow: { items: [{ stockCode: "1006" }], total: 205, windowStart: 6, disclosure: "announced", category: "watch", uiPage: 1, loading: false, error: null },
    marketQueryWarning: null,
    marketListPages: { announced: { watch: 1 } },
    expandedMarketResultIds: new Set(["announced:watch:1006:WATCH"]),
  };
  let resets = 0;
  const coordinator = createMarketQueryCoordinator({
    client: {
      warning: "",
      async loadIndex() { return sameIndex; },
      async loadWindow() { return { items: [{ stockCode: "1006" }], total: 205, windowStart: 6, loading: false, error: null }; },
    },
    state,
    getSelection: () => ({ disclosure: "announced", category: "watch", uiPage: 1 }),
    resetUi() { resets += 1; },
    render() {},
  });
  await coordinator.refresh({ force: true });
  console.log(JSON.stringify({
    resets,
    page: state.marketListPages.announced.watch,
    expanded: [...state.expandedMarketResultIds],
    code: state.marketWindow.items[0].stockCode,
  }));
})();
"""
    )

    assert payload == {
        "resets": 0,
        "page": 1,
        "expanded": ["announced:watch:1006:WATCH"],
        "code": "1006",
    }


def test_market_refresh_command_and_status_contracts_are_strict():
    payload = _run_node_json(
        r"""
const { validateRefreshCommand, validateRefreshStatus } = require("./frontend/market_refresh.js");
const jobId = "a".repeat(32);
const command = { jobId, status: "queued", requestId: "request-1", statusUrl: `/api/scan/market/refresh/${jobId}` };
const status = { jobId, status: "running", queuedAt: "2026-07-13T12:00:00+00:00", hasError: false };
const fullStatus = {
  ...status,
  reason: "routine_refresh",
  startedAt: "2026-07-13 12:01:00",
  finishedAt: null,
  updatedAt: "2026-07-13T12:02:00Z",
  ownerRunId: "123",
  ownerRunUrl: "https://github.com/a/b/actions/runs/123",
  hasError: true,
  dispatchStatus: "failed",
  dispatchErrorCode: "GITHUB_HTTP_403",
};
const invalid = [];
for (const [name, value] of [
  ["external", { ...command, statusUrl: `https://evil.example/api/scan/market/refresh/${jobId}` }],
  ["mismatch", { ...command, statusUrl: `/api/scan/market/refresh/${"b".repeat(32)}` }],
  ["uppercase", { ...command, jobId: "A".repeat(32), statusUrl: `/api/scan/market/refresh/${"A".repeat(32)}` }],
  ["extra", { ...command, entry: [] }],
]) {
  try { validateRefreshCommand(value); } catch { invalid.push(name); }
}
for (const [name, value] of [
  ["wrong-job", { ...status, jobId: "b".repeat(32) }],
  ["unknown-status", { ...status, status: "done" }],
  ["raw-error", { ...status, error: "secret" }],
  ["unsafe-owner", { ...status, ownerRunId: "../secret" }],
  ["owner-only", { ...status, ownerRunId: "123" }],
  ["url-only", { ...status, ownerRunUrl: "https://github.com/a/b/actions/runs/123" }],
  ["owner-mismatch", { ...status, ownerRunId: "123", ownerRunUrl: "https://github.com/a/b/actions/runs/456" }],
  ["reason", { ...status, reason: "Authorization secret" }],
  ["timestamp", { ...status, queuedAt: "not-a-date" }],
  ["has-error", { ...status, hasError: "yes" }],
  ["dispatch", { ...status, dispatchStatus: "complete" }],
  ["dispatch-code", { ...status, dispatchErrorCode: "raw/error" }],
]) {
  try { validateRefreshStatus(value, jobId); } catch { invalid.push(name); }
}
console.log(JSON.stringify({
  command: validateRefreshCommand(command),
  status: validateRefreshStatus(status, jobId),
  fullStatus: validateRefreshStatus(fullStatus, jobId),
  invalid,
}));
"""
    )

    assert payload["command"]["statusUrl"] == f"/api/scan/market/refresh/{'a' * 32}"
    assert payload["status"]["status"] == "running"
    assert payload["fullStatus"]["ownerRunId"] == "123"
    assert payload["fullStatus"]["dispatchStatus"] == "failed"
    assert payload["invalid"] == [
        "external",
        "mismatch",
        "uppercase",
        "extra",
        "wrong-job",
        "unknown-status",
        "raw-error",
        "unsafe-owner",
        "owner-only",
        "url-only",
        "owner-mismatch",
        "reason",
        "timestamp",
        "has-error",
        "dispatch",
        "dispatch-code",
    ]


def test_market_refresh_command_posts_once_polls_returned_url_and_reuses_active_promise():
    payload = _run_node_json(
        r"""
const { createMarketRefreshClient } = require("./frontend/market_refresh.js");
const jobId = "a".repeat(32);
const statusUrl = `/api/scan/market/refresh/${jobId}`;
const calls = [];
const delays = [];
let successes = 0;
let poll = 0;
const apiJson = async (url, options) => {
  calls.push({
    url,
    method: options.method,
    key: options.headers?.["Idempotency-Key"],
    hasSignal: Boolean(options.signal),
  });
  if (options.method === "POST") return { jobId, status: "queued", requestId: "request-1", statusUrl };
  poll += 1;
  return { jobId, status: poll === 1 ? "running" : "success", hasError: false };
};
const client = createMarketRefreshClient({
  apiJson,
  createKey: () => "client-key-1",
  delay: async (ms, signal) => { delays.push(ms); if (signal.aborted) throw signal.reason; },
  onSuccess: async () => { successes += 1; },
});
(async () => {
  const first = client.refresh();
  const shared = client.refresh();
  const [left, right] = await Promise.all([first, shared]);
  const terminalCalls = [];
  const terminal = createMarketRefreshClient({
    apiJson: async (url, options) => {
      terminalCalls.push({ url, method: options.method });
      return { jobId, status: "success", requestId: "request-2", statusUrl };
    },
    createKey: () => "client-key-2",
  });
  await terminal.refresh();
  console.log(JSON.stringify({ calls, delays, successes, same: left.jobId === right.jobId, terminalCalls }));
})().catch((error) => { console.error(error); process.exit(1); });
"""
    )

    assert payload["same"] is True
    assert payload["calls"] == [
        {
            "url": "/api/scan/market/refresh",
            "method": "POST",
            "key": "client-key-1",
            "hasSignal": True,
        },
        {
            "url": f"/api/scan/market/refresh/{'a' * 32}",
            "method": "GET",
            "hasSignal": True,
        },
        {
            "url": f"/api/scan/market/refresh/{'a' * 32}",
            "method": "GET",
            "hasSignal": True,
        },
    ]
    assert payload["delays"] == [1000, 2000]
    assert payload["successes"] == 1
    assert payload["terminalCalls"] == [{"url": "/api/scan/market/refresh", "method": "POST"}]


def test_market_refresh_terminal_next_action_uses_new_key_and_backoff_is_bounded():
    payload = _run_node_json(
        r"""
const { createMarketRefreshClient, DEFAULT_TIMEOUT_MS } = require("./frontend/market_refresh.js");
const jobId = "a".repeat(32);
const statusUrl = `/api/scan/market/refresh/${jobId}`;
(async () => {
  const actionCalls = [];
  let keyNumber = 0;
  const actions = createMarketRefreshClient({
    createKey: () => `action-key-${++keyNumber}`,
    apiJson: async (url, options) => {
      actionCalls.push({ url, method: options.method, key: options.headers?.["Idempotency-Key"] });
      return { jobId, status: "success", requestId: `request-${keyNumber}`, statusUrl };
    },
  });
  await actions.refresh();
  await actions.refresh();

  const delays = [];
  let poll = 0;
  const polling = createMarketRefreshClient({
    createKey: () => "backoff-key",
    delay: async (milliseconds) => { delays.push(milliseconds); },
    apiJson: async (_url, options) => {
      if (options.method === "POST") return { jobId, status: "queued", requestId: "request-backoff", statusUrl };
      poll += 1;
      return { jobId, status: poll === 7 ? "success" : "running", hasError: false };
    },
  });
  await polling.refresh();
  console.log(JSON.stringify({ actionCalls, createdKeys: keyNumber, delays, defaultTimeout: DEFAULT_TIMEOUT_MS }));
})().catch((error) => { console.error(error); process.exit(1); });
"""
    )

    assert payload["actionCalls"] == [
        {"url": "/api/scan/market/refresh", "method": "POST", "key": "action-key-1"},
        {"url": "/api/scan/market/refresh", "method": "POST", "key": "action-key-2"},
    ]
    assert payload["createdKeys"] == 2
    assert payload["delays"] == [1000, 2000, 4000, 8000, 15000, 15000, 15000]
    assert payload["defaultTimeout"] == 60_000


def test_market_refresh_reuses_unacknowledged_key_and_rejects_bad_status_url_before_get():
    payload = _run_node_json(
        r"""
const { createMarketRefreshClient } = require("./frontend/market_refresh.js");
const keys = ["client-key-1", "client-key-2"];
let keyIndex = 0;
const calls = [];
let attempt = 0;
const client = createMarketRefreshClient({
  createKey: () => keys[keyIndex++],
  apiJson: async (url, options) => {
    calls.push({ url, method: options.method, key: options.headers?.["Idempotency-Key"] });
    attempt += 1;
    if (attempt === 1) throw new Error("ambiguous transport failure");
    const jobId = "a".repeat(32);
    return {
      jobId,
      status: "queued",
      requestId: "request-1",
      statusUrl: `https://evil.example/api/scan/market/refresh/${jobId}`,
    };
  },
});
(async () => {
  const errors = [];
  try { await client.refresh(); } catch (error) { errors.push(error.message); }
  try { await client.refresh(); } catch (error) { errors.push(error.message); }
  console.log(JSON.stringify({ calls, errors, createdKeys: keyIndex }));
})().catch((error) => { console.error(error); process.exit(1); });
"""
    )

    assert [call["method"] for call in payload["calls"]] == ["POST", "POST"]
    assert [call["key"] for call in payload["calls"]] == ["client-key-1", "client-key-1"]
    assert payload["createdKeys"] == 1
    assert len(payload["errors"]) == 2


def test_market_refresh_pagehide_and_deadline_abort_inflight_status_without_repost():
    payload = _run_node_json(
        r"""
const { createMarketRefreshClient } = require("./frontend/market_refresh.js");
const jobId = "a".repeat(32);
const statusUrl = `/api/scan/market/refresh/${jobId}`;
function lifecycle() {
  const listeners = new Map();
  return {
    addEventListener(name, listener) { listeners.set(name, listener); },
    removeEventListener(name) { listeners.delete(name); },
    dispatch(name) { listeners.get(name)?.(); },
  };
}
async function pagehideCase() {
  const target = lifecycle();
  const calls = [];
  let entered;
  const enteredPromise = new Promise((resolve) => { entered = resolve; });
  const client = createMarketRefreshClient({
    pageLifecycle: target,
    createKey: () => "pagehide-key",
    delay: async () => {},
    apiJson: async (url, options) => {
      calls.push({ url, method: options.method });
      if (options.method === "POST") return { jobId, status: "queued", requestId: "request-1", statusUrl };
      entered();
      return new Promise((_resolve, reject) => {
        options.signal.addEventListener("abort", () => reject(new Error("rewrapped 503")), { once: true });
      });
    },
  });
  const pending = client.refresh().catch((error) => error.name);
  await enteredPromise;
  target.dispatch("pagehide");
  return { calls, errorName: await pending };
}
async function timeoutCase() {
  const calls = [];
  const client = createMarketRefreshClient({
    timeoutMs: 10,
    createKey: () => "timeout-key",
    delay: async () => {},
    apiJson: async (url, options) => {
      calls.push({ url, method: options.method });
      if (options.method === "POST") return { jobId, status: "queued", requestId: "request-2", statusUrl };
      return new Promise((_resolve, reject) => {
        options.signal.addEventListener("abort", () => reject(new Error("rewrapped timeout")), { once: true });
      });
    },
  });
  let errorCode = "";
  try { await client.refresh(); } catch (error) { errorCode = error.code; }
  return { calls, errorCode };
}
(async () => console.log(JSON.stringify({ pagehide: await pagehideCase(), timeout: await timeoutCase() })))()
  .catch((error) => { console.error(error); process.exit(1); });
"""
    )

    assert [call["method"] for call in payload["pagehide"]["calls"]] == ["POST", "GET"]
    assert payload["pagehide"]["errorName"] == "AbortError"
    assert [call["method"] for call in payload["timeout"]["calls"]] == ["POST", "GET"]
    assert payload["timeout"]["errorCode"] == "REFRESH_TIMEOUT"


def test_market_query_force_refresh_is_not_swallowed_by_existing_index_request():
    payload = _run_node_json(
        MARKET_QUERY_NODE_FIXTURE
        + r"""
(async () => {
  const indexA = marketIndex({ generationId: generationA, watch: 7 });
  const indexB = marketIndex({ generationId: generationB, watch: 9 });
  let indexCalls = 0;
  let releaseOld;
  const oldResponse = new Promise((resolve) => { releaseOld = () => resolve(indexA); });
  const apiJson = async (url) => {
    if (url === "/api/scan/market/index") {
      indexCalls += 1;
      if (indexCalls === 1) return indexA;
      if (indexCalls === 2) return oldResponse;
      return indexB;
    }
    return pagePayload(indexA, "announced", "watch", 0);
  };
  const client = createMarketQueryClient({ apiJson, storage: memoryStorage() });
  await client.loadIndex();
  await client.loadWindow("announced", "watch", 0);
  const stale = client.loadIndex({ force: true });
  await Promise.resolve();
  const fresh = client.loadIndex({ force: true });
  const beforeRelease = { indexCalls, oldPage: client.findLoadedResult("1000")?.stockCode };
  releaseOld();
  await stale.catch(() => null);
  const accepted = await fresh;
  console.log(JSON.stringify({
    indexCalls,
    beforeRelease,
    accepted: accepted.generationId,
    current: client.index.generationId,
    oldPageAfterChange: client.findLoadedResult("1000"),
  }));
})().catch((error) => { console.error(error); process.exit(1); });
"""
    )

    assert payload == {
        "indexCalls": 3,
    "beforeRelease": {"indexCalls": 3, "oldPage": "1000"},
        "accepted": "b" * 24,
        "current": "b" * 24,
        "oldPageAfterChange": None,
    }


def test_market_refresh_same_generation_force_keeps_real_page_cache_and_lookup():
    payload = _run_node_json(
        MARKET_QUERY_NODE_FIXTURE
        + r"""
(async () => {
  const index = marketIndex({ generationId: generationA, watch: 7 });
  let indexCalls = 0;
  let pageCalls = 0;
  const client = createMarketQueryClient({
    storage: memoryStorage(),
    apiJson: async (url) => {
      if (url === "/api/scan/market/index") {
        indexCalls += 1;
        return index;
      }
      pageCalls += 1;
      return pagePayload(index, "announced", "watch", 0);
    },
  });
  await client.loadIndex();
  await client.loadWindow("announced", "watch", 0);
  const before = client.findLoadedResult("1000")?.stockCode;
  await client.loadIndex({ force: true });
  await client.loadWindow("announced", "watch", 0);
  console.log(JSON.stringify({ indexCalls, pageCalls, before, after: client.findLoadedResult("1000")?.stockCode }));
})();
"""
    )

    assert payload == {"indexCalls": 2, "pageCalls": 1, "before": "1000", "after": "1000"}


def test_market_refresh_pagehide_abort_does_not_replace_app_warning():
    payload = _run_node_json(
        r"""
const listeners = new Map();
globalThis.addEventListener = (name, listener) => listeners.set(name, listener);
globalThis.removeEventListener = (name) => listeners.delete(name);
globalThis.StockScannerConfig = { apiMode: "same-origin", marketApiVersion: "v2" };
globalThis.document = { querySelector: () => null, addEventListener() {} };
const app = require("./frontend/app.js");
app.state.marketIndex = { generationId: "a".repeat(24) };
app.state.marketQueryWarning = "last-good-warning";
let commandCalled;
const called = new Promise((resolve) => { commandCalled = resolve; });
const jobId = "a".repeat(32);
globalThis.fetch = async (_url, options) => {
  commandCalled();
  return new Response(JSON.stringify({
    jobId,
    status: "queued",
    requestId: "request-pagehide",
    statusUrl: `/api/scan/market/refresh/${jobId}`,
  }), { status: 202, headers: { "content-type": "application/json" } });
};
(async () => {
  const pending = app.refreshMarketScan({ refreshMode: "force" });
  await called;
  listeners.get("pagehide")?.();
  const result = await pending;
  console.log(JSON.stringify({
    sameIndex: result === app.state.marketIndex,
    warning: app.state.marketQueryWarning,
  }));
})().catch((error) => { console.error(error); process.exit(1); });
"""
    )

    assert payload == {"sameIndex": True, "warning": "last-good-warning"}


def test_app_api_fetch_preserves_abort_instead_of_rewrapping_as_503():
    payload = _run_node_json(
        r"""
const app = require("./frontend/app.js");
const controller = new AbortController();
app.API_CLIENT.request = async () => {
  const error = new Error("The operation was aborted");
  error.name = "AbortError";
  throw error;
};
controller.abort();
(async () => {
  let name = "";
  try { await app.apiFetch("/api/scan/market/refresh/" + "a".repeat(32), { signal: controller.signal }); }
  catch (error) { name = error.name; }
  console.log(JSON.stringify({ name }));
})();
"""
    )

    assert payload == {"name": "AbortError"}
