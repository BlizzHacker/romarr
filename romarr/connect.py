"""One-click account connect, using the browser session you already have.

The point of this module is a button. Not a script, not a pasted key for
the common case -- a button on ROMarr's own page that lands you on a store
you are *already signed in to*, takes one confirming click there, and comes
back with your library connected.

That is possible because of how each store actually works, and the honest
answer differs per store rather than being one grand scheme:

  * **Steam** is a true one-click. "Sign in through Steam" is OpenID 2.0,
    an official public flow that needs no API key, no app registration and
    no secret -- the browser round-trips to Steam, Steam asserts which
    account you are, and ROMarr verifies that assertion directly with
    Steam. Combined with the public profile game list, that is a complete
    connection from one click.
  * **GOG** uses an OAuth login page whose redirect must be a URI GOG
    already trusts, so the code lands in the address bar rather than back
    at ROMarr. One click to open, one copy.
  * **PlayStation, Xbox, itch.io** issue tokens from a page inside your
    signed-in account. One click to open the exact page, one copy.

Everything here treats the outside world as untrusted: an OpenID assertion
is verified with Steam rather than believed, the identity it returns is
matched against Steam's own URL shape, and the `state`/`return_to` values
are generated here rather than accepted from a caller -- an open redirect
in a connect flow is how somebody else's account gets linked to yours.
"""

from __future__ import annotations

import logging
import re
import secrets
import urllib.parse

log = logging.getLogger(__name__)

STEAM_OPENID = "https://steamcommunity.com/openid/login"

#: Steam asserts identity as https://steamcommunity.com/openid/id/<steamid64>.
#: Anchored, and the id is digits only: this is the value that decides whose
#: library gets connected, so a loose match here is an account-takeover bug.
_STEAM_IDENTITY = re.compile(
    r"^https://steamcommunity\.com/openid/id/(\d{17})$")


def steam_login_url(return_to: str, realm: str = "") -> str:
    """Where to send the browser to sign in through Steam.

    `identifier_select` is what makes this one click: it tells Steam "let
    the user tell me who they are", so a signed-in visitor sees a single
    confirm button rather than a login form.
    """
    params = {
        "openid.ns": "http://specs.openid.net/auth/2.0",
        "openid.mode": "checkid_setup",
        "openid.return_to": return_to,
        "openid.realm": realm or return_to,
        "openid.identity": "http://specs.openid.net/auth/2.0/identifier_select",
        "openid.claimed_id": "http://specs.openid.net/auth/2.0/identifier_select",
    }
    return f"{STEAM_OPENID}?{urllib.parse.urlencode(params)}"


def steam_verify(query: dict, *, session=None) -> str:
    """The SteamID64 Steam just vouched for, or "" if it did not.

    The parameters come back through the user's browser, so they are
    attacker-controlled until Steam itself confirms them: this posts them
    straight back with `check_authentication`, which is the only step that
    makes the assertion mean anything.
    """
    import requests

    def one(key: str) -> str:
        value = query.get(key)
        if isinstance(value, list):
            return str(value[0]) if value else ""
        return str(value or "")

    claimed = one("openid.claimed_id")
    match = _STEAM_IDENTITY.match(claimed)
    if not match:
        log.warning("steam openid returned an identity that is not Steam's")
        return ""

    payload = {key: one(key) for key in query if key.startswith("openid.")}
    payload["openid.mode"] = "check_authentication"
    try:
        response = (session or requests).post(STEAM_OPENID, data=payload,
                                              timeout=30)
        response.raise_for_status()
        body = response.text
    except Exception as exc:                # noqa: BLE001
        log.warning("steam openid verification failed: %s",
                    exc.__class__.__name__)
        return ""
    if "is_valid:true" not in body:
        log.warning("steam refused to validate its own assertion")
        return ""
    return match.group(1)


#: What a person actually copies off a token page: the whole document.
#: Every one of these pages is JSON, and telling somebody to select the
#: characters between two quotes is a worse instruction than just accepting
#: the paste. Maps the field ROMarr stores to the key the store calls it.
_JSON_KEYS = {
    "npsso": ("npsso",),
    "ea_token": ("access_token",),
    "epic_code": ("authorizationCode", "code"),
    "gog_username": ("username",),
    "openxbl_key": ("app_key", "apiKey", "key"),
    "itchio_key": ("key", "api_key"),
}


def extract_value(field: str, pasted: str) -> str:
    """The credential inside whatever the user pasted.

    Accepts the bare value, the whole JSON document the page displayed, or
    a URL with the value in its query string -- because all three are
    things people genuinely paste, and refusing two of them turns a working
    flow into "it doesn't work".
    """
    import json
    import urllib.parse

    text = str(pasted or "").strip()
    if not text:
        return ""

    keys = _JSON_KEYS.get(field, (field,))

    # The whole JSON document, which is what select-all-copy produces.
    if text.startswith("{"):
        try:
            body = json.loads(text)
        except ValueError:
            body = {}
        if isinstance(body, dict):
            for key in keys:
                if body.get(key):
                    return str(body[key]).strip()

    # A URL that carries it -- Epic's code arrives this way in the address
    # bar when the redirect lands.
    if text.startswith("http"):
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(text).query)
        for key in keys:
            if query.get(key):
                return str(query[key][0]).strip()

    # A bare value, but tolerate a stray quote or trailing comma from a
    # partial selection.
    return text.strip().strip('",').strip('"').strip()


def new_state() -> str:
    """A one-shot value tying a return to the request that started it."""
    return secrets.token_urlsafe(24)


