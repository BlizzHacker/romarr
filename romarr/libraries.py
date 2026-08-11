"""Game libraries ROMarr can hand a finished ROM to.

ROMarr started against RomM and grew its assumptions: a `/api/roms` shape, a
RomM token, RomM's idea of a platform slug. That made "the *arr for games" true
only for people who had already chosen RomM.

This is the seam. A library is four questions -- is it up, how many games does
it hold, what are they, and please rescan -- and every backend answers those in
its own dialect. Adding one means implementing this protocol and nothing else;
the importer, the indexers and the UI never learn which is in use.

Three are implemented:

  * RomM      -- REST, /api/roms, bearer token from a username/password grant.
  * Gaseous   -- REST, /api/v1.1/Games. Listing is a POST with a mandatory
                 filter body, authenticated by session cookie rather than a
                 bearer token.
  * Retrom    -- grpc-web. Its REST surface fetches one game by id and has no
                 listing at all, so the protobuf wire format is spoken
                 directly.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

import requests

log = logging.getLogger(__name__)

# Short budget for anything a page waits on. A library that is merely slow must
# not read as ROMarr being broken.
HEALTH_TIMEOUT = 5


@dataclass(frozen=True)
class Game:
    """One game, reduced to what a poster grid and an import decision need."""

    id: str
    name: str
    platform: str = ""
    cover: str = ""
    # Which library server this came from. Empty when there is only one, so a
    # single-library install shows no redundant badge on every poster.
    source: str = ""
    # What the library server already knows about this game. Carried because
    # a shelf of 166,578 posters is unusable without axes to cut it on, and
    # asking the server again per filter would be a query per keystroke.
    #
    # Every one of these is optional and frequently absent: a library is
    # mostly UNIDENTIFIED until somebody runs a metadata scan, so the UI has
    # to treat "no genre" as the normal case rather than an error, and say
    # what share of the shelf it can actually filter.
    genres: tuple[str, ...] = ()
    regions: tuple[str, ...] = ()
    year: int = 0
    rating: float = 0.0
    #: Where the bytes are. "local" is on disk here; a plugin-provided entry
    #: (Archive.org, Vimm's) is "cloud" -- the distinction people actually
    #: want to filter on, because one plays now and one has to be fetched.
    origin: str = "local"


# Budget for the background shelf fetch. Nothing waits on it, so it is
# generous: RomM's library query takes around three minutes on a large,
# contended library, and a short budget could never succeed.
#
# It belongs here rather than on one backend. It was defined only on Romm, and
# the service reads it off whichever library is attached -- so Gaseous and
# Retrom raised AttributeError on every refresh, which _refresh_counts catches
# and logs as a bare class name. The Library page stayed empty forever with
# nothing to go on.
DEFAULT_BACKGROUND_TIMEOUT = 300


@runtime_checkable
class Library(Protocol):
    """What ROMarr needs from a game library, and nothing more."""

    name: str

    # Every backend carries this, so the service can ask any of them for a
    # slow background read without knowing which one it has.
    BACKGROUND_TIMEOUT: int

    @property
    def configured(self) -> bool: ...

    def reachable(self) -> bool: ...

    def count(self) -> int: ...

    def games(self, limit: int = 60, offset: int = 0,
              timeout: int | None = None) -> list[Game]: ...

    def rescan(self, platform_slug: str | None = None) -> bool: ...


def _absolute(base: str, url: str) -> str:
    """Make a cover URL absolute, since backends return both forms."""
    if not url:
        return ""
    if url.startswith(("http://", "https://")):
        return url
    return f"{base.rstrip('/')}/{url.lstrip('/')}"


# ------------------------------------------------------------------ Gaseous --

@dataclass(frozen=True)
class GaseousConfig:
    base_url: str
    api_key: str = ""
    username: str = ""
    password: str = ""
    timeout: int = 30


class GaseousLibrary:
    """gaseous-server: https://github.com/gaseous-project/gaseous-server

    Three things about its API are worth stating, because all three were only
    discovered by running one:

      * Listing games is a POST with a JSON body, not a query string. A GET
        returns a redirect to the login page, which reads as a wrong path
        rather than a wrong method.
      * It authenticates with a session cookie, not a bearer token. An
        unauthenticated call does not return 401 -- it 302s to
        /Identity/Account/Login, which then 404s under /api, so the failure
        arrives looking like a missing endpoint.
      * The filter body is not optional. Name, Genre, Theme, GameMode,
        Platform and PlayerPerspective are all required even when empty, and
        omitting them is a 400 listing every one of them.
    """

    name = "Gaseous"
    BACKGROUND_TIMEOUT = DEFAULT_BACKGROUND_TIMEOUT

    def __init__(self, config: GaseousConfig, session: requests.Session | None = None):
        self._config = config
        self._session = session or requests.Session()
        self._logged_in = False

    @property
    def configured(self) -> bool:
        return bool(self._config.base_url)

    def _url(self, path: str) -> str:
        return f"{self._config.base_url.rstrip('/')}/api/v1.1/{path.lstrip('/')}"

    def _headers(self) -> dict[str, str]:
        # An API key is honoured if one is configured, but Gaseous itself
        # issues session cookies, so this is the exception rather than the
        # rule.
        if self._config.api_key:
            return {"Authorization": f"Bearer {self._config.api_key}"}
        return {}

    def _login(self) -> bool:
        """Establish a session, once, if credentials were supplied.

        The cookie lives on the requests.Session, so every later call carries
        it without any explicit token handling.
        """
        if self._logged_in or not self._config.username:
            return self._logged_in
        try:
            r = self._session.post(
                self._url("Account/Login"),
                json={
                    "Email": self._config.username,
                    "Password": self._config.password,
                    "RememberMe": True,
                },
                timeout=self._config.timeout,
            )
        except requests.RequestException as err:
            log.warning("gaseous login failed: %s", err.__class__.__name__)
            return False
        if not r.ok:
            # Never echo the body: a failed auth response can repeat the
            # submitted credentials back at you.
            log.warning("gaseous login rejected with status %s", r.status_code)
            return False
        self._logged_in = True
        return True

    def reachable(self) -> bool:
        try:
            r = self._session.get(self._url("HealthCheck"), headers=self._headers(),
                                  timeout=HEALTH_TIMEOUT)
            return r.ok
        except requests.RequestException as err:
            log.warning("gaseous unreachable: %s", err.__class__.__name__)
            return False

    # Every field here is required by the server even when empty. Kept as one
    # constant so the requirement is stated once rather than repeated.
    _EMPTY_FILTER = {
        "Name": "",
        "Genre": [],
        "Theme": [],
        "GameMode": [],
        "Platform": [],
        "PlayerPerspective": [],
    }

    def _search(self, limit: int, offset: int, timeout: int | None):
        self._login()
        body = dict(self._EMPTY_FILTER)
        body.update({
            "pageNumber": (offset // max(limit, 1)) + 1,
            "pageSize": limit,
        })
        r = self._session.post(self._url("Games"), json=body, headers=self._headers(),
                               timeout=timeout or self._config.timeout)
        r.raise_for_status()
        return r.json()

    def count(self) -> int:
        payload = self._search(limit=1, offset=0, timeout=HEALTH_TIMEOUT)
        if isinstance(payload, dict):
            for key in ("count", "totalCount", "total"):
                if isinstance(payload.get(key), int):
                    return payload[key]
            return len(payload.get("games") or payload.get("items") or [])
        return len(payload or [])

    def games(self, limit: int = 60, offset: int = 0,
              timeout: int | None = None) -> list[Game]:
        payload = self._search(limit, offset, timeout)
        rows = payload if isinstance(payload, list) else (
            payload.get("games") or payload.get("items") or []
        )
        base = self._config.base_url
        out: list[Game] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            gid = str(row.get("id") or row.get("metadataMapId") or "")
            if not gid:
                continue
            out.append(Game(
                id=gid,
                name=str(row.get("name") or row.get("nameThe") or "Unknown"),
                platform=str(row.get("platformName") or row.get("platform") or ""),
                # Cover art is served from the game id rather than a path in
                # the payload.
                cover=_absolute(base, f"/api/v1.1/Games/{gid}/cover/image"),
            ))
        return out

    def rescan(self, platform_slug: str | None = None) -> bool:
        """Nothing to ask. Gaseous has no scan trigger, so this is a no-op.

        It used to POST ContentManager/Rescan, which does not exist -- every
        import logged a 404 for a call that was never going to work. Enumerated
        from the server's own OpenAPI documents: 89 paths under /api/v1 and 93
        under /api/v1.1, and not one of them starts a scan. Gaseous picks up
        files only on its own schedule, via two background tasks:

          * TitleIngestor, every 1 minute, which processes whatever is in
            Gaseous's *Import* directory.
          * LibraryScan, every 1440 minutes -- once a day -- which walks the
            configured library paths.

        So a ROM filed into a Gaseous library path can take up to a day to
        appear, and that is a property of Gaseous rather than something ROMarr
        can fix from out here. Two ways to make it prompt, both in the README:
        point this library's path at Gaseous's Import directory, or lower
        LibraryScan's interval in Gaseous itself.

        Returns True because there is nothing to do and nothing failed. A False
        here would mark every successful import as partly broken over a call
        that does not exist.
        """
        return True


# ------------------------------------------------------------------- Retrom --

@dataclass(frozen=True)
class RetromConfig:
    base_url: str
    api_key: str = ""
    timeout: int = 30


class RetromLibrary:
    """retrom: https://github.com/jmberesford/retrom

    Retrom's REST surface fetches a single game by id and nothing else -- there
    is no list endpoint there at all. Listing is a gRPC call, exposed over
    grpc-web on the same port, so that is what this speaks.

    Rather than pull in protobuf tooling for two messages, the wire format is
    written and read directly. It is small and stable, and the alternative is a
    code-generation step in a project that otherwise has none.
    """

    name = "Retrom"
    BACKGROUND_TIMEOUT = DEFAULT_BACKGROUND_TIMEOUT

    # From packages/codegen/protos. Field numbers are part of the contract and
    # can only change in a breaking release, so pinning them is as safe as a
    # generated stub and far less machinery.
    _F_GAMES, _F_METADATA = 1, 2                              # GetGamesResponse
    _F_GAME_ID, _F_GAME_PATH, _F_GAME_PLATFORM = 1, 3, 4      # Game
    _F_META_GAME_ID, _F_META_NAME, _F_META_COVER = 1, 2, 4    # GameMetadata

    def __init__(self, config: RetromConfig, session: requests.Session | None = None):
        self._config = config
        self._session = session or requests.Session()

    @property
    def configured(self) -> bool:
        return bool(self._config.base_url)

    def _rpc(self, method: str, body: bytes, timeout: int | None) -> bytes:
        """One grpc-web call, returning the raw framed response."""
        url = f"{self._config.base_url.rstrip('/')}/retrom.{method}"
        headers = {"content-type": "application/grpc-web+proto"}
        if self._config.api_key:
            headers["authorization"] = f"Bearer {self._config.api_key}"
        # grpc-web frames a message as one flag byte, four big-endian length
        # bytes, then the payload.
        framed = b"\x00" + len(body).to_bytes(4, "big") + body
        r = self._session.post(url, data=framed, headers=headers,
                               timeout=timeout or self._config.timeout)
        r.raise_for_status()
        return r.content

    @staticmethod
    def _frames(raw: bytes):
        """Yield message payloads, skipping the trailer.

        The final frame sets the high bit in its flag byte and carries trailing
        headers rather than a message; decoding it as protobuf yields nonsense.
        """
        i = 0
        while i + 5 <= len(raw):
            flag = raw[i]
            length = int.from_bytes(raw[i + 1:i + 5], "big")
            payload = raw[i + 5:i + 5 + length]
            i += 5 + length
            if flag & 0x80:
                continue
            yield payload

    @staticmethod
    def _varint(buf: bytes, i: int) -> tuple[int, int]:
        value = shift = 0
        while i < len(buf):
            byte = buf[i]
            i += 1
            value |= (byte & 0x7F) << shift
            if not byte & 0x80:
                break
            shift += 7
        return value, i

    @staticmethod
    def _fields(buf: bytes):
        """Walk a protobuf message, yielding (field_number, value).

        Handles the wire types these messages actually use: varint for numbers
        and length-delimited for strings and nested messages. Fixed-width types
        are skipped rather than misread.
        """
        i = 0
        while i < len(buf):
            tag, i = RetromLibrary._varint(buf, i)
            if tag == 0:
                break
            field, wire = tag >> 3, tag & 7
            if wire == 0:
                value, i = RetromLibrary._varint(buf, i)
                yield field, value
            elif wire == 2:
                length, i = RetromLibrary._varint(buf, i)
                yield field, buf[i:i + length]
                i += length
            elif wire == 5:
                i += 4
            elif wire == 1:
                i += 8
            else:
                break

    def _get_games(self, timeout: int | None) -> list[Game]:
        # with_metadata is field 3, a varint set to true. Without it the
        # response carries paths but no names, and every tile reads as a
        # filename.
        body = bytes([(3 << 3) | 0, 1])
        raw = self._rpc("GameService/GetGames", body, timeout)

        paths: dict[int, str] = {}
        platforms: dict[int, str] = {}
        names: dict[int, str] = {}
        covers: dict[int, str] = {}

        for payload in self._frames(raw):
            for field, value in self._fields(payload):
                if field == self._F_GAMES and isinstance(value, bytes):
                    gid, path, platform = 0, "", ""
                    for f, v in self._fields(value):
                        if f == self._F_GAME_ID and isinstance(v, int):
                            gid = v
                        elif f == self._F_GAME_PATH and isinstance(v, bytes):
                            path = v.decode("utf-8", "replace")
                        elif f == self._F_GAME_PLATFORM and isinstance(v, int):
                            platform = str(v)
                    if gid:
                        paths[gid] = path
                        platforms[gid] = platform
                elif field == self._F_METADATA and isinstance(value, bytes):
                    gid, name, cover = 0, "", ""
                    for f, v in self._fields(value):
                        if f == self._F_META_GAME_ID and isinstance(v, int):
                            gid = v
                        elif f == self._F_META_NAME and isinstance(v, bytes):
                            name = v.decode("utf-8", "replace")
                        elif f == self._F_META_COVER and isinstance(v, bytes):
                            cover = v.decode("utf-8", "replace")
                    if gid:
                        names[gid] = name
                        covers[gid] = cover

        out: list[Game] = []
        for gid, path in paths.items():
            name = names.get(gid) or ""
            if not name:
                # Without metadata, show the filename rather than somebody's
                # entire filesystem path.
                name = path.replace("\\", "/").rstrip("/").split("/")[-1] or str(gid)
            out.append(Game(
                id=str(gid),
                name=name,
                platform=platforms.get(gid, ""),
                cover=_absolute(self._config.base_url, covers.get(gid, "")),
            ))
        return out

    def reachable(self) -> bool:
        try:
            # The service redirects / to its web UI, which is a cheap and
            # honest liveness check.
            r = self._session.get(f"{self._config.base_url.rstrip('/')}/",
                                  timeout=HEALTH_TIMEOUT, allow_redirects=False)
            return r.status_code < 500
        except requests.RequestException as err:
            log.warning("retrom unreachable: %s", err.__class__.__name__)
            return False

    def count(self) -> int:
        return len(self._get_games(HEALTH_TIMEOUT))

    def games(self, limit: int = 60, offset: int = 0,
              timeout: int | None = None) -> list[Game]:
        # GetGames has no paging of its own, so the window is applied here.
        return self._get_games(timeout)[offset:offset + limit]

    def rescan(self, platform_slug: str | None = None) -> bool:
        try:
            self._rpc("LibraryService/UpdateLibrary", b"", self._config.timeout)
            return True
        except requests.RequestException as err:
            log.warning("retrom rescan failed: %s", err)
            return False


# ----------------------------------------------------------------- registry --

# What an operator may put in LIBRARY_KIND. Unknown values are refused rather
# than defaulting: silently importing into the wrong library, or into none at
# all, is worse than starting with a clear error.
LIBRARY_KINDS = ("romm", "gaseous", "retrom", "folder")


def build_library(kind: str, env: dict[str, str]):
    """Construct the configured library backend.

    Reads the same generic variables for every backend -- LIBRARY_URL and
    friends -- so switching is a one-line change rather than a rewrite, while
    still honouring the original ROMM_* names so existing installs keep
    working untouched.
    """
    from .clients import Romm, RommConfig  # local import: avoids a cycle

    kind = (kind or "romm").strip().lower()
    if kind not in LIBRARY_KINDS:
        raise ValueError(
            f"unknown library kind {kind!r}; expected one of {', '.join(LIBRARY_KINDS)}"
        )

    url = env.get("LIBRARY_URL") or env.get("ROMM_URL", "")
    key = env.get("LIBRARY_API_KEY") or env.get("ROMM_API_TOKEN", "")
    user = env.get("LIBRARY_USERNAME") or env.get("ROMM_USERNAME", "")
    pw = env.get("LIBRARY_PASSWORD") or env.get("ROMM_PASSWORD", "")

    if kind == "gaseous":
        return GaseousLibrary(GaseousConfig(base_url=url, api_key=key,
                                            username=user, password=pw))
    if kind == "retrom":
        return RetromLibrary(RetromConfig(base_url=url, api_key=key))
    if kind == "folder":
        # No server, so the path is the whole configuration.
        return FolderLibrary(FolderConfig(
            root=env.get("LIBRARY_PATH") or env.get("ROMM_LIBRARY", "")))
    return Romm(RommConfig(base_url=url, username=user, password=pw, api_token=key))


# -------------------------------------------------------------------- Folder --

@dataclass(frozen=True)
class FolderConfig:
    root: str


class FolderLibrary:
    """A directory, which is what most of the emulation world actually uses.

    RomM, Gaseous and Retrom are servers with APIs. Almost everything else is
    not: Batocera, RetroPie, Recalbox, EmulationStation and ES-DE, EmuDeck,
    Pegasus, Lakka, muOS, ArkOS, LaunchBox, Playnite and Steam ROM Manager all
    read ROMs from a folder laid out by platform. Supporting "a folder" supports
    every one of them at once, and it is also the honest answer for somebody who
    just wants the file in the right place.

    There is no server to talk to, so three of the four questions are answered
    from the filesystem and the fourth is a no-op: nothing needs telling, the
    frontend picks the file up the next time it scans. That is not a limitation
    being papered over -- it is what those frontends do.
    """

    name = "Folder"
    BACKGROUND_TIMEOUT = DEFAULT_BACKGROUND_TIMEOUT

    # Archives count: a zipped ROM is the normal shipping form, and every
    # frontend listed above reads them.
    ARCHIVES = (".zip", ".7z", ".rar")

    # Walking a library of tens of thousands of files on every refresh is not
    # worth it for a number on a badge, so the walk stops here and the count is
    # reported as at-least. An honest ceiling beats a slow exact answer.
    MAX_SCAN = 50_000

    def __init__(self, config: FolderConfig):
        self._config = config

    @property
    def configured(self) -> bool:
        return bool(self._config.root)

    @property
    def _root(self):
        from pathlib import Path
        return Path(self._config.root)

    def _extensions(self) -> set[str]:
        from .platforms import PLATFORMS
        known = {ext for p in PLATFORMS for ext in p.extensions}
        return known | set(self.ARCHIVES)

    def _walk(self, limit: int):
        """ROM-looking files under the root, newest-shallowest first."""
        if not self.configured:
            return
        exts = self._extensions()
        seen = 0
        for path in sorted(self._root.rglob("*")):
            if seen >= limit:
                return
            if not path.is_file():
                continue
            if path.suffix.lower() not in exts:
                continue
            seen += 1
            yield path

    def reachable(self) -> bool:
        """Whether the directory is there. Cheap, because a page waits on it."""
        try:
            return self.configured and self._root.is_dir()
        except OSError as err:
            log.warning("folder library unreachable: %s", err.__class__.__name__)
            return False

    def count(self) -> int:
        try:
            return sum(1 for _ in self._walk(self.MAX_SCAN))
        except OSError as err:
            log.warning("folder library count failed: %s", err.__class__.__name__)
            return 0

    def games(self, limit: int = 60, offset: int = 0,
              timeout: int | None = None) -> list[Game]:
        """One game per ROM file.

        The platform is the containing directory, because that is the layout
        every one of these frontends imposes -- and it is the same layout the
        importer writes, so a ROM filed by ROMarr lands where it will be found.
        """
        out: list[Game] = []
        try:
            for n, path in enumerate(self._walk(offset + limit)):
                if n < offset:
                    continue
                out.append(Game(
                    id=str(path),
                    name=path.stem,
                    platform=path.parent.name if path.parent != self._root else "",
                ))
        except OSError as err:
            log.warning("folder library read failed: %s", err.__class__.__name__)
        return out

    def rescan(self, platform_slug: str | None = None) -> bool:
        """Nothing to tell. The file is on disk; the frontend finds it.

        True rather than False for the same reason as Gaseous: nothing failed,
        and reporting failure would mark every successful import as half-broken.
        """
        return True


class GameyfinLibrary(FolderLibrary):
    """Gameyfin, integrated the only way its architecture allows.

    Gameyfin v2 is a Vaadin server-side application with no public REST API to
    query or to trigger a scan through -- and it does not need one: it watches
    its library directories and picks up new files itself. So the durable
    integration is the same one every folder frontend gets: file the ROM into
    a directory Gameyfin watches, and its own watcher does the rest. Listed as
    its own type rather than hidden behind "Folder" so the Libraries page can
    say Gameyfin plainly and this docstring has somewhere to live.
    """

    name = "Gameyfin"


# ------------------------------------------------------- many servers, one arr --
#
# One library was a fair simplification until it wasn't. People run more than
# one: a RomM for the household and a second for a child's device, a Retrom
# next to a RomM, or a spare instance on a NAS. Radarr and Sonarr solved the
# same shape with multiple root folders, and Overseerr with multiple downstream
# servers and per-request routing, which is the model borrowed here.
#
# Each server carries its own filesystem path, because they are separate
# applications with separate library roots. Sharing one path between two
# libraries is allowed -- some people do point RomM and Retrom at the same tree
# -- but it is not assumed.

SECRET_PLACEHOLDER = "********"

_FIELD = lambda name, label, kind="text", default="", **kw: {  # noqa: E731
    "name": name, "label": label, "type": kind, "default": default, **kw
}

_COMMON_LIBRARY_FIELDS = [
    _FIELD("name", "Name"),
    _FIELD("enable", "Enable", "bool", True),
    _FIELD("url", "URL", default="http://localhost:8080"),
    _FIELD("path", "Library path", help="Where ROMs are filed for THIS server, "
                                       "as ROMarr sees it"),
    _FIELD("layout", "Folder structure", "select", "",
           options=[["", "Use the global default"],
                    ["flat", "Structure A — platform/rom"],
                    ["nested", "Structure B — platform/roms/rom"]],
           help="RomM's Structure A or B, for this server. Empty follows the "
                "Media Management default."),
    _FIELD("is_default", "Default", "bool", False,
           help="Requests with no matching platform rule are filed here"),
    _FIELD("platforms", "Platforms", "list",
           help="Route only these platforms here. Empty means any platform."),
]

LIBRARY_TYPES = {
    "romm": {
        "label": "RomM",
        "default_port": 8080,
        "fields": _COMMON_LIBRARY_FIELDS + [
            _FIELD("username", "Username"),
            _FIELD("password", "Password", "secret"),
            _FIELD("api_key", "API Token", "secret"),
        ],
    },
    "gaseous": {
        "label": "Gaseous",
        "default_port": 80,
        "fields": _COMMON_LIBRARY_FIELDS + [
            _FIELD("username", "Email", help="Gaseous logs in with an email address"),
            _FIELD("password", "Password", "secret"),
            _FIELD("api_key", "API Key", "secret"),
        ],
    },
    "retrom": {
        "label": "Retrom",
        "default_port": 5101,
        "fields": _COMMON_LIBRARY_FIELDS + [
            _FIELD("api_key", "API Key", "secret"),
        ],
    },
    "folder": {
        "label": "Folder",
        "default_port": 0,
        # No URL and no credentials: there is no server. Offering an address
        # field would invite somebody to fill it in and then wonder why nothing
        # connects.
        "fields": [f for f in _COMMON_LIBRARY_FIELDS if f["name"] != "url"],
    },
    "gameyfin": {
        "label": "Gameyfin",
        "default_port": 0,
        # Same shape as Folder on purpose: Gameyfin watches its library
        # directories itself, so the path IS the integration. See
        # GameyfinLibrary for why there is no URL to give it.
        "fields": [f if f["name"] != "path" else
                   {**f, "help": "A directory Gameyfin watches as a library; "
                                 "its own watcher picks up what ROMarr files"}
                   for f in _COMMON_LIBRARY_FIELDS if f["name"] != "url"],
    },
}


# --------------------------------------------------- out-of-tree backends --
#
# The four backends above are compiled in, and for a long time that was the
# whole list -- `build_library_from_config` was an if-chain, so the only way to
# teach ROMarr a new library server was to edit this file and ship a release.
# That is the wrong shape for the long tail. New self-hosted library servers
# appear faster than any one project can absorb them, some are private or
# pre-release and cannot be named in a public tree at all, and a house with an
# in-development server should not have to fork ROMarr to file into it.
#
# So a backend is registrable. A driver is a module that calls
# `register_library_backend` with a label, a field list and a builder; ROMarr
# then treats it exactly like a compiled-in one -- it appears in the Libraries
# page schema, routes by platform, redacts its secrets and imports into it,
# with no further wiring. Registration mutates LIBRARY_TYPES rather than
# shadowing it so every existing reader picks it up without knowing this
# mechanism exists.

#: kind -> callable(cfg) -> Library, for backends registered at runtime.
_REGISTERED: dict[str, callable] = {}


def register_library_backend(kind: str, *, label: str, build,
                             fields: list | None = None,
                             default_port: int = 0,
                             common_fields: bool = True) -> None:
    """Teach ROMarr a library backend defined outside this file.

    `build` takes the stored config dict and returns something satisfying the
    Library protocol. `fields` are added to the standard name/enable/url/path
    set unless `common_fields` is False, in which case they are the whole form.

    Re-registering a kind replaces it, so a driver can be reloaded in place;
    the compiled-in four are refused, because silently swapping the meaning of
    "romm" underneath an existing configuration would be a data-loss bug rather
    than an extension.
    """
    kind = (kind or "").strip().lower()
    if not kind:
        raise ValueError("a backend kind cannot be empty")
    if kind in LIBRARY_KINDS:
        raise ValueError(f"{kind!r} is a built-in backend and cannot be replaced")
    if not callable(build):
        raise TypeError("build must be callable")

    base = list(_COMMON_LIBRARY_FIELDS) if common_fields else []
    LIBRARY_TYPES[kind] = {
        "label": label,
        "default_port": default_port,
        "fields": base + list(fields or []),
        # Marks the row as not shipped with ROMarr, so the UI can say where it
        # came from instead of implying the project supports it.
        "external": True,
    }
    _REGISTERED[kind] = build
    log.info("registered library backend %r (%s)", kind, label)


def registered_backends() -> tuple[str, ...]:
    """Kinds added at runtime, in registration order."""
    return tuple(_REGISTERED)


def load_backend_plugins(directory: str | None = None) -> list[str]:
    """Import every driver in the backends directory.

    Drop-in rather than packaged: a driver is one file in a directory ROMarr
    is pointed at, which is the lowest-ceremony thing that works for a private
    or site-local backend -- no build, no publish, no entry point.

    A driver that raises is logged and skipped. One bad file must not stop the
    service, for the same reason one bad row in the settings file does not:
    the fix would then be locked behind the UI it prevented from starting.
    """
    import importlib.util

    from pathlib import Path

    root = Path(directory or os.environ.get("ROMARR_BACKENDS_DIR", "")
                or "/opt/romarr/backends")
    loaded: list[str] = []
    if not root.is_dir():
        return loaded

    for path in sorted(root.glob("*.py")):
        if path.name.startswith("_"):
            continue
        try:
            spec = importlib.util.spec_from_file_location(
                f"romarr_backend_{path.stem}", path)
            if spec is None or spec.loader is None:
                continue
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        except Exception as err:  # noqa: BLE001 - a driver may fail any way
            log.warning("library backend %s failed to load: %s: %s",
                        path.name, err.__class__.__name__, err)
            continue
        loaded.append(path.stem)
    return loaded


def redact_library(cfg: dict) -> dict:
    """A library configuration safe to send to a browser."""
    spec = LIBRARY_TYPES.get(str(cfg.get("type") or "").lower(), {})
    secrets = {f["name"] for f in spec.get("fields", []) if f["type"] == "secret"}
    return {
        k: (SECRET_PLACEHOLDER if k in secrets and v else v)
        for k, v in cfg.items()
    }


def merge_library_secrets(new: dict, old: dict | None) -> dict:
    """Keep the stored secret when the form sends back the placeholder."""
    if not old:
        return new
    spec = LIBRARY_TYPES.get(str(new.get("type") or "").lower(), {})
    for field in spec.get("fields", []):
        if field["type"] != "secret":
            continue
        name = field["name"]
        if new.get(name) in (SECRET_PLACEHOLDER, "", None):
            new[name] = old.get(name, "")
    return new


def build_library_from_config(cfg: dict):
    """Construct one backend from a stored library entry.

    Returns None for an unknown kind rather than raising: one bad row in the
    settings file must not stop the service from starting, or a typo in the
    Libraries page becomes an outage that cannot be fixed through the UI that
    caused it.
    """
    from .clients import Romm, RommConfig  # local import: avoids a cycle

    kind = str(cfg.get("type") or "").strip().lower()
    url = cfg.get("url", "")
    key = cfg.get("api_key", "")
    user = cfg.get("username", "")
    pw = cfg.get("password", "")

    if kind == "gaseous":
        return GaseousLibrary(GaseousConfig(base_url=url, api_key=key,
                                            username=user, password=pw))
    if kind == "retrom":
        return RetromLibrary(RetromConfig(base_url=url, api_key=key))
    if kind == "folder":
        return FolderLibrary(FolderConfig(root=cfg.get("path", "")))
    if kind == "gameyfin":
        return GameyfinLibrary(FolderConfig(root=cfg.get("path", "")))
    if kind == "romm":
        return Romm(RommConfig(base_url=url, username=user, password=pw,
                               api_token=key))
    # A registered driver is consulted after the built-ins, never before, so
    # no out-of-tree file can take over a shipped backend.
    builder = _REGISTERED.get(kind)
    if builder is not None:
        try:
            return builder(cfg)
        except Exception as err:  # noqa: BLE001 - a driver may fail any way
            log.warning("library backend %r failed to build: %s: %s",
                        kind, err.__class__.__name__, err)
            return None
    log.warning("ignoring library of unknown kind %r", kind)
    return None


def route_library(configs: list[dict], platform_slug: str) -> dict | None:
    """Which library entry a request for this platform belongs to.

    A platform rule wins over the default, because naming a platform is the more
    specific statement -- the same precedence a longest-prefix path mapping uses.
    With no rule and no default marked, the first enabled entry is used rather
    than none: an install with exactly one library must not have to know that
    "default" was a box it needed to tick.
    """
    enabled = [c for c in configs if c.get("enable", True)]
    if not enabled:
        return None
    if platform_slug:
        for cfg in enabled:
            platforms = cfg.get("platforms") or []
            if isinstance(platforms, str):
                platforms = [p.strip() for p in platforms.split(",") if p.strip()]
            if platform_slug in platforms:
                return cfg
    for cfg in enabled:
        if cfg.get("is_default"):
            return cfg
    return enabled[0]
