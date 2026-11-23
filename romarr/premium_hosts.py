"""Premium file-host accounts, and MultiUp's mirrors, for `hosters.HosterRoute`.

Debrid services already unlock links on most file hosts. These cover the
other way people pay for downloads: an account on the host itself.

* **Rapidgator** -- API v2. ``user/login`` (e-mail, password, and the 2FA
  code when the account has one) gives a session token; ``file/download``
  turns a file id into a time-limited direct URL. The token is kept and
  reused, and a fresh login replaces it when Rapidgator says the session is
  gone.
* **1fichier** -- the account's API key; ``download/get_token.cgi`` turns a
  link into a direct URL valid for five minutes. Premium accounts only, by
  1fichier's rules.
* **MultiUp** -- not a host but a mirror service: one link stands for copies
  of the same file on several hosts. `multiup_mirrors` asks MultiUp's public
  ``check-file`` API whether the file still exists and reads its mirror page
  for the links, so `HosterRoute` can try each copy with whatever route
  takes it (a public API, an account here, a debrid service).

The direct URL each resolver produces goes to the direct download client
(Mister Download Manager or Direct HTTP), exactly like pixeldrain's public
API links. Credentials live in the cloud secret store, never in settings.
"""
from __future__ import annotations

import html
import logging
import re
import threading
from urllib.parse import urlsplit

import requests

from .hosters import host_label

log = logging.getLogger(__name__)

TIMEOUT = 30
#: MultiUp is asked once per grab that has a MultiUp link; keep the connection.
_SESSION = requests.Session()
_RG_FILE = re.compile(r"^/file/([A-Za-z0-9]+)(?:/|$)")
_MULTIUP_ID = re.compile(r"^/(?:[a-z]{2}/)?(?:mirror|download|project)/([0-9a-f]{20,40})(?:/([^/?#]+))?",
                         re.I)
_LINKS = re.compile(r"""(?:link|href)\s*=\s*["'](https?://[^"'\s<>]+)["']""", re.I)


class HostAccountError(RuntimeError):
    """The host refused; the message is safe to show."""


class Rapidgator:
    label = "Rapidgator"
    hosts = ("rapidgator.net", "rg.to")
    API = "https://rapidgator.net/api/v2"

    def __init__(self, login: str, password: str, *, token: str = "", on_token=None,
                 session: requests.Session | None = None):
        self._login = login
        self._password = password
        self._token = token
        self._on_token = on_token
        self._session = session or requests.Session()
        self._lock = threading.Lock()

    @property
    def configured(self) -> bool:
        return bool(self._login and (self._password or self._token))

    def supports_host(self, host: str) -> bool:
        return host_label(f"https://{host}/") == self.label

    def _call(self, path: str, params: dict) -> dict:
        try:
            response = self._session.post(f"{self.API}/{path}", data=params, timeout=TIMEOUT)
            body = response.json()
        except (requests.RequestException, ValueError) as err:
            raise HostAccountError(f"Rapidgator did not answer: {err.__class__.__name__}") from None
        if not isinstance(body, dict):
            raise HostAccountError("Rapidgator answered with something unexpected")
        return body

    def sign_in(self, code: str = "") -> dict:
        """Log in and keep the token; returns the account summary."""
        params = {"login": self._login, "password": self._password}
        if code:
            params["code"] = code
        body = self._call("user/login", params)
        response = body.get("response") or {}
        if body.get("status") != 200 or not response.get("token"):
            raise HostAccountError(f"Rapidgator refused the sign-in: {str(body.get('details') or '')[:200]}")
        with self._lock:
            self._token = response["token"]
        if self._on_token:
            self._on_token(self._token)
        user = response.get("user") or {}
        return {"premium": bool(user.get("is_premium")),
                "traffic_left": (user.get("traffic") or {}).get("left")}

    def resolve(self, url: str) -> str:
        found = _RG_FILE.match(urlsplit(url).path or "")
        if not found:
            raise HostAccountError("not a Rapidgator file link")
        for attempt in (1, 2):
            if not self._token:
                self.sign_in()
            body = self._call("file/download", {"file_id": found.group(1), "token": self._token})
            if body.get("status") == 200:
                link = (body.get("response") or {}).get("download_url") or ""
                if link.startswith("https://") or link.startswith("http://"):
                    return link
                raise HostAccountError("Rapidgator gave no download address")
            if body.get("status") == 401 and attempt == 1:
                self._token = ""  # session expired: log in again once
                continue
            raise HostAccountError(f"Rapidgator: {str(body.get('details') or body.get('status'))[:200]}")
        raise HostAccountError("Rapidgator sign-in expired")


