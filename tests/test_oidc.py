"""OIDC sign-in: Authentik or any IdP, authorization-code + PKCE.

The invariants worth defending hardest, because this is a login path and a
login path that trusts the wrong thing is an account-takeover:

  * state/nonce/PKCE are generated here, checked here, never taken on faith;
  * the id_token is validated claim-by-claim (iss, aud, exp, nonce, azp);
  * a required group treats a MISSING claim as refusal, not as "no restriction";
  * the client secret is write-only through the settings API;
  * an http issuer with no signature-verifying library is refused — there is
    no trusted channel to fall back to.
"""

import base64
import hashlib
import hmac
import json
import time

import pytest

from romarr import oidc as oidc_mod
from romarr.oidc import (COOKIE_NONCE, COOKIE_STATE, COOKIE_VERIFIER,
                         OidcClient, OidcError, b64url_encode,
                         client_from_settings, generate_pkce)

ISSUER = "https://idp.example.com/application/o/romarr/"


# -- PKCE ---------------------------------------------------------------------

def test_pkce_challenge_is_the_s256_of_the_verifier():
    verifier, challenge = generate_pkce()
    expected = b64url_encode(hashlib.sha256(verifier.encode()).digest())
    assert challenge == expected
    assert len(verifier) >= 43          # RFC 7636 minimum


def test_pkce_values_are_unique_per_call():
    a = generate_pkce()
    b = generate_pkce()
    assert a[0] != b[0] and a[1] != b[1]


# -- login URL ----------------------------------------------------------------

def test_login_url_carries_every_oidc_parameter():
    client = OidcClient(issuer=ISSUER, client_id="romarr",
                        authorize_url=ISSUER + "authorize/")
    url = client.login_url("https://romarr/oidc/callback",
                           state="ST", nonce="NO", challenge="CH")
    assert url.startswith(ISSUER + "authorize/?")
    for expected in ("client_id=romarr", "response_type=code",
                     "code_challenge=CH", "code_challenge_method=S256",
                     "state=ST", "nonce=NO",
                     "redirect_uri=https%3A%2F%2Fromarr%2Foidc%2Fcallback",
                     "scope=openid+profile+email"):
        assert expected in url, expected


def test_login_url_without_any_authorize_endpoint_refuses():
    client = OidcClient(issuer=ISSUER, client_id="romarr")
    client._discovery = {}          # discovery unavailable, no manual URL
    with pytest.raises(OidcError):
        client.login_url("https://romarr/oidc/callback", "s", "n", "c")


# -- id_token building helpers ------------------------------------------------

def _hs256_token(claims: dict, secret: str, *, header=None) -> str:
    head = {"alg": "HS256", "typ": "JWT"}
    if header:
        head.update(header)
    seg = [b64url_encode(json.dumps(head).encode()),
           b64url_encode(json.dumps(claims).encode())]
    signing_input = ".".join(seg).encode()
    sig = b64url_encode(hmac.new(secret.encode(), signing_input,
                                 hashlib.sha256).digest())
    return signing_input.decode() + "." + sig


def _claims(**over) -> dict:
    now = int(time.time())
    base = {"iss": ISSUER, "aud": "romarr", "sub": "42",
            "preferred_username": "wade", "email": "wade@example.com",
            "exp": now + 300, "iat": now, "nonce": "NONCE"}
    base.update(over)
    return base


def _client(**kw) -> OidcClient:
    kw.setdefault("issuer", ISSUER)
    kw.setdefault("client_id", "romarr")
    kw.setdefault("client_secret", "sekrit")
    kw.setdefault("token_url", ISSUER + "token/")
    return OidcClient(**kw)


# -- claim validation ---------------------------------------------------------

def test_a_valid_hs256_token_verifies():
    client = _client()
    claims = client.verify_id_token(_hs256_token(_claims(), "sekrit"), "NONCE")
    assert claims["preferred_username"] == "wade"


