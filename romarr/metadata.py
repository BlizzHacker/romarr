"""Naming a game, from a hash rather than from a filename.

Every tool in this category matches metadata by parsing the release title:
strip the scene tags, strip the region, strip the revision, hope what is left
is the game. It mostly works, and when it does not it fails silently -- a
cover for the wrong game looks exactly like a cover for the right one.

ROMarr does not have to guess. When a file verified against a No-Intro or
Redump DAT, its **canonical name is known exactly**, so the lookup key is a
fact rather than the output of a regular expression:

    Chrono.Trigger.USA.Retranslated.v1.2.[!].smc
      -> filename parsing: "Chrono Trigger Retranslated v1 2"
      -> DAT verification:  "Chrono Trigger (USA)"

That is the whole idea, and it is why `identify` takes a verification result
before it takes a filename. Filename parsing is still here, because an
unverified file has nothing else -- but it is the fallback, not the method.

Providers are a driver table like the indexers and notifiers. Nothing here
ships an API key, and a provider with no key configured is skipped rather
than failing the lookup: metadata is an enhancement, and a missing cover must
never cost somebody an import.
"""

from __future__ import annotations

import json
import logging
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

TIMEOUT = 20


@dataclass(frozen=True)
class GameInfo:
    """What a provider knows about a game."""

    title: str = ""
    summary: str = ""
    released: str = ""
    rating: float = 0.0
    genres: tuple[str, ...] = ()
    cover_url: str = ""
    source: str = ""
    #: How the lookup key was arrived at. Kept because "we matched a DAT name"
    #: and "we guessed from the filename" deserve different amounts of trust,
    #: and a UI that shows metadata without saying which is inviting somebody
    #: to believe the wrong cover.
    matched_by: str = ""

    @property
    def found(self) -> bool:
        return bool(self.title)


# --- turning a file into a search term -------------------------------------

#: Everything that is decoration on a release name. Order matters: the
#: bracketed groups go before the dotted separators, or `(USA, Europe)`
#: becomes `USA Europe` and survives as words.
_BRACKETED = re.compile(r"[\(\[\{][^\)\]\}]*[\)\]\}]")
_SCENE_TAGS = re.compile(
    r"\b(?:usa|europe|japan|world|ntsc|pal|rev\s*\d+|v\d+(?:\.\d+)*|"
    r"proto|beta|demo|sample|unl|alt|fixed|cracked|repack|multi\d*)\b",
    re.IGNORECASE)
_UNDERSCORES = re.compile(r"_+")
_DOTS = re.compile(r"\.+")
_SPACES = re.compile(r"\s+")


def clean_title(name: str) -> str:
    """A searchable title from a filename, for when there is nothing better.

    This is the method every other tool uses as its *only* method. Here it is
    the fallback, and `identify` records that it was used.

    **Dots are collapsed last, and that ordering is load-bearing.** A scene
    name writes the version as `v1.1`, so replacing separators first turns it
    into `v1 1`; the tag pattern then matches `v1`, strips it, and leaves an
    orphan `1` welded to the title -- `Chrono Trigger 1`, which is a different
    game. Version tags have to be matched while their dots are still there.
    """
    text = str(name or "")
    text = re.sub(r"\.[A-Za-z0-9]{1,5}$", "", text)      # extension
    text = _BRACKETED.sub(" ", text)
    text = _UNDERSCORES.sub(" ", text)
    text = _SCENE_TAGS.sub(" ", text)                    # while dots survive
    text = _DOTS.sub(" ", text)
    text = _SPACES.sub(" ", text).strip(" -–—")
    return text


#: A No-Intro / Redump name is `Game Name (Region) (Extras)`. The part before
#: the first parenthesis is the title, exactly, with no guessing at all.
_DAT_TITLE = re.compile(r"^([^(\[]+)")


def title_from_dat(game: str) -> str:
    found = _DAT_TITLE.match(str(game or "").strip())
    return _SPACES.sub(" ", found.group(1)).strip() if found else ""


def lookup_key(verification=None, filename: str = "") -> tuple[str, str]:
    """(search term, how it was arrived at).

    The DAT name wins whenever there is one. It is the difference between a
    key that is known and a key that is inferred, and it is the only thing
    here that makes ROMarr's metadata better rather than merely present.
    """
    game = getattr(verification, "game", "") or ""
    status = getattr(verification, "status", "") or ""
    if status == "verified" and game:
        title = title_from_dat(game)
        if title:
            return title, "dat"
    cleaned = clean_title(filename)
    return cleaned, "filename" if cleaned else ""


# --- providers --------------------------------------------------------------

def _get_json(url: str, headers: dict | None = None):
    request = urllib.request.Request(url)
    for name, value in (headers or {}).items():
        request.add_header(name, value)
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        log.warning("metadata lookup rejected: HTTP %s", exc.code)
    except Exception as exc:
        log.warning("metadata lookup failed: %s", exc)
    return None


def _rawg(cfg: dict, term: str) -> GameInfo:
    key = str(cfg.get("api_key") or "")
    if not key:
        return GameInfo()
    url = ("https://api.rawg.io/api/games?"
           + urllib.parse.urlencode({"key": key, "search": term,
                                     "page_size": 1, "search_exact": "true"}))
    body = _get_json(url) or {}
    rows = body.get("results") or []
    if not rows:
        return GameInfo()
    row = rows[0]
    return GameInfo(
        title=str(row.get("name") or ""),
        released=str(row.get("released") or ""),
        rating=float(row.get("rating") or 0.0),
        genres=tuple(str(g.get("name")) for g in (row.get("genres") or [])
                     if g.get("name")),
        cover_url=str(row.get("background_image") or ""),
        source="RAWG",
    )


