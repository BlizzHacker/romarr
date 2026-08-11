"""Download clients and RomM.

Deliberately thin. Everything interesting already happened in selection.py and
library.py; these are the pieces that talk to other people's daemons, and the
only thing worth being careful about is that credentials never end up in a log
or an API response.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import requests

from .libraries import DEFAULT_BACKGROUND_TIMEOUT, Game

log = logging.getLogger(__name__)


# --------------------------------------------------------------- qBittorrent --

@dataclass(frozen=True)
class QbitConfig:
    base_url: str
    username: str = ""
    password: str = ""
    category: str = "romarr"
    timeout: int = 30


class QBittorrent:
    """Just enough of the WebUI API to add a torrent and watch it finish."""

    # Declared so the service can pick a client by protocol without knowing
    # which class it is holding -- see downloaders.pick_client.
    protocol = "torrent"
    name = "qBittorrent"

    @property
    def configured(self) -> bool:
        return bool(self._config.base_url)

    def __init__(self, config: QbitConfig, session: requests.Session | None = None):
        self._config = config
        self._session = session or requests.Session()
        self._authed = False

    def _url(self, path: str) -> str:
        return f"{self._config.base_url.rstrip('/')}/api/v2/{path.lstrip('/')}"

    def login(self) -> bool:
        # A qBittorrent with WebUI auth disabled for localhost answers every
        # endpoint without a session cookie, so a failed login is not fatal.
        if not self._config.username:
            self._authed = True
            return True
        response = self._session.post(
            self._url("auth/login"),
            data={"username": self._config.username, "password": self._config.password},
            timeout=self._config.timeout,
        )
        self._authed = response.ok and (response.status_code == 204 or response.text.strip() == "Ok.")
        if not self._authed:
            log.warning("qbittorrent login rejected (status %s)", response.status_code)
        return self._authed

    def add(self, magnet_or_url: str, *, save_path: str | None = None) -> bool:
        if not self._authed:
            self.login()
        data = {"urls": magnet_or_url, "category": self._config.category}
        if save_path:
            data["savepath"] = save_path
        response = self._session.post(self._url("torrents/add"), data=data,
                                      timeout=self._config.timeout)
        if not response.ok:
            log.warning("qbittorrent add failed: %s", response.status_code)
            return False
        return True

    def reachable(self) -> bool:
        """Whether the download client answers at all.

        Used by the status page, so a failure is reported rather than raised --
        an unreachable client is a thing to display, not an outage here.
        """
        if not self._config.base_url:
            return False
        try:
            if not self._authed:
                self.login()
            r = self._session.get(self._url("app/version"), timeout=self._config.timeout)
            return r.ok
        except requests.RequestException as err:
            log.warning("qbittorrent unreachable: %s", err)
            return False

    def completed(self) -> list[dict]:
        """Torrents in our category that have finished."""
        if not self._authed:
            self.login()
        response = self._session.get(
            self._url("torrents/info"),
            params={"category": self._config.category, "filter": "completed"},
            timeout=self._config.timeout,
        )
        response.raise_for_status()
        return response.json()


# ---------------------------------------------------------------------- RomM --

@dataclass(frozen=True)
class RommConfig:
    base_url: str
    username: str = ""
    password: str = ""
    api_token: str = ""
    timeout: int = 30


class Romm:
    """Authenticates and asks RomM to rescan after an import.

    One of several libraries ROMarr can drive -- see libraries.py for the
    protocol every backend implements.
    """

    # Shown in the UI so it is obvious which library is attached.
    name = "RomM"

    # Only what a rescan needs. Asking for more than that on a service account
    # is how an integration quietly becomes a way to delete someone's library.
    SCOPES = "roms.read platforms.read collections.read"

    #: The same, plus permission to run the rescan task. Requested FIRST and
    #: fallen back from, because the two RomM versions disagree in opposite
    #: directions and a single answer breaks one of them:
    #:
    #:   * RomM 5.x enforces scopes, so without `tasks.run` every rescan is
    #:     a bare 403 that reads as an account-permission problem.
    #:   * RomM 4.x refuses to ISSUE a token carrying a scope the account
    #:     does not hold -- so demanding it unconditionally 403s the login
    #:     itself and takes the whole library down with it. Proven against a
    #:     live 4.x install, where this exact change broke everything.
    #:
    #: Asking for the better token and quietly accepting the lesser one is
    #: the only shape that serves both.
    SCOPES_WITH_TASKS = ("roms.read platforms.read collections.read "
                         "tasks.run")

    # Health and badge calls get their own short budget; see count().
    HEALTH_TIMEOUT = 5

    # RomM sorts by name using a natural-sort expression -- nested
    # REGEXP_REPLACE over roms.name. Being computed, no index can serve it, so
    # the database evaluates it for every row and then filesorts; LIMIT cannot
    # help because the sort happens first. On a 72,000-row library that is a
    # sixty-second query.
    #
    # Sorting by the primary key instead is free, and the alphabetical order it
    # gives up means nothing here: this feeds a count and a poster grid, not a
    # browsable index. The two aggregate extras are likewise work nobody here
    # consumes.
    CHEAP_QUERY = {
        "order_by": "id",
        "with_char_index": "false",
        "with_filter_values": "false",
    }

    @property
    def configured(self) -> bool:
        return bool(self._config.base_url)

    def __init__(self, config: RommConfig, session: requests.Session | None = None):
        self._config = config
        self._session = session or requests.Session()
        #: RomM 4.x calls the scan task `scan`; 5.x calls it `scan_library`.
        #: Corrected on first use from the server's own error body, then
        #: remembered, so the version difference costs one extra request
        #: per process rather than a broken rescan.
        self._scan_task = "scan"
        self._token = config.api_token or ""

    def token(self) -> str | None:
        if self._token:
            return self._token
        if not (self._config.username and self._config.password):
            return None
        response = None
        for scope in (self.SCOPES_WITH_TASKS, self.SCOPES):
            response = self._session.post(
                f"{self._config.base_url.rstrip('/')}/api/token",
                data={
                    "grant_type": "password",
                    "username": self._config.username,
                    "password": self._config.password,
                    "scope": scope,
                },
                timeout=self._config.timeout,
            )
            if response.ok:
                if scope is self.SCOPES:
                    log.info("romm issued a read-only token: this account "
                             "cannot run tasks, so imports will not trigger "
                             "a rescan and RomM picks them up on its own "
                             "schedule instead")
                break
        if response is None or not response.ok:
            # Never echo the body: a failed auth response can repeat the
            # submitted credentials back at you.
            log.warning("romm auth failed with status %s",
                        getattr(response, "status_code", "no response"))
            return None
        self._token = response.json().get("access_token", "")
        return self._token or None

    def _headers(self) -> dict[str, str]:
        token = self.token()
        return {"Authorization": f"Bearer {token}"} if token else {}

    def _get(self, url: str, params: dict, timeout: int):
        """GET with one re-authentication if the token has expired.

        RomM's access token is short-lived -- about ten minutes. It was fetched
        once and cached for the life of the process, so every call after that
        expiry returned 401 forever: the library refreshed correctly at startup
        and then failed identically for as long as the service stayed up. A
        restart appeared to fix it, which made it look like a slow leak rather
        than an expired credential.

        Only 401 is retried. A 403 is a permissions problem that another token
        will not solve, and retrying it would just double the load while
        producing the same answer.
        """
        response = self._session.get(url, params=params, headers=self._headers(),
                                     timeout=timeout)
        if response.status_code == 401:
            log.info("romm token rejected; re-authenticating")
            self._token = ""
            response = self._session.get(url, params=params,
                                         headers=self._headers(), timeout=timeout)
        return response

    def reachable(self) -> bool:
        """Whether RomM answers, on the health budget rather than the full one.

        This feeds a status page. A dependency that is merely slow must not
        hold the page open for the whole request timeout -- that turns "RomM is
        struggling" into "ROMarr is broken", which is the wrong diagnosis to
        hand somebody.

        The heartbeat is deliberately unauthenticated here: fetching a token
        first would make this as slow as whatever is wrong with the API, which
        is exactly what it is trying to report on.
        """
        try:
            response = self._session.get(
                f"{self._config.base_url.rstrip('/')}/api/heartbeat",
                timeout=self.HEALTH_TIMEOUT,
            )
            return response.ok
        except requests.RequestException as err:
            log.warning("romm unreachable: %s", err.__class__.__name__)
            return False

    def count(self) -> int:
        """How many ROMs RomM has, without fetching any of them.

        The nav badge only needs the number. Asking for 500 full records to
        call len() on them took thirty seconds against a real library and
        stalled every page that showed the badge -- which looked like RomM
        being down rather than a query being wrong.
        """
        url = f"{self._config.base_url.rstrip('/')}/api/roms"
        # A short, separate budget on purpose. This feeds a nav badge, and a
        # slow library endpoint must degrade to "unknown" rather than holding
        # a page open for the full request timeout -- which reads as the whole
        # application being broken when only one count is unavailable.
        response = self._get(url, {"limit": 1, **self.CHEAP_QUERY},
                             self.HEALTH_TIMEOUT)
        response.raise_for_status()
        payload = response.json()
        if isinstance(payload, dict):
            return int(payload.get("total") or len(payload.get("items") or []))
        return len(payload)

    # The background fetch gets a long budget on purpose. Nothing waits on it,
    # and RomM's library query takes around three minutes on a large, contended
    # library -- so a thirty-second budget could never have succeeded, which is
    # why the shelf stayed empty no matter how often it retried.
    #
    # Shared with the other backends rather than owned here: the service reads
    # this off whichever library is attached, so it is part of the protocol.
    BACKGROUND_TIMEOUT = DEFAULT_BACKGROUND_TIMEOUT

    def platforms(self) -> list[dict]:
        """Every platform and its rom count, in one fast call.

        The shelf's platform chips used to be derived from whatever rows the
        background walk had cached so far, which meant a 166,578-game
        library showed eight platforms for the first several minutes and
        grew them one at a time -- a person looking for PC or Xbox
        concluded they were not supported.

        RomM knows all of this instantly and exactly. Asking it is one
        request against an endpoint with no rows in it, and the answer is
        the whole truth rather than a prefix of it.
        """
        url = f"{self._config.base_url.rstrip('/')}/api/platforms"
        response = self._get(url, {}, self.HEALTH_TIMEOUT)
        response.raise_for_status()
        payload = response.json()
        rows = payload if isinstance(payload, list) else payload.get("items", [])
        out = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            count = int(row.get("rom_count") or 0)
            # Platforms RomM defines but holds nothing for are noise: it
            # ships 393 of them and this library has content in 84.
            if count <= 0:
                continue
            out.append({
                "platform": str(row.get("display_name") or row.get("name")
                                or row.get("slug") or ""),
                "slug": str(row.get("slug") or ""),
                "count": count,
            })
        return sorted(out, key=lambda p: (-p["count"], p["platform"]))

    def hashes(self, limit: int = 500, offset: int = 0,
               timeout: int | None = None) -> tuple[list[dict], int]:
        """One page of `{sha1, name, platform, verified}` straight from RomM.

        Returns `(rows, seen)`, and the second number is load-bearing: most
        of a large RomM is entries with no hash at all -- catalogued cloud
        titles that were never scanned off a disk. Filtering those out makes
        a full page look short, so a caller that paginates on `len(rows)`
        stops after the first page. That is not hypothetical: it read 491
        dumps out of 166,578 and reported success.

        RomM stores `sha1_hash`, `md5_hash` and `crc_hash` against every rom
        and serves them on the normal listing, which means netplay does not
        need ROMarr to have walked and hashed the library itself. That matters
        twice over: it makes matching work on a library too large to audit in
        an evening, and it is what lets ROMarr agree on bytes with somebody
        running a plain RomM that has never heard of this protocol.

        `verified` is deliberately False. A hash from RomM says what the file
        is, not that anybody checked it against No-Intro or Redump -- that
        remains ROMarr's own claim to make, and overstating it here would put
        a "verified" badge on a shelf nobody verified.
        """
        url = f"{self._config.base_url.rstrip('/')}/api/roms"
        response = self._get(url, {"limit": limit, "offset": offset},
                             timeout or self._config.timeout)
        response.raise_for_status()
        payload = response.json()
        items = payload.get("items",
                            payload if isinstance(payload, list) else [])
        out = []
        for it in items:
            if not isinstance(it, dict):
                continue
            sha1 = str(it.get("sha1_hash") or "").strip().lower()
            if not sha1:
                # Unidentified or still scanning. Skipped rather than
                # guessed at: a netplay match on a missing hash is the
                # failure this whole mechanism exists to prevent.
                continue
            out.append({
                "sha1": sha1,
                "name": it.get("name") or it.get("fs_name_no_tags")
                        or it.get("fs_name") or "",
                "platform": it.get("platform_slug") or "",
                "verified": False,
                "path": it.get("fs_name") or "",
            })
        return out, len(items)

    def games(self, limit: int = 60, offset: int = 0,
              timeout: int | None = None) -> list[dict]:
        """The library, flattened to what a poster grid needs.

        RomM's rom payload is large and mostly irrelevant here; sending all of
        it to a browser to render a name and a cover is wasteful, so it is
        reduced server-side.
        """
        url = f"{self._config.base_url.rstrip('/')}/api/roms"
        try:
            response = self._get(
                url, {"limit": limit, "offset": offset, **self.CHEAP_QUERY},
                timeout or self._config.timeout)
            response.raise_for_status()
            payload = response.json()
        except (requests.RequestException, ValueError) as err:
            log.warning("romm library read failed: %s", err)
            raise

        items = payload.get("items", payload if isinstance(payload, list) else [])
        base = self._config.base_url.rstrip("/")
        out = []
        for it in items:
            if not isinstance(it, dict):
                continue
            cover = it.get("path_cover_small") or it.get("path_cover_large") or ""
            if cover and not cover.startswith("http"):
                cover = f"{base}/{cover.lstrip('/')}"
            # RomM keeps what it learned from IGDB and friends in `metadatum`,
            # merged across whichever providers identified the game. Absent
            # for anything unidentified, which is most of a fresh library --
            # so every read is defensive and an empty result is normal.
            meta = it.get("metadatum") if isinstance(it.get("metadatum"), dict) else {}
            genres = tuple(str(g) for g in (meta.get("genres") or [])
                           if isinstance(g, str))
            regions = tuple(str(r) for r in (it.get("regions") or [])
                            if isinstance(r, str))
            year = 0
            released = meta.get("first_release_date")
            if isinstance(released, (int, float)) and released > 0:
                # RomM stores this as a unix timestamp; seconds or ms
                # depending on the provider that filled it in.
                from datetime import datetime, timezone
                stamp = released / 1000 if released > 10_000_000_000 else released
                try:
                    year = datetime.fromtimestamp(stamp, timezone.utc).year
                except (ValueError, OSError, OverflowError):
                    year = 0
            elif isinstance(released, str) and released[:4].isdigit():
                year = int(released[:4])
            try:
                rating = round(float(meta.get("average_rating") or 0), 1)
            except (TypeError, ValueError):
                rating = 0.0

            # Local or catalogued-elsewhere, from RomM's own bookkeeping.
            # `missing_from_fs` means the row exists in the database and the
            # bytes do not exist on disk -- which is precisely what a
            # plugin-catalogued Archive.org or Flashpoint entry looks like.
            # On a large install this is the majority of the shelf and the
            # single most useful axis there is: "plays right now" versus
            # "would have to be fetched first".
            out.append(Game(
                id=str(it.get("id") or ""),
                name=str(it.get("name") or it.get("fs_name") or "Unknown"),
                platform=str(it.get("platform_display_name")
                             or it.get("platform_slug") or ""),
                cover=cover,
                genres=genres,
                regions=regions,
                year=year,
                rating=rating,
                origin="cloud" if it.get("missing_from_fs") else "local",
            ))
        return out

    def rescan(self, platform_slug: str | None = None) -> bool:
        """Ask RomM to pick up newly-imported files.

        Optional by design. The import has already put the ROM in the library
        directory by the time this runs, so RomM finds it on its next scheduled
        scan regardless -- this only makes it immediate. A failure here is
        logged and reported, never raised, because a successful import must not
        be reported as a failure over a courtesy call.

        RomM 4.x runs this through its task API. The older /api/scan is gone and
        returned 404, which looked like a broken service rather than a moved
        endpoint.
        """
        base = self._config.base_url.rstrip("/")
        payload: dict = {"scan_type": "quick"}
        if platform_slug:
            payload["platforms"] = [platform_slug]
        try:
            response = self._session.post(
                f"{base}/api/tasks/run/{self._scan_task}", json=payload,
                headers=self._headers(), timeout=self._config.timeout)
        except requests.RequestException as err:
            log.warning("romm rescan failed: %s", err)
            return False

        # RomM renamed this task between 4.x and 5.x -- `scan` became
        # `scan_library`, and 5.x answers the old name with a 4xx whose body
        # lists the real ones. Proven against a live RomM 5.1.0: the old
        # name failed there and was reported as a PERMISSION problem, which
        # sent the operator hunting through RomM's user settings for a
        # setting that was never involved. Learn the name, once, and retry.
        if not response.ok and self._scan_task in str(response.text or ""):
            for name in ("scan_library", "scan"):
                if name == self._scan_task:
                    continue
                if name not in str(response.text or ""):
                    continue
                log.info("romm names its scan task %r on this version", name)
                self._scan_task = name
                try:
                    response = self._session.post(
                        f"{base}/api/tasks/run/{name}", json=payload,
                        headers=self._headers(),
                        timeout=self._config.timeout)
                except requests.RequestException as err:
                    log.warning("romm rescan failed: %s", err)
                    return False
                break

        if response.status_code in (401, 403) and                 "not found" not in str(response.text or "").lower():
            # Distinguished from a generic failure because the fix is specific
            # and otherwise invisible: the service account needs the task
            # permission in RomM. Everything else about the import worked.
            log.warning(
                "romm rescan refused (%s): the account %r cannot run tasks. "
                "Grant it task permission in RomM, or the library picks the "
                "ROM up on its next scheduled scan.",
                response.status_code, self._config.username or "(token)")
            return False
        if not response.ok:
            log.warning("romm rescan rejected: %s %s", response.status_code,
                        str(response.text or "")[:160])
            return False
        return True
