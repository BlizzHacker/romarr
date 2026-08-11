"""Import lists: hand ROMarr a list, let the clock do the asking.

Radarr calls these Import Lists -- an IMDb watchlist, a Trakt list -- and the
idea translates to ROMs better than it does to films, because ROM lists
already exist everywhere: "top 100 SNES games" articles, homebrew catalogue
dumps, a friend's spreadsheet, the text file beside a 1G1R set. Questarr's
one list is a Steam wishlist; this is the general case. (Full sets and 1G1R
stay with `collections.py`, which plans them from DATs -- a curated list and
a complete set are different promises.)

A list is stored configuration, like a download client: a name, a default
platform, and either pasted text or a URL fetched on the scheduler. Syncing
a list adds its titles to Wanted; the missing-search backoff does the rest.

**Each title is added once, ever.** A list is an instruction to acquire, not
a state to enforce: once a title has been added -- and later imported and
therefore removed from Wanted -- a re-sync must not resurrect it, or every
list becomes a slow loop re-downloading its own history. The ledger of what
each list already added is part of the list's stored state.

Parsing favours the shape lists actually arrive in. "Top 100" articles
number their entries; people paste them numbered. Comments and blank lines
are what text files contain. A tab or " - " splits title from platform for
mixed-platform lists, and a line's platform beats the list's default.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

log = logging.getLogger(__name__)


#: Symbols a storefront puts in a title and an indexer never does. Searching
#: for "World of Warcraft®" matches nothing anywhere, so every connector
#: that reads a store's own display name runs it through here first.
_LEGAL_MARKS = str.maketrans("", "", "®™©")


def clean_title(name: str) -> str:
    """A store's display title, as an indexer would ever see it written."""
    cleaned = str(name or "").translate(_LEGAL_MARKS)
    # Collapse the double spaces stripping a symbol can leave behind.
    return re.sub(r"\s{2,}", " ", cleaned).strip()


@dataclass(frozen=True)
class ListEntry:
    game: str
    platform: str = ""   # empty means "use the list's default"

    @property
    def key(self) -> str:
        return f"{self.platform}/{self.game.strip().lower()}"


#: "1. Super Metroid", "42) Chrono Trigger", "#3 Earthbound", "10 - F-Zero".
_NUMBERED = re.compile(r"^\s*#?\d{1,4}\s*[.)\-:]?\s+")

#: A comment is a `#` that is not a rank: a whole line starting `# ...`, or
#: an inline ` # ...`. `#3 Earthbound` is a rank and stays.
_COMMENT = re.compile(r"(?:^#(?!\d)|\s#).*$")


def parse_list(text: str) -> list[ListEntry]:
    """Titles out of the text people actually paste.

    One title per line. `#` opens a comment. A tab, ` | `, or ` - ` after
    the title names that line's platform. Leading ranking numbers are
    stripped, because "top 100" lists arrive numbered and nobody should have
    to clean 100 lines by hand to use one.
    """
    entries: list[ListEntry] = []
    seen: set[str] = set()
    for raw in (text or "").splitlines():
        line = _COMMENT.sub("", raw).strip()
        if not line:
            continue
        line = _NUMBERED.sub("", line).strip()
        if not line:
            continue
        platform = ""
        for sep in ("\t", " | ", " - "):
            if sep in line:
                candidate, rest = line.rsplit(sep, 1)
                # Only take the split when the right side looks like a
                # platform name, not a subtitle: "Ecco - The Tides of Time"
                # must stay one title. Platform names are short and carry no
                # digits-with-colon shapes; three words is the ceiling
                # ("Super Nintendo Entertainment System" arrives as SNES).
                rest = rest.strip()
                if rest and len(rest.split()) <= 3 and not rest[0].isdigit():
                    line, platform = candidate.strip(), rest
                break
        if not line:
            continue
        entry = ListEntry(game=line, platform=platform)
        if entry.key in seen:
            continue
        seen.add(entry.key)
        entries.append(entry)
    return entries


