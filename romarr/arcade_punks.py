"""Arcade Punks: their download links, their pack names, and their NZBGet setup.

Arcade Punks (https://www.arcadepunks.com) publishes pre-built retro gaming
images -- Batocera, RetroBat, CoinOps, Attract Mode, Raspberry Pi and handheld
builds -- plus arcade conversions and media packs. Its two Usenet guides
(https://www.arcadepunks.com/how-to-get-started-with-nzbget/ and
https://www.arcadepunks.com/how-to-set-up-usenet-for-arcade-punks-retro-gaming-content/)
describe one flow: an account with a Usenet provider over SSL on port 563,
a client such as NZBGet that downloads, PAR2-repairs and unpacks, and an NZB
taken from the download area of a post.

That download area is shown to signed-in members, and each post offers the
same release four ways. A member pastes any of them into ROMarr:

  * NZB -- ``https://arcade-punks.com/arcadepunks/<YYYY-MM>/nzb/<name>.zip``,
    a zip holding one ``.nzb`` (posted to ``alt.binaries.cores``). ROMarr
    fetches and unzips it, validates the NZB, and hands the document itself
    to NZBGet, SABnzbd or the built-in Usenet client. NZBGet cannot open a
    zipped NZB from a URL, so passing the link through would fail there.
  * Torrent -- ``https://arcade-punks.com/arcadepunks/<YYYY-MM>/torrent/<name>.torrent``,
    handed to the configured torrent client.
  * Mega -- ``https://mega.nz/file/<id>#<key>`` or ``/folder/<id>#<key>``.
  * Rapidgator -- ``https://rapidgator.net/file/<id>/...`` or ``/folder/<id>/...``.

Mega and Rapidgator links go through ROMarr's file-host route
(`hosters.HosterRoute`): a Rapidgator premium account (Settings > Cloud & File
Hosts) or a debrid service that unlocks the host (Real-Debrid, AllDebrid and
the others). ROMarr never waits out a free hoster's countdown or solves its captcha.

No credential is stored here. The Usenet login lives in the download client,
the debrid token in the debrid client's settings, and the Arcade Punks member
login stays in the operator's browser, which is where the links come from.
"""

from __future__ import annotations

import hashlib
import io
import logging
import re
import threading
import zipfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote, urlsplit

import requests

log = logging.getLogger(__name__)

SLUG = "arcade-punks"
PLUGIN_REPO = "https://github.com/BlizzHacker/rom-hub-arcade-punks"
#: The release ROMarr preinstalls when it has no bundled copy to install from.
PLUGIN_REF = "v0.1.0"
SITE_URL = "https://www.arcadepunks.com"
GUIDES = (
    "https://www.arcadepunks.com/how-to-get-started-with-nzbget/",
    "https://www.arcadepunks.com/how-to-set-up-usenet-for-arcade-punks-retro-gaming-content/",
)
CREDIT = ("Arcade Punks (https://www.arcadepunks.com) builds and hosts these packs. "
          "ROMarr reads its public catalogue and the links a member pastes; it "
          "includes none of Arcade Punks' content.")

#: Where the members area points its NZB and torrent links.
FILE_HOSTS = frozenset({"arcade-punks.com", "www.arcade-punks.com"})
MEGA_HOSTS = frozenset({"mega.nz", "www.mega.nz", "mega.co.nz", "www.mega.co.nz"})
RAPIDGATOR_HOSTS = frozenset({"rapidgator.net", "www.rapidgator.net", "rg.to", "www.rg.to"})

#: The newsgroup their posted NZBs use, read from a published NZB.
NEWSGROUPS = ("alt.binaries.cores",)
#: The tag their release names end in (``tb.lg.rEAP``).
RELEASE_TAG = "rEAP"

LINK_KINDS = ("nzb", "torrent", "mega", "rapidgator")

#: The zipped NZB is about a quarter of the NZB; a 291 GB build's NZB is
#: 40 MB unpacked, 10 MB zipped. Both bounds leave room for bigger builds
#: while keeping a hostile file from filling memory.
MAX_ZIP_BYTES = 64 * 1024 * 1024
MAX_NZB_BYTES = 256 * 1024 * 1024
MAX_TORRENT_BYTES = 32 * 1024 * 1024
FETCH_TIMEOUT = 60
_CHUNK = 1024 * 1024

