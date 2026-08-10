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


def _steam_entries(cfg: dict, *, session=None) -> list[ListEntry]:
    import requests
    http = session or requests
    steam_id = str(cfg.get("steam_id") or "").strip()
    api_key = str(cfg.get("api_key") or "").strip()
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
        "help": "Owned games or wishlist for a Steam profile. Needs a free "
                "Web API key (steamcommunity.com/dev/apikey) and the 64-bit "
                "SteamID; the profile's game details must be public.",
        "fields": ["name", "enable", "platform", "steam_id", "api_key",
                   "source"],
    },
    "gog": {
        "label": "GOG profile",
        "help": "Every game on a public GOG profile. No credential -- the "
                "profile just has to be public under Privacy settings.",
        "fields": ["name", "enable", "platform", "gog_username"],
    },
}
