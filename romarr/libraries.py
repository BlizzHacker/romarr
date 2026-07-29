"""Game libraries Romarr can hand a finished ROM to.

Romarr started against RomM and grew its assumptions: a `/api/roms` shape, a
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
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

import requests

log = logging.getLogger(__name__)

# Short budget for anything a page waits on. A library that is merely slow must
# not read as Romarr being broken.
HEALTH_TIMEOUT = 5


@dataclass(frozen=True)
class Game:
    """One game, reduced to what a poster grid and an import decision need."""

    id: str
    name: str
    platform: str = ""
    cover: str = ""


@runtime_checkable
class Library(Protocol):
    """What Romarr needs from a game library, and nothing more."""

    name: str

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
        """Ask Gaseous to pick up new files.

        Optional everywhere in Romarr: the ROM is already in the library
        directory by the time this runs, so a failure here is logged and
        reported rather than raised. A successful import must never be
        reported as a failure over a courtesy call.
        """
        self._login()
        try:
            r = self._session.post(self._url("ContentManager/Rescan"),
                                   headers=self._headers(),
                                   timeout=self._config.timeout)
        except requests.RequestException as err:
            log.warning("gaseous rescan failed: %s", err)
            return False
        if not r.ok:
            log.warning("gaseous rescan rejected: %s", r.status_code)
        return r.ok


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
LIBRARY_KINDS = ("romm", "gaseous", "retrom")


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
    return Romm(RommConfig(base_url=url, username=user, password=pw, api_token=key))
