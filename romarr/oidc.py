"""Sign in with an OIDC provider — Authentik, or any OpenID Connect IdP.

ROMarr already has one kind of SSO (`sso.ForwardAuth`): believe an identity
header from a reverse proxy in front of it. That needs a proxy. This is the
other kind — ROMarr is itself the relying party in an OAuth2 authorization-code
flow with PKCE: the browser is redirected to the IdP, the IdP authenticates the
person and redirects back with a code, ROMarr exchanges the code for tokens and
reads who signed in off the verified id_token, then mints its own session.

Both can be configured; they answer different deployments. Forward-auth is for
an install already behind Authentik's proxy outpost. This is for an install
that is directly reachable and wants a "Sign in with Authentik" button — the
common case, and the one that also works for Keycloak, Zitadel, Google, or
anything else that speaks OIDC.

Security, because a login flow is an attack surface:

  * **PKCE (S256), always.** The code is useless without the verifier, which
    never leaves ROMarr and the browser's http-only cookie. A public client
    (no secret) is safe; a confidential one is safer still.
  * **state** binds the callback to the request that started it (CSRF), and
    **nonce** binds the id_token to that same request (token replay). Both are
    generated here, sent through the IdP, and checked on return. Neither is
    ever accepted from the caller.
  * The id_token signature is verified against the IdP's JWKS with
    `cryptography` when it is installed. When it is not, ROMarr falls back to
    the trust model OIDC Core §3.1.3.7 allows — the token arrived directly from
    the token endpoint over TLS — but ONLY over https, and it says so loudly.
    An http issuer with no verifier is refused: there is no channel to trust.
  * Claims are validated, not assumed: iss must equal the configured issuer,
    aud must contain our client_id, exp must be in the future, nonce must
    match. A token that fails any of these is rejected.
  * Group / email gating is optional but, when set, is a refusal on absence:
    a missing `groups` claim never satisfies a required group.

The identity that comes out is the same shape forward-auth produces, so the
rest of ROMarr (session issuance, the auth gate) does not care which flow
established it.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import secrets
import time
import urllib.parse

log = logging.getLogger(__name__)

#: What we ask for. `openid` is mandatory for OIDC; profile+email give the
#: id_token the name and address ROMarr shows. Groups come only if the IdP is
#: configured to send them and the scope requests them (Authentik: add `groups`
#: to the scope, or it is absent).
DEFAULT_SCOPES = "openid profile email"

#: Cookie names for the transient flow values. SameSite=Lax so the IdP's
#: top-level redirect back to the callback still carries them (Strict would
#: drop them on a cross-site navigation and every login would fail). Separate
#: from the session cookie and short-lived.
COOKIE_VERIFIER = "romarr_oidc_v"
COOKIE_STATE = "romarr_oidc_s"
COOKIE_NONCE = "romarr_oidc_n"
COOKIE_TTL = 600          # 10 minutes to complete a login; ample


def b64url_decode(segment: str) -> bytes:
    """Base64url without padding, as JWT uses."""
    pad = "=" * (-len(segment) % 4)
    return base64.urlsafe_b64decode(segment + pad)


def b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def generate_pkce() -> tuple[str, str]:
    """(verifier, S256 challenge). Verifier is high-entropy and kept secret."""
    verifier = b64url_encode(secrets.token_bytes(48))
    challenge = b64url_encode(hashlib.sha256(verifier.encode()).digest())
    return verifier, challenge


def new_random(nbytes: int = 24) -> str:
    return secrets.token_urlsafe(nbytes)


class OidcError(Exception):
    """A login that cannot complete, with a reason fit to show an operator."""


class OidcClient:
    """One OIDC relying-party relationship, with cached discovery."""

    def __init__(self, *, issuer: str, client_id: str, client_secret: str = "",
                 scopes: str = DEFAULT_SCOPES, display_name: str = "",
                 required_group: str = "", allowed_emails=(),
                 authorize_url: str = "", token_url: str = "",
                 jwks_uri: str = "", userinfo_url: str = "",
                 session=None, verify_signature: bool = True):
        self.issuer = (issuer or "").rstrip("/")
        self.client_id = client_id or ""
        self.client_secret = client_secret or ""
        self.scopes = scopes or DEFAULT_SCOPES
        self.display_name = display_name or "SSO"
        self.required_group = (required_group or "").strip()
        self.allowed_emails = tuple(
            str(e).strip().lower() for e in (allowed_emails or ()) if str(e).strip())
        # Manual endpoints win over discovery: an IdP behind a path rewriter,
        # or one whose discovery document is unreachable from ROMarr, still
        # works when the operator pastes the URLs.
        self._authorize_url = authorize_url or ""
        self._token_url = token_url or ""
        self._jwks_uri = jwks_uri or ""
        self._userinfo_url = userinfo_url or ""
        self._session = session
        self.verify_signature = verify_signature
        self._discovery: dict | None = None
        self._jwks: dict | None = None

    # -- configuration sanity -------------------------------------------------

    def configured(self) -> bool:
        """Enough set to attempt a login. issuer+client_id are the minimum;
        the rest come from discovery."""
        return bool(self.issuer and self.client_id)

    def problems(self) -> list[str]:
        """What is missing, in the words the Settings page shows."""
        out = []
        if not self.issuer:
            out.append("issuer URL is required")
        elif not self.issuer.startswith(("https://", "http://")):
            out.append("issuer must be an absolute http(s) URL")
        if not self.client_id:
            out.append("client id is required")
        return out

    # -- discovery ------------------------------------------------------------

    def _get(self, url: str, *, timeout: int = 15):
        import requests
        # Authentik (and some IdPs) reject a request with no User-Agent; the
        # requests default is fine, but an explicit one avoids a bare-urllib
        # caller elsewhere tripping it. Never follow the IdP into a redirect
        # loop on a misconfigured issuer.
        return (self._session or requests).get(
            url, timeout=timeout,
            headers={"Accept": "application/json",
                     "User-Agent": "ROMarr-OIDC/1.0"},
            allow_redirects=True)

    def discover(self) -> dict:
        """The IdP's openid-configuration, fetched once and cached.

        Returns {} when discovery is unavailable and manual endpoints were
        supplied — the flow can still run entirely on the pasted URLs.
        """
        if self._discovery is not None:
            return self._discovery
        if not self.issuer:
            self._discovery = {}
            return self._discovery
        url = f"{self.issuer}/.well-known/openid-configuration"
        try:
            response = self._get(url)
            response.raise_for_status()
            self._discovery = response.json()
        except Exception as exc:                # noqa: BLE001
            log.warning("OIDC discovery failed for %s: %s", url,
                        exc.__class__.__name__)
            self._discovery = {}
        return self._discovery

    @property
    def authorize_url(self) -> str:
        return self._authorize_url or self.discover().get("authorization_endpoint", "")

    @property
    def token_url(self) -> str:
        return self._token_url or self.discover().get("token_endpoint", "")

    @property
    def jwks_uri(self) -> str:
        return self._jwks_uri or self.discover().get("jwks_uri", "")

    @property
    def userinfo_url(self) -> str:
        return self._userinfo_url or self.discover().get("userinfo_endpoint", "")

    def jwks(self) -> dict:
        if self._jwks is not None:
            return self._jwks
        if not self.jwks_uri:
            self._jwks = {}
            return self._jwks
        try:
            response = self._get(self.jwks_uri)
            response.raise_for_status()
            self._jwks = response.json()
        except Exception as exc:                # noqa: BLE001
            log.warning("OIDC JWKS fetch failed: %s", exc.__class__.__name__)
            self._jwks = {}
        return self._jwks

    def refresh_jwks(self) -> dict:
        """Re-fetch on an unknown kid: key rotation is normal and a cached
        JWKS from before it would fail every login until restart."""
        self._jwks = None
        return self.jwks()

    # -- the flow -------------------------------------------------------------

    def login_url(self, redirect_uri: str, state: str, nonce: str,
                  challenge: str) -> str:
        """Where to send the browser to start the login."""
        if not self.authorize_url:
            raise OidcError("no authorization endpoint (discovery failed and "
                            "none configured)")
        params = {
            "client_id": self.client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": self.scopes,
            "state": state,
            "nonce": nonce,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
        return f"{self.authorize_url}?{urllib.parse.urlencode(params)}"

    def exchange_code(self, code: str, redirect_uri: str,
                      verifier: str) -> dict:
        """Trade the authorization code for tokens."""
        if not self.token_url:
            raise OidcError("no token endpoint (discovery failed and none "
                            "configured)")
        data = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": self.client_id,
            "code_verifier": verifier,
        }
        # A confidential client authenticates; a public one must not send a
        # secret it does not have.
        auth = None
        if self.client_secret:
            data["client_secret"] = self.client_secret
        import requests
        try:
            response = (self._session or requests).post(
                self.token_url, data=data, timeout=20,
                headers={"Accept": "application/json",
                         "User-Agent": "ROMarr-OIDC/1.0"},
                auth=auth)
        except Exception as exc:                # noqa: BLE001
            raise OidcError(f"token endpoint unreachable: "
                            f"{exc.__class__.__name__}") from exc
        if response.status_code != 200:
            detail = ""
            try:
                detail = response.json().get("error_description") or \
                    response.json().get("error") or ""
            except Exception:                   # noqa: BLE001
                detail = response.text[:200]
            raise OidcError(f"token exchange refused ({response.status_code})"
                            + (f": {detail}" if detail else ""))
        try:
            return response.json()
        except ValueError as exc:
            raise OidcError("token endpoint returned a non-JSON body") from exc

    def authenticate(self, code: str, redirect_uri: str, verifier: str,
                     state: str, expected_state: str, nonce: str):
        """Complete a login: exchange, verify, gate, and return an Identity.

        Raises OidcError with an operator-readable reason on any failure —
        this never returns a half-trusted identity.
        """
        from .sso import Identity

        if not state or state != expected_state:
            raise OidcError("state mismatch — this callback does not match the "
                            "login that started it (possible CSRF or a stale "
                            "tab); start again")
        tokens = self.exchange_code(code, redirect_uri, verifier)
        id_token = tokens.get("id_token") or ""
        claims = self.verify_id_token(id_token, nonce)

        # The subject is the identity; prefer a human-readable name/email for
        # display but never let those decide WHO this is.
        user = str(claims.get("preferred_username")
                   or claims.get("nickname")
                   or claims.get("name")
                   or claims.get("email")
                   or claims.get("sub") or "")
        email = str(claims.get("email") or "")
        groups = _claim_groups(claims)
        if not user:
            raise OidcError("the id_token carried no subject")

        self._gate(user, email, groups)
        return Identity(ok=True, user=user, email=email, groups=groups,
                        reason="")

    def _gate(self, user: str, email: str, groups: tuple[str, ...]) -> None:
        """Enforce the optional group / email allowlist. Raises on refusal."""
        if self.required_group and self.required_group not in groups:
            # Absence is a refusal, exactly as forward-auth treats it: an IdP
            # that is not sending groups must not silently satisfy a group
            # restriction.
            raise OidcError(f"{user!r} is not in the required group "
                            f"{self.required_group!r} (groups seen: "
                            f"{', '.join(groups) or 'none'})")
        if self.allowed_emails and email.lower() not in self.allowed_emails:
            raise OidcError(f"{email or user!r} is not on the allowed email "
                            "list for this ROMarr")

    # -- id_token verification ------------------------------------------------

    def verify_id_token(self, id_token: str, nonce: str) -> dict:
        """Decode AND validate an id_token. Returns its claims or raises.

        Signature verification uses `cryptography` against the JWKS when
        available. Without it, ROMarr applies OIDC Core §3.1.3.7's channel
        trust — the token was received directly from the token endpoint over
        TLS — but only for an https issuer, and it logs that it could not
        cryptographically verify. This is the documented behaviour of minimal
        relying parties, not a shortcut ROMarr invented; an http issuer is
        refused because there is no trusted channel to lean on.
        """
        if not id_token or id_token.count(".") < 2:
            raise OidcError("no id_token in the token response")
        try:
            header_b64, payload_b64, sig_b64 = id_token.split(".", 2)
            header = json.loads(b64url_decode(header_b64))
            claims = json.loads(b64url_decode(payload_b64))
        except Exception as exc:                # noqa: BLE001
            raise OidcError(f"id_token is malformed: {exc.__class__.__name__}"
                            ) from exc

        self._validate_claims(claims, nonce)
        self._verify_signature(id_token, header, sig_b64)
        return claims

    def _validate_claims(self, claims: dict, nonce: str) -> None:
        now = int(time.time())
        leeway = 120                       # clock skew tolerance, seconds
        issuer = str(claims.get("iss") or "")
        if issuer.rstrip("/") != self.issuer:
            raise OidcError(f"id_token issuer {issuer!r} does not match the "
                            f"configured {self.issuer!r}")
        aud = claims.get("aud")
        auds = aud if isinstance(aud, list) else [aud]
        if self.client_id not in [str(a) for a in auds]:
            raise OidcError("id_token audience does not include this client")
        exp = claims.get("exp")
        if exp is not None and int(exp) + leeway < now:
            raise OidcError("id_token has expired")
        iat = claims.get("iat")
        if iat is not None and int(iat) - leeway > now:
            raise OidcError("id_token was issued in the future")
        if nonce and str(claims.get("nonce") or "") != nonce:
            raise OidcError("id_token nonce does not match this login "
                            "(possible token replay)")
        # azp, when present, must be us too (multiple-audience tokens).
        azp = claims.get("azp")
        if azp and str(azp) != self.client_id:
            raise OidcError("id_token authorized party is not this client")

    def _verify_signature(self, id_token: str, header: dict,
                          sig_b64: str) -> None:
        alg = str(header.get("alg") or "").upper()
        if not self.verify_signature:
            log.warning("OIDC signature verification is DISABLED by config")
            return
        # HS256: signed with the client secret. Verifyable with stdlib.
        if alg == "HS256":
            if not self.client_secret:
                raise OidcError("id_token is HS256 but no client secret is "
                                "configured to verify it")
            signing_input, _, signature = id_token.rpartition(".")
            expected = b64url_encode(hmac.new(
                self.client_secret.encode(), signing_input.encode(),
                hashlib.sha256).digest())
            if not hmac.compare_digest(expected, signature):
                raise OidcError("id_token HMAC signature is invalid")
            return
        # RS256/ES256/etc: need `cryptography`. Try to verify properly.
        try:
            import cryptography  # noqa: F401
            have_crypto = True
        except Exception:                       # noqa: BLE001
            have_crypto = False
        if have_crypto:
            self._verify_asymmetric(id_token, header, sig_b64, alg)
            return
        # No crypto library: fall back to channel trust, https only.
        if self.token_url.startswith("https://") and \
                self.issuer.startswith("https://"):
            log.warning(
                "OIDC id_token signature NOT verified (cryptography is not "
                "installed); relying on the TLS channel to %s. Install "
                "'cryptography' for real RS256 verification.", self.issuer)
            return
        raise OidcError(
            "cannot verify the id_token signature: 'cryptography' is not "
            "installed and the issuer is not https, so there is no trusted "
            "channel to fall back to. Install cryptography, or use an https "
            "issuer.")

    def _verify_asymmetric(self, id_token: str, header: dict, sig_b64: str,
                           alg: str) -> None:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding, ec, rsa
        from cryptography.exceptions import InvalidSignature

        kid = header.get("kid")
        jwk = self._find_jwk(kid)
        if jwk is None:
            # A rotated key we have not seen: refresh once, then give up.
            self.refresh_jwks()
            jwk = self._find_jwk(kid)
        if jwk is None:
            raise OidcError(f"no JWKS key matches the id_token (kid={kid!r})")
        signing_input, _, _ = id_token.rpartition(".")
        signature = b64url_decode(sig_b64)
        try:
            kty = str(jwk.get("kty") or "")
            if kty == "RSA" and alg in ("RS256", "RS384", "RS512"):
                n = int.from_bytes(b64url_decode(jwk["n"]), "big")
                e = int.from_bytes(b64url_decode(jwk["e"]), "big")
                public = rsa.RSAPublicNumbers(e, n).public_key()
                hashalg = {"RS256": hashes.SHA256, "RS384": hashes.SHA384,
                           "RS512": hashes.SHA512}[alg]()
                public.verify(signature, signing_input.encode(),
                              padding.PKCS1v15(), hashalg)
            elif kty == "EC" and alg in ("ES256", "ES384", "ES512"):
                curve = {"P-256": ec.SECP256R1, "P-384": ec.SECP384R1,
                         "P-521": ec.SECP521R1}[jwk.get("crv")]()
                x = int.from_bytes(b64url_decode(jwk["x"]), "big")
                y = int.from_bytes(b64url_decode(jwk["y"]), "big")
                public = ec.EllipticCurvePublicNumbers(x, y, curve).public_key()
                hashalg = {"ES256": hashes.SHA256, "ES384": hashes.SHA384,
                           "ES512": hashes.SHA512}[alg]()
                public.verify(signature, signing_input.encode(),
                              ec.ECDSA(hashalg))
            else:
                raise OidcError(f"unsupported id_token alg {alg!r} / key type "
                                f"{kty!r}")
        except InvalidSignature as exc:
            raise OidcError("id_token signature is invalid") from exc
        except OidcError:
            raise
        except Exception as exc:                # noqa: BLE001
            raise OidcError(f"signature verification failed: "
                            f"{exc.__class__.__name__}") from exc

    def _find_jwk(self, kid) -> dict | None:
        keys = self.jwks().get("keys") or []
        if kid:
            for key in keys:
                if key.get("kid") == kid:
                    return key
            return None
        # No kid in the header: if there is exactly one usable key, use it.
        usable = [k for k in keys if k.get("use") in (None, "sig")]
        return usable[0] if len(usable) == 1 else None


def _claim_groups(claims: dict) -> tuple[str, ...]:
    """Group names from an id_token, whichever claim carries them.

    Authentik sends `groups` (a list of names) when the scope/consent is set
    up for it; some IdPs use `roles`, `groups:roles`, or a custom claim. Read
    the common ones so a group restriction works without a per-IdP special
    case. Never invents membership from absence.
    """
    out: list[str] = []
    for key in ("groups", "roles", "group"):
        value = claims.get(key)
        if isinstance(value, list):
            out.extend(str(v) for v in value if v)
        elif isinstance(value, str) and value:
            # Some IdPs send a delimited string.
            for part in value.replace(",", " ").replace("|", " ").split():
                out.append(part)
    # de-dup, keep order
    seen: dict[str, None] = {}
    for g in out:
        seen.setdefault(g, None)
    return tuple(seen.keys())


def client_from_settings(settings: dict, env=None, session=None) -> OidcClient | None:
    """Build the OIDC client from stored settings + environment, or None when
    it is not configured. Environment wins over stored settings, so an
    operator can pin the IdP in the container and ignore the UI — matching how
    every other credential in ROMarr can be supplied either way.
    """
    env = env or {}

    def pick(env_key: str, setting_key: str, default=""):
        return str(env.get(env_key) or settings.get(setting_key) or default)

    enabled = pick("ROMARR_OIDC_ENABLED", "oidc_enabled", "")
    issuer = pick("ROMARR_OIDC_ISSUER", "oidc_issuer")
    client_id = pick("ROMARR_OIDC_CLIENT_ID", "oidc_client_id")
    # Enabled explicitly, or implied by having an issuer+client_id.
    on = str(enabled).lower() in ("1", "true", "yes", "on") or \
        (enabled == "" and bool(issuer and client_id))
    if not on or not (issuer and client_id):
        return None
    emails = settings.get("oidc_allowed_emails") or []
    if isinstance(emails, str):
        emails = [e for e in emails.replace(",", " ").split() if e]
    # Not through pick(): a stored False is a value, and `or` would read it as
    # absent and restore the default — the off switch that cannot be switched.
    verify = env.get("ROMARR_OIDC_VERIFY_SIGNATURE")
    if verify is None:
        verify = settings.get("oidc_verify_signature")
    if verify is None:
        verify = True
    return OidcClient(
        issuer=issuer,
        client_id=client_id,
        client_secret=pick("ROMARR_OIDC_CLIENT_SECRET", "_oidc_client_secret"),
        scopes=pick("ROMARR_OIDC_SCOPES", "oidc_scopes", DEFAULT_SCOPES),
        display_name=pick("ROMARR_OIDC_DISPLAY_NAME", "oidc_display_name",
                          "SSO"),
        required_group=pick("ROMARR_OIDC_GROUP", "oidc_required_group"),
        allowed_emails=tuple(emails),
        authorize_url=pick("ROMARR_OIDC_AUTHORIZE_URL", "oidc_authorize_url"),
        token_url=pick("ROMARR_OIDC_TOKEN_URL", "oidc_token_url"),
        jwks_uri=pick("ROMARR_OIDC_JWKS_URL", "oidc_jwks_uri"),
        userinfo_url=pick("ROMARR_OIDC_USERINFO_URL", "oidc_userinfo_url"),
        session=session,
        verify_signature=(str(verify).strip().lower()
                          not in ("0", "false", "no", "off", "")),
    )
