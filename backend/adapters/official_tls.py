"""TLS verification shared by every official-source fetch.

www.tpex.org.tw's certificate (issued 2026-09-07) is served without its intermediate CA.
Browsers and Windows fetch the missing intermediate from the AIA URL, but OpenSSL -
Python, httpx and the GitHub runners - does not, so every TPEX endpoint failed with
CERTIFICATE_VERIFY_FAILED and the R2 seed refresh could not rebuild. Adding the public
intermediate to certifi's roots lets the chain build again; verification still ends at a
certifi-trusted root (TWCA CYBER Root CA), so nothing is trusted that was not before.
"""

from __future__ import annotations

import ssl
from functools import lru_cache
from pathlib import Path

import certifi

INTERMEDIATES_PATH = Path(__file__).with_name("certs") / "twca_ssl_ca_2023.pem"


@lru_cache(maxsize=1)
def official_ssl_context() -> ssl.SSLContext:
    context = ssl.create_default_context(cafile=certifi.where())
    context.load_verify_locations(cafile=str(INTERMEDIATES_PATH))
    return context
