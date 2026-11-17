"""Game soundtracks: find them, upload them into RomM's Jukebox, take them back.

A port of the core of the standalone romm-soundtracks tool, so ROMarr is one
application rather than two. This is phase 1 and deliberately small:

  * search Zophar's Domain and the Internet Archive for a game's soundtrack,
    rank what comes back, and list the manual-search sites for the rest;
  * download a chosen candidate, take ONLY audio out of any archive, and
    upload each file through RomM's own API (`POST /api/roms/{id}/soundtracks`)
    so RomM creates `soundtrack/`, registers the tracks and, for a lone ROM,
    moves it into a folder first. ROMarr never writes to the ROM tree;
  * journal exactly what was added (RomM's file ids) so Undo removes that and
    nothing else.

Safety rules, kept from the original because they are the point of it:

  * never overwrite -- a track RomM already has under that name is skipped;
  * only undo what this module added, by the file id RomM gave back;
  * a folder is only ever created by RomM, and only when a real file is being
    sent, so no empty `soundtrack/` is left behind;
  * one request per second per host, with a User-Agent that says who is asking;
  * archives are never extracted to disk: members are streamed out one at a
    time, names are flattened (no path traversal), only audio extensions are
    read, and an archive larger than 6 GB is refused.

Authentication is whatever the RomM library entry already has (see
`Romm.write_token`); this module never stores, accepts or prints a credential.
Phases 2 and 3 live at the end of `Soundtracks`: duplicate merge (identical
stored soundtracks compared by SHA-256, merged and undoable) and albums from a
read-only music library. Not here yet: 3DS decrypt/held originals. See
docs/SOUNDTRACKS.md.
"""

from __future__ import annotations

import difflib
import hashlib
import itertools
import html
import json
import logging
import os
import re
import shutil
import sqlite3
import tempfile
import threading
import time
import unicodedata
import uuid
import zipfile
import collections
import contextlib
import socket
import ipaddress
import stat
from urllib.parse import urljoin
from .file_reports import _open_source, _fd_identity
from .nested_zip import check_zip_central_directory
from requests.adapters import HTTPAdapter
from urllib3.connection import HTTPSConnection
from urllib3.connectionpool import HTTPSConnectionPool
from pathlib import Path
from typing import Callable, Iterator
from urllib.parse import quote, quote_plus, urlencode, urljoin, urlparse

import requests

log = logging.getLogger(__name__)

UA = "ROMarr (self-hosted game library manager; soundtrack lookup)"

#: What RomM's Jukebox accepts. Anything else is refused by RomM, so it is not
#: even offered to it.
JUKEBOX_EXT = frozenset({".mp3", ".ogg", ".oga", ".opus", ".m4a", ".aac", ".wav",
                         ".flac", ".spc", ".vgm", ".vgz", ".gym"})
#: Chiptune rips that are stored by the sources but that RomM cannot play. They
#: may be read out of an archive, but phase 1 only delivers through RomM, so
#: they are counted as skipped.
CHIPTUNE_EXT = frozenset({".nsf", ".nsfe", ".gbs", ".sap", ".kss", ".hes", ".ay", ".gsf",
                          ".minigsf", ".2sf", ".mini2sf", ".usf", ".miniusf", ".psf",
                          ".minipsf", ".psf2", ".minipsf2", ".ssf", ".minissf", ".dsf",
                          ".sgc", ".s98", ".mod", ".xm", ".it", ".s3m", ".mid", ".midi"})
AUDIO_EXT = JUKEBOX_EXT | CHIPTUNE_EXT

#: Past this an archive is refused before anything is read out of it.
MAX_ARCHIVE_BYTES = 6 << 30
#: Hosts a download link may point at. A source page is third-party HTML, so a
#: link in it is not trusted to name any host it likes.
ALLOWED_HOSTS = ("zophar.net", "archive.org")

# RomM platform slug -> regex over Zophar's console label; only used to rank candidates
ZOPHAR_CONSOLE = {
    "nes": r"Nintendo NES", "fds": r"Famicom Disk|Disk System", "snes": r"SNES", "satellaview": r"SNES",
    "gb": r"Game Boy \(GBS\)", "gbc": r"Game Boy \(GBS\)|Game Boy Color", "gba": r"Game Boy Advance",
    "n64": r"Nintendo 64", "nds": r"Nintendo DS", "3ds": r"3DS", "ngc": r"GameCube", "wii": r"Wii(?! U)",
    "psx": r"^Playstation( \(|$)", "psp": r"PSP|Portable", "genesis": r"Genesis|Mega Drive", "segacd": r"Sega CD|Mega CD",
    "sms": r"Master System", "gamegear": r"Game Gear", "tg16": r"PC Engine|TurboGrafx", "turbografx-cd": r"PC Engine|TurboGrafx",
    "arcade": r"Arcade", "cps3": r"Arcade", "model2": r"Arcade", "neogeoaes": r"Neo.?Geo|Arcade", "c64": r"Commodore 64",
    "dos": r"MS-DOS", "atari2600": r"Atari 2600", "atari5200": r"Atari 5200", "atari7800": r"Atari 7800", "lynx": r"Lynx",
    "colecovision": r"Coleco", "zxs": r"Spectrum", "vectrex": r"Vectrex", "ps3": r"Playstation 3", "xbox360": r"360",
}
# Consoles whose "original" rip format RomM's Jukebox can actually play
JUKEBOX_NATIVE = r"\(SPC\)|\(VGM\)|Genesis|Mega Drive|Master System|Game Gear|\(GYM\)"


# ------------------------------------------------------------------ titles --

def clean_title(name: str, is_folder: bool = False) -> str:
    """A ROM file name as a plain game title: no extension, no (tags), "Legend, The" -> "The Legend"."""
    base = name
    if not is_folder:
        stem, ext = os.path.splitext(name)
        if 1 < len(ext) <= 5 and ext[1:].isalnum():
            base = stem
    base = re.sub(r"\s*[\(\[][^)\]]*[\)\]]", "", base).replace("_", " ")
    m = re.match(r"^(.*?),\s*(The|A|An)$", base.strip())
    if m:
        base = f"{m.group(2)} {m.group(1)}"
    return re.sub(r"\s+", " ", base).strip(" -.") or name


def fold(s: str) -> str:
    """Accents to ASCII (Pokémon -> Pokemon): the source sites index plain-ASCII titles."""
    return unicodedata.normalize("NFKD", html.unescape(s)).encode("ascii", "ignore").decode()


def norm(s: str) -> str:
    s = fold(s).lower().replace("&", " and ")
    s = re.sub(r"[^a-z0-9]+", " ", s)
    s = re.sub(r"\b(version|edition)\b", " ", s)         # "Pokemon LeafGreen Version" == "Pokemon LeafGreen"
    s = re.sub(r"\s+", " ", s).strip()
    return re.sub(r"^the ", "", s)


def similarity(a: str, b: str) -> float:
    a, b = norm(a), norm(b)
    return round(difflib.SequenceMatcher(None, a, b).ratio(), 3) if a and b else 0.0


def safe_name(s: str) -> str:
    s = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", s).strip(" .")
    return s or "_"


def header_safe(name: str) -> str:
    """RomM reads the file name from an HTTP header, which can only carry Latin-1."""
    try:
        name.encode("latin-1")
        return name
    except UnicodeEncodeError:
        stem, ext = os.path.splitext(name)
        folded = unicodedata.normalize("NFKD", stem).encode("ascii", "ignore").decode().strip(" .")
        return (folded or "track-" + uuid.uuid4().hex[:6]) + ext


_EXT = re.compile(r"\.(([a-z]+\.)*\w+)$")      # RomM's own extension pattern


def stem_stable(file_name: str) -> bool:
    """Would RomM keep the same name for this ROM once it lives in a folder named after it?

    RomM recomputes the name from the folder name with the same extension
    pattern, so a stem that itself ends in '.<word>' ("Game v1.2") loses that
    tail, and anything matching device saves by that name stops finding them.
    """
    stem = _EXT.sub("", file_name).strip()
    return _EXT.sub("", stem).strip() == stem


def manual_links(title: str, platform_name: str) -> list[dict]:
    """Search pages worth opening by hand. Sites behind a bot challenge are never scraped, only linked."""
    q = quote_plus
    return [
        {"name": "KHInsider", "url": "https://downloads.khinsider.com/search?search=" + q(title)},
        {"name": "VGMRips", "url": "https://vgmrips.net/packs/search?q=" + q(title)},
        {"name": "Zophar's Domain", "url": "https://www.zophar.net/music/search?search=" + q(title)},
        {"name": "Internet Archive", "url": "https://archive.org/search?query=" + q(title + " soundtrack") + "&and[]=mediatype%3Aaudio"},
        {"name": "MusicBrainz", "url": "https://musicbrainz.org/search?type=release_group&query=" + q(title + " soundtrack")},
        {"name": "Bandcamp", "url": "https://bandcamp.com/search?q=" + q(title + " soundtrack")},
        {"name": "YouTube", "url": "https://www.youtube.com/results?search_query=" + q(f"{title} {platform_name} soundtrack")},
    ]


def best_for_auto(cands: list[dict], threshold: float) -> dict | None:
    """The one candidate safe to take without a person looking: same platform and a near-exact title."""
    for x in cands:
        if x["platform_match"] and x["score"] >= threshold:
            return x
    return None


# -------------------------------------------------------------- polite HTTP --

_last: dict[str, float] = {}
_gap_lock = threading.Lock()


def _wait_turn(host: str, gap: float = 1.0) -> None:
    """Block until `host` has not been asked for `gap` seconds. The slot is claimed
    under the lock, so concurrent jobs queue behind one another instead of racing."""
    with _gap_lock:
        now = time.time()
        at = max(now, _last.get(host, 0.0) + gap)
        _last[host] = at
    if at > now:
        time.sleep(at - now)


def library_origin(url: str) -> str:
    """Canonical configured service identity; never include credentials or queries."""
    parts = urlparse(url)
    if parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password or parts.query or parts.fragment:
        raise ValueError("Invalid RomM library URL")
    port = parts.port or (443 if parts.scheme == "https" else 80)
    return f"{parts.scheme}://{parts.hostname.lower()}:{port}{parts.path.rstrip('/')}"


def allowed_url(url: str) -> bool:
    try:
        u = urlparse(url)
        host = (u.hostname or "").lower()
        return (u.scheme == "https" and not u.username and not u.password and not u.fragment
                and u.port in (None, 443)
                and any(host == h or host.endswith("." + h) for h in ALLOWED_HOSTS))
    except ValueError:
        return False


def _public_addresses(host, port):
    """Resolve at connect time and reject every non-public result before connecting."""
    addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(row[4][0]).is_global for row in addresses):
        raise ValueError("Soundtrack source resolves to a non-public address")
    return addresses


class _PublicHTTPSConnection(HTTPSConnection):
    def _new_conn(self):
        # Connect directly to a validated address, retaining original host/SNI and TLS verification.
        last = None
        for family, kind, protocol, _, address in _public_addresses(self.host, self.port):
            sock = socket.socket(family, kind, protocol)
            try:
                sock.settimeout(self.timeout)
                sock.connect(address)
                return sock
            except OSError as exc:
                last = exc
                sock.close()
        raise OSError("Soundtrack source connection failed") from last


class _PublicHTTPSPool(HTTPSConnectionPool):
    ConnectionCls = _PublicHTTPSConnection


