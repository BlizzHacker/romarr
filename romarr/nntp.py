"""Pure-Python standalone Usenet (NNTP / NZB / yEnc) download engine.

Allows ROMarr to fetch NZB releases over NNTP / NNTPS directly with zero external
binaries or daemons (no SABnzbd, NZBGet, or `nntplib` stdlib dependency, which
was removed in Python 3.13). Handles concurrent multi-socket segment downloads,
yEnc decoding with CRC32 verification, atomic `.partial` file assembly, and
permanent missing-article (`430`) retirement via `failed_jobs()`.
"""

from __future__ import annotations

import concurrent.futures
import logging
import os
import re
import secrets
import shutil
import socket
import ssl
import threading
import time
import xml.etree.ElementTree as ET
import zlib
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import requests

from .mdm import (
    _MdmLedger,
    _close_response,
    _safe_name,
    clear_job_control,
    register_job_control,
    set_job_future,
    stop_job,
)

log = logging.getLogger(__name__)

MAX_NZB_BYTES = 32 * 1024 * 1024

_QUOTED_SUBJECT_RE = re.compile(r'"([^"]+)"')
_FILE_EXT_RE = re.compile(
    r"([^\s/\\]+\.(?:zip|7z|rar|iso|chd|rvz|sfc|smc|nes|gb|gbc|gba|nds|n64|z64|bin|cue|rom|par2|nfo|nzb)"
    r"(?:\.(?:[0-9]{2,4}|r[0-9]{2,3}|rev))?)(?=\s|$)",
    re.I,
)
_YENC_KV_RE = re.compile(r"\b([a-z0-9_]+)=([^\s]+)", re.I)
_ENTITY_OR_DOCTYPE_RE = re.compile(r"<!(?:DOCTYPE|ENTITY)\b", re.I)


class NntpError(RuntimeError):
    """Base NNTP / NZB error."""


class NntpAuthError(NntpError):
    """Raised when NNTP server rejects credentials."""


class ArticleMissingError(NntpError):
    """Raised on permanent 430/423 missing articles or corrupt yEnc CRCs."""


@dataclass(frozen=True)
class NzbSegment:
    """Single article segment referenced inside an NZB file entry."""

    number: int
    size: int
    message_id: str


@dataclass(frozen=True)
class NzbFile:
    """Parsed NZB `<file>` entry grouping Usenet groups and sorted segments."""

    subject: str
    filename: str
    groups: tuple[str, ...]
    segments: tuple[NzbSegment, ...]


@dataclass(frozen=True)
class YencPart:
    """Decoded yEnc single-part or multi-part payload and byte-range metadata."""

    name: str
    data: bytes
    begin: int
    end: int
    part: int
    total: int
    expected_crc32: int | None
    file_size: int = 0


def _strip_ns(tag: str) -> str:
    """Return lowercase XML element local name with any namespace URI stripped."""
    return tag.rsplit("}", 1)[-1].lower() if "}" in tag else tag.lower()


def _subject_filename(subject: str, index: int) -> str:
    """Extract a sanitized filename (including split archive suffixes) from an NZB subject."""
    text = str(subject or "").strip()
    quoted = _QUOTED_SUBJECT_RE.findall(text)
    if quoted:
        return _safe_name(quoted[-1], fallback=f"file-{index}.bin")
    match = _FILE_EXT_RE.search(text)
    if match:
        return _safe_name(match.group(1), fallback=f"file-{index}.bin")
    return _safe_name(text, fallback=f"file-{index}.bin")


