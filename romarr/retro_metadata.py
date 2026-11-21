"""ScreenScraper and SteamGridDB, the metadata sources RetroArr adds.

Ported from RetroArr (MIT, Copyright (c) 2026 RetroArr): the ScreenScraper
response classification, the media region order and the SteamGridDB
endpoints follow `ScreenScraperClient.cs` and `SteamGridDbClient.cs`.

ScreenScraper is the one retro source that answers *by hash*. ROMarr already
knows a verified file's CRC, MD5 and SHA-1 from its DAT match, so a lookup
names the exact dump instead of guessing from a filename. SteamGridDB has no
game data worth having, but its covers fill the gap when the provider that
named a game had no box art.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request

from .metadata import GameInfo

log = logging.getLogger(__name__)

TIMEOUT = 20
USER_AGENT = "ROMarr (+https://github.com/BlizzHacker/romarr)"

# --- ScreenScraper ------------------------------------------------------------

SCREENSCRAPER_API = "https://api.screenscraper.fr/api2"
SOFTNAME = "ROMarr"

OK, EMPTY, QUOTA, AUTH, NETWORK = "ok", "empty", "quota", "auth", "network"

#: ROMarr platform slug -> ScreenScraper `systemeid`. Taken from RetroArr's
#: platform table (MIT) and matched to ROMarr's slugs. A platform missing here
#: is looked up by hash and file name alone, which still works.
SCREENSCRAPER_SYSTEMS: dict[str, int] = {
    "3do": 29, "3ds": 17, "acpc": 65, "amiga": 64, "amiga-cd32": 130,
    "apple2": 86, "arcade": 75, "atari-jaguar-cd": 171, "atari-st": 42,
    "atari2600": 26, "atari5200": 40, "atari7800": 41, "atari8bit": 43,
    "c64": 66, "colecovision": 48, "dc": 23, "dos": 135, "famicom": 3,
    "fds": 106, "gamegear": 21, "gb": 9, "gba": 12, "gbc": 10,
    "genesis-slash-megadrive": 1, "intellivision": 115, "jaguar": 27,
    "lynx": 28, "msx": 113, "msx2": 113, "n64": 14, "nds": 15,
    "neo-geo-cd": 70, "neo-geo-pocket": 25, "neo-geo-pocket-color": 82,
    "neogeoaes": 142, "neogeomvs": 142, "nes": 3, "ngc": 13,
    "odyssey-2-slash-videopac-g7000": 104, "pc-fx": 72, "ps2": 58,
    "psp": 61, "psx": 57, "saturn": 22, "sega32": 19, "segacd": 20,
    "sfam": 4, "sg1000": 109, "sharp-x68000": 79, "sinclair-zx81": 77,
    "sms": 2, "snes": 4, "switch": 225, "turbografx16--1": 31,
    "turbografx-16-slash-pc-engine-cd": 114, "vectrex": 102, "vic-20": 73,
    "virtualboy": 11, "wii": 16, "wiiu": 18, "wonderswan": 45,
    "wonderswan-color": 46, "zxs": 76,
}

#: Wording ScreenScraper uses for a spent quota or a closed API. It sometimes
#: answers HTTP 200 with one of these as plain text instead of JSON.
_QUOTA_WORDS = (
    "quota dépassé", "quota depasse", "quota de scrape", "api totalement fermée",
    "api totalement fermee", "api fermee", "api closed", "limite atteinte",
    "scrapes par jour", "scrapes restant",
)


def _is_auth_wording(lower: str) -> bool:
    return ("erreur de login" in lower or "identifiant ou mot de passe" in lower
            or ("login" in lower and "incorrect" in lower))


def classify_screenscraper(code: int, body: str) -> str:
    """What a ScreenScraper answer means, from its status and body.

    A JSON body is never substring-matched as a whole: every successful
    authenticated answer carries an `ssuser.quotarefu` counter, and reading
    that as "quota" would turn every hit into a failure.
    """
    if code in (423, 429, 430, 431):
        return QUOTA
    if code in (401, 403):
        return AUTH
    if code != 200:
        return NETWORK
    raw = body or ""
    trimmed = raw.lstrip()
    if trimmed[:1] in ("{", "["):
        try:
            root = json.loads(raw)
        except ValueError:
            root = None
        if root is not None:
            header = root.get("header") if isinstance(root, dict) else None
            if isinstance(header, dict):
                success = str(header.get("success") or "").lower()
                error = str(header.get("error") or "").lower()
                if success == "true":
                    return OK
                if error and any(w in error for w in _QUOTA_WORDS):
                    return QUOTA
                if error and _is_auth_wording(error):
                    return AUTH
                if success == "false":
                    return NETWORK
            return OK
    lower = raw.lower()
    if any(w in lower for w in _QUOTA_WORDS):
        return QUOTA
    if _is_auth_wording(lower):
        return AUTH
    return NETWORK


def _fetch(url: str, headers: dict | None = None) -> tuple[int, str]:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT,
                                                   **(headers or {})})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            return response.status, response.read(4 * 1024 * 1024).decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        try:
            text = exc.read(4096).decode("utf-8", "replace")
        except OSError:
            text = ""
        return exc.code, text
    except Exception as exc:                    # a down provider is routine
        log.warning("metadata lookup failed: %s", exc.__class__.__name__)
        return 0, ""


#: Media region order: the preferred region, then world, US, Europe, Japan,
#: ScreenScraper's own, then anything.
_REGION_ORDER = ("wor", "us", "eu", "jp", "ss")


def media_url(medias, kind: str, region: str = "wor") -> str:
    """The URL of a `kind` medium, preferring `region`, then the usual order."""
    rows = [m for m in (medias or []) if isinstance(m, dict) and m.get("type") == kind
            and m.get("url")]
    for wanted in (region, *_REGION_ORDER):
        for row in rows:
            if row.get("region") == wanted:
                return str(row["url"])
    return str(rows[0]["url"]) if rows else ""


def _by_region(rows, key: str = "region", prefer=("wor", "us", "eu", "ss", "jp")) -> str:
    rows = [r for r in (rows or []) if isinstance(r, dict) and r.get("text")]
    for wanted in prefer:
        for row in rows:
            if row.get(key) == wanted:
                return str(row["text"])
    return str(rows[0]["text"]) if rows else ""


def screenscraper_game(game: dict) -> GameInfo:
    """A ScreenScraper `jeu` object as ROMarr's GameInfo."""
    if not isinstance(game, dict):
        return GameInfo()
    title = _by_region(game.get("noms")) or str(game.get("nom") or "")
    summary = _by_region(game.get("synopsis"), key="langue", prefer=("en", "fr"))
    released = _by_region(game.get("dates"))
    genres = []
    for genre in game.get("genres") or []:
        if isinstance(genre, dict):
            name = _by_region(genre.get("noms"), key="langue", prefer=("en", "fr"))
            if name:
                genres.append(name)
    try:
        rating = float((game.get("note") or {}).get("text") or 0) / 2.0
    except (TypeError, ValueError, AttributeError):
        rating = 0.0
    medias = game.get("medias")
    return GameInfo(
        title=title, summary=summary, released=released,
        rating=rating, genres=tuple(genres),
        cover_url=media_url(medias, "box-2D") or media_url(medias, "ss"),
        source="ScreenScraper",
    )