def _igdb(cfg: dict, term: str) -> GameInfo:
    """IGDB, which needs a Twitch client id and a bearer token.

    ROMarr does not fetch the token: it is an OAuth client-credentials
    exchange that belongs in the operator's own setup, and a tool that quietly
    holds a Twitch credential to make cover art appear is doing more than
    anybody asked.
    """
    client_id = str(cfg.get("client_id") or "")
    token = str(cfg.get("token") or "")
    if not (client_id and token):
        return GameInfo()
    request = urllib.request.Request(
        "https://api.igdb.com/v4/games",
        data=(f'search "{term}"; fields name,summary,first_release_date,'
              f'rating,genres.name,cover.url; limit 1;').encode(),
        method="POST")
    request.add_header("Client-ID", client_id)
    request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            rows = json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        log.warning("IGDB lookup failed: %s", exc)
        return GameInfo()
    if not rows:
        return GameInfo()
    row = rows[0]
    cover = (row.get("cover") or {}).get("url") or ""
    return GameInfo(
        title=str(row.get("name") or ""),
        summary=str(row.get("summary") or ""),
        rating=float(row.get("rating") or 0.0) / 10.0,
        genres=tuple(str(g.get("name")) for g in (row.get("genres") or [])
                     if g.get("name")),
        # IGDB returns protocol-relative URLs and a thumbnail size by default.
        cover_url=("https:" + cover.replace("t_thumb", "t_cover_big")
                   if cover.startswith("//") else cover),
        source="IGDB",
    )


PROVIDERS: dict[str, dict] = {
    "rawg": {"label": "RAWG.io", "lookup": _rawg, "fields": ("api_key",),
             "help": "A free RAWG API key."},
    "igdb": {"label": "IGDB", "lookup": _igdb,
             "fields": ("client_id", "token"),
             "help": "A Twitch client id and bearer token. ROMarr does not "
                     "perform the OAuth exchange for you."},
}


@dataclass
class Metadata:
    """Every configured provider, asked in order until one answers."""

    providers: list[dict] = field(default_factory=list)

    def identify(self, *, verification=None, filename: str = "") -> GameInfo:
        """What this file is, named from the strongest key available.

        Never raises and never blocks an import. Metadata is an enhancement;
        a missing cover must not cost somebody a game.
        """
        term, how = lookup_key(verification, filename)
        if not term:
            return GameInfo()

        for cfg in self.providers:
            if not cfg.get("enable", True):
                continue
            spec = PROVIDERS.get(str(cfg.get("type") or "").lower())
            if spec is None:
                log.warning("unknown metadata provider %r", cfg.get("type"))
                continue
            try:
                info = spec["lookup"](cfg, term)
            except Exception as exc:
                log.warning("metadata provider %r raised: %s",
                            cfg.get("type"), exc)
                continue
            if info.found:
                return GameInfo(**{**info.__dict__, "matched_by": how})
        return GameInfo(matched_by=how)


# --- release calendar -------------------------------------------------------

def _rawg_calendar(cfg: dict, start: str, end: str, limit: int) -> list[dict]:
    key = str(cfg.get("api_key") or "")
    if not key:
        return []
    url = ("https://api.rawg.io/api/games?"
           + urllib.parse.urlencode({
               "key": key, "dates": f"{start},{end}",
               "ordering": "released", "page_size": limit}))
    body = _get_json(url) or {}
    out = []
    for row in body.get("results") or []:
        out.append({
            "title": str(row.get("name") or ""),
            "released": str(row.get("released") or ""),
            "rating": float(row.get("rating") or 0.0),
            "cover_url": str(row.get("background_image") or ""),
            "platforms": [str((p.get("platform") or {}).get("name") or "")
                          for p in (row.get("platforms") or [])],
            "source": "RAWG",
        })
    return out


#: Which providers can answer a date range. RAWG can; IGDB's date filtering
#: needs a different query shape and is not worth a second code path until
#: somebody asks for it.
CALENDAR_PROVIDERS = {"rawg": _rawg_calendar}


def calendar(providers: list[dict], *, days_back: int = 30,
             days_ahead: int = 60, limit: int = 40) -> dict:
    """Games released recently or due soon.

    A window in both directions on purpose. "Upcoming" alone is the obvious
    reading and the less useful one -- most of what somebody wants to acquire
    came out last month, not next month.
    """
    import datetime

    today = datetime.date.today()
    start = (today - datetime.timedelta(days=days_back)).isoformat()
    end = (today + datetime.timedelta(days=days_ahead)).isoformat()

    for cfg in providers or []:
        if not cfg.get("enable", True):
            continue
        answer = CALENDAR_PROVIDERS.get(str(cfg.get("type") or "").lower())
        if answer is None:
            continue
        try:
            rows = answer(cfg, start, end, limit)
        except Exception as exc:
            log.warning("calendar lookup failed: %s", exc)
            continue
        if rows:
            today_iso = today.isoformat()
            for row in rows:
                row["upcoming"] = bool(row.get("released", "") > today_iso)
            return {"items": rows, "from": start, "to": end, "error": None}
    return {"items": [], "from": start, "to": end,
            "error": "no metadata provider with an API key is configured"}