_FILE_PATH = re.compile(
    r"^/arcadepunks/(?P<month>\d{4}-\d{2})/(?P<kind>nzb|torrent)/(?P<file>[^/]+)$")
_SAFE_FILE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._()\[\] +-]{0,199}$")
_MEGA_PATH = re.compile(r"^/(?P<what>file|folder)/(?P<id>[A-Za-z0-9_-]{6,64})/?$")
_MEGA_KEY = re.compile(r"^[A-Za-z0-9_-]{8,128}$")
_RG_PATH = re.compile(
    r"^/(?P<what>file|folder)/(?P<id>[A-Za-z0-9]{1,64})(?:/(?P<name>[^/]{1,200}))?/?$")


class LinkError(ValueError):
    """A pasted link that is not an Arcade Punks NZB, torrent, Mega or Rapidgator link."""


@dataclass(frozen=True)
class Link:
    """One classified download link: what it is, and a name to label the job."""

    url: str
    kind: str
    name: str


def _stem(filename: str) -> str:
    """A filename without its download extension (``.zip``, ``.nzb``, ``.torrent``, ``.html``)."""
    return re.sub(r"\.(zip|nzb|torrent|html?)$", "", filename, flags=re.IGNORECASE)


def classify(url: str) -> Link:
    """Classify a pasted link, or raise `LinkError` saying why it is refused.

    Only https links on the four hosts are accepted, in the exact path shapes
    those hosts use. Credentials in the URL are refused rather than stripped:
    a link carrying a password was pasted from somewhere it should not travel
    on from.
    """
    text = str(url or "").strip()
    if not text:
        raise LinkError("paste an Arcade Punks NZB, torrent, Mega or Rapidgator link")
    if len(text) > 2048:
        raise LinkError("link is too long")
    parts = urlsplit(text)
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or not host:
        raise LinkError("links must start with https://")
    if parts.username or parts.password or parts.port not in (None, 443):
        raise LinkError("links must not carry a login or a port")
    if host in FILE_HOSTS:
        return _classify_file_host(text, parts)
    if host in MEGA_HOSTS:
        return _classify_mega(text, parts)
    if host in RAPIDGATOR_HOSTS:
        return _classify_rapidgator(text, parts)
    raise LinkError(f"{host} is not an Arcade Punks, Mega or Rapidgator address")


def _classify_file_host(text: str, parts) -> Link:
    if parts.query or parts.fragment:
        raise LinkError("Arcade Punks file links have no query or fragment")
    match = _FILE_PATH.match(parts.path)
    if not match:
        raise LinkError("expected https://arcade-punks.com/arcadepunks/<YYYY-MM>/nzb|torrent/<file>")
    filename = unquote(match.group("file"))
    if not _SAFE_FILE.match(filename) or ".." in filename:
        raise LinkError("the file name in the link is not one Arcade Punks uses")
    kind = match.group("kind")
    lower = filename.lower()
    if kind == "nzb" and not lower.endswith((".zip", ".nzb")):
        raise LinkError("an Arcade Punks NZB link ends in .zip or .nzb")
    if kind == "torrent" and not lower.endswith(".torrent"):
        raise LinkError("an Arcade Punks torrent link ends in .torrent")
    return Link(url=text, kind=kind, name=_stem(filename))


def _classify_mega(text: str, parts) -> Link:
    match = _MEGA_PATH.match(parts.path)
    if not match or parts.query:
        raise LinkError("expected https://mega.nz/file/<id>#<key> or /folder/<id>#<key>")
    if not _MEGA_KEY.match(parts.fragment or ""):
        raise LinkError("a Mega link needs its #key; copy the whole link")
    return Link(url=text, kind="mega", name=f"mega-{match.group('id')}")


def _classify_rapidgator(text: str, parts) -> Link:
    match = _RG_PATH.match(parts.path)
    if not match or parts.query or parts.fragment:
        raise LinkError("expected https://rapidgator.net/file/<id>/... or /folder/<id>/...")
    name = _stem(unquote(match.group("name") or ""))
    if not name or not _SAFE_FILE.match(name):
        name = f"rapidgator-{match.group('id')}"
    return Link(url=text, kind="rapidgator", name=name)