def screenscraper_url(cfg: dict, term: str, context: dict | None = None) -> str:
    """The `jeuInfos.php` request: strongest hash first, file name always."""
    context = context or {}
    params = {
        "devid": str(cfg.get("dev_id") or ""),
        "devpassword": str(cfg.get("dev_password") or ""),
        "softname": SOFTNAME, "output": "json",
    }
    if cfg.get("username") and cfg.get("password"):
        params["ssid"] = str(cfg["username"])
        params["sspassword"] = str(cfg["password"])
    system = SCREENSCRAPER_SYSTEMS.get(str(context.get("platform") or ""))
    if system:
        params["systemeid"] = str(system)
    for key in ("sha1", "md5", "crc"):
        if context.get(key):
            params[key] = str(context[key]).lower()
            break
    params["romnom"] = str(context.get("filename") or term)
    return f"{SCREENSCRAPER_API}/jeuInfos.php?" + urllib.parse.urlencode(params)


def screenscraper(cfg: dict, term: str, context: dict | None = None) -> GameInfo:
    if not (cfg.get("dev_id") and cfg.get("dev_password")):
        return GameInfo()
    code, body = _fetch(screenscraper_url(cfg, term, context))
    status = classify_screenscraper(code, body)
    if status != OK:
        if status in (QUOTA, AUTH):
            log.warning("ScreenScraper refused the lookup: %s", status)
        return GameInfo()
    try:
        game = ((json.loads(body) or {}).get("response") or {}).get("jeu")
    except (ValueError, AttributeError):
        return GameInfo()
    return screenscraper_game(game)