class _SourceAdapter(HTTPAdapter):
    def init_poolmanager(self, *args, **kwargs):
        super().init_poolmanager(*args, **kwargs)
        self.poolmanager.pool_classes_by_scheme = dict(self.poolmanager.pool_classes_by_scheme)
        self.poolmanager.pool_classes_by_scheme["https"] = _PublicHTTPSPool


@contextlib.contextmanager
def _source_response(url):
    """Follow only a bounded chain of approved, public HTTPS source locations."""
    with requests.Session() as session:
        session.trust_env = False
        session.mount("https://", _SourceAdapter())
        for hop in range(6):
            if not allowed_url(url):
                raise ValueError("download link points outside the supported sources")
            _wait_turn(urlparse(url).netloc)
            response = session.get(url, headers={"User-Agent": UA}, stream=True,
                                   timeout=(10, 60), allow_redirects=False)
            try:
                if response.status_code in (301, 302, 303, 307, 308):
                    if hop == 5 or not response.headers.get("Location"):
                        raise ValueError("Soundtrack source redirect limit exceeded")
                    url = urljoin(url, response.headers["Location"])
                    continue
                response.raise_for_status()
                yield response
                return
            finally:
                response.close()


def polite_get(url: str, binary: bool = False, gap: float = 1.0):
    """Fetch bounded source metadata through the same validated redirect transport."""
    with _source_response(url) as response:
        chunks, size = [], 0
        deadline = time.monotonic() + 45
        for chunk in response.iter_content(65536):
            size += len(chunk)
            if size > 8 * 1024 * 1024 or time.monotonic() > deadline:
                raise ValueError("Soundtrack metadata exceeds size limit")
            chunks.append(chunk)
        data = b"".join(chunks)
        return data if binary else data.decode("utf-8", "replace")


def stream(url: str, on_total: Callable[[int], None] | None = None) -> Iterator[bytes]:
    """Stream approved source bytes with a total limit, including plain audio files."""
    with _source_response(url) as response:
        if on_total and response.headers.get("Content-Length", "").isdigit():
            on_total(int(response.headers["Content-Length"]))
        size = 0
        deadline = time.monotonic() + 1800
        for chunk in response.iter_content(1 << 20):
            size += len(chunk)
            if size > MAX_ARCHIVE_BYTES or time.monotonic() > deadline:
                raise ValueError("Soundtrack exceeds transfer limit")
            yield chunk


# ---------------------------------------------------------------- providers --

def _text(fragment: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", fragment)).strip()


def zophar_search(title: str, platform: str) -> list[dict]:
    out, seen = [], set()
    title = fold(title)
    # the site's search wants every word in its title, so lead with the form without "Version"/"Edition"
    queries: list[str] = []
    for q in (norm(title), title, re.split(r"\s*[:\-–]\s+", title)[0]):
        q = " ".join(q.split())
        if q and q not in queries:
            queries.append(q)
    for q in queries:
        page = polite_get("https://www.zophar.net/music/search?" + urlencode({"search": q}))
        for m in re.finditer(r'<tr class="regularrow[^"]*">(.*?)</tr>', page, re.S):
            row = m.group(1)
            a = re.search(r'<td class="name"><a href="(/music/[^"]+)">(.*?)</a>', row, re.S)
            if not a or a.group(1) in seen:
                continue
            seen.add(a.group(1))
            cons = _text((re.search(r'<td class="console">(.*?)</td>', row, re.S) or [0, ""])[1])
            year = _text((re.search(r'<td class="year">(.*?)</td>', row, re.S) or [0, ""])[1])
            name = _text(a.group(2))
            plat_ok = bool(re.search(ZOPHAR_CONSOLE.get(platform, r"(?!x)x"), cons))
            out.append({"provider": "zophar", "id": a.group(1), "title": name, "console": cons, "year": year,
                        "url": "https://www.zophar.net" + a.group(1), "platform_match": plat_ok,
                        "score": similarity(title, name), "jukebox_original": bool(re.search(JUKEBOX_NATIVE, cons))})
        if out:
            break
    return out


def valid_zophar_id(cand_id: str) -> bool:
    return bool(re.fullmatch(r"/music/[A-Za-z0-9._~%/\-]+", cand_id)) and ".." not in cand_id.split("/")


def zophar_plan(cand_id: str) -> dict:
    if not valid_zophar_id(cand_id):
        raise ValueError("not a Zophar music page")
    base = "https://www.zophar.net" + cand_id
    page = polite_get(base)
    box = re.search(r'<div id="mass_download">(.*?)</div>', page, re.S)
    plans = []
    for m in re.finditer(r'<a href="([^"]+)">\s*<p>(.*?)</p>', box.group(1) if box else "", re.S):
        label = _text(m.group(2))
        kind = "mp3" if "mp3" in label.lower() else "original" if "original" in label.lower() else "flac" if "flac" in label.lower() else "other"
        plans.append({"kind": kind, "label": label, "url": urljoin(base, html.unescape(m.group(1)))})
    tracks = len(re.findall(r'class="regularrow trackrow"', page))
    return {"downloads": plans, "tracks": tracks}


def archive_search(title: str, platform: str) -> list[dict]:
    words = " ".join(norm(title).split())          # already accent-folded
    q = f'title:({words}) AND mediatype:audio AND (subject:soundtrack OR subject:"video game" OR title:soundtrack OR title:ost)'
    url = "https://archive.org/advancedsearch.php?" + urlencode(
        {"q": q, "fl[]": ["identifier", "title", "item_size", "downloads"], "rows": 8, "output": "json"}, doseq=True)
    docs = json.loads(polite_get(url))["response"]["docs"]
    return [{"provider": "archive", "id": d["identifier"], "title": d.get("title", d["identifier"]), "console": "Internet Archive",
             "year": "", "url": "https://archive.org/details/" + d["identifier"], "platform_match": False,
             "score": similarity(title, d.get("title", "")), "size": d.get("item_size"), "jukebox_original": False} for d in docs]


def archive_plan(ident: str) -> dict:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._\-]*", ident):
        raise ValueError("not an Internet Archive identifier")
    meta = json.loads(polite_get("https://archive.org/metadata/" + quote(ident)))
    files = [f for f in meta.get("files", []) if f.get("source") == "original" and os.path.splitext(f["name"])[1].lower() in JUKEBOX_EXT]
    base = "https://archive.org/download/" + quote(ident) + "/"
    return {"downloads": [{"kind": "files", "label": f"{len(files)} audio files from the item", "files": [
        {"url": base + quote(f["name"]), "name": os.path.basename(f["name"])} for f in files]}], "tracks": len(files)}


PROVIDERS: dict[str, tuple[Callable, Callable]] = {
    "zophar": (zophar_search, zophar_plan), "archive": (archive_search, archive_plan)}

#: Download formats a candidate offers, before its page is opened. The real set
#: comes from the plan at download time, which refuses a format that is absent.
FORMATS = {"zophar": ["mp3", "original"], "archive": ["files"]}


# ----------------------------------------------------------------- archives --

def audio_member_name(filename: str) -> str | None:
    """The flat name to store a zip member under, or None when it is not audio worth taking.

    The member's own path is discarded (`../../etc/x.mp3` becomes `x.mp3`), so
    nothing can land outside wherever the caller puts it.
    """
    name = os.path.basename(filename.replace("\\", "/"))
    if not name or name.startswith(".") or os.path.splitext(name)[1].lower() not in AUDIO_EXT:
        return None
    return name


class _BoundArchive:
    """Expose an already pinned handle to the shared ZIP metadata validator."""
    def __init__(self, source):
        self.source = source
    def open(self, mode):
        return contextlib.nullcontext(self.source)


@contextlib.contextmanager
def _audio_zip(path):
    with _open_source(Path(path).parent, Path(path).name) as source:
        check_zip_central_directory(_BoundArchive(source), max_members=10000)
        with zipfile.ZipFile(source) as archive:
            if sum(info.file_size for info in archive.infolist()) > MAX_ARCHIVE_BYTES:
                raise ValueError("Soundtrack archive exceeds size limit")
            yield archive


def check_zip(path: str, limit: int = MAX_ARCHIVE_BYTES) -> int:
    """Refuse an archive whose declared contents exceed `limit`; returns how many audio members it holds."""
    with _audio_zip(path) as z:
        infos = [i for i in z.infolist() if not i.is_dir()]
        if sum(i.file_size for i in infos) > limit:
            raise ValueError("archive too large (>6 GB)")
        return sum(1 for i in infos if audio_member_name(i.filename))


def iter_zip_audio(path: str, limit: int = MAX_ARCHIVE_BYTES) -> Iterator[tuple[str, Iterator[bytes]]]:
    """Yield (flat name, chunks) for each audio member of a zip, lazily and without extracting to disk.

    Each member's chunks must be consumed before asking for the next, while the
    generator (and so the archive) is still open. Non-audio members are never
    opened. A member that decompresses to more than its header declared is cut
    off, so a lying header cannot get past the size cap.
    """
    check_zip(path, limit)
    with _audio_zip(path) as z:
        for i in z.infolist():
            name = None if i.is_dir() else audio_member_name(i.filename)
            if name is None:
                continue

            def chunks(info=i):
                left = info.file_size
                with z.open(info) as src:
                    while True:
                        b = src.read(1 << 20)
                        if not b:
                            return
                        left -= len(b)
                        if left < 0:
                            raise ValueError(f"{info.filename} is larger than its header says")
                        yield b
            yield name, chunks()


def do_download(item: dict, sink: Callable[[str, Iterator[bytes]], int | None], tmp_dir: str,
                progress: Callable[..., None], limit: int = MAX_ARCHIVE_BYTES) -> tuple[int, int]:
    """Fetch one download and hand each audio file in it to `sink(name, chunks)`.

    `sink` returns the bytes it took, or None when it skipped the file. Returns
    (made, skipped). A plain-files item (Internet Archive) is streamed file by
    file; anything else is a zip, saved to a temp file in `tmp_dir` and removed.
    """
    made = skipped = 0
    if item.get("files"):
        n = len(item["files"])
        for i, f in enumerate(item["files"]):
            progress(f"Adding track {i + 1} of {n}", pct=i / n)
            name = os.path.basename(f["name"])
            if os.path.splitext(name)[1].lower() not in AUDIO_EXT:
                skipped += 1
                continue
            w = sink(name, stream(f["url"]))
            made, skipped = (made + 1, skipped) if w is not None else (made, skipped + 1)
        return made, skipped
    if not allowed_url(item["url"]):
        raise ValueError("download link points outside the supported sources")
    os.makedirs(tmp_dir, exist_ok=True)
    fd, tmpzip = tempfile.mkstemp(prefix="dl-", suffix=".zip", dir=tmp_dir)
    try:
        total = [0]
        done = 0
        with os.fdopen(fd, "wb") as f:
            for b in stream(item["url"], lambda n: total.__setitem__(0, n)):
                f.write(b)
                done += len(b)
                if done > limit:
                    raise ValueError("archive too large (>6 GB)")
                progress("Downloading the soundtrack", pct=(done / total[0]) if total[0] else None)
        total_members = check_zip(tmpzip, limit)
        for k, (name, chunks) in enumerate(iter_zip_audio(tmpzip, limit)):
            progress(f"Adding track {k + 1} of {total_members}", pct=k / max(total_members, 1))
            w = sink(name, chunks)
            made, skipped = (made + 1, skipped) if w is not None else (made, skipped + 1)
    finally:
        try:
            os.unlink(tmpzip)
        except OSError:
            pass
    return made, skipped