class OneFichier:
    label = "1fichier"
    API = "https://api.1fichier.com/v1"

    def __init__(self, api_key: str, *, session: requests.Session | None = None):
        self._key = api_key
        self._session = session or requests.Session()

    @property
    def configured(self) -> bool:
        return bool(self._key)

    def supports_host(self, host: str) -> bool:
        return host_label(f"https://{host}/") == self.label

    def _call(self, path: str, payload: dict) -> dict:
        try:
            response = self._session.post(f"{self.API}/{path}", json=payload, timeout=TIMEOUT,
                                          headers={"Authorization": f"Bearer {self._key}"})
            body = response.json()
        except (requests.RequestException, ValueError) as err:
            raise HostAccountError(f"1fichier did not answer: {err.__class__.__name__}") from None
        if body.get("status") == "KO" or response.status_code >= 400:
            raise HostAccountError(f"1fichier: {str(body.get('message') or response.status_code)[:200]}")
        return body

    def sign_in(self, code: str = "") -> dict:
        body = self._call("user/info.cgi", {})
        return {"premium": str(body.get("offer") or "").lower() not in ("", "free"),
                "email": "set" if body.get("email") else ""}

    def resolve(self, url: str) -> str:
        link = str(self._call("download/get_token.cgi", {"url": url}).get("url") or "")
        if not link.startswith(("https://", "http://")):
            raise HostAccountError("1fichier gave no download address")
        return link


def is_multiup(url: str) -> bool:
    return host_label(url) == "MultiUp"


def multiup_mirrors(url: str, *, session: requests.Session | None = None) -> list[str]:
    """The links on other hosts that a MultiUp link stands for; [] if none."""
    session = session or _SESSION
    parts = urlsplit(url)
    found = _MULTIUP_ID.match(parts.path or "")
    if not found:
        return []
    file_hash, name = found.group(1), found.group(2) or "file"
    try:
        check = session.post("https://multiup.io/api/check-file", data={"link": url},
                             timeout=TIMEOUT).json()
    except (requests.RequestException, ValueError):
        check = {}
    if isinstance(check, dict) and str(check.get("error", "")).lower() not in ("success", ""):
        log.info("MultiUp says %s is gone: %s", file_hash, str(check.get("error"))[:80])
        return []
    links = _links_in(check.get("hosts") if isinstance(check, dict) else None)
    if not links:
        try:
            page = session.get(f"https://multiup.io/en/mirror/{file_hash}/{name}",
                               timeout=TIMEOUT, headers={"User-Agent": "Mozilla/5.0 ROMarr"})
            links = [html.unescape(m) for m in _LINKS.findall(page.text or "")]
        except requests.RequestException:
            links = []
    seen, mirrors = set(), []
    for link in links:
        label = host_label(link)
        if label and label != "MultiUp" and link not in seen:
            seen.add(link)
            mirrors.append(link)
    return mirrors


def _links_in(hosts) -> list[str]:
    """check-file's `hosts` value, whatever shape it came in, as URLs."""
    out = []
    if isinstance(hosts, dict):
        for value in hosts.values():
            if isinstance(value, str) and value.startswith("http"):
                out.append(value)
            elif isinstance(value, dict):
                out.extend(v for v in value.values() if isinstance(v, str) and v.startswith("http"))
    elif isinstance(hosts, list):
        for value in hosts:
            if isinstance(value, str) and value.startswith("http"):
                out.append(value)
            elif isinstance(value, dict):
                out.extend(v for v in value.values() if isinstance(v, str) and v.startswith("http"))
    return out
