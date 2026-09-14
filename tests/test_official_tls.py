"""www.tpex.org.tw (certificate issued 2026-09-07) stopped sending its intermediate CA, so
every TPEX fetch failed certificate verification under Python/certifi and the R2 seed
refresh could not rebuild. The bundled intermediate restores chain building."""

import hashlib
import ssl

import httpx

from backend.adapters import official_fundamentals, official_monthly_revenue
from backend.adapters.official_tls import INTERMEDIATES_PATH, official_ssl_context

TWCA_SSL_CA_SHA256 = "01af2324d098098f5e0cdf6faabada430b21cce777f47eacb26248b2fda3e531"


def test_bundled_intermediate_is_the_published_twca_ssl_ca():
    pem = INTERMEDIATES_PATH.read_text(encoding="ascii")
    der = ssl.PEM_cert_to_DER_cert(pem[pem.index("-----BEGIN CERTIFICATE-----") :])
    assert hashlib.sha256(der).hexdigest() == TWCA_SSL_CA_SHA256


def test_context_trusts_certifi_roots_plus_the_intermediate():
    context = official_ssl_context()
    subjects = [str(cert["subject"]) for cert in context.get_ca_certs()]
    subjects = [name for name in ("TWCA SSL Certification Authority", "TWCA CYBER Root CA") if any(name in s for s in subjects)]
    assert "TWCA SSL Certification Authority" in subjects
    assert "TWCA CYBER Root CA" in subjects  # the intermediate's issuer still has to be trusted
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True


def test_official_adapters_pass_the_context(monkeypatch):
    seen = []

    class _Response:
        def raise_for_status(self):
            return None

        def json(self):
            return []

    def fake_get(url, **kwargs):
        seen.append(kwargs.get("verify"))
        return _Response()

    monkeypatch.setattr(httpx, "get", fake_get)
    official_monthly_revenue.OfficialMonthlyRevenueAdapter(retry_attempts=1)._fetch_json("https://www.tpex.org.tw/x")
    official_fundamentals.OfficialFundamentalsAdapter()._fetch_json("https://www.tpex.org.tw/x")

    assert seen == [official_ssl_context(), official_ssl_context()]
