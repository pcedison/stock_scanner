const SECURITY_HEADERS = {
  "Strict-Transport-Security": "max-age=31536000; includeSubDomains; preload",
  "Content-Security-Policy":
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; font-src 'self' data:; connect-src 'self'; base-uri 'self'; form-action 'self'; frame-ancestors 'none'; object-src 'none'; upgrade-insecure-requests",
  "X-Frame-Options": "DENY",
  "X-Content-Type-Options": "nosniff",
  "Referrer-Policy": "strict-origin-when-cross-origin",
  "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=(), usb=()",
};

export async function onRequest(context) {
  const apiOrigin = context.env.API_ORIGIN;
  if (!apiOrigin) {
    return Response.json(
      { detail: "Cloudflare Pages API_ORIGIN is not configured." },
      { status: 503 },
    );
  }

  const requestUrl = new URL(context.request.url);
  const upstreamUrl = new URL(`${requestUrl.pathname}${requestUrl.search}`, apiOrigin);
  const headers = new Headers(context.request.headers);
  headers.delete("host");

  const init = {
    method: context.request.method,
    headers,
    redirect: "manual",
  };
  if (!["GET", "HEAD"].includes(context.request.method)) {
    init.body = context.request.body;
  }

  const upstreamResponse = await fetch(new Request(upstreamUrl, init));
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
