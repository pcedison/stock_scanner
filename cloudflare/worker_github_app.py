"""GitHub App credentials for the Worker's refresh dispatch.

A GitHub App private key has no expiry, unlike a personal access token, so the
Worker mints its own installation token for each dispatch instead of holding a
long-lived credential that a human has to rotate before it lapses. Installation
tokens last an hour and a dispatch only happens when a refresh is actually
queued, so minting one per call is cheap and leaves nothing to cache or expire.

`js` is imported lazily inside the functions that touch the runtime: the module
must stay importable under plain CPython so the pure pieces can be unit-tested.
"""

from __future__ import annotations

import base64
import json
import time

# GitHub rejects an app JWT whose lifetime exceeds 10 minutes.
JWT_LIFETIME_SECONDS = 540
# Backdate `iat` so a Worker clock running slightly fast is still accepted.
JWT_BACKDATE_SECONDS = 60
GITHUB_API_ROOT = "https://api.github.com"
GITHUB_API_VERSION = "2022-11-28"
USER_AGENT = "stock-scanner-worker-cron/1.0"

# Credential faults that retrying cannot clear: a missing secret, an unreadable key, or
# GitHub rejecting the app JWT. They are reported as a failed dispatch straight away so
# the health monitor surfaces them on its next run instead of after three silent retries.
PERMANENT_ERROR_CODES = frozenset(
    {
        "GITHUB_APP_NOT_CONFIGURED",
        "GITHUB_APP_SIGN_FAILED",
        "GITHUB_APP_TOKEN_MALFORMED",
        "GITHUB_APP_TOKEN_HTTP_401",
        "GITHUB_APP_TOKEN_HTTP_403",
        "GITHUB_APP_TOKEN_HTTP_404",
        "GITHUB_APP_TOKEN_HTTP_422",
    }
)


def is_permanent_error(code: str | None) -> bool:
    return str(code or "").strip().upper() in PERMANENT_ERROR_CODES


def b64url(raw: bytes) -> str:
    """Base64url without padding, as JWT requires."""
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def pem_to_der(pem: str) -> bytes:
    """Strip the PEM armour and decode the PKCS#8 body WebCrypto expects."""
    body = "".join(
        line.strip() for line in str(pem or "").splitlines() if line.strip() and not line.strip().startswith("-----")
    )
    if not body:
        raise ValueError("private key is empty")
    return base64.b64decode(body)


def jwt_claims(app_id: str, now_epoch: float) -> dict:
    # exp - iat stays at JWT_LIFETIME_SECONDS, comfortably inside GitHub's 10-minute
    # ceiling; backdating moves the whole window earlier rather than widening it.
    issued = int(now_epoch) - JWT_BACKDATE_SECONDS
    return {"iat": issued, "exp": issued + JWT_LIFETIME_SECONDS, "iss": str(app_id)}


def jwt_signing_input(claims: dict) -> str:
    header = b64url(json.dumps({"alg": "RS256", "typ": "JWT"}, separators=(",", ":")).encode("utf-8"))
    payload = b64url(json.dumps(claims, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    return f"{header}.{payload}"


async def sign_rs256(private_key_pem: str, message: str) -> bytes:
    """Sign with RSASSA-PKCS1-v1_5/SHA-256 through WebCrypto.

    Python's standard library has no RSA and the Worker runs without external
    packages, so the runtime's own WebCrypto is the only signer available.
    """
    import js
    from pyodide.ffi import to_js

    key = await js.crypto.subtle.importKey(
        "pkcs8",
        to_js(pem_to_der(private_key_pem)),
        to_js({"name": "RSASSA-PKCS1-v1_5", "hash": {"name": "SHA-256"}}, dict_converter=js.Object.fromEntries),
        False,
        to_js(["sign"]),
    )
    signature = await js.crypto.subtle.sign("RSASSA-PKCS1-v1_5", key, to_js(message.encode("utf-8")))
    return bytes(js.Uint8Array.new(signature).to_py())


async def build_app_jwt(app_id: str, private_key_pem: str, now_epoch: float, *, signer=None) -> str:
    message = jwt_signing_input(jwt_claims(app_id, now_epoch))
    signature = await (signer or sign_rs256)(private_key_pem, message)
    return f"{message}.{b64url(signature)}"


async def post_installation_token(url: str, app_jwt: str) -> tuple[int, str]:
    import js

    response = await js.fetch(
        url,
        {
            "method": "POST",
            "headers": {
                "authorization": f"Bearer {app_jwt}",
                "accept": "application/vnd.github+json",
                "content-type": "application/json",
                "user-agent": USER_AGENT,
                "x-github-api-version": GITHUB_API_VERSION,
            },
        },
    )
    return int(response.status), str(await response.text())


async def installation_access_token(
    app_id: str,
    installation_id: str,
    private_key_pem: str,
    *,
    now_epoch: float | None = None,
    signer=None,
    poster=None,
) -> tuple[str | None, str | None]:
    """Mint an installation token, returning ``(token, error_code)``.

    Exactly one of the two is ever set. Error codes stay coarse on purpose: they
    reach `/api/health` and the monitor, so they must never carry key material or
    a raw GitHub error body.
    """
    app_id = str(app_id or "").strip()
    installation_id = str(installation_id or "").strip()
    if not app_id or not installation_id or not str(private_key_pem or "").strip():
        return None, "GITHUB_APP_NOT_CONFIGURED"

    try:
        app_jwt = await build_app_jwt(app_id, private_key_pem, now_epoch if now_epoch is not None else time.time(), signer=signer)
    except Exception:
        # A malformed PEM or an unavailable WebCrypto both land here; neither is
        # retryable without human action, and the cause must not be echoed out.
        return None, "GITHUB_APP_SIGN_FAILED"

    url = f"{GITHUB_API_ROOT}/app/installations/{installation_id}/access_tokens"
    try:
        status, body = await (poster or post_installation_token)(url, app_jwt)
    except Exception:
        return None, "GITHUB_APP_TOKEN_NETWORK"

    if not 200 <= int(status) < 300:
        return None, f"GITHUB_APP_TOKEN_HTTP_{int(status)}"

    try:
        token = str((json.loads(body) or {}).get("token") or "").strip()
    except (TypeError, ValueError):
        return None, "GITHUB_APP_TOKEN_MALFORMED"
    return (token, None) if token else (None, "GITHUB_APP_TOKEN_MALFORMED")