def test_wrong_hmac_signature_is_refused():
    client = _client()
    with pytest.raises(OidcError, match="signature is invalid"):
        client.verify_id_token(_hs256_token(_claims(), "other"), "NONCE")


def test_wrong_issuer_is_refused():
    client = _client()
    bad = _hs256_token(_claims(iss="https://evil.example.com/"), "sekrit")
    with pytest.raises(OidcError, match="issuer"):
        client.verify_id_token(bad, "NONCE")


def test_wrong_audience_is_refused():
    client = _client()
    bad = _hs256_token(_claims(aud="somebody-else"), "sekrit")
    with pytest.raises(OidcError, match="audience"):
        client.verify_id_token(bad, "NONCE")


def test_an_audience_list_containing_us_is_accepted():
    client = _client()
    ok = _hs256_token(_claims(aud=["romarr", "other"]), "sekrit")
    assert client.verify_id_token(ok, "NONCE")["sub"] == "42"


def test_an_expired_token_is_refused():
    client = _client()
    bad = _hs256_token(_claims(exp=int(time.time()) - 600), "sekrit")
    with pytest.raises(OidcError, match="expired"):
        client.verify_id_token(bad, "NONCE")


def test_a_token_from_the_future_is_refused():
    client = _client()
    bad = _hs256_token(_claims(iat=int(time.time()) + 3600), "sekrit")
    with pytest.raises(OidcError, match="future"):
        client.verify_id_token(bad, "NONCE")


def test_a_mismatched_nonce_is_refused():
    """The nonce binds the token to THIS login; without the check, a captured
    token replays into any later flow."""
    client = _client()
    tok = _hs256_token(_claims(nonce="ATTACKER"), "sekrit")
    with pytest.raises(OidcError, match="nonce"):
        client.verify_id_token(tok, "NONCE")


def test_a_wrong_azp_is_refused():
    client = _client()
    bad = _hs256_token(_claims(azp="another-client"), "sekrit")
    with pytest.raises(OidcError, match="authorized party"):
        client.verify_id_token(bad, "NONCE")


def test_a_malformed_token_is_refused():
    client = _client()
    with pytest.raises(OidcError, match="malformed"):
        client.verify_id_token("not.a.jwt", "NONCE")
    with pytest.raises(OidcError, match="no id_token"):
        client.verify_id_token("", "NONCE")


# -- the no-cryptography fallback --------------------------------------------

def test_rs256_without_cryptography_over_https_falls_back_to_channel_trust(
        monkeypatch):
    """ROMarr's venv has no cryptography: an RS256 token over an https issuer
    is accepted on the TLS channel (OIDC Core §3.1.3.7) with a warning, not
    silently and not refused."""
    client = _client(token_url="https://idp.example.com/token/")

    def no_crypto(name):
        if name == "cryptography":
            raise ImportError("not installed")
        return real_import(name)

    real_import = __import__
    monkeypatch.setattr("builtins.__import__", no_crypto)
    head = b64url_encode(json.dumps({"alg": "RS256", "kid": "k"}).encode())
    payload = b64url_encode(json.dumps(_claims()).encode())
    tok = f"{head}.{payload}.c2ln"          # signature never checked here
    claims = client.verify_id_token(tok, "NONCE")
    assert claims["sub"] == "42"


def test_rs256_without_cryptography_over_http_is_refused(monkeypatch):
    client = OidcClient(issuer="http://idp.lan/o/romarr/", client_id="romarr",
                        token_url="http://idp.lan/o/romarr/token/")

    def no_crypto(name):
        if name == "cryptography":
            raise ImportError("not installed")
        return real_import(name)

    real_import = __import__
    monkeypatch.setattr("builtins.__import__", no_crypto)
    head = b64url_encode(json.dumps({"alg": "RS256"}).encode())
    payload = b64url_encode(json.dumps(
        _claims(iss="http://idp.lan/o/romarr/")).encode())
    with pytest.raises(OidcError, match="cannot verify"):
        client.verify_id_token(f"{head}.{payload}.c2ln", "NONCE")