def parse_nzb(xml_payload: bytes | str) -> list[NzbFile]:
    """Parse an `.nzb` XML document into sorted file & segment descriptors."""
    if isinstance(xml_payload, bytes):
        raw_text = xml_payload.decode("utf-8", errors="replace")
    else:
        raw_text = str(xml_payload or "")
    stripped = raw_text.lstrip("\ufeff \t\r\n")
    if not stripped:
        raise NntpError("empty NZB payload")
    if _ENTITY_OR_DOCTYPE_RE.search(stripped[:4096]):
        # Allow harmless NZB DOCTYPE without any <!ENTITY declarations, reject ENTITY bombs.
        if re.search(r"<!ENTITY\b", stripped, re.I):
            raise NntpError("unsafe DOCTYPE/ENTITY declaration in NZB")

    try:
        root = ET.fromstring(stripped)
    except ET.ParseError as err:
        raise NntpError(f"invalid NZB XML: {err}") from err

    if _strip_ns(root.tag) != "nzb":
        raise NntpError("uploaded document is not an <nzb> root element")

    files: list[NzbFile] = []
    seen_names: set[str] = set()
    for idx, elem in enumerate(root, start=1):
        if _strip_ns(elem.tag) != "file":
            continue
        subject = str(elem.attrib.get("subject") or "")
        base_name = _subject_filename(subject, idx)
        candidate = base_name
        counter = 2
        while candidate.lower() in seen_names:
            stem, dot, ext = base_name.rpartition(".")
            candidate = f"{stem}_{counter}.{ext}" if dot else f"{base_name}_{counter}"
            counter += 1
        seen_names.add(candidate.lower())

        groups: list[str] = []
        segments: list[NzbSegment] = []
        for child in elem:
            ctag = _strip_ns(child.tag)
            if ctag == "groups":
                for g in child:
                    if _strip_ns(g.tag) == "group" and g.text and g.text.strip():
                        groups.append(g.text.strip())
            elif ctag == "segments":
                for seg in child:
                    if _strip_ns(seg.tag) != "segment":
                        continue
                    msg_id = (seg.text or "").strip().strip("<>")
                    if not msg_id:
                        continue
                    try:
                        number = int(seg.attrib.get("number") or len(segments) + 1)
                    except ValueError:
                        number = len(segments) + 1
                    try:
                        seg_bytes = int(seg.attrib.get("bytes") or 0)
                    except ValueError:
                        seg_bytes = 0
                    segments.append(NzbSegment(
                        number=number,
                        size=max(0, seg_bytes),
                        message_id=f"<{msg_id}>",
                    ))
        if not segments:
            continue
        segments.sort(key=lambda s: s.number)
        files.append(NzbFile(
            subject=subject,
            filename=candidate,
            groups=tuple(groups),
            segments=tuple(segments),
        ))

    if not files:
        raise NntpError("NZB contains no downloadable segments")
    return files


def _parse_yenc_params(line: str) -> dict[str, str]:
    """Parse `key=value` tokens from a `=ybegin`, `=ypart`, or `=yend` line."""
    out: dict[str, str] = {}
    name_idx = line.find(" name=")
    head = line[:name_idx] if name_idx >= 0 else line
    for key, val in _YENC_KV_RE.findall(head):
        out[key.lower()] = val
    if name_idx >= 0:
        out["name"] = line[name_idx + 6:].strip()
    return out


