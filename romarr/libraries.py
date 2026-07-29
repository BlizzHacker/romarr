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
  * Gaseous   -- REST, /api/v1.1/Games. Search is a POST with a JSON body
                 rather than a query string, which is unusual enough to be
                 worth stating.
  * Retrom    -- REST gateway in front of a gRPC service, so its JSON is
                 protobuf-shaped: fields are camelCase and a "game" carries its
                 metadata in a nested object.
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

    Its games endpoint is a POST even for a plain listing -- the filter is a
    JSON body, not a query string -- so a GET returns 405 and looks like a
    wrong path rather than a wrong method.
    """

    name = "Gaseous"

    def __init__(self, config: GaseousConfig, session: requests.Session | None = None):
        self._config = config
        self._session = session or requests.Session()

    @property
    def configured(self) -> bool:
        return bool(self._config.base_url)

    def _url(self, path: str) -> str:
        return f"{self._config.base_url.rstrip('/')}/api/v1.1/{path.lstrip('/')}"

    def _headers(self) -> dict[str, str]:
        if self._config.api_key:
            return {"Authorization": f"Bearer {self._config.api_key}"}
        return {}

    def reachable(self) -> bool:
        try:
            r = self._session.get(self._url("HealthCheck"), headers=self._headers(),
                                  timeout=HEALTH_TIMEOUT)
            return r.ok
        except requests.RequestException as err:
            log.warning("gaseous unreachable: %s", err.__class__.__name__)
            return False

    def _search(self, limit: int, offset: int, timeout: int | None):
        body = {
            "pageNumber": (offset // max(limit, 1)) + 1,
            "pageSize": limit,
            "sortField": "NameThe",
            "sortAscending": True,
        }
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
                # Gaseous serves cover art from the game id rather than a path
                # in the payload.
                cover=_absolute(base, f"/api/v1.1/Games/{gid}/cover/image"),
            ))
        return out

    def rescan(self, platform_slug: str | None = None) -> bool:
        """Ask Gaseous to pick up new files.

        Optional everywhere in Romarr: the ROM is already in the library
        directory by the time this runs, so a failure here is logged and
        reported rather than raised. A successful import must never be called
        a failure over a courtesy call.
        """
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

    Retrom is gRPC with a REST gateway in front, so its JSON is
    protobuf-shaped: camelCase keys, and a game's human details sit in a
    nested metadata object rather than on the row itself.
    """

    name = "Retrom"

    def __init__(self, config: RetromConfig, session: requests.Session | None = None):
        self._config = config
        self._session = session or requests.Session()

    @property
    def configured(self) -> bool:
        return bool(self._config.base_url)

    def _url(self, path: str) -> str:
        return f"{self._config.base_url.rstrip('/')}/{path.lstrip('/')}"

    def _headers(self) -> dict[str, str]:
        if self._config.api_key:
            return {"Authorization": f"Bearer {self._config.api_key}"}
        return {}

    def reachable(self) -> bool:
        try:
            r = self._session.get(self._url("health"), headers=self._headers(),
                                  timeout=HEALTH_TIMEOUT)
            return r.ok
        except requests.RequestException as err:
            log.warning("retrom unreachable: %s", err.__class__.__name__)
            return False

    def _fetch(self, limit: int, offset: int, timeout: int | None):
        r = self._session.get(
            self._url("rest/games"),
            params={"limit": limit, "offset": offset},
            headers=self._headers(),
            timeout=timeout or self._config.timeout,
        )
        r.raise_for_status()
        return r.json()

    def count(self) -> int:
        payload = self._fetch(limit=1, offset=0, timeout=HEALTH_TIMEOUT)
        if isinstance(payload, dict):
            for key in ("totalCount", "total", "count"):
                if isinstance(payload.get(key), int):
                    return payload[key]
            return len(payload.get("games") or [])
        return len(payload or [])

    def games(self, limit: int = 60, offset: int = 0,
              timeout: int | None = None) -> list[Game]:
        payload = self._fetch(limit, offset, timeout)
        rows = payload if isinstance(payload, list) else (payload.get("games") or [])
        base = self._config.base_url
        out: list[Game] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            meta = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
            gid = str(row.get("id") or "")
            if not gid:
                continue
            name = str(meta.get("name") or row.get("path") or "Unknown")
            # A path fallback would otherwise show the whole filesystem path.
            if name == row.get("path"):
                name = name.replace("\\", "/").rstrip("/").split("/")[-1]
            out.append(Game(
                id=gid,
                name=name,
                platform=str(row.get("platformId") or ""),
                cover=_absolute(base, str(meta.get("coverUrl") or "")),
            ))
        return out

    def rescan(self, platform_slug: str | None = None) -> bool:
        try:
            r = self._session.post(self._url("rest/library/scan"),
                                   headers=self._headers(),
                                   timeout=self._config.timeout)
        except requests.RequestException as err:
            log.warning("retrom rescan failed: %s", err)
            return False
        if not r.ok:
            log.warning("retrom rescan rejected: %s", r.status_code)
        return r.ok


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
