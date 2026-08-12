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

import datetime
import hashlib
import json
import logging
import re
import threading
import time
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


# --- IGDB --------------------------------------------------------------------
#
# IGDB sits behind Twitch's OAuth, and this module used to refuse to do the
# exchange: "a tool that quietly holds a Twitch credential to make cover art
# appear is doing more than anybody asked". That was wrong about what the
# credential is. `grant_type=client_credentials` authenticates the
# *application*, not a person -- it reads nobody's Twitch account and reaches
# nothing but the games database. What the refusal actually bought was an
# operator pasting a bearer token by hand every sixty days, which is how an
# install ended up with valid IGDB credentials and three pages insisting no
# provider was configured.
#
# So ROMarr performs the exchange, keeps the token for the ~60 days it lasts,
# and never writes either half of the credential anywhere.

IGDB_TOKEN_URL = "https://id.twitch.tv/oauth2/token"
IGDB_API = "https://api.igdb.com/v4"

#: IGDB publishes a ceiling of four requests a second. One gate for the whole
#: process, because the limit is per credential and ROMarr answers page views
#: on several threads at once.
IGDB_RATE = 4.0

#: Popularity type 4, "Played": how many IGDB users have logged playing the
#: game. Chosen over the alternatives after looking at what each one actually
#: returns from the live API rather than at what it is called:
#:
#:   * type 1 "Visits" is page views, and the live top of it is spam -- "The
#:     Choicer Voicer", "A Weird Game About Sausage".
#:   * types 3 "Playing", 5 "24hr Peak Players" and 9 "Global Top Sellers"
#:     are live-service and storefront signals: Roblox, Fortnite, Genshin.
#:     A ROM manager will never file any of them.
#:   * `hypes` is a pre-release wishlist counter, zero for everything that
#:     already shipped -- right for Upcoming, useless for a shelf that is
#:     mostly back catalogue.
#:   * `total_rating_count` ranks by who bothered to review, which tracks
#:     what got covered rather than what got played.
#:
#: "Played" answers GTA V, Witcher 3, Portal 2, Skyrim, Breath of the Wild --
#: a canon, which is what a Popular shelf is for, and the nearest thing IGDB
#: has to the `-added` ordering the RAWG shelf uses.
IGDB_POPULARITY_PLAYED = 4

#: Everything a browse row needs, written once so the three shelves and the
#: calendar cannot drift into returning different shapes.
IGDB_SHELF_FIELDS = ("fields id,name,summary,first_release_date,total_rating,"
                     "total_rating_count,hypes,cover.url,platforms.name;")

#: Main games only. Without it a dated shelf is half DLC: "Helldivers 2: Face
#: the Unknown" next Tuesday is a warbond, not a release. Note the field --
#: `category = 0` was the documented way to say this and now matches nothing
#: at all rather than erroring, which is a very quiet way to get an empty
#: shelf out of a query that looks correct.
IGDB_MAIN_GAMES = "game_type = 0"

#: IGDB's own ceiling on `limit`. Asking for more is a 400, not a truncation.
IGDB_MAX_LIMIT = 500


def _igdb_limit(limit: int) -> int:
    try:
        return max(1, min(int(limit or 40), IGDB_MAX_LIMIT))
    except (TypeError, ValueError):
        return 40

_IGDB_GATE = threading.Lock()
_IGDB_NEXT = [0.0]
_IGDB_TOKENS: dict[str, tuple[str, float]] = {}
_IGDB_TOKEN_LOCK = threading.Lock()


def _igdb_wait() -> None:
    """Four requests a second, counted across every thread.

    The sleep happens with the gate held, and that is the point: releasing it
    first would let every waiting thread through together and put the burst
    straight back.
    """
    with _IGDB_GATE:
        now = time.monotonic()
        if now < _IGDB_NEXT[0]:
            time.sleep(_IGDB_NEXT[0] - now)
        _IGDB_NEXT[0] = max(now, _IGDB_NEXT[0]) + 1.0 / IGDB_RATE