# --- SteamGridDB --------------------------------------------------------------

STEAMGRIDDB_API = "https://www.steamgriddb.com/api/v2"


def classify_steamgriddb(code: int, body: str) -> str:
    if code in (401, 403):
        return AUTH
    if code == 404:
        return EMPTY
    if code != 200:
        return NETWORK
    trimmed = (body or "").lstrip()
    if trimmed[:1] not in ("{", "["):
        return NETWORK
    try:
        root = json.loads(body)
    except ValueError:
        return NETWORK
    if not isinstance(root, dict):
        return NETWORK
    if root.get("success") is False:
        return AUTH
    return OK


def _steamgriddb(cfg: dict, path: str) -> list:
    code, body = _fetch(STEAMGRIDDB_API + path,
                        {"Authorization": f"Bearer {cfg.get('api_key', '')}"})
    if classify_steamgriddb(code, body) != OK:
        return []
    data = (json.loads(body) or {}).get("data")
    return data if isinstance(data, list) else []


def steamgriddb(cfg: dict, term: str, context: dict | None = None) -> GameInfo:
    """A cover for `term`: the closest game's first grid image."""
    if not cfg.get("api_key") or not term:
        return GameInfo()
    games = _steamgriddb(cfg, "/search/autocomplete/" + urllib.parse.quote(term, safe=""))
    game = next((g for g in games if isinstance(g, dict) and g.get("id")), None)
    if game is None:
        return GameInfo()
    grids = _steamgriddb(cfg, f"/grids/game/{int(game['id'])}")
    cover = next((str(g["url"]) for g in grids if isinstance(g, dict) and g.get("url")), "")
    return GameInfo(title=str(game.get("name") or ""), cover_url=cover,
                    source="SteamGridDB")


PROVIDERS: dict[str, dict] = {
    "screenscraper": {
        "label": "ScreenScraper", "lookup": screenscraper, "context": True,
        "fields": ("dev_id", "dev_password", "username", "password"),
        "ready": lambda cfg: bool(cfg.get("dev_id") and cfg.get("dev_password")),
        "offer": "ScreenScraper developer credentials",
        "needs": "its developer id or developer password is empty",
        "labels": {"dev_id": "Developer ID", "dev_password": "Developer Password",
                   "username": "ScreenScraper Username",
                   "password": "ScreenScraper Password"},
        "help": "Looks a verified ROM up by its hash. Needs ScreenScraper "
                "developer credentials; a user account is optional and "
                "raises your daily quota.",
    },
    "steamgriddb": {
        "label": "SteamGridDB", "lookup": steamgriddb, "artwork": True,
        "fields": ("api_key",),
        "ready": lambda cfg: bool(cfg.get("api_key")),
        "offer": "a free API key",
        "needs": "its API key is empty",
        "help": "Covers only: fills in box art when the provider that named "
                "the game had none. A free SteamGridDB API key.",
    },
}


def _register() -> None:
    from .metadata import PROVIDERS as everything
    everything.update(PROVIDERS)


_register()
