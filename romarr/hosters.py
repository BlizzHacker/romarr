"""File-host links: which ones ROMarr can fetch, and who fetches them.

A file host (gofile, 1fichier, pixeldrain, buzzheavier…) hands out a page,
not a file. Downloading that URL directly saves an HTML page under a ROM's
name. So a release whose link is a file host's page has its own protocol,
``hoster``, and `HosterRoute` decides per link:

1. **The host's own public download API**, where it has one that needs no
   account and no challenge: pixeldrain's ``/api/file/<id>?download``. The
   resulting file URL goes to the direct downloader -- Mister Download
   Manager or the Direct HTTP client -- like any other direct link.
2. **A direct file** already (fileditch and similar serve the file itself).
3. **A premium account on the host itself** (Rapidgator, 1fichier --
   `premium_hosts`), which turns the link into a direct URL for the direct
   downloader.
4. **A debrid service** (Real-Debrid, AllDebrid) that unlocks hoster links
   on the account the operator configured.

A MultiUp link is a list of mirrors rather than a file; it is replaced by
the mirrors MultiUp reports, and each of those goes through the same steps.

ROMarr never answers a captcha, waits out a countdown or replays a browser
session to get round a host's free-user limits. A link no route can take
fails the grab with a sentence naming the host and what would fetch it.
"""
from __future__ import annotations

import logging
import re
from collections import deque
from urllib.parse import urlsplit

log = logging.getLogger(__name__)

#: Hosts whose links are pages, not files. Matching is on the registrable
#: name, so www./cdn. prefixes and the hosts' alternate domains all count.
FILE_HOSTS = {
    "1fichier.com": "1fichier", "alterupload.com": "1fichier",
    "cjoint.net": "1fichier", "desfichiers.com": "1fichier",
    "dfichiers.com": "1fichier", "megadl.fr": "1fichier", "mesfichiers.org": "1fichier",
    "piecejointe.net": "1fichier", "pjointe.com": "1fichier", "tenvoi.com": "1fichier",
    "dl4free.com": "1fichier",
    "gofile.io": "Gofile",
    "pixeldrain.com": "pixeldrain", "pixeldra.in": "pixeldrain",
    "buzzheavier.com": "BuzzHeavier", "bzzhr.to": "BuzzHeavier",
    "bzzhr.co": "BuzzHeavier", "flashbang.sh": "BuzzHeavier",
    "megadb.net": "MegaDB",
    "mediafire.com": "MediaFire",
    "mega.nz": "MEGA", "mega.co.nz": "MEGA",
    "datanodes.to": "DataNodes",
    "vikingfile.com": "VikingFile", "vikingf.com": "VikingFile",
    "rapidgator.net": "Rapidgator", "rg.to": "Rapidgator",
    "uploadhaven.com": "UploadHaven",
    "krakenfiles.com": "KrakenFiles",
    "qiwi.gg": "Qiwi",
    "multiup.io": "MultiUp", "multiup.org": "MultiUp",
    "filecrypt.cc": "FileCrypt", "filecrypt.co": "FileCrypt",
    "send.cm": "Send.cm", "usersdrive.com": "UsersDrive",
    "uptobox.com": "Uptobox", "hexload.com": "Hexload",
    "ddownload.com": "DDownload", "nitroflare.com": "Nitroflare",
    "turbobit.net": "Turbobit", "katfile.com": "Katfile",
    "upload.ee": "Upload.ee", "akirabox.com": "AkiraBox", "akirabox.to": "AkiraBox",
    "torbox.app": "TorBox",
}

#: Link containers: a page listing other links behind a challenge. Nothing
#: can fetch these unattended, so they are never chosen when anything else is.
CONTAINERS = frozenset({"FileCrypt"})

#: Hosts a premium account on the host itself can unlock (`premium_hosts`).
PREMIUM_LABELS = ("Rapidgator", "1fichier")

#: Hosts that serve the file itself at the URL a source lists.
DIRECT_HOSTS = ("fileditchfiles.st", "itsfileditchfiles.st", "fileditch.com")