def fetch_entries(cfg: dict, *, session=None) -> list[ListEntry]:
    """The entries a stored list currently names.

    A `paste` list reads its stored content; the others fetch. A fetch
    failure raises so the caller can report WHICH list failed -- swallowing
    it here would make an expired URL look like an empty list, and an empty
    list looks like success.
    """
    kind = str(cfg.get("type") or "paste").lower()
    if kind == "paste":
        return parse_list(cfg.get("content") or "")
    if kind == "url":
        import requests
        url = str(cfg.get("url") or "").strip()
        if not url:
            return []
        response = (session or requests).get(url, timeout=30)
        response.raise_for_status()
        return parse_list(response.text)
    if kind == "steam":
        return _steam_entries(cfg, session=session)
    if kind == "gog":
        return _gog_entries(cfg, session=session)
    if kind == "xbox":
        return _xbox_entries(cfg, session=session)
    if kind == "psn":
        return _psn_entries(cfg, session=session)
    if kind == "itchio":
        return _itchio_entries(cfg, session=session)
    if kind == "epic":
        return _epic_entries(cfg, session=session)
    if kind == "ea":
        return _ea_entries(cfg, session=session)
    if kind == "battlenet":
        return _battlenet_entries(cfg, session=session)
    if kind == "humble":
        return _humble_entries(cfg, session=session)
    raise ValueError(f"unknown list type {kind!r}")


# --- Epic, EA, Battle.net ----------------------------------------------------
#
# ROMarr's README used to say these three "have no usable web API". That was
# wrong, and Playnite and LaunchBox were the standing counter-example: they
# have pulled *owned* libraries -- not just installed games -- from all
# three for years. Each does have a web API; what none of them has is an
# API key you can request. They authenticate with the session you already
# have in your browser, which is why the shape here is "open the page you
# are signed in to, copy what it shows you".
#
# So the credential ROMarr stores is the same one Playnite stores, obtained
# the same way, and every one of these is a real remote library sync rather
# than a local file scan.

#: Epic's launcher OAuth client. Public and unchanged for years -- it is
#: what Legendary, Heroic and Playnite all authenticate as, because Epic
#: issues no per-application credentials for this.
EPIC_CLIENT = "34a02cf8f4414e29b15921876da36f9a"
EPIC_SECRET = "daafbccc737745039dffe53d94fc76cf"
EPIC_TOKEN = ("https://account-public-service-prod03.ol.epicgames.com"
              "/account/api/oauth/token")
EPIC_LIBRARY = ("https://library-service.live.use1a.on.epicgames.com"
                "/library/api/public/items")
#: Where a signed-in browser is sent to mint an authorization code.
EPIC_CODE_PAGE = ("https://www.epicgames.com/id/api/redirect"
                  f"?clientId={EPIC_CLIENT}&responseType=code")