# --------------------------------------------------------------------- RomM --

class RommError(Exception):
    def __init__(self, status: int, body: str = ""):
        super().__init__(f"RomM answered {status}" + (f": {body}" if body else ""))
        self.status = status


class _Multipart:
    """A one-file multipart/form-data body RomM's streaming parser accepts.

    Sized up front (`__len__`) so the request carries a Content-Length rather
    than chunked encoding, and read from disk a megabyte at a time.
    """

    def __init__(self, name: str, path: str, size: int, boundary: str):
        self.boundary = boundary
        self.head = (f'--{self.boundary}\r\nContent-Disposition: form-data; name="{name}"; filename="{name}"\r\n'
                     f'Content-Type: application/octet-stream\r\n\r\n').encode("latin-1")
        self.tail = f"\r\n--{self.boundary}--\r\n".encode()
        self.path, self.size = path, size
        self._source = _open_source(Path(path).parent, Path(path).name)
        self._fh = self._source.__enter__()
        self._identity = _fd_identity(self._fh)
        if self._identity[2] != size:
            self.close()
            raise ValueError("Soundtrack source size changed")
        self._stage = 0

    def __len__(self) -> int:
        return len(self.head) + self.size + len(self.tail)

    def read(self, n: int = -1) -> bytes:
        if self._stage == 0:
            self._stage = 1
            return self.head
        if self._stage == 1:
            b = self._fh.read(1 << 20 if n is None or n < 0 else n)
            if b:
                return b
            if _fd_identity(self._fh) != self._identity:
                raise ValueError("Soundtrack source changed during upload")
            self._stage = 2
            return self.tail
        return b""

    def __iter__(self):
        while True:
            b = self.read(1 << 20)
            if not b:
                return
            yield b

    def close(self) -> None:
        if self._source is not None:
            context, self._source = self._source, None
            context.__exit__(None, None, None)


class RommApi:
    """The few RomM calls soundtracks needs, authenticated by the library entry's own login.

    `token_fn(refresh)` returns a bearer token (see `Romm.write_token`); a 401 is
    retried once with a fresh one. The token is only ever put in a request
    header, and is scrubbed from any text this class raises.
    """

    def __init__(self, base_url: str, token_fn: Callable[[bool], str | None], session: requests.Session | None = None, *, before_upload=None):
        self.base = base_url.rstrip("/")
        self._token_fn = token_fn
        self.before_upload = before_upload
        self._session = session or requests.Session()
        self.receipts = {}
        # Tokens actually sent: a refresh by another job must not leave an
        # older token, echoed in an error body, unredacted.
        self._sent_tokens = collections.deque(maxlen=8)

    def _redact(self, text: str) -> str:
        tokens = set(self._sent_tokens)
        current = self._token_fn(False)
        if current:
            tokens.add(current)
        for tok in sorted(tokens, key=len, reverse=True):
            if tok and len(tok) > 6:
                text = text.replace(tok, "***")
        return text

    def _request(self, method: str, path: str, make_body: Callable[[], object] | None = None,
                 headers: dict | None = None, timeout: int = 60, stream: bool = False) -> requests.Response:
        for attempt in (0, 1):
            tok = self._token_fn(bool(attempt))
            if not tok:
                raise RommError(401, "ROMarr could not sign in to RomM with the saved library login")
            self._sent_tokens.append(tok)
            body = make_body() if make_body else None
            try:
                r = self._session.request(method, self.base + path, data=body, timeout=timeout, stream=True, allow_redirects=False,
                                          headers={"Authorization": "Bearer " + tok, "User-Agent": UA, **(headers or {})})
            finally:
                if hasattr(body, "close"):
                    body.close()
            if r.status_code != 401 or attempt:
                return r
            r.close()
        return r

    @staticmethod
    def _error_text(response):
        try:
            return response.raw.read(300).decode("utf-8", "replace")
        finally:
            response.close()

    def json(self, method: str, path: str):
        with self._request(method, path) as response:
            if not response.ok:
                raise RommError(response.status_code, self._redact(self._error_text(response)))
            size, chunks = 0, []
            for chunk in response.iter_content(65536):
                size += len(chunk)
                if size > 16 * 1024 * 1024:
                    raise ValueError("RomM metadata exceeds size limit")
                chunks.append(chunk)
            data = b"".join(chunks)
            return json.loads(data) if data else None

    def rom(self, rom_id: str) -> dict:
        return self.json("GET", f"/api/roms/{int(rom_id)}") or {}

    def tracks(self, rom_id: str) -> list[dict]:
        """The soundtrack files RomM holds for this ROM: [{file_name, file_id, ...}]."""
        out = self.json("GET", f"/api/roms/{int(rom_id)}/soundtracks/metadata")
        return out if isinstance(out, list) else []

    def upload(self, rom_id: str, name: str, path: str, size: int) -> None:
        """POST one file as multipart whose part is named after the file, with its name in x-upload-filename too."""
        if self.before_upload is not None:
            self.before_upload(path)
        with _open_source(Path(path).parent, Path(path).name) as source:
            expected = _fd_identity(source)
            digest = hashlib.sha256()
            for chunk in iter(lambda: source.read(1 << 20), b""):
                digest.update(chunk)
            if _fd_identity(source) != expected:
                raise ValueError("Soundtrack source changed before upload")
        boundary = "----romarr" + uuid.uuid4().hex
        # A fresh body per attempt: a retry after a 401 has to start from byte 0.
        r = self._request("POST", f"/api/roms/{int(rom_id)}/soundtracks",
                          make_body=lambda: _Multipart(name, path, size, boundary),
                          headers={"x-upload-filename": name, "Content-Type": "multipart/form-data; boundary=" + boundary},
                          timeout=600)
        if r.status_code != 201:
            raise RommError(r.status_code, self._redact(self._error_text(r)))
        r.close()
        self.receipts[(str(rom_id), name)] = (digest.hexdigest(), expected[2])

    def roms_with_files(self) -> Iterator[dict]:
        """Every ROM RomM lists, with its file records, a page at a time (the duplicate scan reads the soundtrack ones)."""
        offset = 0
        while offset < 500000:
            page = self.json("GET", f"/api/roms?limit=500&offset={offset}&order_by=name&order_dir=asc&with_files=true")
            items = page.get("items", []) if isinstance(page, dict) else (page or [])
            yield from items
            if len(items) < 500:
                return
            offset += 500

    def iter_track(self, rom_id: str, file_id: str, name: str) -> Iterator[bytes]:
        """The bytes of one stored soundtrack file, streamed from RomM's file-content endpoint."""
        r = self._request("GET", f"/api/roms/{int(rom_id)}/files/{int(file_id)}/content/{quote(name, safe='')}",
                          timeout=600, stream=True)
        try:
            if not r.ok:
                raise RommError(r.status_code, self._redact(self._error_text(r)))
            yield from r.iter_content(1 << 20)
        finally:
            r.close()

    def delete_track(self, rom_id: str, file_id: str) -> bool:
        """Remove one registered soundtrack file. A file RomM no longer has counts as already gone."""
        r = self._request("DELETE", f"/api/roms/{int(rom_id)}/soundtracks/{int(file_id)}")
        if r.status_code == 404:
            r.close()
            return False
        if not r.ok:
            raise RommError(r.status_code, self._redact(self._error_text(r)))
        r.close()
        return True


class RommSink:
    """Sends each file to RomM instead of writing it, skipping what RomM already has."""

    def __init__(self, api: RommApi, rom_id: str, tmp_dir: str):
        self.api, self.rom_id, self.tmp_dir = api, str(rom_id), tmp_dir
        self.sent: list[str] = []
        self.refused: list[str] = []
        self.existing = {t["file_name"] for t in api.tracks(self.rom_id)}

    def __call__(self, name: str, data_iter: Iterator[bytes]) -> int | None:
        name = header_safe(safe_name(os.path.basename(name)))
        if name.startswith(".") or os.path.splitext(name)[1].lower() not in JUKEBOX_EXT or name in self.existing:
            return None                                                  # not something RomM's Jukebox accepts, or already there
        os.makedirs(self.tmp_dir, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix="up-", dir=self.tmp_dir)
        size = 0
        try:
            with os.fdopen(fd, "wb") as f:
                for chunk in data_iter:
                    f.write(chunk)
                    size += len(chunk)
                    if size > MAX_ARCHIVE_BYTES:
                        raise ValueError("Soundtrack exceeds size limit")
            try:
                self.api.upload(self.rom_id, name, tmp, size)
            except RommError as ex:
                if ex.status in (400, 409):                              # this one file is unacceptable; carry on with the rest
                    log.info("RomM refused %s: %s", name, ex)
                    self.refused.append(name)
                    return None
                raise
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass
        self.sent.append(name)
        self.existing.add(name)
        return size


# ------------------------------------------------------------ music library --
#
# An album from the user's own music library, offered as a game's soundtrack.
# Everything here only READS the library. A folder that holds audio is an
# album; CD1/CD2 style subfolders roll up into the album above them.

_DISC = re.compile(r"^(cd|disc|disk)\s*\d+\b", re.I)
#: How deep below the library root an album folder may sit (artist/year/album/CD1).
ALBUM_DEPTH = 4


def _is_audio(name: str) -> bool:
    return not name.startswith(".") and os.path.splitext(name)[1].lower() in JUKEBOX_EXT


def scan_albums(root: str) -> list[dict]:
    """Every folder under `root` that holds Jukebox audio: [{id, tracks, bytes}], `id` being the path below the root.

    Symlinked folders are not followed, so the scan cannot leave the library.
    """
    root = os.path.realpath(root)
    found: dict[str, dict] = {}

    def walk(d: str, depth: int) -> None:
        try:
            entries = list(itertools.islice(os.scandir(d), 10001))
            if len(entries) > 10000:
                raise ValueError("Music directory entry limit exceeded")
        except OSError:
            return
        audio = [e for e in entries if e.is_file(follow_symlinks=False) and _is_audio(e.name)]
        if audio:
            found[d] = {"tracks": len(audio), "bytes": sum(e.stat().st_size for e in audio)}
        if depth < ALBUM_DEPTH:
            for e in entries:
                if e.is_dir(follow_symlinks=False) and not e.name.startswith("."):
                    walk(e.path, depth + 1)

    walk(root, 0)
    for d in [d for d in found if _DISC.match(os.path.basename(d))]:      # CD1/CD2 -> one album
        agg = found.setdefault(os.path.dirname(d), {"tracks": 0, "bytes": 0})
        agg["tracks"] += found[d]["tracks"]
        agg["bytes"] += found[d]["bytes"]
        del found[d]
    out = [{"id": Path(os.path.relpath(d, root)).as_posix(), "tracks": v["tracks"], "bytes": v["bytes"]}
           for d, v in found.items() if d != root]
    out.sort(key=lambda a: a["id"].lower())
    return out


def _local_fold(s: str) -> str:
    """Fold local paths and queries without dropping native scripts (ゼルダ stays ゼルダ)."""
    out = []
    for char in unicodedata.normalize("NFKC", str(s)).casefold():
        if not char.isascii() and unicodedata.name(char, "").startswith("LATIN"):
            char = unicodedata.normalize("NFKD", char)[0]
        out.append(char)
    return "".join(out)