class StateStore:
    """Short-lived, single-use tokens for an OpenID round trip.

    **This is what makes the return leg work at all.** ROMarr's session
    cookie is `SameSite=Strict`, so when Steam redirects the browser back,
    the browser deliberately withholds the cookie and the request arrives
    unauthenticated -- a 401, every time, no matter how correct the rest of
    the flow is. Loosening the cookie to Lax would fix the symptom by
    weakening every other endpoint.

    So the return leg carries its own credential instead: a token minted
    here by an *authenticated* request, unguessable, usable once, and
    expiring in minutes. Presenting it proves the flow was started from a
    signed-in session, which is exactly what the cookie would have proved.
    """

    #: Long enough to sign in to Steam if you were not already, short
    #: enough that a token left in a browser history is worthless.
    TTL = 600

    def __init__(self, ttl: int | None = None):
        import threading
        self._ttl = ttl if ttl is not None else self.TTL
        self._lock = threading.Lock()
        self._issued: dict[str, float] = {}

    def issue(self) -> str:
        import time
        token = new_state()
        with self._lock:
            now = time.monotonic()
            # Opportunistic sweep: no timer, and the dict cannot grow
            # without bound because every entry expires.
            self._issued = {k: v for k, v in self._issued.items() if v > now}
            self._issued[token] = now + self._ttl
        return token

    def spend(self, token: str) -> bool:
        """True once per token, and never after it expires."""
        import time
        if not token:
            return False
        with self._lock:
            expires = self._issued.pop(token, None)
        return expires is not None and expires > time.monotonic()


# --- the stores that hand out a token from a signed-in page ------------------
#
# No OAuth here on purpose. Each of these issues a credential from a page
# inside the account you are already signed in to, so the useful thing
# ROMarr can do is take you to the exact page and accept the paste -- which
# is one click and one copy, and is honest about being that.

TOKEN_SOURCES = {
    "psn": {
        "label": "PlayStation",
        "open": "https://ca.account.sony.com/api/v1/ssocookie",
        "field": "npsso",
        "how": "Signed in to playstation.com, this page shows "
               '{"npsso":"..."}. Copy the value between the quotes.',
    },
    "xbox": {
        "label": "Xbox",
        # xbl.io/console bounces a signed-out visitor to its docs, which
        # looks like a dead button. The root page is the one with the
        # Microsoft sign-in on it.
        "open": "https://xbl.io/",
        "field": "openxbl_key",
        "how": "Click 'Login with Xbox Live' there and approve with your "
               "Microsoft account; the console then shows an API key to "
               "copy. OpenXBL is a third-party bridge because Microsoft "
               "publishes no owned-games API of its own.",
    },
    "itchio": {
        "label": "itch.io",
        "open": "https://itch.io/user/settings/api-keys",
        "field": "itchio_key",
        "how": "Generate a key on that page and copy it.",
    },
    "gog": {
        "label": "GOG",
        # gog.com/account is just the library page -- it never shows the
        # username in a copyable form, which made this button useless.
        # userData.json is what a signed-in browser gets, and the first
        # field in it is the username.
        "open": "https://embed.gog.com/userData.json",
        "field": "gog_username",
        "how": 'Signed in to GOG, that page shows {"username":"..."} near '
               "the start. Paste that username. Your profile's games list "
               "must be public (gog.com/account/settings/privacy) — that "
               "is the whole credential, there is no token.",
    },
    "epic": {
        "label": "Epic Games",
        # Hitting /id/api/redirect directly answers EULA_ACCEPTANCE on an
        # account that has not accepted the launcher terms in a browser --
        # a hard error with no way forward from that page. Going through
        # /id/login first shows the EULA if one is owed, then lands on the
        # redirect and prints the code.
        "open": "https://www.epicgames.com/id/login?redirectUrl="
                "https%3A%2F%2Fwww.epicgames.com%2Fid%2Fapi%2Fredirect"
                "%3FclientId%3D34a02cf8f4414e29b15921876da36f9a"
                "%26responseType%3Dcode",
        "field": "epic_code",
        "how": 'Sign in there; the page ends up showing '
               '{"authorizationCode":"..."}. Copy the code. If Epic asks you '
               "to accept terms first, do that and it continues. The code is "
               "single-use and expires in minutes, so paste it straight away "
               "-- ROMarr swaps it for a refresh token and never asks again.",
    },
    "ea": {
        # `prompt=none` means "only succeed if a session is already visible
        # to this request", and EA answers login_required whenever the
        # browser will not hand its session over -- which is most of the
        # time. Dropping it lets EA show its own login and then return the
        # token, which is what the page is for.
        "label": "EA",
        "open": "https://accounts.ea.com/connect/auth?client_id=ORIGIN_JS_SDK"
                "&response_type=token&redirect_uri=nucleus%3Arest"
                "&release_type=prod",
        "field": "ea_token",
        "how": 'Sign in if asked; the page then shows {"access_token":"..."}. '
               "Copy the token. EA issues no application keys, so this is "
               "the same credential Playnite uses. It is short-lived — "
               "re-paste when a sync says it expired.",
    },
    "battlenet": {
        "label": "Battle.net",
        "open": "https://account.blizzard.com/api/games-and-subs",
        "field": "battlenet_json",
        "how": "Signed in to Blizzard, that page is a plain JSON list of "
               "your games. Copy the whole document and paste it -- it is "
               "data, not a credential, so nothing secret is stored.",
    },
}