def _epic_token(cfg: dict, http) -> tuple[str, str]:
    """An access token, from a stored refresh token or a fresh auth code.

    Epic's authorization code is single-use and dies in minutes, so it is
    exchanged once and the refresh token that comes back is what persists.
    The caller writes the new refresh token back, which is why this returns
    both.
    """
    import base64

    auth = base64.b64encode(f"{EPIC_CLIENT}:{EPIC_SECRET}".encode()).decode()
    headers = {"Authorization": f"basic {auth}",
               "Content-Type": "application/x-www-form-urlencoded"}
    refresh = str(cfg.get("epic_refresh") or "").strip()
    code = str(cfg.get("epic_code") or "").strip()
    if refresh:
        data = {"grant_type": "refresh_token", "refresh_token": refresh}
    elif code:
        data = {"grant_type": "authorization_code", "code": code}
    else:
        return "", ""
    response = http.post(EPIC_TOKEN, data=data, headers=headers, timeout=30)
    if getattr(response, "status_code", 200) >= 400:
        # Epic names its refusals, and the two common ones need opposite
        # advice -- a live run pasted two fresh codes and was told both
        # times that the code was stale, when the account actually owed a
        # EULA acceptance.
        try:
            error = response.json() or {}
        except Exception:
            error = {}
        action = str((error.get("metadata") or {}).get("correctiveAction")
                     or "")
        if "corrective_action" in str(error.get("errorCode") or ""):
            if action == "EULA_ACCEPTANCE":
                raise ValueError(
                    "Epic says this account has updated terms to accept "
                    "(EULA), and the token API refuses everything until "
                    "then. Sign in at epicgames.com and accept the prompt "
                    "-- opening the Epic Games Launcher once also does it "
                    "-- then paste a fresh code.")
            raise ValueError(
                f"Epic requires corrective action on this account first: "
                f"{action or 'see epicgames.com'}. Resolve it there, then "
                "paste a fresh code.")
        raise ValueError(
            "Epic refused that code. It is single-use and expires in "
            "minutes -- open the code page again and paste a fresh one.")
    body = response.json() or {}
    return (str(body.get("access_token") or ""),
            str(body.get("refresh_token") or ""))


def _epic_entries(cfg: dict, *, session=None) -> list[ListEntry]:
    import requests
    http = session or requests
    access, refresh = _epic_token(cfg, http)
    if not access:
        return []
    # Handed back so the caller can persist it: the next sync uses the
    # refresh token and never asks for a code again.
    cfg["epic_refresh"] = refresh or cfg.get("epic_refresh", "")

    out: list[ListEntry] = []
    cursor, pages = "", 0
    while pages < 20:
        params = {"includeMetadata": "true"}
        if cursor:
            params["cursor"] = cursor
        response = http.get(EPIC_LIBRARY, params=params,
                            headers={"Authorization": f"bearer {access}"},
                            timeout=30)
        response.raise_for_status()
        body = response.json() or {}
        for record in body.get("records") or []:
            name = str(record.get("sandboxName")
                       or (record.get("metadata") or {}).get("title")
                       or "").strip()
            if name:
                out.append(ListEntry(game=clean_title(name)))
        cursor = str((body.get("responseMetadata") or {}).get("nextCursor") or "")
        pages += 1
        if not cursor:
            break
    return out


#: EA's own JS SDK client, the one accounts.ea.com issues browser tokens to.
EA_AUTH_PAGE = ("https://accounts.ea.com/connect/auth?client_id=ORIGIN_JS_SDK"
                "&response_type=token&redirect_uri=nucleus%3Arest"
                "&prompt=none&release_type=prod")
EA_IDENTITY = "https://gateway.ea.com/proxy/identity/pids/me"
EA_ENTITLEMENTS = ("https://api1.origin.com/ecommerce2/"
                   "consolidatedentitlements/{pid}?machine_hash=1")