def decode_yenc(lines: list[bytes]) -> YencPart:
    """Decode dot-unstuffed yEnc article body lines and verify CRC32."""
    ybegin: dict[str, str] = {}
    ypart: dict[str, str] = {}
    yend: dict[str, str] = {}
    in_body = False
    decoded = bytearray()

    for raw in lines:
        line = raw.rstrip(b"\r\n")
        if line.startswith(b"=ybegin "):
            ybegin = _parse_yenc_params(line[8:].decode("latin1"))
            in_body = True
            continue
        if not in_body:
            continue
        if line.startswith(b"=ypart "):
            ypart = _parse_yenc_params(line[7:].decode("latin1"))
            continue
        if line.startswith(b"=yend "):
            yend = _parse_yenc_params(line[6:].decode("latin1"))
            break

        i = 0
        n = len(line)
        while i < n:
            b = line[i]
            if b == 0x3D and i + 1 < n:  # '=' escape marker
                i += 1
                c = line[i]
                decoded.append((c - 64 - 42) & 0xFF)
            else:
                decoded.append((b - 42) & 0xFF)
            i += 1

    if not ybegin or not yend:
        raise ArticleMissingError("article is missing yEnc header or trailer")

    payload = bytes(decoded)
    expected_size_str = yend.get("size")
    if expected_size_str:
        try:
            expected_size = int(expected_size_str)
            if len(payload) != expected_size:
                raise ArticleMissingError(
                    f"yEnc size mismatch: expected {expected_size}, got {len(payload)}"
                )
        except ValueError as err:
            raise ArticleMissingError("invalid yEnc size trailer") from err

    file_size = 0
    header_size_str = ybegin.get("size")
    if header_size_str:
        try:
            file_size = int(header_size_str)
        except ValueError as err:
            raise ArticleMissingError("invalid yEnc header size") from err
        if file_size < 0:
            raise ArticleMissingError("invalid yEnc header size")

    part_num = int(ybegin.get("part") or 0)
    total_parts = int(ybegin.get("total") or (1 if not part_num else 0))
    begin = int(ypart.get("begin") or 1)
    end = int(ypart.get("end") or len(payload))
    if ypart and (begin < 1 or end < begin or (end - begin + 1) != len(payload)):
        raise ArticleMissingError("yEnc part range does not match payload size")

    crc_hex = yend.get("pcrc32") if ypart else (yend.get("crc32") or yend.get("pcrc32"))
    expected_crc: int | None = None
    if crc_hex:
        try:
            expected_crc = int(crc_hex, 16)
        except ValueError as err:
            raise ArticleMissingError(f"invalid yEnc CRC32 {crc_hex!r}") from err
        actual_crc = zlib.crc32(payload) & 0xFFFFFFFF
        if actual_crc != expected_crc:
            raise ArticleMissingError(
                f"yEnc CRC32 mismatch: expected {expected_crc:08x}, got {actual_crc:08x}"
            )

    return YencPart(
        name=ybegin.get("name") or "",
        data=payload,
        begin=begin,
        end=end,
        part=part_num,
        total=total_parts,
        expected_crc32=expected_crc,
        file_size=file_size or len(payload),
    )


class NntpConnection:
    """Minimal RFC 3977 NNTP/NNTPS client with explicit auth and dot-unstuffing."""

    def __init__(
        self,
        host: str,
        port: int = 563,
        *,
        use_ssl: bool = True,
        verify_tls: bool = True,
        username: str = "",
        password: str = "",
        timeout: int = 30,
    ):
        self.host = str(host or "").strip()
        self.port = int(port or (563 if use_ssl else 119))
        self.use_ssl = bool(use_ssl)
        self.verify_tls = bool(verify_tls)
        self.username = str(username or "")
        self.password = str(password or "")
        self.timeout = max(2, int(timeout or 30))
        self._sock: socket.socket | ssl.SSLSocket | None = None
        self._reader = None

    def connect(self) -> str:
        """Open TCP/TLS socket to the NNTP server and authenticate if credentials are set."""
        raw = socket.create_connection((self.host, self.port), timeout=self.timeout)
        if self.use_ssl:
            ctx = ssl.create_default_context()
            if not self.verify_tls:
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
            try:
                sock: socket.socket | ssl.SSLSocket = ctx.wrap_socket(
                    raw, server_hostname=self.host,
                )
            except Exception:
                raw.close()
                raise
        else:
            sock = raw
        sock.settimeout(self.timeout)
        self._sock = sock
        self._reader = sock.makefile("rb")
        banner = self._readline()
        if not banner.startswith(("200", "201")):
            raise NntpError(f"unexpected NNTP greeting: {banner}")
        if self.username:
            self._authenticate()
        return banner

    def close(self) -> None:
        """Send QUIT and close the NNTP socket and buffered reader safely."""
        if self._sock is not None:
            try:
                self._sock.sendall(b"QUIT\r\n")
            except OSError:
                pass
            if self._reader is not None:
                try:
                    self._reader.close()
                except OSError:
                    pass
            try:
                self._sock.close()
            except OSError:
                pass
        self._reader = None
        self._sock = None

    def _readline(self) -> str:
        assert self._reader is not None
        line = self._reader.readline(1024 * 64)
        if not line:
            raise NntpError("NNTP connection closed unexpectedly")
        return line.rstrip(b"\r\n").decode("utf-8", errors="replace")

    def _command(self, cmd: str) -> str:
        assert self._sock is not None
        if "\r" in cmd or "\n" in cmd:
            raise ValueError("unsafe control characters in NNTP command")
        self._sock.sendall(cmd.encode("utf-8") + b"\r\n")
        return self._readline()

    def _authenticate(self) -> None:
        resp = self._command(f"AUTHINFO USER {self.username}")
        if resp.startswith("281"):
            return
        if not resp.startswith("381"):
            raise NntpAuthError(f"NNTP username rejected: {resp}")
        resp2 = self._command(f"AUTHINFO PASS {self.password}")
        if not resp2.startswith("281"):
            raise NntpAuthError(f"NNTP password rejected: {resp2}")

    def _read_multiline(self) -> list[bytes]:
        assert self._reader is not None
        lines: list[bytes] = []
        while True:
            raw = self._reader.readline(1024 * 256)
            if not raw:
                raise NntpError("NNTP stream ended before terminator")
            stripped = raw.rstrip(b"\r\n")
            if stripped == b".":
                break
            if raw.startswith(b".."):
                raw = raw[1:]
            lines.append(raw)
        return lines

    def fetch_body(self, message_id: str) -> list[bytes]:
        """Fetch raw unstuffed body lines for a single article Message-ID."""
        msg = str(message_id or "").strip()
        if not msg.startswith("<"):
            msg = f"<{msg}>"
        status = self._command(f"BODY {msg}")
        if status.startswith("222"):
            return self._read_multiline()
        if status.startswith(("500", "502")):
            status = self._command(f"ARTICLE {msg}")
            if status.startswith("220"):
                all_lines = self._read_multiline()
                for idx, line in enumerate(all_lines):
                    if line in (b"\r\n", b"\n", b""):
                        return all_lines[idx + 1:]
                return all_lines
        if status.startswith(("430", "423", "411")):
            raise ArticleMissingError(f"article unavailable ({msg}): {status}")
        if status.startswith(("480", "481", "482")):
            raise NntpAuthError(f"NNTP authentication required/failed: {status}")
        raise NntpError(f"NNTP fetch failed ({msg}): {status}")