# -- the authenticate flow ----------------------------------------------------

class _FakeTokenResponse:
    def __init__(self, body, status=200):
        self._body = body
        self.status_code = status
        self.text = json.dumps(body)

    def json(self):
        return self._body


def _patch_token_exchange(monkeypatch, client, id_token):
    def fake_post(url, data=None, **kw):
        assert data["grant_type"] == "authorization_code"
        assert data["code_verifier"] == "VERIFIER"
        assert data["client_id"] == "romarr"
        return _FakeTokenResponse({"id_token": id_token,
                                   "access_token": "at"})
    monkeypatch.setattr(client, "_session",
                        type("S", (), {"post": staticmethod(fake_post)})())


def test_authenticate_returns_an_identity(monkeypatch):
    client = _client()
    _patch_token_exchange(monkeypatch, client,
                          _hs256_token(_claims(), "sekrit"))
    ident = client.authenticate("CODE", "https://romarr/oidc/callback",
                                "VERIFIER", "ST", "ST", "NONCE")
    assert ident.ok and ident.user == "wade"
    assert ident.email == "wade@example.com"


def test_authenticate_refuses_a_state_mismatch(monkeypatch):
    client = _client()
    with pytest.raises(OidcError, match="state mismatch"):
        client.authenticate("CODE", "https://r/cb", "V", "GOT", "EXPECTED",
                            "NONCE")


def test_a_refused_token_exchange_names_the_error(monkeypatch):
    client = _client()

    def fake_post(url, data=None, **kw):
        return _FakeTokenResponse({"error": "invalid_grant",
                                   "error_description": "code expired"}, 400)

    monkeypatch.setattr(client, "_session",
                        type("S", (), {"post": staticmethod(fake_post)})())
    with pytest.raises(OidcError, match="code expired"):
        client.authenticate("CODE", "https://r/cb", "V", "S", "S", "N")


# -- group / email gating -----------------------------------------------------

def test_a_required_group_is_enforced_and_absence_refuses():
    client = _client(required_group="romarr-users")
    # No groups claim at all: refusal, not pass. Absence must never satisfy.
    with pytest.raises(OidcError, match="not in the required group"):
        client._gate("wade", "wade@example.com", ())
    with pytest.raises(OidcError, match="not in the required group"):
        client._gate("wade", "wade@example.com", ("other-group",))
    client._gate("wade", "wade@example.com", ("romarr-users",))   # passes


def test_the_email_allowlist_is_enforced_case_insensitively():
    client = _client(allowed_emails=("Wade@Example.com",))
    client._gate("wade", "wade@example.com", ())
    with pytest.raises(OidcError, match="not on the allowed email"):
        client._gate("mallory", "mallory@example.com", ())


def test_groups_are_read_from_whichever_claim_carries_them():
    assert oidc_mod._claim_groups({"groups": ["a", "b"]}) == ("a", "b")
    assert oidc_mod._claim_groups({"roles": ["admin"]}) == ("admin",)
    assert oidc_mod._claim_groups({"groups": "a,b|c"}) == ("a", "b", "c")
    assert oidc_mod._claim_groups({}) == ()


# -- settings -> client -------------------------------------------------------

def test_client_from_settings_is_none_until_configured():
    assert client_from_settings({}, {}) is None
    assert client_from_settings({"oidc_enabled": True}, {}) is None  # no issuer


def test_client_from_settings_reads_settings_and_env_wins():
    settings = {"oidc_enabled": True, "oidc_issuer": ISSUER,
                "oidc_client_id": "romarr", "oidc_required_group": "users",
                "_oidc_client_secret": "stored"}
    client = client_from_settings(settings, {})
    assert client is not None and client.client_id == "romarr"
    assert client.client_secret == "stored"
    assert client.required_group == "users"

    env = {"ROMARR_OIDC_ISSUER": "https://other.example.com/o/r/",
           "ROMARR_OIDC_CLIENT_ID": "env-client"}
    client = client_from_settings(settings, env)
    assert client.issuer == "https://other.example.com/o/r"
    assert client.client_id == "env-client"