def _ea_entries(cfg: dict, *, session=None) -> list[ListEntry]:
    """Owned EA titles, via the same entitlements API Playnite uses.

    The access token comes from EA's own auth endpoint in a signed-in
    browser -- EA issues no application keys, so this is the only
    credential that exists for it.
    """
    import requests
    http = session or requests
    token = str(cfg.get("ea_token") or "").strip()
    if not token:
        return []
    # tokeninfo is the public way from a token to the account id, and the
    # entitlements API is Origin-era: it wants the token in an `authtoken`
    # header with Origin's own Accept string, not a Bearer -- sending
    # Bearer is a 401 on a perfectly good token, which a live run hit.
    response = http.get("https://accounts.ea.com/connect/tokeninfo",
                        params={"access_token": token}, timeout=30)
    if getattr(response, "status_code", 200) >= 400:
        raise ValueError(
            "EA rejected that token. They are short-lived -- open the EA "
            "token page again and paste a fresh one.")
    pid = (response.json() or {}).get("pid_id")
    if not pid:
        raise ValueError("EA did not return an account id for that token; "
                         "paste a fresh one from the EA token page")
    response = http.get(
        EA_ENTITLEMENTS.format(pid=pid),
        headers={"authtoken": token,
                 "Accept": "application/vnd.origin.v3+json; "
                           "x-cache/force-write"},
        timeout=30)
    if getattr(response, "status_code", 200) >= 400:
        raise ValueError(
            f"EA's entitlements API refused the request "
            f"(HTTP {response.status_code}). The token may have expired -- "
            "they last a few hours -- paste a fresh one.")
    out = []
    for entitlement in (response.json() or {}).get("entitlements") or []:
        name = str(entitlement.get("originDisplayName")
                   or entitlement.get("productName")
                   or entitlement.get("offerId") or "").strip()
        # Base games only: EA lists every DLC and beta as an entitlement.
        if name and str(entitlement.get("offerType") or "").upper() in (
                "", "BASE_GAME", "BASEGAME"):
            out.append(ListEntry(game=clean_title(name)))
    return out


#: What a signed-in browser gets from Blizzard's own account page. It is a
#: plain JSON document listing the games on the account -- no key, no OAuth,
#: and the same source Playnite's Battle.net library reads.
BATTLENET_PAGE = "https://account.blizzard.com/api/games-and-subs"


def _battlenet_entries(cfg: dict, *, session=None) -> list[ListEntry]:
    """Owned Blizzard games.

    Two ways in, because Blizzard's page is JSON a person can simply copy:
    paste that document, or paste the session cookie and let ROMarr fetch
    it on every sync.
    """
    import json as _json
    import requests

    pasted = str(cfg.get("battlenet_json") or "").strip()
    if pasted:
        try:
            body = _json.loads(pasted)
        except ValueError:
            raise ValueError(
                "That is not the JSON from account.blizzard.com/api/"
                "games-and-subs -- copy the whole document the page shows.")
    else:
        cookie = str(cfg.get("battlenet_cookie") or "").strip()
        if not cookie:
            return []
        http = session or requests
        response = http.get(BATTLENET_PAGE,
                            headers={"Cookie": cookie,
                                     "Accept": "application/json"},
                            timeout=30)
        if getattr(response, "status_code", 200) >= 400:
            raise ValueError("Blizzard rejected that session cookie; sign in "
                             "again and copy a fresh one.")
        body = response.json() or {}

    games = body.get("gameAccounts") or body.get("games") or []
    out, seen = [], set()
    for game in games:
        name = clean_title(str((game.get("localizedGameName")
                                or game.get("gameName")
                                or game.get("name") or "")))
        # Blizzard lists one row per game ACCOUNT, not per game: two WoW
        # characters on separate accounts are two rows of the same title,
        # and Wanted must not hold it twice.
        if name and name.lower() not in seen:
            seen.add(name.lower())
            out.append(ListEntry(game=name))
    return out


# --- Steam ------------------------------------------------------------------
#
# The retro case for a Steam list is real: a lot of a Steam library IS retro
# -- the collections, the DOS re-releases, the ports -- and "get me the
# original cartridge versions of everything I own on Steam" is a sensible
# thing to ask an *arr for. The titles land in Wanted; the scorer decides
# what a title means on the chosen platform, exactly as it does for a pasted
# list.

STEAM_API = "https://api.steampowered.com"
STEAM_STORE = "https://store.steampowered.com"

#: How many wishlist appids get their names resolved per sync. The store's
#: appdetails endpoint answers one app per call and rate-limits around 200
#: per five minutes; a cap keeps a big wishlist from turning one sync into a
#: ban. The rest resolve on later syncs -- the ledger makes that cheap.
STEAM_WISHLIST_CAP = 150


STEAM_COMMUNITY = "https://steamcommunity.com"