def _origin_key(url: str) -> tuple[str, str, int] | None:
    """Return normalized `(scheme, hostname, port)` for an HTTP(S) URL without userinfo."""
    try:
        parts = urlsplit(str(url or "").strip())
        scheme = parts.scheme.lower()
        if scheme not in ("http", "https") or not parts.hostname:
            return None
        if parts.username is not None or parts.password is not None:
            return None
        port = parts.port or (443 if scheme == "https" else 80)
        return scheme, parts.hostname.lower(), port
    except ValueError:
        return None


@dataclass(frozen=True)
class BuiltinNzbConfig:
    """Connection and storage settings for `BuiltinNzbClient`."""

    host: str = ""
    port: int = 563
    use_ssl: bool = True
    verify_tls: bool = True
    username: str = ""
    password: str = ""
    connections: int = 8
    save_path: str = "/downloads/usenet"
    category: str = "romarr"
    timeout: int = 30
    allowed_origins: tuple[str, ...] = ()

    @property
    def base_url(self) -> str:
        """Return the formatted `nntp(s)://host:port` identifier string."""
        clean = str(self.host or "").strip()
        if not clean:
            return ""
        scheme = "nntps" if self.use_ssl else "nntp"
        return f"{scheme}://{clean}:{int(self.port or (563 if self.use_ssl else 119))}"


