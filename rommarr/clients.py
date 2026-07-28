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

log = logging.getLogger(__name__)


# --------------------------------------------------------------- qBittorrent --

@dataclass(frozen=True)
class QbitConfig:
    base_url: str
    username: str = ""
    password: str = ""
    category: str = "rommarr"
    timeout: int = 30


class QBittorrent:
    """Just enough of the WebUI API to add a torrent and watch it finish."""

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
        self._authed = response.ok and response.text.strip() == "Ok."
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
    """Authenticates and asks RomM to rescan after an import."""

    # Only what a rescan needs. Asking for more than that on a service account
    # is how an integration quietly becomes a way to delete someone's library.
    SCOPES = "roms.read platforms.read collections.read"

    def __init__(self, config: RommConfig, session: requests.Session | None = None):
        self._config = config
        self._session = session or requests.Session()
        self._token = config.api_token or ""

    def token(self) -> str | None:
        if self._token:
            return self._token
        if not (self._config.username and self._config.password):
            return None
        response = self._session.post(
            f"{self._config.base_url.rstrip('/')}/api/token",
            data={
                "grant_type": "password",
                "username": self._config.username,
                "password": self._config.password,
                "scope": self.SCOPES,
            },
            timeout=self._config.timeout,
        )
        if not response.ok:
            # Never echo the body: a failed auth response can repeat the
            # submitted credentials back at you.
            log.warning("romm auth failed with status %s", response.status_code)
            return None
        self._token = response.json().get("access_token", "")
        return self._token or None

    def _headers(self) -> dict[str, str]:
        token = self.token()
        return {"Authorization": f"Bearer {token}"} if token else {}

    def reachable(self) -> bool:
        try:
            response = self._session.get(
                f"{self._config.base_url.rstrip('/')}/api/heartbeat",
                headers=self._headers(), timeout=self._config.timeout,
            )
            return response.ok
        except requests.RequestException as err:
            log.warning("romm unreachable: %s", err)
            return False

    def rescan(self, platform_slug: str | None = None) -> bool:
        """Ask RomM to pick up newly-imported files."""
        url = f"{self._config.base_url.rstrip('/')}/api/scan"
        payload: dict = {"scan_type": "quick"}
        if platform_slug:
            payload["platforms"] = [platform_slug]
        try:
            response = self._session.post(url, json=payload, headers=self._headers(),
                                          timeout=self._config.timeout)
        except requests.RequestException as err:
            log.warning("romm rescan failed: %s", err)
            return False
        if not response.ok:
            log.warning("romm rescan rejected: %s", response.status_code)
            return False
        return True