def _steam_public_entries(cfg: dict, http) -> list[ListEntry]:
    """Owned games from a PUBLIC Steam profile, with no API key at all.

    Steam's community pages expose an account's games as XML when the
    profile's game details are public -- the same data the Web API returns,
    reachable without a secret. This is the keyless path: a profile URL, a
    vanity name, or a 64-bit id is all it needs, so ROMarr never has to hold
    a Steam credential for the common case.
    """
    import re as _re
    import xml.etree.ElementTree as ET

    handle = str(cfg.get("profile") or cfg.get("steam_id") or "").strip()
    if not handle:
        return []
    # Accept a full profile URL, a bare vanity name, or a 64-bit id.
    handle = handle.rstrip("/")
    if "steamcommunity.com" in handle:
        base = handle
    elif handle.isdigit():
        base = f"{STEAM_COMMUNITY}/profiles/{handle}"
    else:
        base = f"{STEAM_COMMUNITY}/id/{handle}"
    response = http.get(f"{base}/games?tab=all&xml=1", timeout=30)
    response.raise_for_status()
    try:
        root = ET.fromstring(response.text)
    except ET.ParseError:
        # A private games list answers with an HTML error page, not XML.
        raise ValueError(
            "Steam returned no game list -- set the profile's Game Details to "
            "Public (Steam -> Edit Profile -> Privacy Settings), or use an "
            "API key instead")
    out = []
    for game in root.findall(".//game/name"):
        name = (game.text or "").strip()
        if name:
            out.append(ListEntry(game=name))
    if not out and root.tag == "response":
        raise ValueError("Steam did not recognise that profile")
    return out


def _steam_entries(cfg: dict, *, session=None) -> list[ListEntry]:
    import requests
    http = session or requests
    steam_id = str(cfg.get("steam_id") or "").strip()
    api_key = str(cfg.get("api_key") or "").strip()
    # No key? Take the keyless public-profile path -- owned games only, which
    # is what a public profile exposes (a wishlist still needs the API).
    if not api_key and str(cfg.get("source") or "owned").lower() != "wishlist":
        return _steam_public_entries(cfg, http)
    if not steam_id or not api_key:
        return []
    source = str(cfg.get("source") or "owned").lower()

    if source == "owned":
        response = http.get(
            f"{STEAM_API}/IPlayerService/GetOwnedGames/v1/",
            params={"key": api_key, "steamid": steam_id,
                    "include_appinfo": 1, "include_played_free_games": 1},
            timeout=30)
        response.raise_for_status()
        games = ((response.json().get("response") or {}).get("games")) or []
        return [ListEntry(game=clean_title(g["name"]))
                for g in games if g.get("name")]

    # Wishlist appids come without names; each name is a store lookup.
    response = http.get(
        f"{STEAM_API}/IWishlistService/GetWishlist/v1/",
        params={"key": api_key, "steamid": steam_id}, timeout=30)
    response.raise_for_status()
    items = ((response.json().get("response") or {}).get("items")) or []
    out: list[ListEntry] = []
    for item in items[:STEAM_WISHLIST_CAP]:
        appid = item.get("appid")
        if not appid:
            continue
        try:
            detail = http.get(f"{STEAM_STORE}/api/appdetails",
                              params={"appids": appid,
                                      "filters": "basic"}, timeout=15)
            body = (detail.json() or {}).get(str(appid)) or {}
            name = ((body.get("data") or {}).get("name")) if body.get("success") else ""
        except Exception:
            # One unnamed app must not fail the list; it resolves next sync.
            continue
        if name:
            out.append(ListEntry(game=name))
    return out


# --- GOG --------------------------------------------------------------------
#
# GOG's public profile pages are backed by a JSON endpoint that needs no
# credential at all -- the profile just has to be public. GOG's catalogue is
# the most retro-relevant of the stores: most of it IS DOS and Amiga-era
# software sold again.

GOG_PROFILE = "https://www.gog.com/u/{username}/games/stats?page={page}"