def search_albums(albums: list[dict], query: str, limit: int = 40) -> list[dict]:
    """Albums whose path holds every word of the query; a match in the album's own folder name ranks first."""
    words = _local_fold(query).split()
    if not words:
        return []
    hits = []
    for a in albums:
        hay = _local_fold(a["id"])
        if all(w in hay for w in words):
            hits.append((sum(w in _local_fold(os.path.basename(a["id"])) for w in words), a))
    hits.sort(key=lambda h: (-h[0], h[1]["id"].lower()))
    return [dict(h[1], name=os.path.basename(h[1]["id"])) for h in hits[:limit]]


def resolve_album(root: str, album_id: str) -> str:
    """The real path of an album folder, refusing anything that is not inside the library root."""
    if not isinstance(album_id, str) or not album_id or "\x00" in album_id:
        raise ValueError("choose an album")
    real_root = os.path.realpath(root)
    src = os.path.realpath(os.path.join(real_root, album_id))
    if src == real_root or os.path.commonpath([src, real_root]) != real_root or not os.path.isdir(src):
        raise ValueError("that folder is not inside the music library")
    return src


def browse_library(root: str, rel: str = "") -> dict:
    """One level of the music library: its subfolders and how many audio files each holds directly.

    `rel` is a path under `root` ("" is the root itself). Nothing outside the root is ever listed.
    """
    if not isinstance(rel, str) or "\x00" in rel:
        raise ValueError("that folder is not inside the music library")
    real_root = os.path.realpath(root)
    here = os.path.realpath(os.path.join(real_root, rel))
    if os.path.commonpath([here, real_root]) != real_root or not os.path.isdir(here):
        raise ValueError("that folder is not inside the music library")
    dirs, audio = [], 0
    for e in sorted(os.scandir(here), key=lambda e: e.name.lower()):
        if e.name.startswith("."):
            continue
        real = os.path.realpath(e.path)
        if e.is_dir() and os.path.commonpath([real, real_root]) == real_root:
            try:
                direct = sum(1 for f in os.scandir(real) if f.is_file() and _is_audio(f.name))
            except OSError:
                direct = 0
            dirs.append({"name": e.name, "tracks": direct})
        elif e.is_file() and _is_audio(e.name):
            audio += 1
    path = "" if here == real_root else os.path.relpath(here, real_root)
    return {"path": path, "dirs": dirs, "tracks": audio}


class AudioFiles(list):
    """Track selections retain the pinned root and original object identities."""
    def __init__(self, entries=(), identities=None):
        super().__init__(entries)
        self.identities = identities or {}


def collect_album(src: str) -> list[tuple[str, str, int]]:
    """[(file name to store, path, size)] for the audio under `src`.

    Only Jukebox audio: cover art, logs and cue sheets stay behind. A subfolder's
    name is folded into the file name ("CD2" + "01 Intro.flac" -> "CD2 - 01 Intro.flac"),
    since a game's soundtrack is a flat list. A symlink that leads out of the
    album is skipped.
    """
    out = AudioFiles()
    root_id = list((os.stat(src).st_dev, os.stat(src).st_ino))
    for dp, dns, fns in os.walk(src):
        dns[:] = sorted(d for d in dns if not d.startswith("."))
        for fn in sorted(fns):
            path = os.path.join(dp, fn)
            if not _is_audio(fn) or os.path.commonpath([os.path.realpath(path), src]) != src:
                continue
            sub = os.path.relpath(dp, src)
            name = fn if sub == "." else sub.replace(os.sep, " - ") + " - " + fn
            with _open_source(Path(path).parent, Path(path).name) as source:
                identity = _fd_identity(source)
            out.identities[path] = (str(Path(path).parent), list((os.stat(Path(path).parent).st_dev, os.stat(Path(path).parent).st_ino)), identity)
            if len(out) >= 10000:
                raise ValueError("Album track limit exceeded")
            out.append((header_safe(safe_name(name)), path, os.path.getsize(path)))
    return out


# ------------------------------------------------------------------ storage --

DDL = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS sound(key TEXT PRIMARY KEY, status TEXT, cands TEXT, chosen TEXT, files INT DEFAULT 0,
    dest TEXT, provider TEXT, note TEXT, ts REAL);
CREATE TABLE IF NOT EXISTS journal(id INTEGER PRIMARY KEY, key TEXT, path TEXT, size INT, ts REAL, undone INT DEFAULT 0, sha256 TEXT);
CREATE TABLE IF NOT EXISTS merges(id INTEGER PRIMARY KEY, drop_id TEXT, keep_id TEXT, drop_title TEXT, keep_title TEXT,
    files TEXT, ts REAL, state TEXT);
CREATE TABLE IF NOT EXISTS pending(key TEXT, name TEXT, sha256 TEXT, size INT, ts REAL,
                                   PRIMARY KEY(key, name));
CREATE TABLE IF NOT EXISTS dup_scan(id INTEGER PRIMARY KEY CHECK (id = 1), ts REAL, groups TEXT);
CREATE TABLE IF NOT EXISTS watch(id INTEGER PRIMARY KEY, key TEXT, tag TEXT, release TEXT, state TEXT, message TEXT,
    pct REAL DEFAULT 0, added REAL, updated REAL, seen REAL, client_id TEXT DEFAULT '');
"""


def db_path_for(data_file: str) -> Path:
    """The sqlite file that sits beside ROMarr's own data file (ROMARR_DATA)."""
    return Path(data_file).parent / "romarr-soundtracks.db"