_FILE_SUFFIXES = (
    ".zip", ".7z", ".rar", ".iso", ".bin", ".cue", ".chd", ".rvz", ".wbfs",
    ".nsp", ".nsz", ".xci", ".xcz", ".pkg", ".cso", ".pbp", ".gdi", ".cdi",
    ".tar", ".gz", ".xz", ".zst", ".wua", ".wux", ".3ds", ".cia", ".nds",
    ".gba", ".gb", ".gbc", ".nes", ".sfc", ".smc", ".n64", ".z64", ".v64",
    ".md", ".gen", ".sms", ".gg", ".pce", ".a26", ".lnx", ".exe", ".msi",
)
_PART_SUFFIX = re.compile(r"\.(?:r\d{2}|z\d{2}|\d{3}|part\d+\.rar)$")


def host_of(url: str) -> str:
    try:
        host = (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""
    return host


def host_label(url: str) -> str:
    """The file host a link belongs to, or ""."""
    host = host_of(url)
    while host:
        if host in FILE_HOSTS:
            return FILE_HOSTS[host]
        if "." not in host:
            break
        host = host.split(".", 1)[1]
    return ""


def is_file_host(url: str) -> bool:
    return bool(host_label(url))


def looks_like_file(url: str) -> bool:
    """True when the URL serves a file rather than a page."""
    host = host_of(url)
    if any(host == d or host.endswith("." + d) for d in DIRECT_HOSTS):
        return True
    try:
        path = urlsplit(url).path.lower()
    except ValueError:
        return False
    return path.endswith(_FILE_SUFFIXES) or bool(_PART_SUFFIX.search(path))


_PIXELDRAIN_FILE = re.compile(r"^/u/([A-Za-z0-9]+)/?$")
_PIXELDRAIN_LIST = re.compile(r"^/l/([A-Za-z0-9]+)/?$")


def public_file_url(url: str) -> str:
    """The direct file URL a host's own public API gives for `url`, or "".

    Only hosts that publish such an API for anonymous use are here. A host
    that gates downloads behind a challenge or a timer is deliberately not.
    """
    if host_label(url) == "pixeldrain":
        path = urlsplit(url).path
        file_match = _PIXELDRAIN_FILE.match(path)
        if file_match:
            return f"https://pixeldrain.com/api/file/{file_match.group(1)}?download"
        list_match = _PIXELDRAIN_LIST.match(path)
        if list_match:
            return f"https://pixeldrain.com/api/list/{list_match.group(1)}/zip"
    return ""


#: Where `HosterRoute.for_clients` finds premium host accounts. The cloud
#: storage service registers a callable at startup; until then there are none.
_accounts_source = None


def register_accounts(source) -> None:
    """Set the callable that returns the configured premium host accounts."""
    global _accounts_source
    _accounts_source = source


def premium_accounts() -> list:
    if _accounts_source is None:
        return []
    try:
        return [a for a in (_accounts_source() or []) if getattr(a, "configured", True)]
    except Exception:   # an account store problem must never break a grab
        log.warning("premium host accounts could not be read", exc_info=True)
        return []


def fresh_link(link: str) -> str:
    """A newly unlocked direct URL for a premium-host link, or "".

    Some hosts' download URLs expire within minutes (1fichier's tokens last
    five), so a queued download asks again just before it fetches.
    """
    host = host_of(link)
    for account in premium_accounts():
        supports = getattr(account, "supports_host", None)
        if callable(supports) and supports(host):
            try:
                return str(account.resolve(link) or "")
            except Exception as err:   # noqa: BLE001 - the queued URL is still tried
                log.info("%s could not renew a link: %s", getattr(account, "label", "account"), err)
    return ""


def _hand(client, url: str, name: str, *, source: str = "") -> bool:
    if source and getattr(client, "TAKES_SOURCE", False):
        return bool(client.add(url, name=name, source=source))
    if name and getattr(client, "TAKES_NAME", False):
        return bool(client.add(url, name=name))
    return bool(client.add(url))


class HosterRoute:
    """The download client for ``hoster`` releases, assembled per grab.

    It is not configured anywhere: it exists when something it can hand a
    link to exists -- a direct client (Mister Download Manager, Direct HTTP)
    for links a host's public API turns into files, and debrid clients for
    the rest. After `add` it reports the client that took the job, so the
    queue row records the real owner and the import sweep matches the
    finished download to it.
    """

    protocol = "hoster"
    TAKES_NAME = True
    name = "File host"

    def __init__(self, *, direct=None, debrid=(), premium=(), expand=None):
        self._direct = direct
        self._debrid = [c for c in debrid if c is not None]
        # A list, or a callable that produces one: read only when a link's host
        # could use an account, so ordinary grabs never touch the account store.
        self._premium_source = premium
        self._premium_cache: list | None = None
        self._expand = expand
        self._taken_by = None
        self.last_error = ""

    @classmethod
    def for_clients(cls, clients) -> "HosterRoute | None":
        from .premium_hosts import multiup_mirrors

        direct = next((c for c in clients
                       if getattr(c, "protocol", "") == "direct"
                       and getattr(c, "configured", True)), None)
        debrid = [c for c in clients
                  if getattr(c, "takes_hoster_links", False)
                  and getattr(c, "configured", True)]
        if direct is None and not debrid:
            return None
        premium = premium_accounts if direct is not None else ()
        return cls(direct=direct, debrid=debrid, premium=premium, expand=multiup_mirrors)

    @property
    def _premium(self) -> list:
        if self._premium_cache is None:
            source = self._premium_source
            items = source() if callable(source) else source
            self._premium_cache = [a for a in items or () if a is not None]
        return self._premium_cache

    def _mirrors(self, link: str) -> list[str]:
        """What a MultiUp link stands for; read only when its turn comes."""
        try:
            return list(self._expand(link) or [])
        except Exception:   # MultiUp being down is "no mirrors", not a crash
            log.warning("MultiUp mirrors could not be read", exc_info=True)
            return []

    def _premium_take(self, link: str, name: str) -> bool:
        if self._direct is None:
            return False
        host = host_of(link)
        if not any(host_label(f"https://{host}/") == label for label in PREMIUM_LABELS):
            return False
        for account in self._premium:
            supports = getattr(account, "supports_host", None)
            if not callable(supports) or not supports(host):
                continue
            try:
                file_url = account.resolve(link)
            except Exception as err:   # noqa: BLE001 - one account's refusal is not fatal
                log.info("%s could not unlock a link: %s", getattr(account, "label", "account"), err)
                continue
            # The original link travels with the job, so a queued download can
            # unlock it again if the first URL has expired by its turn.
            if file_url and _hand(self._direct, file_url, name, source=link):
                self._taken_by = self._direct
                return True
        return False

    @property
    def configured(self) -> bool:
        return self._direct is not None or bool(self._debrid)

    @property
    def romarr_id(self) -> str:
        return getattr(self._taken_by, "romarr_id", "") if self._taken_by else ""

    @property
    def last_job_id(self) -> str:
        return getattr(self._taken_by, "last_job_id", "") if self._taken_by else ""

    def add(self, url: str, *, name: str = "", mirrors=(), save_path=None) -> bool:
        """Hand the first link some route can take; try mirrors in order."""
        candidates = [u for u in [url, *mirrors] if u]
        candidates.sort(key=lambda u: host_label(u) in CONTAINERS)
        queue, seen, tried = deque(candidates), set(), []
        while queue:
            link = queue.popleft()
            if link in seen:
                continue
            seen.add(link)
            if self._expand is not None and host_label(link) == "MultiUp":
                expanded = [m for m in self._mirrors(link) if m not in seen]
                if expanded:
                    queue.extendleft(reversed(expanded))
                    continue
            label = host_label(link) or host_of(link) or "link"
            tried.append(label)
            if host_label(link) in CONTAINERS:
                continue
            file_url = public_file_url(link) or (
                link if looks_like_file(link) and not is_file_host(link) else "")
            if file_url and self._direct is not None:
                if _hand(self._direct, file_url, name):
                    self._taken_by = self._direct
                    return True
            if self._premium_take(link, name):
                return True
            for debrid in self._debrid:
                supports = getattr(debrid, "supports_host", None)
                if callable(supports) and not supports(host_of(link)):
                    continue
                # Ask before taking it: a service records a link it cannot
                # fetch and fails later, after the mirrors were given up.
                available = getattr(debrid, "link_available", None)
                if callable(available) and not available(link):
                    continue
                if _hand(debrid, link, name):
                    self._taken_by = debrid
                    return True
        hosts = ", ".join(dict.fromkeys(tried)) or "none"
        needs = []
        if self._direct is None:
            needs.append("a direct download client (Mister Download Manager or Direct HTTP)")
        if not self._debrid and not self._premium:
            needs.append("Real-Debrid or AllDebrid, or a Rapidgator or 1fichier account "
                         "(Settings -> Cloud & File Hosts), for file hosts without a public API")
        self.last_error = (f"no route could fetch this release's file-host links ({hosts})"
                           + (f"; add {' or '.join(needs)}" if needs else ""))
        return False