def _gog_entries(cfg: dict, *, session=None) -> list[ListEntry]:
    import requests
    http = session or requests
    username = str(cfg.get("gog_username") or "").strip()
    if not username:
        return []
    out: list[ListEntry] = []
    page, pages = 1, 1
    while page <= pages and page <= 50:
        response = http.get(GOG_PROFILE.format(username=username, page=page),
                            timeout=30)
        response.raise_for_status()
        body = response.json() or {}
        pages = int(body.get("pages") or 1)
        for item in ((body.get("_embedded") or {}).get("items")) or []:
            title = ((item.get("game") or {}).get("title") or "").strip()
            if title:
                out.append(ListEntry(game=title))
        page += 1
    return out


# --- Xbox -------------------------------------------------------------------
#
# Microsoft's own Xbox API requires an Azure AD application and a full OAuth
# dance no self-hosted tool should ask its users to perform. OpenXBL
# (xbl.io) exists precisely for this: the user signs in there once, gets a
# personal API key, and the title history -- every game the account has
# played -- is one authenticated GET. That history IS the practical library:
# Microsoft exposes no purchased-games list even to OpenXBL.

XBOX_API = "https://xbl.io/api/v2"


def _xbox_entries(cfg: dict, *, session=None) -> list[ListEntry]:
    import requests
    http = session or requests
    key = str(cfg.get("openxbl_key") or "").strip()
    if not key:
        return []
    response = http.get(f"{XBOX_API}/achievements",
                        headers={"X-Authorization": key,
                                 "Accept": "application/json"}, timeout=30)
    response.raise_for_status()
    titles = (response.json() or {}).get("titles") or []
    out = []
    for title in titles:
        name = clean_title(str(title.get("name") or ""))
        if name:
            out.append(ListEntry(game=name))
    return out


# --- PlayStation ------------------------------------------------------------
#
# Sony has no public API either, but the community flow behind psn-api is
# stable and needs only the NPSSO token from the user's own browser session:
# NPSSO -> authorization code -> access token, then the trophy-title list is
# the played-games list. The NPSSO is a credential and is stored as one.

PSN_AUTH = "https://ca.account.sony.com/api/authz/v3/oauth"
PSN_API = "https://m.np.playstation.com/api"


def _psn_entries(cfg: dict, *, session=None) -> list[ListEntry]:
    import requests
    http = session or requests
    npsso = str(cfg.get("npsso") or "").strip()
    if not npsso:
        return []
    # Every failure below means the same thing to the operator -- the token
    # is wrong or has aged out -- so it is worth saying that rather than
    # letting an HTTPError bubble up as "something went wrong".
    stale = ValueError(
        "PlayStation refused that token. Open "
        "ca.account.sony.com/api/v1/ssocookie while signed in and paste "
        "what it shows; NPSSO tokens expire about every two months.")
    # Step 1: the code. Sony answers with a 302 whose Location carries it.
    response = http.get(
        f"{PSN_AUTH}/authorize",
        params={"access_type": "offline",
                "client_id": "09515159-7237-4370-9b40-3806e67c0891",
                "response_type": "code",
                "scope": "psn:mobile.v2.core psn:clientapp",
                "redirect_uri": "com.scee.psxandroid.scecompcall://redirect"},
        headers={"Cookie": f"npsso={npsso}"},
        allow_redirects=False, timeout=30)
    location = response.headers.get("Location") or ""
    if "code=" not in location:
        raise stale
    code = location.split("code=")[1].split("&")[0]
    # Step 2: the token.
    response = http.post(
        f"{PSN_AUTH}/token",
        data={"code": code, "grant_type": "authorization_code",
              "redirect_uri": "com.scee.psxandroid.scecompcall://redirect",
              "token_format": "jwt"},
        headers={"Authorization": "Basic MDk1MTUxNTktNzIzNy00MzcwLTliNDAtMzgw"
                                  "NmU2N2MwODkxOnVjUGprYTV0bnRCMktxc1A="},
        timeout=30)
    if getattr(response, "status_code", 200) >= 400:
        raise stale
    token = (response.json() or {}).get("access_token") or ""
    if not token:
        raise stale
    # Step 3: the shelf.
    out: list[ListEntry] = []
    offset = 0
    while offset <= 800:
        response = http.get(
            f"{PSN_API}/trophy/v1/users/me/trophyTitles",
            params={"limit": 100, "offset": offset},
            headers={"Authorization": f"Bearer {token}"}, timeout=30)
        response.raise_for_status()
        body = response.json() or {}
        titles = body.get("trophyTitles") or []
        for title in titles:
            name = clean_title(str(title.get("trophyTitleName") or ""))
            if name:
                out.append(ListEntry(game=name))
        if len(titles) < 100:
            break
        offset += 100
    return out