# --- pack names -------------------------------------------------------------

#: Front ends and operating systems Arcade Punks builds on, longest first so
#: "BigBox" is not read as "Box" and "CoinOps" wins over "Coin".
FRONTENDS = (
    ("attract mode", "Attract Mode"), ("emulationstation", "EmulationStation"),
    ("launchbox", "LaunchBox"), ("bigbox", "LaunchBox"), ("hyperspin", "HyperSpin"),
    ("teknoparrot", "TeknoParrot"), ("retrobat", "RetroBat"), ("batocera", "Batocera"),
    ("recalbox", "Recalbox"), ("retropie", "RetroPie"), ("retrofe", "RetroFE"),
    ("playnite", "Playnite"), ("jukebox", "JukeBox"), ("coinops", "CoinOps"),
    ("knulli", "Knulli"), ("arkos", "ArkOS"), ("lakka", "Lakka"),
)
DEVICES = (
    (r"steam\s*deck", "Steam Deck"), (r"raspberry\s*pi\s*(?:5|4|3|2|0|zero)?", "Raspberry Pi"),
    (r"\bpi\s*[0345]\b", "Raspberry Pi"), (r"anbernic|\brg\s?\d{2,3}", "Anbernic"),
    (r"ally\s*x|rog\s*ally", "ROG Ally"), (r"odroid", "Odroid"), (r"\bmister\b", "MiSTer"),
    (r"xbox\s*(?:original|og)", "Original Xbox"), (r"\bpc\b", "PC"),
)
MEDIA_WORDS = re.compile(
    r"\b(?:media(?:\s+pack)?|artwork|video\s+snaps?|snaps|themes?|wheels?|bezels?|"
    r"flyers?|marquees?)\b", re.IGNORECASE)
BUILD_WORDS = re.compile(r"\b(?:build|image|edition|base|pack|collection|loaded)\b", re.IGNORECASE)
_CAPACITY = re.compile(r"(?<![\w.])(\d{1,4}(?:\.\d+)?)\s*(gb|tb)\b", re.IGNORECASE)
_VERSION = re.compile(r"\bv(\d{1,3}(?:\.\d{1,3}){0,2})\b", re.IGNORECASE)
_ATOMISWAVE = re.compile(
    r"^(?P<game>.+?)\s+(?:is\s+now\s+available\s+|just\s+released\s+)?for\s+the\s+"
    r"sega\s+dreamcast\b", re.IGNORECASE)
_DUMP = re.compile(r"^(?P<game>.+?)\s+teknoparrot\s+dump\b", re.IGNORECASE)
_TAG = re.compile(r"(?:^|[.\s_-])reap$", re.IGNORECASE)


