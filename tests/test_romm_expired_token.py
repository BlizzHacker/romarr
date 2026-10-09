"""RomM 5.3.1 answers an expired OAuth bearer token with HTTP 500, not 401.

Issue #39. An unhandled `OAuthCredentialsException` in RomM's
`HybridAuthBackend` (rommapp/romm#4788) escapes Starlette's auth middleware, so
every endpoint -- even `/api/heartbeat` -- returns a bare 500 for a bearer token
that no longer validates. ROMarr re-authenticated only on 401, kept sending the
dead token, and the library read `HTTP 500` until restart.

The 500 carries no body worth reading ("Internal Server Error"), so the
detection is by behaviour: the unauthenticated heartbeat is healthy, and the
same heartbeat WITH our token is the 500. A server that is really broken fails
the first probe and is left alone.
"""

from __future__ import annotations

import base64
import json
import time

from romarr.clients import Romm, RommConfig


class Resp:
    def __init__(self, status, body=None, text=""):
        self.status_code = status
        self._body = body if body is not None else {}
        self.text = text or json.dumps(self._body)

    @property
    def ok(self):
        return self.status_code < 400

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            raise requests.HTTPError(response=self)


def jwt(exp):
    def b64(d):
        return base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()
    return f"{b64({'alg': 'HS256'})}.{b64({'sub': 'u', 'exp': exp})}.sig"


class Romm531:
    """RomM 5.3.1's behaviour towards bearer tokens, as #4788 describes it."""

    def __init__(self, *, broken=False, always_reject=False, lifetimes=None, jwt=True):
        self.jwt = jwt
        self.valid = set()
        self.broken = broken            # the whole server is down (500 everywhere)
        self.always_reject = always_reject
        self.lifetimes = list(lifetimes or [])
        self.issued = 0
        self.gets = []

    def post(self, url, **kw):
        assert url.endswith("/api/token")
        self.issued += 1
        exp = self.lifetimes.pop(0) if self.lifetimes else time.time() + 1800
        token = jwt(exp) if self.jwt else f"t{self.issued}"
        if not self.always_reject:
            self.valid.add(token)
        return Resp(200, {"access_token": token})

    def get(self, url, **kw):
        auth = (kw.get("headers") or {}).get("Authorization", "")
        self.gets.append((url.split("/api/", 1)[1].split("?")[0], bool(auth)))
        if self.broken:
            return Resp(500, text="Internal Server Error")
        if auth and auth.split(" ", 1)[1] not in self.valid:
            return Resp(500, text="Internal Server Error")      # the bug
        if url.endswith("/api/heartbeat"):
            return Resp(200, {"SYSTEM": {}})
        if not auth:
            return Resp(401, {"detail": "Not authenticated"})
        return Resp(200, {"total": 3, "items": []})


def romm(session, **cfg):
    cfg = {"username": "u", "password": "p", **cfg}
    return Romm(RommConfig(base_url="http://romm", **cfg), session=session)


def _expire_everything_issued(server, client):
    server.valid.clear()


def test_an_expired_token_that_romm_answers_with_500_is_replaced_and_retried():
    server = Romm531()
    client = romm(server)
    assert client.count() == 3            # logs in, reads
    _expire_everything_issued(server, client)
    assert client.count() == 3            # 500 -> re-auth -> retry succeeds
    assert server.issued == 2


def test_a_server_that_is_genuinely_down_is_not_mistaken_for_an_expired_token():
    server = Romm531(broken=True)
    client = romm(server)
    client.token()
    before = server.issued
    response = client._get("http://romm/api/roms", {}, 5)
    assert response.status_code == 500
    assert server.issued == before, "a real outage must not trigger a login storm"


def test_a_500_is_not_retried_when_a_fixed_token_was_configured():
    """A configured `rmm_` client token cannot be re-issued, so there is nothing
    to re-authenticate with and a 500 is just a 500."""
    server = Romm531()
    client = romm(server, username="", password="", api_token="rmm_abc")
    response = client._get("http://romm/api/roms", {}, 5)
    assert response.status_code == 500
    assert server.issued == 0


def test_re_authentication_happens_once_and_never_loops():
    server = Romm531(always_reject=True)      # every token RomM issues is refused
    client = romm(server)
    response = client._get("http://romm/api/roms", {}, 5)
    assert response.status_code == 500
    assert server.issued == 2, "one login to start, one retry, then stop"
    roms = [g for g in server.gets if g[0] == "roms"]
    assert len(roms) == 2


def test_a_token_close_to_expiry_is_refreshed_before_it_is_ever_rejected():
    now = time.time()
    server = Romm531(lifetimes=[now + 20, now + 1800])
    client = romm(server)
    assert client.count() == 3            # logs in; the token has 20 s left
    assert client.count() == 3            # swapped before it can be refused
    assert server.issued == 2, "the 20-second token was swapped before use"
    # and the request that carried it never saw a 500
    assert all(path != "heartbeat" for path, _ in server.gets)


def test_an_unreadable_token_is_left_alone():
    server = Romm531(jwt=False)           # opaque tokens, no exp claim
    client = romm(server)
    client.count()
    client.count()
    assert server.issued == 1