# --- itch.io ----------------------------------------------------------------
#
# The straightforward one: itch.io hands out API keys in account settings
# and the owned-keys endpoint is the purchase list, paginated.

ITCHIO_API = "https://itch.io/api/1"


def _itchio_entries(cfg: dict, *, session=None) -> list[ListEntry]:
    import requests
    http = session or requests
    key = str(cfg.get("itchio_key") or "").strip()
    if not key:
        return []
    out: list[ListEntry] = []
    page = 1
    while page <= 50:
        response = http.get(f"{ITCHIO_API}/{key}/my-owned-keys",
                            params={"page": page}, timeout=30)
        response.raise_for_status()
        keys = (response.json() or {}).get("owned_keys") or []
        for row in keys:
            name = clean_title(str(((row.get("game") or {}).get("title")) or ""))
            if name:
                out.append(ListEntry(game=name))
        if not keys:
            break
        page += 1
    return out


# The Settings page renders list forms from this, the same way download
# clients and indexers describe themselves.
LIST_TYPES = {
    "paste": {
        "label": "Pasted list",
        "help": "One title per line. '# comments', ranking numbers and "
                "'Title<TAB>Platform' lines are understood.",
        "fields": ["name", "enable", "platform", "content"],
    },
    "url": {
        "label": "List at a URL",
        "help": "A plain-text list fetched on the List Sync schedule, so a "
                "list somebody else maintains keeps feeding Wanted.",
        "fields": ["name", "enable", "platform", "url"],
    },
    "steam": {
        "label": "Steam library / wishlist",
        "help": "Owned games need NO credential: paste your profile URL or "
                "vanity name with Game Details set to Public. A Web API key "
                "(steamcommunity.com/dev/apikey) is only required for the "
                "wishlist.",
        "fields": ["name", "enable", "platform", "profile", "steam_id",
                   "api_key", "source"],
    },
    "gog": {
        "label": "GOG profile",
        "help": "Every game on a public GOG profile. No credential -- the "
                "profile just has to be public under Privacy settings.",
        "fields": ["name", "enable", "platform", "gog_username"],
    },
    "xbox": {
        "label": "Xbox (via OpenXBL)",
        "help": "Your Xbox title history -- every game the account has "
                "played. Sign in once at xbl.io for a personal API key; "
                "Microsoft exposes no purchase list, so played IS the "
                "practical library.",
        "fields": ["name", "enable", "platform", "openxbl_key"],
    },
    "psn": {
        "label": "PlayStation (via NPSSO)",
        "help": "Your PSN played-titles list. Sign in at playstation.com, "
                "then copy the NPSSO token from ca.account.sony.com/api/authz"
                "/v3/ssocookie -- it expires every couple of months.",
        "fields": ["name", "enable", "platform", "npsso"],
    },
    "itchio": {
        "label": "itch.io purchases",
        "help": "Everything you own on itch.io, via an API key from "
                "itch.io/user/settings/api-keys.",
        "fields": ["name", "enable", "platform", "itchio_key"],
    },
    "epic": {
        "label": "Epic Games library",
        "help": "Everything you own on Epic. Open Epic's code page while "
                "signed in and paste the authorizationCode -- it is "
                "exchanged once for a refresh token, so later syncs need "
                "nothing from you.",
        "fields": ["name", "enable", "platform", "epic_code"],
    },
    "ea": {
        "label": "EA library",
        "help": "Everything you own on EA, via the entitlements API. Open "
                "EA's token page while signed in and paste the access_token.",
        "fields": ["name", "enable", "platform", "ea_token"],
    },
    "battlenet": {
        "label": "Battle.net library",
        "help": "Your Blizzard games. Open account.blizzard.com's games "
                "page while signed in and paste the JSON it shows.",
        "fields": ["name", "enable", "platform", "battlenet_json"],
    },
    "humble": {
        "label": "Humble Bundle library",
        "help": "Every game ever bought or claimed on Humble, via its own "
                "API. Needs the _simpleauth_sess cookie from a signed-in "
                "browser -- no key programme exists.",
        "fields": ["name", "enable", "platform", "humble_cookie"],
    },
}