@dataclass(frozen=True)
class Pack:
    """What an Arcade Punks title or release name says about its contents.

    `kind` is ``game`` (one game for one platform), ``build`` (a whole
    front-end or device image), ``media`` (artwork or video packs) or
    ``other``. Only a ``game`` names a platform ROMarr can import into; a
    build is a disk image to flash or a folder to run, not ROMs to verify.
    """

    title: str
    kind: str = "other"
    game: str = ""
    platform: str = ""
    frontend: str = ""
    device: str = ""
    capacity_bytes: int = 0
    version: str = ""
    tagged: bool = False
    notes: tuple[str, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict:
        """JSON-friendly form for the API and UI."""
        return {
            "title": self.title, "kind": self.kind, "game": self.game,
            "platform": self.platform, "frontend": self.frontend,
            "device": self.device, "capacity_bytes": self.capacity_bytes,
            "version": self.version, "tagged": self.tagged,
        }


def _capacity(text: str) -> int:
    """The largest storage size a title names (``256GB``, ``1TB``), in bytes."""
    best = 0
    for number, unit in _CAPACITY.findall(text):
        try:
            value = float(number)
        except ValueError:
            continue
        scale = 1000 ** 4 if unit.lower() == "tb" else 1000 ** 3
        best = max(best, int(value * scale))
    return best


def parse_pack(title: str) -> Pack:
    """Read an Arcade Punks post title or release name into a `Pack`.

    Word-boundary matches only, so a "Pi" in "Pinball" is not a Raspberry Pi
    and "Base" in "Baseball" is not a base build.
    """
    raw = str(title or "").strip()
    text = re.sub(r"[_]+", " ", raw)
    folded = text.casefold()
    tagged = bool(_TAG.search(raw))

    match = _ATOMISWAVE.match(text)
    if match and "atomiswave" in folded:
        return Pack(title=raw, kind="game", game=match.group("game").strip(" -:"),
                    platform="dreamcast", frontend="Atomiswave conversion",
                    tagged=tagged)
    match = _DUMP.match(text)
    if match:
        # A TeknoParrot dump is a PC arcade game, not a MAME set; ROMarr's
        # arcade platform verifies MAME zips, so no platform is claimed.
        return Pack(title=raw, kind="game", game=match.group("game").strip(" -:"),
                    frontend="TeknoParrot", tagged=tagged)

    frontend = ""
    for needle, label in FRONTENDS:
        if re.search(r"\b" + re.escape(needle).replace(r"\ ", r"\s*") + r"\b", folded):
            frontend = label
            break
    device = ""
    for pattern, label in DEVICES:
        if re.search(pattern, folded):
            device = label
            break
    capacity = _capacity(text)
    version = (_VERSION.search(text) or [None, ""])[1]
    if MEDIA_WORDS.search(text) and not capacity:
        kind = "media"
    elif frontend or device or capacity or BUILD_WORDS.search(text):
        kind = "build"
    else:
        kind = "other"
    return Pack(title=raw, kind=kind, frontend=frontend, device=device,
                capacity_bytes=capacity, version=version, tagged=tagged)


# --- fetching the NZB -----------------------------------------------------

class FetchError(RuntimeError):
    """The NZB or torrent behind a link could not be fetched or is not valid."""


@dataclass(frozen=True)
class NzbDocument:
    """A validated NZB: its bytes, a job name, and what it posts to."""

    name: str
    data: bytes
    files: int
    size_bytes: int
    groups: tuple[str, ...]


def _read_bounded(response, limit: int, what: str) -> bytes:
    declared = (getattr(response, "headers", None) or {}).get("Content-Length")
    if declared and str(declared).isdigit() and int(declared) > limit:
        raise FetchError(f"{what} is larger than the {limit // (1024 * 1024)} MiB limit")
    chunks, total = [], 0
    for chunk in response.iter_content(_CHUNK):
        total += len(chunk)
        if total > limit:
            raise FetchError(f"{what} is larger than the {limit // (1024 * 1024)} MiB limit")
        chunks.append(chunk)
    return b"".join(chunks)


def _get(session, link: Link, limit: int, what: str) -> bytes:
    """GET a file-host link without following redirects off the file host."""
    try:
        response = session.get(link.url, stream=True, timeout=FETCH_TIMEOUT,
                               allow_redirects=False)
    except requests.RequestException as err:
        raise FetchError(f"could not fetch the {what}: {err.__class__.__name__}") from err
    try:
        status = getattr(response, "status_code", 0)
        if status in (301, 302, 303, 307, 308):
            raise FetchError(f"the {what} link redirected; paste the final link")
        if status != 200:
            raise FetchError(f"Arcade Punks answered HTTP {status} for the {what}")
        return _read_bounded(response, limit, what)
    finally:
        close = getattr(response, "close", None)
        if callable(close):
            close()


def _validate_nzb(name: str, data: bytes) -> NzbDocument:
    from .nntp import NntpError, parse_nzb
    try:
        files = parse_nzb(data)
    except NntpError as err:
        raise FetchError(f"{name} is not a usable NZB: {err}") from err
    groups = sorted({g for f in files for g in f.groups})
    size = sum(seg.size for f in files for seg in f.segments)
    return NzbDocument(name=name, data=data, files=len(files), size_bytes=size,
                       groups=tuple(groups))


def _unzip_nzb(link: Link, blob: bytes) -> NzbDocument:
    """The one NZB inside an Arcade Punks NZB zip, read within the limits."""
    try:
        archive = zipfile.ZipFile(io.BytesIO(blob))
    except zipfile.BadZipFile as err:
        raise FetchError("the NZB link did not return a zip file") from err
    with archive:
        members = [m for m in archive.infolist()
                   if not m.is_dir() and m.filename.lower().endswith(".nzb")]
        if len(members) != 1:
            raise FetchError(f"the zip holds {len(members)} NZB files; expected exactly one")
        member = members[0]
        base = member.filename.replace("\\", "/").rsplit("/", 1)[-1]
        if not _SAFE_FILE.match(base):
            raise FetchError("the NZB inside the zip has an unusable name")
        if member.file_size > MAX_NZB_BYTES:
            raise FetchError("the NZB inside the zip is larger than the 256 MiB limit")
        with archive.open(member) as handle:
            data = handle.read(MAX_NZB_BYTES + 1)
        if len(data) > MAX_NZB_BYTES:
            raise FetchError("the NZB inside the zip is larger than the 256 MiB limit")
    return _validate_nzb(_stem(base), data)


def fetch_nzb(link: Link, session=None) -> NzbDocument:
    """Fetch an Arcade Punks NZB link and return the validated NZB inside it."""
    if link.kind != "nzb":
        raise FetchError("not an NZB link")
    session = session or requests.Session()
    if link.url.lower().endswith(".nzb"):
        return _validate_nzb(link.name, _get(session, link, MAX_NZB_BYTES, "NZB"))
    return _unzip_nzb(link, _get(session, link, MAX_ZIP_BYTES, "NZB zip"))


def fetch_torrent(link: Link, session=None) -> bytes:
    """Fetch an Arcade Punks .torrent, for clients that take the file, not a URL."""
    if link.kind != "torrent":
        raise FetchError("not a torrent link")
    data = _get(session or requests.Session(), link, MAX_TORRENT_BYTES, "torrent")
    if not data.startswith(b"d") or b"4:info" not in data[:MAX_TORRENT_BYTES]:
        raise FetchError("the torrent link did not return a .torrent file")
    return data


# --- handing a link to a client ---------------------------------------------

class ArcadePunksLane:
    """Send one pasted link to whichever configured client can take it.

    Shaped like a download client (`add`, `last_job_id`, `romarr_id`) so
    ROMarr's ordinary grab records the queue row; the row is labelled with
    the client that really has the job, so the import sweep pairs the
    finished download with it exactly as it would any other grab.
    """

    protocol = "arcadepunks"
    name = "Arcade Punks"
    TAKES_NAME = True

    def __init__(self, clients, session=None):
        self._clients = list(clients or [])
        self._session = session
        self._state = threading.local()

    # The grab reads these straight after `add`, on the same thread.
    @property
    def last_job_id(self) -> str:
        return getattr(self._state, "job_id", "")

    @property
    def last_error(self) -> str:
        return getattr(self._state, "error", "")

    @property
    def romarr_id(self) -> str:
        return getattr(self._state, "client_id", "")

    @property
    def delegate_name(self) -> str:
        return getattr(self._state, "client_name", "")

    @property
    def configured(self) -> bool:
        return any(self.target(kind) is not None for kind in LINK_KINDS)

    def _usable(self):
        return [c for c in self._clients if getattr(c, "configured", True)]

    def target(self, kind: str):
        """The client a link of this kind goes to, or None."""
        usable = self._usable()
        if kind == "nzb":
            return next((c for c in usable if getattr(c, "protocol", "") == "usenet"
                         and callable(getattr(c, "add_nzb", None))), None)
        if kind == "torrent":
            return next((c for c in usable if getattr(c, "protocol", "") == "torrent"
                         and not getattr(c, "magnet_only", False)), None) \
                or next((c for c in usable if callable(getattr(c, "add_torrent_file", None))), None)
        if kind in ("mega", "rapidgator"):
            from .hosters import HosterRoute
            return HosterRoute.for_clients(usable)
        return None

    def routes(self) -> dict:
        """Which client each link kind would go to right now, for the UI."""
        out = {}
        for kind in LINK_KINDS:
            client = self.target(kind)
            out[kind] = _route_name(kind, client) if client else ""
        return out

    def _fail(self, message: str) -> bool:
        self._state.error = message
        return False

    def add(self, url: str, *, name: str = "") -> bool:
        """Hand the link to its client; False with `last_error` when it cannot."""
        self._state.job_id = ""
        self._state.error = ""
        self._state.client_id = ""
        self._state.client_name = ""
        try:
            link = classify(url)
        except LinkError as err:
            return self._fail(str(err))
        client = self.target(link.kind)
        if client is None:
            return self._fail(_NO_CLIENT[link.kind])
        label = _safe_label(name or link.name)
        try:
            ok = self._dispatch(client, link, label)
        except FetchError as err:
            return self._fail(str(err))
        except Exception as err:  # noqa: BLE001 -- a client bug must not take the request down
            log.warning("arcade punks hand-off to %s failed: %s",
                        getattr(client, "name", client), err)
            return self._fail(f"{getattr(client, 'name', 'the client')} failed: "
                              f"{err.__class__.__name__}")
        if not ok:
            detail = str(getattr(client, "last_error", "") or "")
            return self._fail(detail or f"{getattr(client, 'name', 'the client')} rejected the link")
        self._state.job_id = str(getattr(client, "last_job_id", "") or "")
        self._state.client_id = str(getattr(client, "romarr_id", "") or "")
        self._state.client_name = str(getattr(client, "name", "") or "")
        return True

    def _dispatch(self, client, link: Link, label: str) -> bool:
        if link.kind == "nzb":
            doc = fetch_nzb(link, self._session)
            return bool(client.add_nzb(doc.data, name=label))
        if link.kind == "torrent":
            if getattr(client, "magnet_only", False):
                return bool(client.add_torrent_file(fetch_torrent(link, self._session)))
            from .downloaders import hand_off
            return bool(hand_off(client, link.url, name=label))
        return bool(client.add(link.url, name=label))


def _route_name(kind: str, client) -> str:
    """What a link kind goes to, in words: a client, or the accounts and
    debrid services behind the file-host route."""
    if hasattr(client, "_debrid"):          # hosters.HosterRoute
        names = []
        if kind == "rapidgator":
            names += [f"{a.label} account" for a in client._premium
                      if getattr(a, "label", "") == "Rapidgator"]
        names += [str(getattr(d, "name", "") or "a debrid service") for d in client._debrid]
        return " or ".join(dict.fromkeys(names)) or "no account or debrid service set up"
    return str(getattr(client, "display_name", getattr(client, "name", "")) or "")


_NO_CLIENT = {
    "nzb": "No Usenet client is set up. Add NZBGet, SABnzbd or the built-in Usenet "
           "client under Settings > Download Clients.",
    "torrent": "No torrent client that accepts .torrent links is set up.",
    "mega": "Mega links need Real-Debrid or AllDebrid under Settings > Download "
            "Clients. ROMarr does not download from Mega directly.",
    "rapidgator": "Rapidgator links need Real-Debrid or AllDebrid under Settings > "
                  "Download Clients. ROMarr does not wait out free download timers.",
}


def no_client(kind: str) -> str:
    """Why a link of this kind cannot be sent anywhere yet."""
    return _NO_CLIENT.get(kind, "no download client can take this link")


def job_name(link: Link) -> str:
    """The name the job carries in its client, which the import sweep matches."""
    return _safe_label(link.name)


def _safe_label(name: str) -> str:
    """A job name every client and filesystem accepts."""
    cleaned = re.sub(r"[^A-Za-z0-9._()\[\] +-]+", " ", str(name or "")).strip(" .")
    return cleaned[:150] or "arcade-punks"


def link_id(url: str) -> str:
    """A stable identity for a pasted link, for the queue and blocklist."""
    return "ap:" + hashlib.sha1(str(url).encode("utf-8")).hexdigest()[:20]


# --- setup hints ------------------------------------------------------------

def setup_hints(env: Mapping[str, str] | None = None) -> dict:
    """The guides' NZBGet setup, translated to where each value lives in ROMarr.

    Values only, never secrets: the provider login belongs in NZBGet's own
    News-Servers page, and ROMarr only needs NZBGet's address and its control
    login.
    """
    env = env or {}
    category = str(env.get("NZBGET_CATEGORY") or env.get("DOWNLOAD_CATEGORY") or "romarr")
    return {
        "guides": list(GUIDES),
        "newsgroups": list(NEWSGROUPS),
        "provider": {
            "summary": "Any Usenet provider that carries alt.binaries.cores with long "
                       "retention. The guides use Newshosting.",
            "example_host": "news.newshosting.com",
            "port": 563,
            "encryption": True,
            "connections": "Up to what your plan allows (Newshosting allows 100).",
        },
        "nzbget": [
            "Install NZBGet from https://nzbget.com/ (Windows, macOS, Linux, Docker or NAS).",
            ("In NZBGet open Settings > News-Servers and add your provider: Host, Port 563, "
             "Encryption yes, your provider username and password."),
            "Press Test Connection, then Save all changes.",
            ("Keep PAR check and Unpack on (Settings > Download Queue and Unpack): Arcade "
             "Punks posts RAR sets with PAR2 recovery files."),
            ("In ROMarr add NZBGet under Settings > Download Clients (or NZBGET_URL, "
             f"NZBGET_USER and NZBGET_PASS). Downloads go to category '{category}'."),
        ],
        "sabnzbd": "SABnzbd works the same way: add your provider under Config > Servers "
                   "(port 563, SSL), then add SABnzbd to ROMarr.",
        "links": {
            "nzb": "https://arcade-punks.com/arcadepunks/<YYYY-MM>/nzb/<name>.zip",
            "torrent": "https://arcade-punks.com/arcadepunks/<YYYY-MM>/torrent/<name>.torrent",
            "mega": "https://mega.nz/file/<id>#<key>",
            "rapidgator": "https://rapidgator.net/folder/<id>/<name>.html",
        },
    }


# --- the plugin -------------------------------------------------------------

def preinstall(*, home: Path | None = None, env: Mapping[str, str] | None = None) -> str:
    """Install the Arcade Punks ROM Hub plugin once; see preinstalled_plugins."""
    from . import preinstalled_plugins
    return preinstalled_plugins.preinstall(SLUG, PLUGIN_REPO, PLUGIN_REF, home=home, env=env)


def preinstall_in_background() -> None:
    """`preinstall` on a daemon thread, so a slow git never delays serving."""
    threading.Thread(target=preinstall, name="preinstall-arcade-punks",
                     daemon=True).start()


def plugin_enabled(home: Path | None = None) -> bool:
    """Whether the plugin is installed and enabled in ROM Hub."""
    from . import hub
    if not hub.available():
        return False
    try:
        _, registry = hub._import()
        plugin = registry.Registry(Path(home or hub.HOME)).get(SLUG)
    except Exception:  # noqa: BLE001 -- ROM Hub is optional; any failure means "not enabled"
        return False
    return bool(getattr(plugin, "enabled", True))


def search(query: str, *, limit: int = 25) -> dict:
    """Search the Arcade Punks catalogue through its ROM Hub plugin."""
    from . import hub
    text = str(query or "").strip()
    if not text:
        return {"items": [], "error": ""}
    rows, error = hub.search(text, limit=max(1, min(int(limit or 25), 50)), only=(SLUG,))
    items = []
    for row in rows:
        if str(row.get("plugin") or "") != SLUG:
            continue
        pack = parse_pack(str(row.get("title") or ""))
        items.append({
            "title": row.get("title") or "",
            "url": row.get("url") or "",
            "platform": row.get("platform") or pack.platform,
            "extra": row.get("extra") if isinstance(row.get("extra"), dict) else {},
            "pack": pack.as_dict(),
        })
    return {"items": items, "error": error}


def status(env: Mapping[str, str], clients) -> dict:
    """What the Arcade Punks card shows: plugin state, routes and setup hints."""
    lane = ArcadePunksLane(clients)
    return {
        "plugin_slug": SLUG,
        "plugin_repo": PLUGIN_REPO,
        "plugin_enabled": plugin_enabled(),
        "site": SITE_URL,
        "routes": lane.routes(),
        "hints": setup_hints(env),
        "credit": CREDIT,
    }
