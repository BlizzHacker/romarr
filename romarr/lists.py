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
    raise ValueError(f"unknown list type {kind!r}")


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
        return [ListEntry(game=g["name"]) for g in games if g.get("name")]

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
        name = str(title.get("name") or "").strip()
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
        raise ValueError("PSN refused the NPSSO token; grab a fresh one from "
                         "ca.account.sony.com after signing in")
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
    response.raise_for_status()
    token = (response.json() or {}).get("access_token") or ""
    if not token:
        raise ValueError("PSN token exchange failed")
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
            name = str(title.get("trophyTitleName") or "").strip()
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
            name = str(((row.get("game") or {}).get("title")) or "").strip()
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
}

#: Stores with no usable *web* API, and how ROMarr connects them anyway.
#:
#: This list used to say EA, Battle.net and Epic simply "could not be
#: connected". That was wrong, and Playnite and LaunchBox were the standing
#: counter-example: they have managed those libraries for years. The way
#: they do it is not a private API -- the launcher already wrote your
#: library to disk when it installed the game, and reading that needs no
#: credential at all. `romarr.launchers` does the same, and
#: `scripts/connect_launchers.py` pushes the result here.
NO_API_STORES = {
    "EA (formerly Origin)": "No public web API — but the EA app writes an "
                            "installerdata.xml beside every game it installs. "
                            "Run scripts/connect_launchers.py on your gaming "
                            "PC and they arrive here. No credential involved.",
    "Battle.net": "Blizzard's web API exposes game data, not an owned list — "
                  "but Battle.net records installed products in product.db. "
                  "The launcher connector reads it.",
    "Epic Games": "No public library API, and the OAuth workarounds are "
                  "brittle — but Epic writes a JSON manifest per installed "
                  "game. The launcher connector reads those.",
    "Nintendo": "No API, and nothing written to a PC to read either. This is "
                "the one that is genuinely paste-only — and it is also the "
                "one where DAT-verified acquisition matters most.",
}