# --- Humble Bundle -----------------------------------------------------------

HUMBLE_API = "https://www.humblebundle.com/api/v1"


def _humble_entries(cfg: dict, *, session=None) -> list[ListEntry]:
    """Everything ever bought or claimed on Humble, via its own API.

    Humble's API answers to the browser's session cookie
    (`_simpleauth_sess`) and nothing else -- no key programme exists. Two
    calls: the order keys, then the orders in batches, keeping the
    subproducts that are actually games (a bundle also carries soundtracks
    and ebooks, identified by their download platforms).
    """
    import requests
    http = session or requests
    cookie = str(cfg.get("humble_cookie") or "").strip()
    if not cookie:
        return []
    if "_simpleauth_sess" not in cookie:
        cookie = f"_simpleauth_sess={cookie}"
    headers = {"Cookie": cookie, "Accept": "application/json"}

    response = http.get(f"{HUMBLE_API}/user/order", headers=headers,
                        timeout=30)
    if getattr(response, "status_code", 200) in (401, 403):
        raise ValueError(
            "Humble rejected that cookie. Sign in at humblebundle.com, then "
            "copy the _simpleauth_sess cookie value again -- it rotates "
            "when you sign out.")
    response.raise_for_status()
    keys = [str(o.get("gamekey") or "") for o in (response.json() or [])
            if o.get("gamekey")]

    out: list[ListEntry] = []
    seen: set[str] = set()
    for start in range(0, len(keys), 40):
        batch = keys[start:start + 40]
        query = "&".join(f"gamekey={k}" for k in batch)
        response = http.get(f"{HUMBLE_API}/orders?all_tpkds=true&{query}",
                            headers=headers, timeout=60)
        response.raise_for_status()
        for order in (response.json() or {}).values():
            for sub in (order.get("subproducts") or []):
                platforms = {str(d.get("platform") or "")
                             for d in (sub.get("downloads") or [])}
                # Games download for a platform; soundtracks and ebooks
                # download as "audio" and "ebook".
                if not platforms & {"windows", "mac", "linux", "android"}:
                    continue
                name = clean_title(str(sub.get("human_name") or ""))
                if name and name.lower() not in seen:
                    seen.add(name.lower())
                    out.append(ListEntry(game=name))
    return out


#: The one store with nothing to connect to, and why. This used to list EA,
#: Battle.net and Epic too, which was wrong: all three have web APIs that
#: return an owned library -- they authenticate with a browser session
#: rather than an issued key, which is what Playnite and LaunchBox have
#: always done and what ROMarr now does.
NO_API_STORES = {
    "Nintendo": "No web API, and nothing written to a PC to read either. "
                "Genuinely paste-only — and the one where DAT-verified "
                "acquisition matters most anyway.",
}

