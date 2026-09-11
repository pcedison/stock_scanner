"""GitHub App credential minting for the Worker's refresh dispatch.

The WebCrypto signer itself needs the Worker runtime and is exercised by the
wrangler dev smoke; everything around it is pure and is pinned here, especially the
error codes, because those are what the health monitor acts on.
"""

from __future__ import annotations

import asyncio
import base64
import json

import pytest

from cloudflare import worker_github_app as github_app

APP_ID = "123456"
INSTALLATION_ID = "7891011"
# Not a key: pem_to_der only base64-decodes the armoured body.
FAKE_PEM = "-----BEGIN PRIVATE KEY-----\nAAECAwQFBgcICQoLDA0ODw==\n-----END PRIVATE KEY-----\n"


def _run(coro):
    return asyncio.run(coro)


async def _signer(_pem, _message):
    return b"\x01\x02\x03\x04"


def _poster(status, body, *, calls=None):
    async def post(url, app_jwt):
        if calls is not None:
            calls.append((url, app_jwt))
        return status, body

    return post


def _token(**overrides):
    kwargs = {
        "app_id": APP_ID,
        "installation_id": INSTALLATION_ID,
        "private_key_pem": FAKE_PEM,
        "now_epoch": 1_700_000_000,
        "signer": _signer,
        "poster": _poster(201, json.dumps({"token": "ghs_installation"})),
    }
    kwargs.update(overrides)
    app_id = kwargs.pop("app_id")
    installation_id = kwargs.pop("installation_id")
    private_key_pem = kwargs.pop("private_key_pem")
    return _run(github_app.installation_access_token(app_id, installation_id, private_key_pem, **kwargs))


def test_b64url_drops_padding_and_uses_the_url_alphabet():
    assert github_app.b64url(b"\xfb\xff") == "-_8"
    assert "=" not in github_app.b64url(b"abcd")


def test_pem_to_der_strips_the_armour_and_decodes_the_body():
    assert github_app.pem_to_der(FAKE_PEM) == bytes(range(16))


def test_pem_to_der_rejects_an_empty_key():
    with pytest.raises(ValueError):
        github_app.pem_to_der("-----BEGIN PRIVATE KEY-----\n-----END PRIVATE KEY-----\n")


def test_jwt_is_backdated_and_stays_inside_githubs_ten_minute_ceiling():
    claims = github_app.jwt_claims(APP_ID, 1_700_000_000)

    assert claims["iss"] == APP_ID
    assert claims["iat"] == 1_700_000_000 - github_app.JWT_BACKDATE_SECONDS
    # GitHub rejects an app JWT whose lifetime exceeds 600s; never sit on the boundary.
    assert claims["exp"] - claims["iat"] < 600


def test_signing_input_declares_rs256():
    message = github_app.jwt_signing_input(github_app.jwt_claims(APP_ID, 1_700_000_000))
    header_b64 = message.split(".")[0]
    header = json.loads(base64.urlsafe_b64decode(header_b64 + "=" * (-len(header_b64) % 4)))

    assert header == {"alg": "RS256", "typ": "JWT"}


def test_build_app_jwt_appends_the_signature_as_a_third_segment():
    jwt = _run(github_app.build_app_jwt(APP_ID, FAKE_PEM, 1_700_000_000, signer=_signer))
    message, _, signature = jwt.rpartition(".")

    assert message == github_app.jwt_signing_input(github_app.jwt_claims(APP_ID, 1_700_000_000))
    assert signature == github_app.b64url(b"\x01\x02\x03\x04")


def test_installation_token_is_returned_on_success():
    token, error = _token()

    assert (token, error) == ("ghs_installation", None)


def test_installation_token_request_targets_the_installation_and_presents_the_jwt():
    calls: list[tuple] = []
    token, error = _token(poster=_poster(201, json.dumps({"token": "ghs_x"}), calls=calls))

    assert (token, error) == ("ghs_x", None)
    url, app_jwt = calls[0]
    assert url == f"{github_app.GITHUB_API_ROOT}/app/installations/{INSTALLATION_ID}/access_tokens"
    assert app_jwt.count(".") == 2


@pytest.mark.parametrize(
    "overrides",
    [
        {"app_id": ""},
        {"installation_id": ""},
        {"private_key_pem": "   "},
    ],
)
def test_missing_credentials_are_reported_without_calling_github(overrides):
    calls: list[tuple] = []
    kwargs = {"poster": _poster(201, json.dumps({"token": "ghs_x"}), calls=calls)}
    kwargs.update(overrides)
    token, error = _token(**kwargs)

    assert (token, error) == (None, "GITHUB_APP_NOT_CONFIGURED")
    assert calls == []


def test_a_signer_failure_is_reported_as_a_signing_fault():
    async def broken(_pem, _message):
        raise ValueError("bad key")

    assert _token(signer=broken) == (None, "GITHUB_APP_SIGN_FAILED")


def test_a_non_2xx_token_response_carries_its_status_in_the_error_code():
    assert _token(poster=_poster(401, "{}")) == (None, "GITHUB_APP_TOKEN_HTTP_401")
    assert _token(poster=_poster(404, "{}")) == (None, "GITHUB_APP_TOKEN_HTTP_404")
    assert _token(poster=_poster(500, "{}")) == (None, "GITHUB_APP_TOKEN_HTTP_500")


def test_a_transport_failure_is_reported_as_a_network_fault():
    async def broken(_url, _jwt):
        raise OSError("connection reset")

    assert _token(poster=broken) == (None, "GITHUB_APP_TOKEN_NETWORK")


@pytest.mark.parametrize("body", ["not json", "{}", json.dumps({"token": ""})])
def test_a_response_without_a_usable_token_is_reported_as_malformed(body):
    assert _token(poster=_poster(201, body)) == (None, "GITHUB_APP_TOKEN_MALFORMED")


def test_no_error_code_leaks_key_material_or_a_github_body():
    codes = {_token(poster=_poster(status, "secret-body"))[1] for status in (401, 403, 500)}
    codes.add(_token(app_id="")[1])

    for code in codes:
        assert "secret-body" not in code
        assert code == code.upper()


def test_unambiguous_configuration_faults_are_permanent():
    # A permanent fault is failed straight away, so the monitor names the broken secret
    # on its next run rather than waiting out three retries.
    assert github_app.is_permanent_error("GITHUB_APP_NOT_CONFIGURED")
    assert github_app.is_permanent_error("GITHUB_APP_SIGN_FAILED")
    assert github_app.is_permanent_error("GITHUB_APP_TOKEN_HTTP_401")
    assert github_app.is_permanent_error("GITHUB_APP_TOKEN_HTTP_404")


def test_ambiguous_and_transport_faults_take_the_slower_retry_path():
    # GitHub returns 403 both for a suspended app and for a secondary rate limit, and a
    # 2xx with an unreadable body is usually a blip; alerting instantly on either would
    # reintroduce the false alarms this change set exists to remove. They still surface,
    # just through `unknown` after three attempts.
    assert not github_app.is_permanent_error("GITHUB_APP_TOKEN_HTTP_403")
    assert not github_app.is_permanent_error("GITHUB_APP_TOKEN_MALFORMED")
    assert not github_app.is_permanent_error("GITHUB_APP_TOKEN_NETWORK")
    assert not github_app.is_permanent_error("GITHUB_APP_TOKEN_HTTP_500")
    assert not github_app.is_permanent_error(None)


def test_the_module_imports_without_the_worker_runtime():
    # `js` only exists inside the Worker; the module must stay importable so these
    # tests - and mypy - can reach the pure helpers.
    assert "js" not in dir(github_app)
