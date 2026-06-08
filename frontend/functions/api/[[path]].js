const SECURITY_HEADERS = {
  "Strict-Transport-Security": "max-age=31536000; includeSubDomains; preload",
  "Content-Security-Policy":
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; font-src 'self' data:; connect-src 'self' https://stock-scanner-beta-api.pcedison.workers.dev; base-uri 'self'; form-action 'self'; frame-ancestors 'none'; object-src 'none'; upgrade-insecure-requests",
  "X-Frame-Options": "DENY",
  "X-Content-Type-Options": "nosniff",
  "Referrer-Policy": "strict-origin-when-cross-origin",
  "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=(), usb=()",
};

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

async function proxyToWorker(request, upstreamUrl) {
  const headers = new Headers(request.headers);
  headers.delete("host");

  const init = {
    method: request.method,
    headers,
    redirect: "manual",
  };
  if (!["GET", "HEAD"].includes(request.method)) {
    init.body = request.body;
  }

  const upstreamResponse = await fetch(new Request(upstreamUrl, init));
  const response = new Response(upstreamResponse.body, upstreamResponse);
  response.headers.set("cache-control", "no-store");
  for (const [key, value] of Object.entries(SECURITY_HEADERS)) {
    response.headers.set(key, value);
  }
  return response;
}

export async function onRequest(context) {
  const apiOrigin = context.env.API_ORIGIN;
  if (!apiOrigin) {
    return jsonError("Cloudflare Pages API_ORIGIN is not configured.", 503);
  }

  const requestUrl = new URL(context.request.url);
  const upstreamUrl = new URL(`${requestUrl.pathname}${requestUrl.search}`, apiOrigin);
  return proxyToWorker(context.request, upstreamUrl);
}