class BuiltinNzbClient:
    """Standalone built-in Usenet/NZB download client speaking raw NNTP."""

    protocol = "usenet"
    name = "Standalone Usenet (NNTP)"
    TAKES_NAME = True

    def __init__(
        self,
        config: BuiltinNzbConfig,
        *,
        session: requests.Session | None = None,
        allowed_origins: Iterable[str] = (),
    ):
        self._config = config
        self._session = session or requests.Session()
        self._save_path = str(config.save_path or "").strip()
        self._connections = max(1, min(32, int(config.connections or 8)))
        self._static_origins = tuple(
            str(u).strip() for u in (*config.allowed_origins, *allowed_origins) if str(u).strip()
        )
        self.allowed_origins_provider: Callable[[], Iterable[str]] | None = None
        self._ledger = _MdmLedger(Path(self._save_path or ".") / ".romarr-builtin-nzb.json")
        self._pool = concurrent.futures.ThreadPoolExecutor(
            max_workers=2,
            thread_name_prefix="romarr-nzb-job",
        )
        self._conn_cond = threading.Condition()
        self._idle_conns: list[NntpConnection] = []
        self._leased_conns = 0
        self._active_jobs = 0
        self._added = threading.local()
        self.detail = ""

    @property
    def last_job_id(self) -> str:
        return str(getattr(self._added, "job_id", "") or "")

    @last_job_id.setter
    def last_job_id(self, value: str) -> None:
        self._added.job_id = str(value or "")

    @property
    def last_error(self) -> str:
        return str(getattr(self._added, "error", "") or "")

    @last_error.setter
    def last_error(self, value: str) -> None:
        self._added.error = str(value or "")

    @property
    def configured(self) -> bool:
        """Whether both NNTP host and destination save directory are configured."""
        return bool(str(self._config.host or "").strip() and self._save_path)

    def _new_conn(self) -> NntpConnection:
        return NntpConnection(
            self._config.host,
            port=self._config.port,
            use_ssl=self._config.use_ssl,
            verify_tls=self._config.verify_tls,
            username=self._config.username,
            password=self._config.password,
            timeout=self._config.timeout,
        )

    def _borrow_conn(self, cancel_ev: threading.Event) -> NntpConnection:
        """Acquire an NNTP socket within the shared cross-job connection budget."""
        need_connect = False
        conn: NntpConnection | None = None
        with self._conn_cond:
            while True:
                if cancel_ev.is_set():
                    raise NntpError("cancelled by user")
                if self._idle_conns:
                    conn = self._idle_conns.pop()
                    self._leased_conns += 1
                    break
                if (self._leased_conns + len(self._idle_conns)) < self._connections:
                    self._leased_conns += 1
                    need_connect = True
                    break
                self._conn_cond.wait(timeout=0.2)
        if not need_connect:
            assert conn is not None
            return conn
        conn = self._new_conn()
        try:
            conn.connect()
        except Exception:
            conn.close()
            with self._conn_cond:
                self._leased_conns = max(0, self._leased_conns - 1)
                self._conn_cond.notify_all()
            raise
        return conn

    def _release_conn(self, conn: NntpConnection | None, *, reusable: bool) -> None:
        """Return a healthy NNTP socket to the pool or close and free its slot."""
        if conn is None:
            return
        with self._conn_cond:
            self._leased_conns = max(0, self._leased_conns - 1)
            if reusable and (self._leased_conns + len(self._idle_conns)) < self._connections:
                self._idle_conns.append(conn)
                conn_to_close = None
            else:
                conn_to_close = conn
            self._conn_cond.notify_all()
        if conn_to_close is not None:
            conn_to_close.close()

    def _drain_idle_conns(self) -> None:
        """Close idle pooled NNTP connections once no jobs remain active."""
        to_close: list[NntpConnection] = []
        with self._conn_cond:
            if self._active_jobs == 0 and self._leased_conns == 0 and self._idle_conns:
                to_close = list(self._idle_conns)
                self._idle_conns.clear()
        for conn in to_close:
            conn.close()

    def _allowed_origin_keys(self) -> set[tuple[str, str, int]]:
        origins: list[str] = list(self._static_origins)
        if callable(self.allowed_origins_provider):
            try:
                origins.extend(str(u).strip() for u in self.allowed_origins_provider() if str(u).strip())
            except Exception:  # noqa: BLE001
                pass
        keys: set[tuple[str, str, int]] = set()
        for origin in origins:
            k = _origin_key(origin)
            if k is not None:
                keys.add(k)
        return keys

    def _is_allowed_nzb_url(self, url: str) -> bool:
        target = _origin_key(url)
        if target is None:
            return False
        return target in self._allowed_origin_keys()

    @staticmethod
    def _read_bounded_nzb(resp) -> bytes:
        headers = getattr(resp, "headers", None) or {}
        content_length = headers.get("Content-Length")
        if content_length is not None:
            try:
                declared = int(content_length)
            except (TypeError, ValueError) as err:
                raise NntpError("NZB server returned invalid Content-Length") from err
            if declared < 0:
                raise NntpError("NZB server returned invalid Content-Length")
            if declared > MAX_NZB_BYTES:
                raise NntpError("NZB download exceeds the 32 MiB safety limit")
        iter_fn = getattr(resp, "iter_content", None)
        if callable(iter_fn):
            chunks: list[bytes] = []
            total = 0
            for chunk in iter_fn(chunk_size=64 * 1024):
                if not chunk:
                    continue
                total += len(chunk)
                if total > MAX_NZB_BYTES:
                    raise NntpError("NZB download exceeds the 32 MiB safety limit")
                chunks.append(chunk)
            if chunks:
                return b"".join(chunks)
        raw = bytes(getattr(resp, "content", b"") or b"")
        if len(raw) > MAX_NZB_BYTES:
            raise NntpError("NZB download exceeds the 32 MiB safety limit")
        return raw

    def reachable(self) -> bool:
        """Verify TCP/TLS reachability and authentication against the configured NNTP server."""
        if not self.configured:
            self.detail = "NNTP Host and Save Path are required"
            return False
        conn = self._new_conn()
        try:
            conn.connect()
            self.detail = f"Connected to {self._config.base_url}"
            return True
        except Exception as err:  # noqa: BLE001
            self.detail = str(err)
            return False
        finally:
            conn.close()

    def _fetch_nzb_bytes(self, source: str) -> bytes:
        text = str(source or "").strip()
        if text.lstrip().startswith("<"):
            return text.encode("utf-8")
        scheme = urlsplit(text).scheme.lower()
        if scheme not in ("http", "https"):
            raise NntpError(f"unsupported NZB source scheme: {scheme or 'none'}")
        current = text
        for _ in range(6):
            if not self._is_allowed_nzb_url(current):
                raise NntpError("NZB download URL is outside configured indexer origins")
            resp = None
            try:
                resp = self._session.get(
                    current,
                    timeout=self._config.timeout,
                    stream=True,
                    allow_redirects=False,
                )
            except requests.RequestException as err:
                raise NntpError(f"could not fetch NZB: {err.__class__.__name__}") from err

            try:
                status = int(getattr(resp, "status_code", 200) or 200)
                if status in (301, 302, 303, 307, 308):
                    location = str((getattr(resp, "headers", None) or {}).get("Location") or "").strip()
                    if not location:
                        raise NntpError("NZB redirect missing Location")
                    current = urljoin(current, location)
                    continue
                if hasattr(resp, "raise_for_status"):
                    resp.raise_for_status()
                return self._read_bounded_nzb(resp)
            except requests.RequestException as err:
                raise NntpError(f"could not fetch NZB: {err.__class__.__name__}") from err
            finally:
                _close_response(resp)
        raise NntpError("too many redirects while fetching NZB")

    def add(self, url: str, *, save_path: str | None = None, name: str = "") -> bool:
        """Fetch and validate an NZB (inline XML or allowed indexer URL) and enqueue download."""
        del save_path
        if not self.configured:
            self.last_error = "Standalone Usenet client is not configured"
            return False
        try:
            xml_bytes = self._fetch_nzb_bytes(url)
            nzb_files = parse_nzb(xml_bytes)
        except NntpError as err:
            self.last_error = str(err)
            return False

        token = secrets.token_hex(8)
        job_id = f"nzb-{token}"
        safe_title = _safe_name(name or token, fallback=f"nzb-{token}")
        job_dir = Path(self._save_path) / f"{safe_title}-{token[:6]}"
        job_dir.mkdir(parents=True, exist_ok=True)
        total_bytes = sum(seg.size for nf in nzb_files for seg in nf.segments)

        self._ledger.upsert(job_id, {
            "name": name or safe_title,
            "status": "queued",
            "job_dir": str(job_dir),
            "content_path": "",
            "save_path": str(job_dir),
            "size": total_bytes,
            "downloaded": 0,
            "error": "",
            "release_fault": False,
            "created_at": time.time(),
        })
        self.last_job_id = job_id
        self.last_error = ""
        ctrl = register_job_control(self._ledger.path, job_id)
        fut = self._pool.submit(self._run_job, job_id, job_dir, nzb_files, ctrl)
        set_job_future(ctrl, fut)
        return True

    @staticmethod
    def _verify_assembled_ranges(
        ranges: list[tuple[int, int]],
        expected_file_size: int | None,
        expected_parts: int | None,
        seen_parts: set[int],
    ) -> None:
        if not ranges:
            raise ArticleMissingError("NZB file produced no decoded segments")
        cursor = 1
        for begin, end in sorted(ranges):
            if begin > cursor:
                raise ArticleMissingError(
                    f"yEnc segment gap before byte {begin} (expected {cursor})"
                )
            if begin < cursor:
                raise ArticleMissingError(
                    f"yEnc segment overlap at byte {begin} (cursor {cursor})"
                )
            cursor = end + 1
        assembled = cursor - 1
        if expected_file_size and assembled != expected_file_size:
            raise ArticleMissingError(
                f"yEnc assembled size {assembled} does not match declared file size {expected_file_size}"
            )
        if expected_parts and seen_parts and len(seen_parts) != expected_parts:
            raise ArticleMissingError(
                f"yEnc part count mismatch: got {len(seen_parts)}, expected {expected_parts}"
            )

    def _run_job(
        self,
        job_id: str,
        job_dir: Path,
        nzb_files: list[NzbFile],
        ctrl,
    ) -> None:
        cancel_ev = ctrl.cancel
        with self._conn_cond:
            self._active_jobs += 1
        if cancel_ev.is_set() or self._ledger.get(job_id) is None:
            with self._conn_cond:
                self._active_jobs = max(0, self._active_jobs - 1)
            self._drain_idle_conns()
            clear_job_control(self._ledger.path, job_id, ctrl)
            return

        self._ledger.update(job_id, {"status": "downloading"})
        bytes_lock = threading.Lock()
        downloaded = 0

        def _fetch_segment(seg: NzbSegment) -> YencPart:
            if cancel_ev.is_set():
                raise NntpError("cancelled by user")
            last_exc: Exception | None = None
            for attempt in range(2):
                conn: NntpConnection | None = None
                reusable = False
                try:
                    conn = self._borrow_conn(cancel_ev)
                    lines = conn.fetch_body(seg.message_id)
                    part = decode_yenc(lines)
                    reusable = True
                    return part
                except (ArticleMissingError, NntpAuthError) as err:
                    # Connection stayed protocol-clean on 430; drop auth-failed connections.
                    reusable = isinstance(err, ArticleMissingError)
                    raise
                except (OSError, NntpError) as err:
                    last_exc = err
                    reusable = False
                    if attempt == 0 and not cancel_ev.is_set():
                        continue
                    raise
                finally:
                    self._release_conn(conn, reusable=reusable)
            raise NntpError(str(last_exc or "NNTP segment failed"))

        part_files: list[Path] = []
        try:
            max_socks = min(
                self._connections,
                max(1, max(len(nf.segments) for nf in nzb_files)),
            )
            window = max(2, max_socks * 2)
            with concurrent.futures.ThreadPoolExecutor(
                max_workers=max_socks, thread_name_prefix="romarr-nntp",
            ) as seg_pool:
                for nf in nzb_files:
                    if cancel_ev.is_set():
                        raise NntpError("cancelled by user")
                    final_path = job_dir / nf.filename
                    part_path = job_dir / f".{nf.filename}.partial"
                    part_files.append(part_path)
                    is_par2 = nf.filename.lower().endswith(".par2")
                    ranges: list[tuple[int, int]] = []
                    seen_parts: set[int] = set()
                    expected_file_size: int | None = None
                    expected_parts: int | None = None
                    try:
                        with open(part_path, "wb") as out:
                            seg_iter = iter(nf.segments)
                            pending: set[concurrent.futures.Future] = set()
                            for _ in range(window):
                                seg = next(seg_iter, None)
                                if seg is None:
                                    break
                                pending.add(seg_pool.submit(_fetch_segment, seg))
                            while pending:
                                if cancel_ev.is_set():
                                    for rem in pending:
                                        rem.cancel()
                                    raise NntpError("cancelled by user")
                                done, pending = concurrent.futures.wait(
                                    pending, timeout=0.2,
                                    return_when=concurrent.futures.FIRST_COMPLETED,
                                )
                                for fut in done:
                                    part = fut.result()
                                    out.seek(max(0, part.begin - 1))
                                    out.write(part.data)
                                    ranges.append((part.begin, part.end))
                                    if part.part > 0:
                                        seen_parts.add(part.part)
                                    if part.total > 0 and expected_parts is None:
                                        expected_parts = part.total
                                    if part.file_size > 0:
                                        if expected_file_size is None:
                                            expected_file_size = part.file_size
                                        elif part.file_size != expected_file_size:
                                            raise ArticleMissingError(
                                                "inconsistent yEnc file sizes across parts"
                                            )
                                    with bytes_lock:
                                        downloaded += len(part.data)
                                    del part, fut
                                while len(pending) < window and not cancel_ev.is_set():
                                    seg = next(seg_iter, None)
                                    if seg is None:
                                        break
                                    pending.add(seg_pool.submit(_fetch_segment, seg))
                        self._verify_assembled_ranges(
                            ranges, expected_file_size, expected_parts, seen_parts,
                        )
                        os.replace(part_path, final_path)
                        part_files.remove(part_path)
                    except ArticleMissingError:
                        part_path.unlink(missing_ok=True)
                        if is_par2:
                            continue
                        raise
                    self._ledger.update(job_id, {"downloaded": downloaded})

            if cancel_ev.is_set():
                raise NntpError("cancelled by user")
            self._ledger.update(job_id, {
                "status": "complete",
                "content_path": str(job_dir),
                "save_path": str(job_dir),
                "downloaded": downloaded,
            })
        except ArticleMissingError as err:
            if not cancel_ev.is_set():
                self._ledger.update(job_id, {
                    "status": "error",
                    "error": str(err),
                    "release_fault": True,
                })
        except Exception as err:  # noqa: BLE001
            if not cancel_ev.is_set():
                self._ledger.update(job_id, {
                    "status": "error",
                    "error": str(err),
                    "release_fault": False,
                })
        finally:
            for pf in part_files:
                pf.unlink(missing_ok=True)
            with self._conn_cond:
                self._active_jobs = max(0, self._active_jobs - 1)
            self._drain_idle_conns()
            clear_job_control(self._ledger.path, job_id, ctrl)

    def completed(self, timeout: float | None = None) -> list[dict]:
        """Return completed NZB job directories ready for ROMarr import."""
        del timeout
        if not self.configured:
            return []
        return [
            {
                "id": row["id"],
                "name": row.get("name", ""),
                "content_path": row.get("content_path", ""),
                "save_path": row.get("save_path", ""),
                "state": "complete",
                "size": int(row.get("size") or 0),
            }
            for row in self._ledger.rows()
            if row.get("status") == "complete" and row.get("content_path")
        ]

    def active_downloads(self) -> list[dict]:
        """Return queued or downloading NZB jobs from the persistent ledger."""
        return [
            {
                "id": row["id"],
                "name": row.get("name", ""),
                "state": row.get("status", "downloading"),
                "worker_active": row.get("status") in ("queued", "downloading"),
                "size": int(row.get("size") or 0),
                "downloaded": int(row.get("downloaded") or 0),
            }
            for row in self._ledger.rows()
            if row.get("status") in ("queued", "downloading")
        ]

    def failed_jobs(self) -> list[dict]:
        """Return failed NZB jobs, preserving `release_fault` for 430/CRC failures."""
        return [
            {
                "id": row["id"],
                "name": row.get("name", ""),
                "detail": str(row.get("error") or "NNTP download failed"),
                "release_fault": bool(row.get("release_fault", False)),
            }
            for row in self._ledger.rows()
            if row.get("status") == "error"
        ]

    def remove_download(self, download_id: str, *, delete_files: bool = False) -> bool:
        """Cancel any active worker, wait for stop, remove ledger row, and optionally delete files."""
        raw_id = str(download_id or "").strip()
        if not raw_id:
            return False
        stopped = stop_job(self._ledger.path, raw_id, timeout=10.0)
        if not stopped:
            return False
        row = self._ledger.remove(raw_id)
        if row is None:
            return False
        if delete_files and stopped:
            job_dir = str(row.get("job_dir") or "")
            base = Path(self._save_path).resolve()
            if job_dir:
                p = Path(job_dir).resolve()
                if p.is_relative_to(base) and p != base:
                    shutil.rmtree(p, ignore_errors=True)
        return True