def _igdb_bearer(client_id: str, secret: str, *, refresh: bool = False) -> str:
    """A Twitch app token, fetched once and kept until it expires.

    Cached against a digest rather than against the credential itself, so
    that a stray `repr()` of this dict in a log or a debugger carries no
    secret. A token per request would be two round trips for every cover and
    would spend the credential's own rate limit on nothing.
    """
    key = hashlib.sha256(f"{client_id}\0{secret}".encode()).hexdigest()
    if not refresh:
        with _IGDB_TOKEN_LOCK:
            cached = _IGDB_TOKENS.get(key)
        # A minute of slack, so a token that expires mid-flight surfaces here
        # rather than as a 401 the caller has to interpret.
        if cached and cached[1] > time.time() + 60:
            return cached[0]
    query = urllib.parse.urlencode({"client_id": client_id,
                                    "client_secret": secret,
                                    "grant_type": "client_credentials"})
    request = urllib.request.Request(f"{IGDB_TOKEN_URL}?{query}", data=b"",
                                     method="POST")
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        body = json.loads(response.read().decode("utf-8"))
    token = str(body.get("access_token") or "")
    if not token:
        return ""
    with _IGDB_TOKEN_LOCK:
        _IGDB_TOKENS[key] = (token, time.time()
                             + float(body.get("expires_in") or 0))
    return token