def test_verification_can_be_explicitly_disabled():
    client = client_from_settings(
        {"oidc_enabled": True, "oidc_issuer": ISSUER,
         "oidc_client_id": "romarr", "oidc_verify_signature": False}, {})
    assert client.verify_signature is False


def test_problems_name_what_is_missing():
    assert OidcClient(issuer="", client_id="").problems()
    assert OidcClient(issuer=ISSUER, client_id="romarr").problems() == []
    assert "absolute" in " ".join(
        OidcClient(issuer="idp.example.com", client_id="x").problems())


# -- service wiring -----------------------------------------------------------

def _svc(tmp_path, **settings):
    from romarr.app import ROMarr
    svc = ROMarr(env={"ROMARR_DATA": str(tmp_path / "s.json")})
    svc.store.update_settings(settings)
    svc.reload_oidc()
    return svc


def test_the_service_builds_a_client_when_configured(tmp_path):
    svc = _svc(tmp_path, oidc_enabled=True, oidc_issuer=ISSUER,
               oidc_client_id="romarr", oidc_display_name="Authentik")
    assert svc.oidc is not None
    status = svc.oidc_status()
    assert status["enabled"] and status["display_name"] == "Authentik"
    assert "client_secret" not in json.dumps(status)   # never leaked


def test_the_service_stays_off_by_default(tmp_path):
    svc = _svc(tmp_path)
    assert svc.oidc is None
    assert svc.oidc_status() == {"enabled": False}


def test_an_incomplete_configuration_does_not_arm_a_broken_client(tmp_path):
    svc = _svc(tmp_path, oidc_enabled=True, oidc_issuer=ISSUER)  # no client id
    assert svc.oidc is None


def test_the_redirect_uri_prefers_public_url(tmp_path):
    svc = _svc(tmp_path, oidc_enabled=True, oidc_issuer=ISSUER,
               oidc_client_id="romarr", public_url="https://romarr.example.com")
    assert svc.oidc_redirect_uri("internal:7878") == \
        "https://romarr.example.com/oidc/callback"


def test_the_redirect_uri_falls_back_to_the_request_host(tmp_path):
    svc = _svc(tmp_path, oidc_enabled=True, oidc_issuer=ISSUER,
               oidc_client_id="romarr")
    assert svc.oidc_redirect_uri("romarr.lan:7878", "http") == \
        "http://romarr.lan:7878/oidc/callback"


def test_the_login_page_shows_the_button_only_when_enabled(tmp_path):
    from romarr.ui import login_page
    off = login_page(claimed=True)
    assert "/oidc/login" not in off
    on = login_page(claimed=True,
                    oidc={"enabled": True, "display_name": "Authentik"})
    assert "/oidc/login" in on and "Sign in with Authentik" in on


def test_the_login_page_escapes_a_hostile_button_label():
    from romarr.ui import login_page
    html = login_page(claimed=True, oidc={
        "enabled": True, "display_name": "<script>alert(1)</script>"})
    assert "<script>alert(1)</script>" not in html


def test_the_settings_page_exposes_the_oidc_fields():
    from romarr.ui import page
    html = page()
    for key in ("oidc_enabled", "oidc_issuer", "oidc_client_id",
                "_oidc_client_secret", "oidc_required_group",
                "oidc_allowed_emails"):
        assert key in html, key


def test_the_session_carries_the_oidc_subject(tmp_path):
    from romarr.auth import Auth, new_api_key
    auth = Auth(api_key=new_api_key())
    token = auth.issue_session(subject="wade")
    assert auth.check_session(token)
    body = token.split(".")[0]
    claims = json.loads(base64.urlsafe_b64decode(body + "=="))
    assert claims["sub"] == "wade"
    # And a subject-less session keeps the old shape and still validates.
    assert auth.check_session(auth.issue_session())