class Soundtracks:
    """Search, download-through-RomM and undo for one ROMarr install.

    The caller supplies the per-game facts (`game`: id, name, platform_slug,
    platform_name) and a `RommApi` for it; nothing here knows ROMarr's service
    class, which keeps it testable with a fake RomM and a temp directory.
    """

    MAX_JOBS = 50

    def __init__(self, db_path: str | os.PathLike, providers: dict | None = None, *, scope: str = ""):
        self.scope = scope
        self.db_path = Path(db_path)
        self.tmp_dir = str(self.db_path.parent / "soundtracks-tmp")
        self.providers = providers if providers is not None else PROVIDERS
        self._lock = threading.Lock()
        self._jobs: dict[str, dict] = {}
        self._init_db()

    def _check_api(self, api):
        """A journal can act only on its originally configured service."""
        if self.scope and (api is None or library_origin(api.base) != self.scope):
            raise ValueError("Soundtrack journal belongs to a different library")

    # -- db --
    def _db(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.db_path, timeout=30)
        c.row_factory = sqlite3.Row
        return c

    def _init_db(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            descriptor = os.open(self.db_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            if self.db_path.is_symlink() or not self.db_path.is_file():
                raise ValueError("Soundtrack database must be a regular private file")
        else:
            os.close(descriptor)
        os.chmod(self.db_path, 0o600)
        c = self._db()
        try:
            c.executescript(DDL)
            for table, column, declaration in [("journal", "sha256", "TEXT"), ("watch", "client_id", "TEXT DEFAULT ''")]:
                if column not in {row[1] for row in c.execute("PRAGMA table_info(" + table + ")")}:
                    c.execute("ALTER TABLE " + table + " ADD COLUMN " + column + " " + declaration)
            try:
                c.execute("ALTER TABLE sound ADD COLUMN title TEXT")      # added with phase 2: who holds a soundtrack
            except sqlite3.OperationalError:
                pass                                                      # already there
            # No job survives a restart: a merge still 'merging' stopped part-way
            # through deleting, so offer it for restoration like any partial merge.
            c.execute("UPDATE merges SET state='partial' WHERE state='merging'")
            c.commit()
        finally:
            c.close()

    def _set_sound(self, key: str, **kw) -> None:
        with self._lock:
            c = self._db()
            try:
                c.execute("INSERT OR IGNORE INTO sound(key,status,ts) VALUES(?,?,?)", (key, "unsearched", time.time()))
                for k, v in kw.items():
                    if k not in ("status", "cands", "chosen", "files", "dest", "provider", "note", "title"):
                        raise ValueError(k)
                    c.execute(f"UPDATE sound SET {k}=? WHERE key=?", (v, key))
                c.execute("UPDATE sound SET ts=? WHERE key=?", (time.time(), key))
                c.commit()
            finally:
                c.close()

    def _journal_count(self, key: str) -> int:
        c = self._db()
        try:
            return c.execute("SELECT COUNT(*) FROM journal WHERE key=? AND undone=0", (key,)).fetchone()[0]
        finally:
            c.close()

    # -- status --
    def status(self, game: dict, api: RommApi | None) -> dict:
        self._check_api(api)
        key = str(game["id"])
        c = self._db()
        try:
            s = c.execute("SELECT * FROM sound WHERE key=?", (key,)).fetchone()
        finally:
            c.close()
        tracks, error = None, ""
        if api is not None:
            try:
                tracks = [{"name": t.get("file_name", ""), "id": t.get("file_id")} for t in api.tracks(key)]
            except (RommError, requests.RequestException, ValueError) as ex:
                error = f"Could not ask RomM what it holds: {ex.__class__.__name__}"
        else:
            error = "No RomM library is configured"
        job = self.job_for(key)
        return {"id": key, "title": game.get("name", ""), "tracks": tracks, "track_count": len(tracks) if tracks is not None else None,
                "undoable": self._journal_count(key), "state": s["status"] if s else "unsearched",
                "note": (s["note"] if s else "") or "", "provider": (s["provider"] if s else "") or "",
                "job": job, "watch": self.watch_for(key), "error": error}

    # -- search --
    def search(self, game: dict, enabled: tuple[str, ...] = ("zophar", "archive"), term: str = "") -> dict:
        key, title = str(game["id"]), (term.strip()[:200] or game["name"])
        cands: list[dict] = []
        errors: list[str] = []
        for name, (search_fn, _) in self.providers.items():
            if name not in enabled:
                continue
            try:
                found = search_fn(title, game.get("platform_slug", ""))
            except Exception as ex:  # noqa: BLE001 - one dead source must not hide the others
                log.warning("%s search failed: %s", name, ex.__class__.__name__)
                errors.append(f"{name}: {ex.__class__.__name__}")
                continue
            cands += found
        cands = [x for x in cands if x["score"] >= 0.45]
        seen, uniq = set(), []
        for x in sorted(cands, key=lambda x: (x["platform_match"], x["score"]), reverse=True):
            if (x["provider"], x["id"]) not in seen:
                seen.add((x["provider"], x["id"]))
                x["formats"] = FORMATS.get(x["provider"], [])
                uniq.append(x)
        uniq = uniq[:8]
        self._set_sound(key, status="found" if uniq else "none", cands=json.dumps(uniq))
        return {"id": key, "title": title, "candidates": uniq, "errors": errors,
                "links": manual_links(title, game.get("platform_name", ""))}

    # -- torrent watches --
    def add_watch(self, key: str, tag: str, release: str, client_id: str = "") -> int:
        """Start watching a torrent (identified by its qBittorrent tag) for this game."""
        now = time.time()
        with self._lock:
            c = self._db()
            try:
                c.execute("UPDATE watch SET state='cancelled', updated=? WHERE key=? AND state NOT IN ('added','failed','lost','cancelled')",
                          (now, key))
                cur = c.execute("INSERT INTO watch(key,tag,release,state,message,pct,added,updated,seen) VALUES(?,?,?,?,?,?,?,?,?)",
                                (key, tag, release[:300], "downloading", "Waiting for the torrent to start", 0.0, now, now, now))
                c.execute("UPDATE watch SET client_id=? WHERE tag=?", (client_id, tag))
                c.commit()
                return int(cur.lastrowid)
            finally:
                c.close()


    def _set_watch(self, wid: int, **kw) -> bool:
        """Update a live watch; returns False when it was cancelled meanwhile."""
        with self._lock:
            c = self._db()
            try:
                if not c.execute("SELECT 1 FROM watch WHERE id=? AND state!='cancelled'", (wid,)).fetchone():
                    return False
                for k, v in kw.items():
                    if k not in ("state", "message", "pct", "seen"):
                        raise ValueError(k)
                    c.execute(f"UPDATE watch SET {k}=? WHERE id=?", (v, wid))
                c.execute("UPDATE watch SET updated=? WHERE id=?", (time.time(), wid))
                c.commit()
                return True
            finally:
                c.close()

    def active_watches(self) -> list[dict]:
        c = self._db()
        try:
            return [dict(r) for r in c.execute("SELECT * FROM watch WHERE state NOT IN ('added','failed','lost','cancelled') ORDER BY id")]
        finally:
            c.close()

    def watch_for(self, key: str) -> dict | None:
        """The active torrent watch for a game, else the most recent one (so a failure stays visible)."""
        c = self._db()
        try:
            r = c.execute("SELECT * FROM watch WHERE key=? ORDER BY (state NOT IN ('added','failed','lost','cancelled')) DESC, id DESC",
                          (key,)).fetchone()
        finally:
            c.close()
        return dict(r) if r else None

    def cancel_watch(self, key: str) -> None:
        with self._lock:
            c = self._db()
            try:
                c.execute("UPDATE watch SET state='cancelled', message='Stopped watching; the torrent is still in your download client', "
                          "updated=? WHERE key=? AND state NOT IN ('added','failed','lost','cancelled')", (time.time(), key))
                c.commit()
            finally:
                c.close()

    def process_watches(self, rows: list[dict], resolve: Callable[[str], tuple[dict, RommApi]],
                        map_path: Callable[[str], str], *, client_id: str | None = None) -> list[str]:
        """One pass over the watched torrents. `rows` is the client's torrent list for the soundtrack category.

        `resolve(key)` returns (game, RommApi) or raises LookupError while the library is still loading.
        Returns human-readable notes about what changed, for the scheduler's log.
        """
        notes = []
        for w in self.active_watches():
            if client_id is not None and w.get("client_id") != client_id:
                continue
            row = next((r for r in rows if w["tag"] in {t.strip() for t in str(r.get("tags") or "").split(",")}), None)
            now = time.time()
            if row is None:
                if now - (w["seen"] or w["added"]) > WATCH_LOST_AFTER:
                    self._set_watch(w["id"], state="lost", message="The torrent is no longer in the download client")
                    notes.append(f"{w['release']}: lost")
                continue
            pct = float(row.get("progress") or 0.0)
            if "error" in str(row.get("state") or "").lower() and "missing" in str(row.get("state") or "").lower():
                self._set_watch(w["id"], state="failed", message="The download client reports missing files", seen=now)
                continue
            if pct < 1.0:
                self._set_watch(w["id"], pct=pct, seen=now, state="downloading",
                                message=f"Downloading {pct * 100:.0f}%" + (f", {int(row['num_seeds'])} seeders" if row.get("num_seeds") else ""))
                continue
            try:
                game, api = resolve(w["key"])
            except (LookupError, RuntimeError, ValueError):
                self._set_watch(w["id"], pct=1.0, seen=now, message="Finished; waiting for the RomM library to load")
                continue
            path = map_path(str(row.get("content_path") or row.get("save_path") or ""))
            if not self._set_watch(w["id"], pct=1.0, seen=now, state="importing",
                                   message="Download finished; choosing tracks"):
                continue                                     # cancelled or replaced while resolving
            try:
                done = self._import_torrent(w, game, api, path, str(row.get("name") or w["release"]))
            except Exception as ex:  # noqa: BLE001 - recorded on the watch, never raised into the scheduler
                msg = api._redact(str(ex)) if isinstance(ex, RommError) else (str(ex) if isinstance(ex, (ValueError, JobBusy)) else ex.__class__.__name__)
                log.warning("soundtrack torrent import for %s failed: %s", w["key"], ex.__class__.__name__)
                if isinstance(ex, JobBusy):
                    self._set_watch(w["id"], state="downloading", message="Finished; waiting for the current soundtrack job")
                else:
                    self._set_watch(w["id"], state="failed", message=msg[:300])
                continue
            notes.append(f"{w['release']}: {done}")
        return notes

    def _import_torrent(self, w: dict, game: dict, api: RommApi, path: str, name: str) -> str:
        files, zips = torrent_content(path)
        members = zip_members(zips)
        # One format family is chosen across loose files AND zip members together.
        _kept, ext = choose_audio(files + [(m[0], "", 0) for m in members])
        key = str(game["id"])
        if not ext:
            hint = ""
            if not os.path.exists(path):
                hint = f"; the path {path!r} is not visible to ROMarr (mount it or add a remote path mapping)"
            elif any(f.lower().endswith((".rar", ".7z")) for _dp, _d, fns in os.walk(path) for f in fns):
                hint = "; it only holds archives ROMarr cannot open (rar/7z)"
            self._set_watch(w["id"], state="failed", message=("No audio RomM can play was found in the torrent" + hint)[:300])
            return "no audio"
        loose = AudioFiles([f for f in files if os.path.splitext(f[0])[1].lower() == ext],
                           getattr(files, "identities", {}))
        zipped = [m for m in members if os.path.splitext(m[0])[1].lower() == ext]
        made, notes = 0, []
        if loose:
            job = self._begin_job([key], game["name"], "torrent")
            self._run_album(job, game, api, "torrent:" + name, loose, provider="torrent")
            if job["state"] == "error":
                self._set_watch(w["id"], state="failed", message=job["message"][:300])
                return "failed"
            made += job["made"]
        if zipped:
            made += self._import_zip_members(game, api, zipped, name)
        left = len(files) + len(members) - len(loose) - len(zipped)
        self._set_watch(w["id"], state="added" if made else "failed",
                        message=(f"Added {made} {ext[1:].upper()} track{'s' if made != 1 else ''}" +
                                 (f"; {left} files in other formats left out" if left else "")) if made
                        else "Everything in the torrent was already on this game or refused by RomM")
        return f"added {made}"

    def _import_zip_members(self, game: dict, api: RommApi, members: list[tuple[str, str, str]], name: str) -> int:
        """Upload the chosen zip members (name, zip path, member name) through RomM; returns how many were taken."""
        key = str(game["id"])
        job = self._begin_job([key], game["name"], "torrent")
        sink, journaled, failure = None, False, None
        try:
            self._require_stable_stem(api, key, "nothing was added")
            sink = RommSink(api, key, self.tmp_dir)
            for zpath, group in itertools.groupby(sorted(members, key=lambda m: m[1]), key=lambda m: m[1]):
                wanted = {m[2]: m[0] for m in group}
                with _audio_zip(zpath) as z:
                    for info in z.infolist():
                        if info.filename not in wanted:
                            continue

                        def chunks(info=info, z=z):
                            left = info.file_size
                            with z.open(info) as src:
                                while True:
                                    b = src.read(1 << 20)
                                    if not b:
                                        return
                                    left -= len(b)
                                    if left < 0:
                                        raise ValueError(f"{info.filename} is larger than its header says")
                                    yield b
                        sink(wanted[info.filename], chunks())
                        job["made"] = len(sink.sent)
            made = len(sink.sent)
            self._set_sound(key, status="downloaded" if made else "found", provider="torrent", title=game["name"],
                            chosen=json.dumps({"provider": "torrent", "id": "torrent:" + name, "title": name}),
                            files=len(api.tracks(key)) if made else 0, note=f"{made} track(s) from a torrent's zip")
            self._journal_sent(key, sink.sent, api)
            journaled = True
            job.update(state="done", made=made, pct=1.0, step="Done")
            return made
        except Exception as ex:  # noqa: BLE001
            failure = str(ex) if isinstance(ex, ValueError) else ex.__class__.__name__
            raise
        finally:
            if sink is not None and not journaled:
                try:
                    self._journal_sent(key, sink.sent, api)
                except Exception:  # noqa: BLE001
                    log.warning("could not journal the torrent upload for %s", key)
            if failure is not None:                            # terminal only after ownership is recorded
                job.update(state="error", step="Failed", message=failure)
            job["finished"] = time.time()

    # -- jobs --
    def job_for(self, key: str) -> dict | None:
        """The running job for a game, else its most recent finished one."""
        with self._lock:
            mine = [j for j in self._jobs.values() if key in j["games"]]
        mine.sort(key=lambda j: j["started"])
        running = [j for j in mine if j["state"] == "running"]
        return dict(running[-1] if running else mine[-1]) if mine else None

    def jobs(self) -> list[dict]:
        with self._lock:
            return sorted((dict(j) for j in self._jobs.values()), key=lambda j: j["started"], reverse=True)

    def start_download(self, game: dict, api: RommApi, cand: dict, fmt: str, run_async: bool = True,
                       force: bool = False) -> dict:
        """Begin a download job. One job per game at a time: a second request while one runs is refused.

        A soundtrack another game already holds (same source item) is not fetched
        a second time unless `force` says so: that is how duplicates start.
        """
        self._check_api(api)
        key = str(game["id"])
        provider, cid = str(cand.get("provider") or ""), str(cand.get("id") or "")
        if provider not in self.providers:
            raise ValueError("unknown source")
        if fmt not in ("mp3", "original", "both", "files", "flac"):
            raise ValueError("unknown format")
        if provider == "zophar" and not valid_zophar_id(cid):
            raise ValueError("not a Zophar music page")
        if provider == "archive" and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._\-]*", cid):
            raise ValueError("not an Internet Archive identifier")
        holders = [] if force else self.held_elsewhere(key, provider, cid)
        if holders:
            raise AlreadyStored(holders)
        job = self._begin_job([key], game["name"], "download")
        self._spawn(self._run_download, job, game, api, {"provider": provider, "id": cid}, fmt, run_async=run_async)
        return dict(job)

    def _begin_job(self, keys: list[str], title: str, kind: str) -> dict:
        """Register a running job over these games. A game is in at most one running job, so any overlap is refused."""
        with self._lock:
            if sum(j["state"] == "running" for j in self._jobs.values()) >= 4:
                raise JobBusy("Four soundtrack jobs are already running; wait for one to finish")
            if any(j["state"] == "running" and set(j["games"]) & set(keys) for j in self._jobs.values()):
                raise JobBusy("a soundtrack job is already running for this game")
            job = {"id": uuid.uuid4().hex[:12], "game_id": keys[0] if keys else "", "games": list(keys), "kind": kind, "title": title,
                   "state": "running", "step": "Starting", "pct": None, "made": 0, "skipped": 0, "message": "",
                   "started": time.time(), "finished": None}
            self._jobs[job["id"]] = job
            for old in sorted(self._jobs.values(), key=lambda j: j["started"])[:-self.MAX_JOBS]:
                if old["state"] != "running":
                    del self._jobs[old["id"]]
        return job

    def _spawn(self, fn: Callable[..., None], job: dict, *args, run_async: bool = True) -> None:
        if run_async:
            threading.Thread(target=fn, args=(job, *args), daemon=True, name=f"soundtrack-{job['id']}").start()
        else:
            fn(job, *args)

    def _progress(self, job: dict) -> Callable[..., None]:
        def progress(step: str, pct: float | None = None) -> None:
            job["step"], job["pct"] = step, (round(pct, 3) if pct is not None else None)
        return progress

    def _run_download(self, job: dict, game: dict, api: RommApi, cand: dict, fmt: str) -> None:
        self._check_api(api)
        key = job["game_id"]
        try:
            made, skipped = self._download(job, game, api, cand, fmt)
            job.update(state="done", made=made, skipped=skipped, pct=1.0, step="Done",
                       message=(f"Added {made} track{'s' if made != 1 else ''}" +
                                (f"; {skipped} skipped (already there or not a format Jukebox plays)" if skipped else "")))
        except Exception as ex:  # noqa: BLE001 - reported on the job, never raised into a thread nobody joins
            msg = api._redact(str(ex)) if isinstance(ex, RommError) else (str(ex) if isinstance(ex, ValueError) else ex.__class__.__name__)
            log.warning("soundtrack download for %s failed: %s", key, ex.__class__.__name__)
            job.update(state="error", step="Failed", message=msg)
            self._set_sound(key, note=f"download failed: {msg}"[:300])
        finally:
            job["finished"] = time.time()

    def _download(self, job: dict, game: dict, api: RommApi, cand: dict, fmt: str) -> tuple[int, int]:
        key = job["game_id"]
        progress = self._progress(job)
        progress(f"Looking up the download on {cand['provider'].capitalize()}")
        plan = self.providers[cand["provider"]][1](cand["id"])
        picks = pick_downloads(plan["downloads"], fmt)
        if not picks:
            raise ValueError(f"no '{fmt}' download is offered for this item")
        self._require_stable_stem(api, key, "not downloaded")
        sink = RommSink(api, key, self.tmp_dir)
        made = skipped = 0
        try:
            for item in picks:
                m, s_ = do_download(item, sink, self.tmp_dir, progress)
                made, skipped = made + m, skipped + s_
                job["made"], job["skipped"] = made, skipped
        except RommError as ex:
            if ex.status in (401, 403):
                raise ValueError("RomM refused the upload: the library login needs permission to edit ROMs (roms.write)") from ex
            raise
        finally:
            self._record(key, sink, api)
        total = len(api.tracks(key)) if made else None
        self._set_sound(key, status="downloaded" if (made or total) else "found", files=total or 0,
                        provider=cand["provider"], chosen=json.dumps(cand), title=game["name"],
                        note=f"{made} new file(s), {skipped} skipped (already there, or not a format RomM Jukebox plays)")
        return made, skipped

    def _record(self, key: str, sink: RommSink, api: RommApi) -> None:
        """Journal what was uploaded, with RomM's file ids, so it can be undone through RomM.

        Runs even when the job failed part-way, so a half-finished download can
        still be taken back.
        """
        self._journal_sent(key, sink.sent, api)

    def _track_digest(self, api, key, file_id, name):
        digest, size = hashlib.sha256(), 0
        for chunk in api.iter_track(key, file_id, name):
            size += len(chunk)
            if size > MAX_ARCHIVE_BYTES:
                raise ValueError("Stored soundtrack exceeds size limit")
            digest.update(chunk)
        return digest.hexdigest(), size

    def _journal_sent(self, key: str, names: list[str], api: RommApi) -> None:
        """Own only uploaded bytes verified against the sender's receipt.

        Each verified track is committed on its own, and repeating a call is
        harmless. Whatever cannot be verified yet is kept as a pending receipt,
        so Undo can still claim it once RomM answers.
        """
        self._check_api(api)
        if not names:
            return
        receipts = getattr(api, "receipts", {})
        done: set[str] = set()
        try:
            tracks = {track["file_name"]: track["file_id"] for track in api.tracks(key)}
            for name in names:
                if name not in tracks:
                    raise ValueError("Uploaded soundtrack could not be verified")
                expected = receipts.get((str(key), name))
                actual = self._track_digest(api, key, tracks[name], name)
                if expected is None or expected != actual:
                    raise ValueError("Remote soundtrack differs from the upload receipt; review it manually")
                self._own_track(key, f"romm://{key}/{tracks[name]}/{name}", name, actual)
                done.add(name)
        except Exception:
            self._keep_pending(key, [n for n in names if n not in done], receipts)
            raise

    def _own_track(self, key: str, path: str, name: str, digest_size: tuple[str, int]) -> None:
        digest, size = digest_size
        with self._lock:
            c = self._db()
            try:
                if not c.execute("SELECT 1 FROM journal WHERE key=? AND path=? AND undone=0", (key, path)).fetchone():
                    c.execute("INSERT INTO journal(key,path,size,ts,sha256) VALUES(?,?,?,?,?)",
                              (key, path, size, time.time(), digest))
                c.execute("DELETE FROM pending WHERE key=? AND name=?", (key, name))
                c.commit()
            finally:
                c.close()

    def _keep_pending(self, key: str, names: list[str], receipts: dict) -> None:
        rows = [(key, name, *receipts[(str(key), name)], time.time())
                for name in names if (str(key), name) in receipts]
        if not rows:
            return
        with self._lock:
            c = self._db()
            try:
                c.executemany("INSERT OR REPLACE INTO pending(key,name,sha256,size,ts) VALUES(?,?,?,?,?)", rows)
                c.commit()
            finally:
                c.close()

    def _reconcile_pending(self, key: str, api: RommApi) -> None:
        """Journal earlier uploads whose verification failed, now that RomM answers."""
        c = self._db()
        try:
            rows = c.execute("SELECT name, sha256, size FROM pending WHERE key=?", (key,)).fetchall()
        finally:
            c.close()
        for row in rows:
            api.receipts[(str(key), row["name"])] = (row["sha256"], row["size"])
            try:
                self._journal_sent(key, [row["name"]], api)
            except (RommError, ValueError, requests.RequestException):
                log.info("soundtrack upload for %s is still unverified", key)

    @staticmethod
    def _require_stable_stem(api: RommApi, key: str, outcome: str) -> None:
        """Refuse an upload that would move a lone ROM whose stored name would change."""
        rom = api.rom(key)
        if not (rom.get("has_multiple_files") or rom.get("has_nested_single_file")) \
                and not stem_stable(str(rom.get("fs_name") or "")):
            raise ValueError("moving this ROM into a folder would change the name RomM stores for it (its name ends in a "
                             f"dotted number), which breaks name-based save matching; {outcome}")

    def _ensure_idle(self, *keys: str) -> None:
        with self._lock:
            if sum(j["state"] == "running" for j in self._jobs.values()) >= 4:
                raise JobBusy("Four soundtrack jobs are already running; wait for one to finish")
            if any(j["state"] == "running" and set(j["games"]) & set(keys) for j in self._jobs.values()):
                raise JobBusy("a soundtrack job is running for this game")

    def held_elsewhere(self, key: str, provider: str, cid: str) -> list[str]:
        """Titles of the other games that already hold this exact source item (downloaded here, or merged away)."""
        c = self._db()
        try:
            rows = c.execute("SELECT key, title, chosen FROM sound WHERE key!=? AND status IN ('downloaded','merged') "
                             "AND chosen IS NOT NULL", (key,)).fetchall()
        finally:
            c.close()
        out = []
        for r in rows:
            try:
                ch = json.loads(r["chosen"])
            except ValueError:
                continue
            if isinstance(ch, dict) and ch.get("provider") == provider and str(ch.get("id")) == cid:
                out.append(r["title"] or f"game {r['key']}")
        return out

    # -- undo --
    def undo(self, game: dict, api: RommApi) -> dict:
        """Remove exactly what this module added to the game, by the file ids it journaled."""
        self._check_api(api)
        key = str(game["id"])
        # Registered as a job so a merge or upload cannot start on this game
        # between reading the journal and the last deletion.
        job = self._begin_job([key], game.get("name", ""), "undo")
        try:
            return self._undo(job, key, api)
        except Exception as ex:
            job.update(state="error", step="Failed",
                       message=api._redact(str(ex)) if isinstance(ex, RommError) else ex.__class__.__name__)
            raise
        finally:
            job["finished"] = time.time()

    def _undo(self, job: dict, key: str, api: RommApi) -> dict:
        self._reconcile_pending(key, api)
        c = self._db()
        try:
            rows = c.execute("SELECT * FROM journal WHERE key=? AND undone=0", (key,)).fetchall()
        finally:
            c.close()
        removed = 0
        try:
            for r in rows:
                parts = r["path"][len("romm://"):].split("/", 2) if r["path"].startswith("romm://") else []
                if len(parts) != 3 or parts[0] != key:
                    continue                                        # not ours to act on
                current = {str(track["file_id"]): track["file_name"] for track in api.tracks(key)}
                if parts[1] in current:
                    if (not r["sha256"] or current[parts[1]] != parts[2]
                            or self._track_digest(api, key, parts[1], parts[2]) != (r["sha256"], r["size"])):
                        raise ValueError("Stored soundtrack changed; undo refused to remove it")
                if api.delete_track(key, parts[1]):
                    removed += 1
                with self._lock:
                    c = self._db()
                    try:
                        c.execute("UPDATE journal SET undone=1 WHERE id=?", (r["id"],))
                        c.commit()
                    finally:
                        c.close()
        finally:
            # Committed per file above, so a failure part-way leaves the rest journaled to retry.
            left = self._journal_count(key) + self._pending_count(key)
            self._set_sound(key, status="unsearched" if not left else "downloaded", files=left,
                            note=f"undone: {removed} file(s) removed")
        unverified = self._pending_count(key)
        job.update(state="done", made=removed, pct=1.0, step="Done",
                   message=(f"{unverified} upload(s) could not be verified yet; Undo will retry them" if unverified else ""))
        result = {"removed": removed, "remaining": self._journal_count(key) + unverified}
        if unverified:
            result["unverified"] = unverified
        return result

    def _pending_count(self, key: str) -> int:
        c = self._db()
        try:
            return int(c.execute("SELECT COUNT(*) FROM pending WHERE key=?", (key,)).fetchone()[0])
        finally:
            c.close()


    # -- phase 2: duplicate soundtracks --
    #
    # Games whose STORED soundtracks are identical. Candidates come from the file
    # lists RomM already has (names and sizes), but nothing is offered or done on
    # that alone: the bytes are streamed from RomM and compared by SHA-256, names
    # included, and a copy that differs is refused.

    def _hash_track(self, api: RommApi, rom_id: str, track: dict) -> str:
        h = hashlib.sha256()
        size = 0
        for chunk in api.iter_track(rom_id, track["file_id"], track["file_name"]):
            size += len(chunk)
            if size > MAX_ARCHIVE_BYTES:
                raise ValueError("Stored soundtrack exceeds size limit")
            h.update(chunk)
        return h.hexdigest()

    def _hash_tracks(self, api: RommApi, rom_id: str) -> tuple[dict, list[dict]]:
        """({file name: sha1}, the track records) for what RomM stores for this ROM."""
        tracks = api.tracks(rom_id)
        return {t["file_name"]: self._hash_track(api, rom_id, t) for t in tracks}, tracks

    def dups(self) -> dict:
        """The verified duplicate groups from the last scan, the merges that can be undone, and the scan job."""
        c = self._db()
        try:
            row = c.execute("SELECT ts, groups FROM dup_scan WHERE id=1").fetchone()
            merged = [dict(r) for r in c.execute(
                "SELECT id, drop_id, keep_id, drop_title, keep_title, ts, state FROM merges "
                "WHERE state IN ('merged','partial') ORDER BY ts DESC")]
        finally:
            c.close()
        groups = json.loads(row["groups"]) if row else []
        with self._lock:
            scans = sorted((dict(j) for j in self._jobs.values() if j["kind"] == "dup-scan"), key=lambda j: j["started"])
        return {"scanned": row["ts"] if row else None,
                "groups": [{k: v for k, v in g.items() if k != "files"} for g in groups],
                "merged": merged, "scan": scans[-1] if scans else None}

    def start_dup_scan(self, api: RommApi, run_async: bool = True) -> dict:
        self._check_api(api)
        with self._lock:
            if any(j["kind"] == "dup-scan" and j["state"] == "running" for j in self._jobs.values()):
                raise JobBusy("a duplicate scan is already running")
        job = self._begin_job([], "Duplicate soundtrack scan", "dup-scan")
        self._spawn(self._run_dup_scan, job, api, run_async=run_async)
        return dict(job)

    def _run_dup_scan(self, job: dict, api: RommApi) -> None:
        progress = self._progress(job)
        try:
            progress("Reading RomM's library")
            by_sig: dict[tuple, list[dict]] = {}
            for r in api.roms_with_files():
                tr = sorted((f.get("file_name") or "", int(f.get("file_size_bytes") or 0))
                            for f in r.get("files") or [] if f.get("category") == "soundtrack")
                if tr:
                    by_sig.setdefault(tuple(tr), []).append({"id": str(r["id"]), "name": r.get("name") or r.get("fs_name") or str(r["id"]),
                                                             "platform": r.get("platform_display_name") or r.get("platform_slug") or ""})
            cands = [(sig, g) for sig, g in by_sig.items() if len(g) > 1]
            found = []
            for i, (sig, group) in enumerate(cands):
                progress(f"Comparing file contents ({i + 1} of {len(cands)} candidate sets)", i / max(len(cands), 1))
                by_files: dict[str, tuple[dict, list[dict]]] = {}
                for g in group:
                    files, _tracks = self._hash_tracks(api, g["id"])
                    if not files:
                        continue
                    key = hashlib.sha1(json.dumps(sorted(files.items())).encode()).hexdigest()
                    by_files.setdefault(key, (files, []))[1].append(g)
                for key, (files, games) in by_files.items():
                    if len(games) > 1:
                        found.append({"id": key[:16], "tracks": len(files), "bytes": sum(sz for _n, sz in sig),
                                      "files": files, "games": games})
            found.sort(key=lambda g: (-g["bytes"] * (len(g["games"]) - 1), g["id"]))
            with self._lock:
                c = self._db()
                try:
                    c.execute("INSERT OR REPLACE INTO dup_scan(id, ts, groups) VALUES(1,?,?)", (time.time(), json.dumps(found)))
                    c.commit()
                finally:
                    c.close()
            job.update(state="done", made=len(found), pct=1.0, step="Done",
                       message=f"Found {len(found)} set{'s' if len(found) != 1 else ''} of identical soundtracks")
        except Exception as ex:  # noqa: BLE001 - reported on the job
            msg = api._redact(str(ex)) if isinstance(ex, RommError) else ex.__class__.__name__
            log.warning("duplicate scan failed: %s", ex.__class__.__name__)
            job.update(state="error", step="Failed", message=msg)
        finally:
            job["finished"] = time.time()

    def start_merge(self, keep: dict, drops: list[dict], api: RommApi, run_async: bool = True) -> dict:
        """Keep one game's soundtrack and remove the identical copies from the others (through RomM). One job over all of them."""
        self._check_api(api)
        ids = [str(d["id"]) for d in drops if str(d["id"]) != str(keep["id"])]
        if not ids:
            raise ValueError("choose at least one other game to merge into the one you keep")
        job = self._begin_job([str(keep["id"])] + ids, keep["name"], "merge")
        self._spawn(self._run_merge, job, keep, [d for d in drops if str(d["id"]) in ids], api, run_async=run_async)
        return dict(job)

    def _run_merge(self, job: dict, keep: dict, drops: list[dict], api: RommApi) -> None:
        self._check_api(api)
        progress = self._progress(job)
        problems: list[str] = []
        done = 0
        try:
            kid = str(keep["id"])
            progress(f"Reading {keep['name']}")
            kfiles, _ = self._hash_tracks(api, kid)
            if not kfiles:
                raise ValueError("the game you chose to keep has no soundtrack files in RomM")
            for i, drop in enumerate(drops):
                did = str(drop["id"])
                progress(f"Comparing {drop['name']} ({i + 1} of {len(drops)})", i / len(drops))
                dfiles, dtracks = self._hash_tracks(api, did)
                if dfiles != kfiles:                         # byte for byte, names included
                    problems.append(f"{drop['name']}: soundtrack is not identical to the one being kept; left alone")
                    continue
                row = self._add_merge(did, kid, drop["name"], keep["name"], kfiles)
                deleted: list[str] = []
                try:
                    for t in dtracks:
                        api.delete_track(did, t["file_id"])
                        deleted.append(str(t["file_id"]))
                except Exception as ex:  # noqa: BLE001 - the rest of the drops can still go ahead
                    problems.append(f"{drop['name']}: could not remove every file ({api._redact(str(ex)) if isinstance(ex, RommError) else ex.__class__.__name__}); "
                                    "undo restores what is missing")
                    self._finish_merge(row, did, deleted, "partial", None)
                    continue
                self._finish_merge(row, did, deleted, "merged", f"soundtrack kept on {keep['name']}")
                done += 1
            self._prune_dup_groups(self._merged_ids())
            job.update(state="done", made=done, skipped=len(problems), pct=1.0, step="Done",
                       message=f"Merged {done} cop{'ies' if done != 1 else 'y'} into {keep['name']}"
                               + (": " + "; ".join(problems) if problems else ""))
        except Exception as ex:  # noqa: BLE001
            msg = api._redact(str(ex)) if isinstance(ex, RommError) else (str(ex) if isinstance(ex, ValueError) else ex.__class__.__name__)
            log.warning("soundtrack merge failed: %s", ex.__class__.__name__)
            job.update(state="error", step="Failed", message=msg)
        finally:
            job["finished"] = time.time()

    def _add_merge(self, did: str, kid: str, dtitle: str, ktitle: str, files: dict) -> int:
        with self._lock:
            c = self._db()
            try:
                cur = c.execute("INSERT INTO merges(drop_id,keep_id,drop_title,keep_title,files,ts,state) VALUES(?,?,?,?,?,?, 'merging')",
                                (did, kid, dtitle, ktitle, json.dumps(files), time.time()))
                c.commit()
                return cur.lastrowid
            finally:
                c.close()

    def _finish_merge(self, row: int, did: str, deleted: list[str], state: str, note: str | None) -> None:
        """Record the outcome. Files that are gone from RomM are no longer something Undo (phase 1) could remove."""
        with self._lock:
            c = self._db()
            try:
                c.execute("UPDATE merges SET state=? WHERE id=?", (state, row))
                for r in c.execute("SELECT id, path FROM journal WHERE key=? AND undone=0", (did,)).fetchall():
                    parts = r["path"][len("romm://"):].split("/", 2) if r["path"].startswith("romm://") else []
                    if len(parts) == 3 and parts[1] in deleted:
                        c.execute("UPDATE journal SET undone=1 WHERE id=?", (r["id"],))
                c.execute("INSERT OR IGNORE INTO sound(key,status,ts) VALUES(?,?,?)", (did, "unsearched", time.time()))
                if state == "merged":
                    c.execute("UPDATE sound SET status='merged', files=0, note=?, ts=? WHERE key=?", (note, time.time(), did))
                else:
                    c.execute("UPDATE sound SET note=?, ts=? WHERE key=?", ("merge incomplete; Undo restores what is missing", time.time(), did))
                c.commit()
            finally:
                c.close()

    def _merged_ids(self) -> set[str]:
        c = self._db()
        try:
            return {r[0] for r in c.execute("SELECT drop_id FROM merges WHERE state IN ('merged','partial')")}
        finally:
            c.close()

    def _prune_dup_groups(self, merged: set[str]) -> None:
        """A game that lost its copy is no longer a duplicate; a set left with one game is not a set."""
        with self._lock:
            c = self._db()
            try:
                row = c.execute("SELECT groups FROM dup_scan WHERE id=1").fetchone()
                if not row:
                    return
                groups = []
                for g in json.loads(row["groups"]):
                    g["games"] = [x for x in g["games"] if x["id"] not in merged]
                    if len(g["games"]) > 1:
                        groups.append(g)
                c.execute("UPDATE dup_scan SET groups=? WHERE id=1", (json.dumps(groups),))
                c.commit()
            finally:
                c.close()

    def start_unmerge(self, drop: dict, api: RommApi, run_async: bool = True) -> dict:
        """Give a merged game its own copy back, re-uploaded from the keeper's files after checking them against the merge's hashes."""
        self._check_api(api)
        did = str(drop["id"])
        c = self._db()
        try:
            m = c.execute("SELECT * FROM merges WHERE drop_id=? AND state IN ('merged','partial') ORDER BY ts DESC", (did,)).fetchone()
        finally:
            c.close()
        if not m:
            raise ValueError("this game is not merged")
        job = self._begin_job([did, m["keep_id"]], drop["name"], "unmerge")
        self._spawn(self._run_unmerge, job, drop, dict(m), api, run_async=run_async)
        return dict(job)

    def _run_unmerge(self, job: dict, drop: dict, m: dict, api: RommApi) -> None:
        self._check_api(api)
        progress = self._progress(job)
        did, kid, want = str(drop["id"]), m["keep_id"], json.loads(m["files"])
        work = None
        sent: list[str] = []
        journaled = False
        failure = None
        try:
            work = tempfile.mkdtemp(prefix="unmerge-", dir=self._tmp())
            self._require_stable_stem(api, did, "not restored")
            ktracks = {t["file_name"]: t for t in api.tracks(kid)}
            if set(ktracks) != set(want):
                raise ValueError("the kept soundtrack has changed since the merge; cannot restore a verified copy")
            staged: list[tuple[str, str, int]] = []
            for i, (name, t) in enumerate(sorted(ktracks.items())):          # fetch and verify everything before touching the game
                progress(f"Fetching {name} from the kept copy", i / max(len(ktracks), 1) / 2)
                path = os.path.join(work, f"{i}.bin")
                h, size = hashlib.sha256(), 0
                with open(path, "wb") as f:
                    for chunk in api.iter_track(kid, t["file_id"], name):
                        size += len(chunk)
                        if size > MAX_ARCHIVE_BYTES:
                            raise ValueError("Stored soundtrack exceeds size limit")
                        h.update(chunk)
                        f.write(chunk)
                if h.hexdigest() != want[name]:
                    raise ValueError("the kept soundtrack has changed since the merge; cannot restore a verified copy")
                staged.append((name, path, size))
            existing = {t["file_name"]: t for t in api.tracks(did)}
            # A track already on the game only counts as restored if it is the
            # recorded copy; anything else is a conflict to review, not overwrite.
            conflicts = [name for name, _path, _size in staged if name in existing
                         and self._track_digest(api, did, existing[name]["file_id"], name)[0] != want[name]]
            if conflicts:
                raise ValueError(f"{len(conflicts)} track(s) on this game differ from the merged copy "
                                 f"({', '.join(sorted(conflicts)[:3])}); the merge stays recorded")
            problems = []
            for i, (name, path, size) in enumerate(staged):
                progress(f"Restoring {name}", 0.5 + i / max(len(staged), 1) / 2)
                if name in existing:
                    continue
                up = header_safe(name)
                try:
                    api.upload(did, up, path, size)
                except RommError as ex:
                    if ex.status in (401, 403):
                        raise ValueError("RomM refused the upload: the library login needs permission to edit ROMs (roms.write)") from ex
                    if ex.status in (400, 409):
                        problems.append(name)
                        continue
                    raise
                sent.append(up)
            if problems:
                raise ValueError(f"RomM refused {len(problems)} of the files; the merge stays recorded so you can retry")
            self._journal_sent(did, sent, api)
            journaled = True
            with self._lock:
                c = self._db()
                try:
                    c.execute("UPDATE merges SET state='unmerged' WHERE id=?", (m["id"],))
                    c.commit()
                finally:
                    c.close()
            self._set_sound(did, status="downloaded", files=len(want), note=f"restored from {m['keep_title']}", title=drop["name"])
            job.update(state="done", made=len(sent), pct=1.0, step="Done",
                       message=f"Restored {len(want)} file{'s' if len(want) != 1 else ''} to {drop['name']}")
        except Exception as ex:  # noqa: BLE001
            msg = api._redact(str(ex)) if isinstance(ex, RommError) else (str(ex) if isinstance(ex, ValueError) else ex.__class__.__name__)
            log.warning("soundtrack unmerge failed: %s", ex.__class__.__name__)
            failure = msg
        finally:
            if not journaled:
                try:
                    self._journal_sent(did, sent, api)      # what was re-uploaded can be removed again from the game page
                except Exception:  # noqa: BLE001
                    log.warning("could not journal the restored soundtrack for %s", did)
            if work is not None:
                shutil.rmtree(work, ignore_errors=True)
            if failure is not None:                         # terminal only after ownership is recorded
                job.update(state="error", step="Failed", message=failure)
            job["finished"] = time.time()

    def _tmp(self) -> str:
        os.makedirs(self.tmp_dir, exist_ok=True)
        return self.tmp_dir

    # -- phase 3: albums from the user's music library --

    ALBUM_TTL = 120

    def library_albums(self, root: str) -> list[dict]:
        """Albums under the music library root, listed once and kept for a couple of minutes (walking a big library is slow)."""
        if not root or not os.path.isdir(root):
            raise ValueError("the music library folder is not available; set music_library_root and mount it read-only")
        real = os.path.realpath(root)
        with self._lock:
            cached = getattr(self, "_albums", None)
            if cached and cached[0] == real and time.time() - cached[1] < self.ALBUM_TTL:
                return cached[2]
        albums = scan_albums(real)
        with self._lock:
            self._albums = (real, time.time(), albums)
        return albums

    def library_browse(self, root: str, rel: str) -> dict:
        if not root or not os.path.isdir(root):
            raise ValueError("the music library folder is not available; set music_library_root and mount it read-only")
        return browse_library(root, rel)

    def library_search(self, root: str, query: str) -> dict:
        albums = self.library_albums(root)
        artists = sorted({a["id"].split("/")[0] for a in albums if "/" in a["id"]}, key=str.lower)
        step = max(len(artists) // 6, 1)
        return {"albums": search_albums(albums, query), "total": len(albums), "artists": len(artists),
                "samples": artists[::step][:6]}

    def start_album(self, game: dict, api: RommApi, root: str, album_id: str, run_async: bool = True) -> dict:
        """Upload an album's audio from the music library into the game's soundtrack. The library is only ever read."""
        self._check_api(api)
        key = str(game["id"])
        if not root or not os.path.isdir(root):
            raise ValueError("the music library folder is not available; set music_library_root and mount it read-only")
        src = resolve_album(root, album_id)
        files = collect_album(src)
        if not files:
            raise ValueError("no audio RomM can play in that folder")
        job = self._begin_job([key], game["name"], "album")
        self._spawn(self._run_album, job, game, api, album_id, files, run_async=run_async)
        return dict(job)

    def _run_album(self, job: dict, game: dict, api: RommApi, album_id: str, files: list[tuple[str, str, int]],
                   provider: str = "library") -> None:
        self._check_api(api)
        key, progress = job["game_id"], self._progress(job)
        origin = "the music library" if provider == "library" else "a torrent"
        sent: list[str] = []
        made = skipped = 0
        journaled = False
        failure = None
        try:
            self._require_stable_stem(api, key, "nothing was added")
            existing = {t["file_name"] for t in api.tracks(key)}
            for i, (name, path, size) in enumerate(files):
                progress(f"Adding track {i + 1} of {len(files)}", i / len(files))
                if name in existing:
                    skipped += 1
                    continue
                try:
                    expected = getattr(files, "identities", {}).get(path)
                    root, root_id, identity = expected if expected else (str(Path(path).parent), None, None)
                    fd, snapshot = tempfile.mkstemp(prefix="album-", dir=self._tmp())
                    try:
                        with os.fdopen(fd, "wb") as destination:
                            with _open_source(root, os.path.relpath(path, root), root_id) as source:
                                before = _fd_identity(source)
                                if identity is not None and before != identity:
                                    raise ValueError("Selected soundtrack source changed")
                                copied = 0
                                for data in iter(lambda: source.read(1 << 20), b""):
                                    copied += len(data)
                                    if copied > MAX_ARCHIVE_BYTES:
                                        raise ValueError("Soundtrack exceeds size limit")
                                    destination.write(data)
                                if _fd_identity(source) != before or copied != before[2]:
                                    raise ValueError("Soundtrack source changed during copy")
                        api.upload(key, name, snapshot, copied)
                    finally:
                        os.unlink(snapshot)
                except RommError as ex:
                    if ex.status in (401, 403):
                        raise ValueError("RomM refused the upload: the library login needs permission to edit ROMs (roms.write)") from ex
                    if ex.status in (400, 409):
                        skipped += 1
                        continue
                    raise
                sent.append(name)
                existing.add(name)
                made += 1
                job["made"], job["skipped"] = made, skipped
            title = os.path.basename(album_id.rstrip("/"))
            self._set_sound(key, status="downloaded" if made else "found", files=len(api.tracks(key)) if made else 0,
                            provider=provider, chosen=json.dumps({"provider": provider, "id": album_id, "title": title}),
                            title=game["name"], note=f"{made} track(s) from {origin}, {skipped} skipped (already there or refused)")
            self._journal_sent(key, sent, api)
            journaled = True
            job.update(state="done", made=made, skipped=skipped, pct=1.0, step="Done",
                       message=f"Added {made} track{'s' if made != 1 else ''} from {title}"
                               + (f"; {skipped} skipped (already there)" if skipped else ""))
        except Exception as ex:  # noqa: BLE001
            msg = api._redact(str(ex)) if isinstance(ex, RommError) else (str(ex) if isinstance(ex, ValueError) else ex.__class__.__name__)
            log.warning("album upload for %s failed: %s", key, ex.__class__.__name__)
            failure = msg
            self._set_sound(key, note=f"album upload failed: {msg}"[:300])
        finally:
            if not journaled:
                try:
                    self._journal_sent(key, sent, api)         # also after a failure, so a partial add can be undone
                except Exception:  # noqa: BLE001
                    log.warning("could not journal the album upload for %s", key)
            if failure is not None:                            # terminal only after ownership is recorded
                job.update(state="error", step="Failed", message=failure)
            job["finished"] = time.time()


# ------------------------------------------------------------------ torrents --
#
# A torrent the user picked for a game is handed to qBittorrent under its own
# category and watched here. When it finishes, the audio RomM can play is
# chosen and uploaded exactly like a music-library album (journaled, undoable).
# The torrent itself is never touched: it keeps seeding and nothing is deleted.

#: Best first. One family is kept so a torrent that ships MP3 and FLAC of the same
#: album does not store it twice; chiptune rips only win when nothing else is there.
FORMAT_ORDER = (".mp3", ".ogg", ".oga", ".opus", ".m4a", ".aac", ".flac", ".wav", ".spc", ".vgm", ".vgz", ".gym")
#: How long a watched torrent may be missing from the client before it is given up on.
WATCH_LOST_AFTER = 600
WATCH_DONE = ("added", "failed", "lost", "cancelled")


def choose_audio(files: list[tuple[str, str, int]]) -> tuple[list[tuple[str, str, int]], str]:
    """Keep the files of the single best audio format present. Returns (files, extension kept)."""
    by_ext: dict[str, list] = {}
    for f in files:
        by_ext.setdefault(os.path.splitext(f[0])[1].lower(), []).append(f)
    for ext in FORMAT_ORDER:
        if by_ext.get(ext):
            return AudioFiles(by_ext[ext], getattr(files, "identities", {})), ext
    return [], ""


def zip_members(zips: list[str]) -> list[tuple[str, str, str]]:
    """[(name to store, zip path, member name)] for the audio inside each zip.

    Folders inside the zip are folded into the name ("CD2 - 01 Intro.mp3") so discs do not collide.
    An unreadable or oversized zip is skipped.
    """
    out = []
    for zp in zips:
        try:
            check_zip(zp)
            with _audio_zip(zp) as z:
                for i in z.infolist():
                    if i.is_dir() or not audio_member_name(i.filename):
                        continue
                    parts = [p for p in i.filename.replace("\\", "/").split("/") if p and p != ".."]
                    out.append((header_safe(safe_name(" - ".join(parts))), zp, i.filename))
        except (zipfile.BadZipFile, ValueError, OSError):
            continue
    return out


def torrent_content(path: str) -> tuple[list[tuple[str, str, int]], list[str]]:
    """Audio files in a finished torrent's content path, plus zips inside it that were not opened."""
    if not path or not os.path.exists(path):
        return [], []
    real = os.path.realpath(path)
    if os.path.isfile(real):
        if _is_audio(os.path.basename(real)):
            return [(header_safe(safe_name(os.path.basename(real))), real, os.path.getsize(real))], []
        return [], ([real] if real.lower().endswith(".zip") else [])
    files = collect_album(real)
    zips = []
    for dp, _dns, fns in os.walk(real):
        zips += [os.path.join(dp, f) for f in sorted(fns) if f.lower().endswith(".zip") and not f.startswith(".")]
    return files, zips


class JobBusy(Exception):
    pass


class AlreadyStored(Exception):
    """Another game already holds this soundtrack; downloading it again would only make a duplicate."""

    def __init__(self, holders: list[str]):
        super().__init__("already stored on " + ", ".join(holders[:3]) + (f" and {len(holders) - 3} more" if len(holders) > 3 else "")
                         + "; not downloaded again (send force to download it anyway)")
        self.holders = holders


def pick_downloads(downloads: list[dict], fmt: str) -> list[dict]:
    """Which of a plan's downloads a format choice means. Plain file lists always count; FLAC only when asked for."""
    want = {"mp3": ["mp3"], "original": ["original"], "both": ["mp3", "original"], "flac": ["flac"]}.get(fmt, [])
    picks = [d for d in downloads if d["kind"] in want or d["kind"] == "files"]
    if not picks and fmt == "mp3":
        picks = [d for d in downloads if d["kind"] == "flac"]      # a set with no MP3 still has a lossless one
    return picks


def purge_tmp(tmp_dir: str) -> None:
    """Drop temp files a killed process left behind. Only ever called on startup."""
    shutil.rmtree(tmp_dir, ignore_errors=True)
