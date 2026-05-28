const SECURITY_HEADERS = {
  "Strict-Transport-Security": "max-age=31536000; includeSubDomains; preload",
  "Content-Security-Policy":
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; font-src 'self' data:; connect-src 'self' https://stock-scanner-beta-api.pcedison.workers.dev; base-uri 'self'; form-action 'self'; frame-ancestors 'none'; object-src 'none'; upgrade-insecure-requests",
  "X-Frame-Options": "DENY",
  "X-Content-Type-Options": "nosniff",
  "Referrer-Policy": "strict-origin-when-cross-origin",
  "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=(), usb=()",
};

const UPSTREAM_HEADER_ALLOWLIST = new Set([
  "accept",
  "accept-language",
  "authorization",
  "content-type",
  "cookie",
  "user-agent",
  "x-stock-scanner-csrf",
]);

function jsonError(detail, status = 502) {
  return new Response(JSON.stringify({ detail }), {
    status,
    headers: {
      "content-type": "application/json; charset=utf-8",
      "cache-control": "no-store",
      ...SECURITY_HEADERS,
    },
  });
}

function buildUpstreamHeaders(request, requestUrl) {
  const headers = new Headers();
  for (const [key, value] of request.headers.entries()) {
    const normalized = key.toLowerCase();
    if (UPSTREAM_HEADER_ALLOWLIST.has(normalized)) {
      headers.set(normalized, value);
    }
  }

  headers.set("x-forwarded-host", requestUrl.host);
  headers.set("x-forwarded-proto", requestUrl.protocol.replace(":", ""));
  headers.set("x-stock-scanner-proxy", "cloudflare-pages");

  const clientIp = request.headers.get("cf-connecting-ip") || request.headers.get("x-forwarded-for");
  if (clientIp) {
    headers.set("x-forwarded-for", clientIp.split(",", 1)[0].trim());
  }
  if (!headers.has("accept")) {
    headers.set("accept", "application/json");
  }
  return headers;
}

export async function onRequest(context) {
  const apiOrigin = context.env.API_ORIGIN;
  if (!apiOrigin) {
    return jsonError("Cloudflare Pages API_ORIGIN is not configured.", 503);
  }

  const requestUrl = new URL(context.request.url);
  const upstreamUrl = new URL(`${requestUrl.pathname}${requestUrl.search}`, apiOrigin);
  const method = context.request.method.toUpperCase();
  const headers = buildUpstreamHeaders(context.request, requestUrl);

  const init = {
    method,
    headers,
    redirect: "manual",
  };
  if (!["GET", "HEAD"].includes(method)) {
    init.body = await context.request.arrayBuffer();
  }

  let upstreamResponse;
  try {
    upstreamResponse = await fetch(upstreamUrl.toString(), init);
  } catch (error) {
    console.error("Pages API proxy upstream fetch failed", error);
    return jsonError("Cloudflare Pages API proxy could not reach the Worker API.", 502);
  }

  const responseHeaders = new Headers(upstreamResponse.headers);
  responseHeaders.set("cache-control", "no-store");
  for (const [key, value] of Object.entries(SECURITY_HEADERS)) {
    responseHeaders.set(key, value);
  }

  return new Response(upstreamResponse.body, {
    status: upstreamResponse.status,
    statusText: upstreamResponse.statusText,
    headers: responseHeaders,
  });
}
