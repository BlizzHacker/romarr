"""Who is allowed to ask ROMarr to do something.

There was nothing here, and `_guard` -- the only thing that looked like it
might be a gate -- is an exception handler. So every endpoint answered anybody
who could reach the port, including the ones that queue a torrent, write into
the library and rewrite settings. On a home LAN that is one curl from a guest
network or a compromised IoT device.

**Secure by default, with one deliberate way out.** A key is generated on
first run rather than left blank, because an install that is insecure until
somebody reads the documentation is insecure. The way out is
`ROMARR_AUTH=disabled`, for the real case of an install already sitting behind
an authenticating reverse proxy -- asking that operator to carry a second
credential past a proxy that already established identity is friction with no
security gain. It is a setting, never a default, and crucially never something
a *missing* key degrades into: `test_off_is_only_ever_explicit` fails if that
ever changes.

Three ways to present a credential, because the senders differ:

  * `X-Api-Key`, which is what the other *arrs use and what a script will send.
  * `Authorization: Bearer`, for anything speaking generic HTTP.
  * `?apikey=`, for senders that cannot set a header at all. A request
    front-end posting a notification is the case that forces this, and the
    alternative was leaving the one endpoint that queues downloads open.

A browser gets a signed session cookie instead, so the key is presented once
rather than sitting in every request and in the page.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import secrets
import time

log = logging.getLogger(__name__)

#: Cookie a browser session lives in.
SESSION_COOKIE = "romarr_session"

#: How long a browser session lasts before the operator logs in again.
SESSION_SECONDS = 14 * 24 * 3600

#: `ROMARR_AUTH` set to this, and only this, turns the gate off.
DISABLED = "disabled"

#: scrypt parameters. Deliberately not a bare hash: a ROMarr password is
#: chosen by a person and will be short, so the cost has to come from the
#: derivation rather than from the input.
_SCRYPT_N, _SCRYPT_R, _SCRYPT_P = 2 ** 14, 8, 1


def new_api_key() -> str:
    """A key worth generating. 32 hex characters, from the CSPRNG."""
    return secrets.token_hex(16)


class Auth:
    """The gate. Holds no state a request can change."""

    def __init__(self, api_key: str = "", password_hash: str = "",
                 enabled: bool = True,
                 session_seconds: int = SESSION_SECONDS):
        self.api_key = api_key or ""
        self.password_hash = password_hash or ""
        self.enabled = enabled
        self.session_seconds = session_seconds

    # -- credentials --------------------------------------------------------

    def check_key(self, presented: str | None) -> bool:
        """Whether `presented` is the configured key.

        The emptiness check is load-bearing and is not a shortcut for speed:
        `hmac.compare_digest("", "")` is True, so an install whose key went
        missing would authorise every request that sent no key at all. That is
        an outage that fails open, which is the worst way for one to fail.
        """
        if not self.api_key or not presented:
            return False
        return hmac.compare_digest(str(presented), self.api_key)

    def hash_password(self, password: str) -> str:
        """`scrypt$<salt>$<digest>`, salted per call."""
        salt = secrets.token_bytes(16)
        digest = hashlib.scrypt(password.encode(), salt=salt,
                                n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P)
        return (f"scrypt${base64.b64encode(salt).decode()}"
                f"${base64.b64encode(digest).decode()}")

    def check_password(self, password: str | None) -> bool:
        """Whether `password` matches the stored hash.

        No password configured means password login is *off*, not open.
        """
        if not self.password_hash or not password:
            return False
        try:
            kind, salt_b64, digest_b64 = self.password_hash.split("$", 2)
            if kind != "scrypt":
                return False
            salt = base64.b64decode(salt_b64)
            expected = base64.b64decode(digest_b64)
        except Exception:
            log.warning("stored password hash is unreadable; refusing login")
            return False
        got = hashlib.scrypt(password.encode(), salt=salt,
                             n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P)
        return hmac.compare_digest(got, expected)

    # -- sessions -----------------------------------------------------------

    def issue_session(self) -> str:
        """A signed `<expiry>.<signature>` token.

        Signed with the API key, so a session cannot be carried to another
        install and a rotated key invalidates every outstanding session --
        which is what rotating a key is for.
        """
        expires = int(time.time()) + self.session_seconds
        body = base64.urlsafe_b64encode(
            json.dumps({"exp": expires}).encode()).decode().rstrip("=")
        return f"{body}.{self._sign(body)}"

    def check_session(self, token: str | None) -> bool:
        if not token or not self.api_key or "." not in token:
            return False
        body, _, signature = token.rpartition(".")
        if not hmac.compare_digest(signature, self._sign(body)):
            return False
        try:
            padding = "=" * (-len(body) % 4)
            claims = json.loads(base64.urlsafe_b64decode(body + padding))
            return int(claims["exp"]) > time.time()
        except Exception:
            return False

    def _sign(self, body: str) -> str:
        return hmac.new(self.api_key.encode(), body.encode(),
                        hashlib.sha256).hexdigest()

    # -- the actual question a request asks ---------------------------------

    def authorised(self, headers, query, cookies) -> bool:
        """Whether this request may proceed.

        `headers` is anything with case-insensitive `.get`, or a plain dict --
        both appear, because the server hands over `http.client.HTTPMessage`
        and the tests hand over a dict.
        """
        if not self.enabled:
            return True
        presented = (_header(headers, "X-Api-Key")
                     or _bearer(_header(headers, "Authorization"))
                     or _first(query.get("apikey")))
        if self.check_key(presented):
            return True
        return self.check_session(cookies.get(SESSION_COOKIE))


def _header(headers, name: str) -> str:
    """One header, case-insensitively, from a dict or an HTTPMessage."""
    if headers is None:
        return ""
    got = headers.get(name)
    if got:
        return str(got)
    lowered = name.lower()
    for key, value in getattr(headers, "items", lambda: [])():
        if str(key).lower() == lowered and value:
            return str(value)
    return ""


def _bearer(value: str) -> str:
    parts = str(value or "").split(None, 1)
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1].strip()
    return ""


def _first(values) -> str:
    if isinstance(values, (list, tuple)):
        return str(values[0]) if values else ""
    return str(values or "")


def parse_cookies(raw: str) -> dict[str, str]:
    """`a=1; b=2` into a dict, tolerating whatever a browser actually sends."""
    out: dict[str, str] = {}
    for part in str(raw or "").split(";"):
        name, sep, value = part.strip().partition("=")
        if sep and name:
            out[name] = value
    return out