def igdb_query(cfg: dict, endpoint: str, body: str) -> list[dict]:
    """One Apicalypse query against IGDB, with the token handled.

    The body is IGDB's own query language, not JSON. Posting JSON here earns
    a 400 that reads like a field error and sends people looking in entirely
    the wrong place.
    """
    client_id = str(cfg.get("client_id") or "")
    secret = str(cfg.get("token") or "")
    if not (client_id and secret):
        return []
    try:
        bearer = _igdb_bearer(client_id, secret)
    except Exception as exc:
        # Configs written before ROMarr did the exchange hold a bearer token
        # in this field, because that is what the help text asked for. The
        # exchange failing is the signal; try the stored value verbatim
        # rather than telling somebody their working setup is broken.
        log.debug("IGDB token exchange refused (%s); using the stored value "
                  "as a bearer token", exc.__class__.__name__)
        bearer = secret
    if not bearer:
        return []
    for attempt in (1, 2):
        _igdb_wait()
        request = urllib.request.Request(f"{IGDB_API}/{endpoint}",
                                         data=body.encode("utf-8"),
                                         method="POST")
        request.add_header("Client-ID", client_id)
        request.add_header("Authorization", f"Bearer {bearer}")
        request.add_header("Accept", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
                rows = json.loads(response.read().decode("utf-8"))
            return rows if isinstance(rows, list) else []
        except urllib.error.HTTPError as exc:
            # Sixty days outlives most processes but not all of them, and
            # Twitch can revoke early. One forced refresh, then it is a real
            # failure rather than a loop.
            if exc.code == 401 and attempt == 1:
                try:
                    bearer = _igdb_bearer(client_id, secret, refresh=True)
                except Exception:
                    return []
                continue
            log.warning("IGDB %s rejected: HTTP %s", endpoint, exc.code)
            return []
        except Exception as exc:
            log.warning("IGDB %s failed: %s", endpoint, exc)
            return []
    return []


#: IGDB hands back `//images.igdb.com/igdb/image/upload/t_thumb/co2lbd.jpg`:
#: protocol-relative, and a 90-pixel thumbnail whatever the row is for. Both
#: halves have to be corrected or the grid draws broken images at postage
#: stamp size -- which is why this is one function rather than a `.replace`
#: at each call site, and why it matches the size token by shape instead of
#: looking for the literal `t_thumb`.
_IGDB_SIZE = re.compile(r"/t_[a-z0-9_]+/")


def igdb_cover(url: str, size: str = "t_cover_big") -> str:
    text = str(url or "").strip()
    if not text:
        return ""
    if text.startswith("//"):
        text = "https:" + text
    return _IGDB_SIZE.sub(f"/{size}/", text, count=1)


def _igdb_shelf_row(row: dict) -> dict:
    stamp = row.get("first_release_date")
    released = ""
    if stamp:
        released = datetime.datetime.fromtimestamp(
            float(stamp), datetime.timezone.utc).date().isoformat()
    # IGDB rates out of 100 and RAWG out of 5, and the UI draws one star next
    # to whichever number arrives. Converted to RAWG's scale here, so a shelf
    # is not half "84" and half "4.6" with the same symbol in front of both.
    score = float(row.get("total_rating") or row.get("rating") or 0.0)
    return {
        "title": str(row.get("name") or ""),
        "summary": str(row.get("summary") or ""),
        "released": released,
        "rating": round(score / 20.0, 1),
        "cover_url": igdb_cover((row.get("cover") or {}).get("url") or ""),
        "platforms": [str(p.get("name") or "")
                      for p in (row.get("platforms") or []) if p.get("name")],
        "source": "IGDB",
    }


def _igdb(cfg: dict, term: str) -> GameInfo:
    """Name one game, for `identify`."""
    rows = igdb_query(cfg, "games",
                      f'search "{term}"; fields name,summary,'
                      f'first_release_date,rating,genres.name,cover.url;'
                      f' limit 1;')
    if not rows:
        return GameInfo()
    row = rows[0]
    return GameInfo(
        title=str(row.get("name") or ""),
        summary=str(row.get("summary") or ""),
        rating=float(row.get("rating") or 0.0) / 10.0,
        genres=tuple(str(g.get("name")) for g in (row.get("genres") or [])
                     if g.get("name")),
        cover_url=igdb_cover((row.get("cover") or {}).get("url") or ""),
        source="IGDB",
    )


PROVIDERS: dict[str, dict] = {
    "rawg": {"label": "RAWG.io", "lookup": _rawg, "fields": ("api_key",),
             "ready": lambda cfg: bool(cfg.get("api_key")),
             # `offer` is what to go and get when nothing is configured;
             # `needs` is what is blank when something is. Two sentences,
             # because "add RAWG" and "your key is empty" are different
             # problems and printing one for the other is how somebody
             # re-registers a key they already have.
             "offer": "a free API key",
             "needs": "its API key is empty",
             "help": "A free RAWG API key."},
    "igdb": {"label": "IGDB", "lookup": _igdb,
             "fields": ("client_id", "token"),
             "ready": lambda cfg: bool(cfg.get("client_id")
                                       and cfg.get("token")),
             "offer": "a Twitch client id and secret",
             "needs": "its client id or client secret is empty",
             # `token` is the storage key and stays one, because renaming it
             # would drop the credential the first time somebody saved the
             # form -- `put_item` replaces the whole entry. What it holds is
             # a client secret, so that is what the editor calls it.
             "labels": {"token": "Client Secret"},
             "help": "A Twitch application's client id and client secret. "
                     "ROMarr performs the OAuth exchange itself and caches "
                     "the token, so you never paste a bearer token by hand. "
                     "An older config holding a bearer token in this field "
                     "still works."},
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


def _igdb_calendar(cfg: dict, start: str, end: str, limit: int) -> list[dict]:
    """A date window, which IGDB counts in unix seconds rather than in days."""
    def stamp(day: str, offset: int) -> int:
        return int(datetime.datetime.fromisoformat(day).replace(
            tzinfo=datetime.timezone.utc).timestamp()) + offset

    try:
        # The far end is inclusive to the second: a game dated on the last
        # day of the window is in the window, and comparing against its
        # midnight would drop the whole day.
        low, high = stamp(start, 0), stamp(end, 86399)
    except ValueError:
        return []
    return [_igdb_shelf_row(row) for row in igdb_query(
        cfg, "games",
        f"{IGDB_SHELF_FIELDS} where first_release_date >= {low}"
        f" & first_release_date <= {high} & {IGDB_MAIN_GAMES};"
        f" sort first_release_date asc; limit {_igdb_limit(limit)};")]


#: Which providers can answer a date range. Both can: RAWG takes ISO dates in
#: a query string, IGDB takes unix seconds in an Apicalypse `where`.
CALENDAR_PROVIDERS = {"rawg": _rawg_calendar, "igdb": _igdb_calendar}


# --- discovery --------------------------------------------------------------
#
# Browse, not search: the three shelves every storefront opens with --
# popular, new, upcoming -- answered from whichever configured provider can
# serve them, whose catalogues reach back through every platform ROMarr
# files. Discovery is an enhancement exactly like the rest of this module: no
# provider configured means an empty page that says why, never an error --
# and, since the page says why, the reason has to be the real one rather
# than a fixed line naming one provider out of the several that would do.

#: The three shelves, named the same on every provider. Each one decides for
#: itself what "popular" means in its own data; what does not vary is that a
#: shelf asked for here comes back in one shape.
DISCOVER_SHELVES = ("popular", "new", "upcoming")


def _rawg_discover(cfg: dict, shelf: str, limit: int) -> list[dict]:
    import datetime

    key = str(cfg.get("api_key") or "")
    if not key:
        return []
    today = datetime.date.today()
    params = {"key": key, "page_size": limit}
    if shelf == "popular":
        params.update({"ordering": "-added", "metacritic": "60,100"})
    elif shelf == "new":
        start = (today - datetime.timedelta(days=90)).isoformat()
        params.update({"dates": f"{start},{today.isoformat()}",
                       "ordering": "-added"})
    else:  # upcoming
        end = (today + datetime.timedelta(days=180)).isoformat()
        params.update({"dates": f"{today.isoformat()},{end}",
                       "ordering": "released"})
    body = _get_json("https://api.rawg.io/api/games?"
                     + urllib.parse.urlencode(params)) or {}
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


def _igdb_discover(cfg: dict, shelf: str, limit: int) -> list[dict]:
    now = int(time.time())
    count = _igdb_limit(limit)
    if shelf == "upcoming":
        body = (f"{IGDB_SHELF_FIELDS} where first_release_date > {now}"
                f" & {IGDB_MAIN_GAMES}; sort first_release_date asc;"
                f" limit {count};")
    elif shelf == "new":
        body = (f"{IGDB_SHELF_FIELDS} where"
                f" first_release_date > {now - 30 * 86400}"
                f" & first_release_date <= {now} & {IGDB_MAIN_GAMES};"
                f" sort first_release_date desc; limit {count};")
    else:
        return _igdb_popular(cfg, count)
    return [_igdb_shelf_row(row) for row in igdb_query(cfg, "games", body)]


def _igdb_popular(cfg: dict, limit: int) -> list[dict]:
    """The Popular shelf, which costs two requests rather than one.

    `popularity_primitives` answers with game ids and a score and nothing
    else, so the names and covers are a second call -- both inside the same
    four-a-second gate.
    """
    primitives = igdb_query(
        cfg, "popularity_primitives",
        f"fields game_id,value; where popularity_type ="
        f" {IGDB_POPULARITY_PLAYED}; sort value desc; limit {limit};")
    ranked = [int(row["game_id"]) for row in primitives if row.get("game_id")]
    if not ranked:
        return []
    rows = igdb_query(cfg, "games",
                      f"{IGDB_SHELF_FIELDS} where id ="
                      f" ({','.join(str(i) for i in ranked)});"
                      f" limit {len(ranked)};")
    found = {int(row["id"]): row for row in rows if row.get("id")}
    # `where id = (...)` answers in IGDB's own order, not in the order it was
    # asked for. Reapplied here, because a Popular shelf silently sorted by
    # internal database id looks entirely plausible and means nothing.
    return [_igdb_shelf_row(found[i]) for i in ranked if i in found]


DISCOVER_PROVIDERS = {"rawg": _rawg_discover, "igdb": _igdb_discover}


# --- who can answer, and what to say when nobody can -------------------------

def _label(cfg: dict) -> str:
    name = str(cfg.get("type") or "").lower()
    spec = PROVIDERS.get(name) or {}
    return str(spec.get("label") or cfg.get("name") or name or "a provider")


def _first_answer(providers: list[dict], table: dict, call):
    """Ask each capable provider in turn. Returns (rows, cfg, asked, failed).

    `asked` and `failed` are what was actually reached and what blew up,
    which is the whole point of the tuple: "nothing is configured", "IGDB is
    configured and had nothing for this shelf" and "IGDB could not be
    reached" are three different situations that used to render the same
    sentence, and that sentence named a provider the operator did not use.
    """
    asked: list[str] = []
    failed: list[str] = []
    for cfg in providers or []:
        if not cfg.get("enable", True):
            continue
        name = str(cfg.get("type") or "").lower()
        answer = table.get(name)
        if answer is None:
            continue
        ready = (PROVIDERS.get(name) or {}).get("ready")
        if ready and not ready(cfg):
            continue
        try:
            rows = call(answer, cfg)
        except Exception as exc:
            log.warning("browse via %r failed: %s", name, exc)
            failed.append(_label(cfg))
            continue
        asked.append(_label(cfg))
        if rows:
            return rows, cfg, asked, failed
    return [], None, asked, failed


def _gap(providers: list[dict], table: dict, what: str,
         asked: list[str], failed: list[str]) -> str:
    """Why nothing answered, named after what is actually missing."""
    if asked:
        return (f"{', '.join(sorted(set(asked)))} answered, but has nothing "
                f"to {what} right now")
    if failed:
        return (f"{', '.join(sorted(set(failed)))} could not be reached -- "
                f"see the log")
    known = [cfg for cfg in providers or []
             if str(cfg.get("type") or "").lower() in table]
    if not known:
        offer = " or ".join(
            f"{PROVIDERS[name]['label']} ({PROVIDERS[name]['offer']})"
            for name in sorted(table) if name in PROVIDERS)
        return (f"no metadata provider that can {what} is configured -- add "
                f"{offer} under Settings -> Metadata")
    live = [cfg for cfg in known if cfg.get("enable", True)]
    if not live:
        return (f"{', '.join(sorted({_label(c) for c in known}))} is "
                f"configured but switched off -- enable it under "
                f"Settings -> Metadata")
    return "; ".join(sorted({
        f"{_label(cfg)} is configured but "
        f"{(PROVIDERS.get(str(cfg.get('type') or '').lower()) or {}).get('needs', 'incomplete')}"
        for cfg in live})) + " -- fill it in under Settings -> Metadata"


def discover(providers: list[dict], *, shelf: str = "popular",
             limit: int = 40) -> dict:
    """One storefront shelf, from the first provider able to serve it."""
    if shelf not in DISCOVER_SHELVES:
        return {"shelf": shelf, "items": [], "provider": None,
                "provider_label": "",
                "error": f"unknown shelf; one of {', '.join(DISCOVER_SHELVES)}"}
    items, cfg, asked, failed = _first_answer(
        providers, DISCOVER_PROVIDERS, lambda fn, c: fn(c, shelf, limit))
    if items:
        # Marked on every row, not just in a heading. These are catalogue
        # entries from somebody else's database sitting one card away from
        # games the operator actually holds, and a shelf that lets the two
        # read alike is the quiet kind of lie this project exists to avoid.
        for row in items:
            row["owned"] = False
        return {"shelf": shelf, "items": items, "provider": cfg.get("type"),
                "provider_label": _label(cfg), "error": None}
    return {"shelf": shelf, "items": [], "provider": None,
            "provider_label": "",
            "error": _gap(providers, DISCOVER_PROVIDERS, "browse",
                          asked, failed)}


def calendar(providers: list[dict], *, days_back: int = 30,
             days_ahead: int = 60, limit: int = 40) -> dict:
    """Games released recently or due soon.

    A window in both directions on purpose. "Upcoming" alone is the obvious
    reading and the less useful one -- most of what somebody wants to acquire
    came out last month, not next month.

    `days_back` goes negative, which is how the Calendar's Upcoming view asks
    for the forward half: `days_back=-1` opens the window tomorrow. Zero is
    not the same thing and is the wrong answer for that view -- it opens on
    today, and today's releases are forty games that are already out sitting
    under a heading that says they are not.
    """
    today = datetime.date.today()
    start = (today - datetime.timedelta(days=days_back)).isoformat()
    end = (today + datetime.timedelta(days=days_ahead)).isoformat()

    rows, cfg, asked, failed = _first_answer(
        providers, CALENDAR_PROVIDERS,
        lambda fn, c: fn(c, start, end, limit))
    if rows:
        today_iso = today.isoformat()
        for row in rows:
            row["upcoming"] = bool(row.get("released", "") > today_iso)
            row["owned"] = False
        return {"items": rows, "from": start, "to": end,
                "provider": cfg.get("type"), "provider_label": _label(cfg),
                "error": None}
    return {"items": [], "from": start, "to": end, "provider": None,
            "provider_label": "",
            "error": _gap(providers, CALENDAR_PROVIDERS,
                          "answer for a date range", asked, failed)}
