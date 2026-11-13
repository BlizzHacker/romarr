"""Standalone download manager integration powered by Mister Download Manager.

Wraps the external `misterdownloadmanager` / `misterdownloadmanager-cli` binaries
and Remote HTTPS API (`anefzaoui/misterdownloadmanager`, GPL-3.0-or-later) via OS
process boundaries and authenticated HTTP requests. Keeping the companion binary
outside the Python process lets ROMarr stay standalone-capable for BitTorrent
(including single-file selective collection downloads) and multi-connection
direct HTTP/HTTPS without linking GPL code into MIT-licensed ROMarr.
"""

from __future__ import annotations

import concurrent.futures
import glob
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import platform
import re
import secrets
import shutil
import stat
import signal
import socket
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from urllib.parse import parse_qs, quote, urljoin, urlsplit

import requests

from .retained_collection import magnet_hash

log = logging.getLogger(__name__)

MDM_REPO = "anefzaoui/misterdownloadmanager"
MDM_VERSION = "v0.5.2"
MDM_RELEASE_BASE = f"https://github.com/{MDM_REPO}/releases/download"
MDM_CLI_NAMES = ("misterdownloadmanager-cli", "misterdownloadmanager-cli.exe")
MDM_APP_NAMES = ("misterdownloadmanager", "misterdownloadmanager.exe")
MDM_CATEGORIES = frozenset({"programs", "compressed", "documents", "music", "videos", "other"})
TAG_PREFIX = "mdm-tag:"
MAX_TORRENT_BYTES = 32 * 1024 * 1024

_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_SAFE_SEGMENT_RE = re.compile(r"[^\w.\- ()\[\]]+", re.ASCII)
_SUBPROCESS_FLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
_PROGRESS_RE = re.compile(r"\s+")


class MdmError(RuntimeError):
    """Raised on binary verification, extraction, or MDM transport failures."""


def _safe_name(name: str, fallback: str = "download") -> str:
    """Return a single sanitized directory/file component with no traversal."""
    raw = str(name or "").replace("\\", "/").split("/")[-1].strip()
    cleaned = _SAFE_SEGMENT_RE.sub("_", raw).strip(" .")
    if not cleaned or cleaned in (".", ".."):
        return fallback
    return cleaned[:160]


def _normalize_rel_path(path: str) -> str:
    """Normalize a relative torrent member path, rejecting traversal."""
    wanted = str(path or "").replace("\\", "/").strip("/")
    parts = PurePosixPath(wanted)
    if (not wanted or parts.is_absolute() or ".." in parts.parts
            or ":" in wanted or "\x00" in wanted):
        raise ValueError("unsafe torrent member path")
    return "/".join(parts.parts)


def detect_libc() -> str:
    """Return 'musl', 'glibc', or '' on non-Linux hosts."""
    if not sys.platform.startswith("linux"):
        return ""
    libc_name, _ = platform.libc_ver()
    lowered = str(libc_name or "").lower()
    if lowered == "glibc":
        return "glibc"
    if "musl" in lowered or glob.glob("/lib/ld-musl-*.so*"):
        return "musl"
    return "glibc"


def asset_spec(
    version: str = MDM_VERSION,
    *,
    sys_platform: str | None = None,
    machine: str | None = None,
    libc: str | None = None,
) -> dict:
    """Describe the official MDM release asset and installability for this host."""
    tag = str(version or MDM_VERSION).strip()
    if not tag.startswith("v"):
        tag = f"v{tag}"
    ver = tag.lstrip("v")
    plat = (sys_platform or sys.platform).lower()
    raw_arch = (machine or platform.machine() or "").lower()
    arch = "x86_64" if raw_arch in ("x86_64", "amd64") else (
        "arm64" if raw_arch in ("arm64", "aarch64") else (raw_arch or "unknown")
    )

    if plat.startswith("linux"):
        c_lib = libc if libc is not None else detect_libc()
        label = f"linux-{arch} ({c_lib or 'unknown'})"
        if c_lib == "musl":
            return {
                "tag": tag, "version": ver, "platform": label,
                "installable": False, "kind": "", "asset": "",
                "reason": (
                    "Alpine/musl containers cannot run prebuilt glibc MDM binaries "
                    "directly; configure Remote Mode against `misterdownloadmanager "
                    "--headless` or mount a compatible CLI via MDM_CLI_PATH."
                ),
            }
        if arch == "x86_64":
            return {
                "tag": tag, "version": ver, "platform": label,
                "installable": True, "kind": "tar.gz",
                "asset": f"MisterDownloadManager-{ver}-linux-x86_64.tar.gz",
                "reason": "",
            }
        return {
            "tag": tag, "version": ver, "platform": label,
            "installable": False, "kind": "", "asset": "",
            "reason": (
                f"No prebuilt Linux {arch} release archive in MDM {tag}; "
                "set MDM_CLI_PATH to a local binary or use Remote Mode."
            ),
        }

    if plat == "win32":
        label = f"windows-{arch}"
        if arch == "x86_64":
            return {
                "tag": tag, "version": ver, "platform": label,
                "installable": True, "kind": "zip",
                "asset": f"MisterDownloadManager-{ver}-portable.zip",
                "reason": "",
            }
        return {
            "tag": tag, "version": ver, "platform": label,
            "installable": False, "kind": "", "asset": "",
            "reason": f"No prebuilt Windows {arch} MDM archive; install manually.",
        }

    if plat == "darwin":
        label = f"macos-{arch}"
        return {
            "tag": tag, "version": ver, "platform": label,
            "installable": False, "kind": "dmg",
            "asset": f"MisterDownloadManager-{ver}-macos-{arch}-experimental.dmg",
            "reason": (
                "macOS ships as a .dmg bundle; install Mister Download Manager "
                "manually or set MDM_CLI_PATH to the CLI inside the app bundle."
            ),
        }

    return {
        "tag": tag, "version": ver, "platform": f"{plat}-{arch}",
        "installable": False, "kind": "", "asset": "",
        "reason": f"Unsupported host platform {plat}/{arch} for automatic MDM install.",
    }


def default_install_dir(data_dir: str | Path | None = None) -> Path:
    """Return the managed directory where ROMarr stores downloaded MDM binaries."""
    env_dir = os.environ.get("MDM_INSTALL_DIR", "").strip()
    if env_dir:
        return Path(env_dir)
    if data_dir:
        return Path(data_dir) / "tools" / "mdm"
    state = os.environ.get("ROMARR_DATA", "").strip()
    base = Path(state).parent if state else Path(".")
    return base / "tools" / "mdm"


def _is_executable_file(path: Path) -> bool:
    if not path.is_file():
        return False
    if sys.platform == "win32":
        return True
    return os.access(path, os.X_OK)


def find_binary(
    configured_path: str = "",
    *,
    binary_names: tuple[str, ...] = MDM_CLI_NAMES,
    env_var: str = "MDM_CLI_PATH",
    data_dir: str | Path | None = None,
) -> str:
    """Locate the MDM executable in configured path, env, managed dir, or PATH."""
    candidates: list[Path] = []
    for raw in (configured_path, os.environ.get(env_var, "")):
        text = str(raw or "").strip()
        if not text:
            continue
        p = Path(text)
        if p.is_dir():
            candidates.extend(p / name for name in binary_names)
        else:
            candidates.append(p)

    managed = default_install_dir(data_dir)
    for name in binary_names:
        candidates.append(managed / name)
    if managed.is_dir():
        for child in sorted(managed.iterdir()):
            if child.is_dir():
                for name in binary_names:
                    candidates.append(child / name)

    for cand in candidates:
        if _is_executable_file(cand):
            return str(cand)

    for name in binary_names:
        found = shutil.which(name)
        if found:
            return found
    return ""


def probe_cli(cli_path: str, timeout: float = 5.0) -> tuple[bool, str]:
    """Run `--version` against an MDM CLI binary and report usability."""
    if not cli_path:
        return False, "misterdownloadmanager-cli not found"
    try:
        proc = subprocess.run(
            [cli_path, "--version"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            creationflags=_SUBPROCESS_FLAGS,
        )
    except (OSError, subprocess.SubprocessError) as err:
        return False, f"could not execute MDM CLI: {err}"
    output = (proc.stdout or proc.stderr or "").strip().splitlines()
    first = _PROGRESS_RE.sub(" ", output[0]).strip() if output else ""
    if proc.returncode != 0:
        return False, first or f"MDM CLI exited with code {proc.returncode}"
    return True, first or "misterdownloadmanager-cli"


def availability(
    configured_path: str = "",
    *,
    data_dir: str | Path | None = None,
    version: str = MDM_VERSION,
) -> dict:
    """Summarize MDM binary availability and verified-install capability."""
    spec = asset_spec(version=version)
    cli = find_binary(configured_path, data_dir=data_dir)
    ok, detail = probe_cli(cli) if cli else (False, "")
    reason = detail if cli else (
        spec["reason"]
        or f"Not installed yet; ready to install verified {spec['asset']} ({spec['platform']})."
    )
    return {
        "available": bool(ok),
        "cli_path": cli,
        "version": detail if ok else "",
        "installable": bool(spec["installable"]),
        "platform": spec["platform"],
        "asset": spec["asset"],
        "release_tag": spec["tag"],
        "install_dir": str(default_install_dir(data_dir)),
        "reason": reason,
        "credit": f"https://github.com/{MDM_REPO}",
    }


def parse_sha256sums(text: str) -> dict[str, str]:
    """Parse standard `SHA256SUMS.txt` lines into `{filename: sha256_hex}`."""
    out: dict[str, str] = {}
    for line in str(text or "").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        parts = stripped.split(None, 1)
        if len(parts) != 2 or not _SHA256_RE.fullmatch(parts[0]):
            continue
        fname = parts[1].lstrip("* ").replace("\\", "/").split("/")[-1]
        if fname:
            out[fname] = parts[0].lower()
    return out


def _safe_extract_tar(archive_path: Path, stage_dir: Path, wanted: set[str]) -> list[Path]:
    extracted: list[Path] = []
    stage_resolved = stage_dir.resolve()
    with tarfile.open(archive_path, "r:gz") as tf:
        for member in tf.getmembers():
            if not member.isreg() or member.issym() or member.islnk():
                continue
            parts = PurePosixPath(member.name.replace("\\", "/").lstrip("/"))
            if ".." in parts.parts or not parts.parts:
                continue
            leaf = parts.parts[-1]
            if leaf not in wanted and not leaf.upper().startswith(("LICENSE", "ATTRIBUTION")):
                continue
            dest = (stage_dir / leaf).resolve()
            if not dest.is_relative_to(stage_resolved):
                continue
            src = tf.extractfile(member)
            if src is None:
                continue
            with src, open(dest, "wb") as out:
                shutil.copyfileobj(src, out)
            extracted.append(dest)
    return extracted


def _safe_extract_zip(archive_path: Path, stage_dir: Path, wanted: set[str]) -> list[Path]:
    extracted: list[Path] = []
    stage_resolved = stage_dir.resolve()
    with zipfile.ZipFile(archive_path, "r") as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            # Reject symlinks serialized cleanly in Unix zip external attributes.
            if stat.S_ISLNK((info.external_attr >> 16) & 0o170000):
                continue
            parts = PurePosixPath(info.filename.replace("\\", "/").lstrip("/"))
            if ".." in parts.parts or not parts.parts:
                continue
            leaf = parts.parts[-1]
            if leaf not in wanted and not leaf.upper().startswith(("LICENSE", "ATTRIBUTION")):
                continue
            dest = (stage_dir / leaf).resolve()
            if not dest.is_relative_to(stage_resolved):
                continue
            with zf.open(info, "r") as src, open(dest, "wb") as out:
                shutil.copyfileobj(src, out)
            extracted.append(dest)
    return extracted


def install_mdm(
    target_dir: str | Path | None = None,
    *,
    version: str = MDM_VERSION,
    session: requests.Session | None = None,
    timeout: int = 120,
    runner=None,
) -> dict:
    """Download, SHA-256 verify, and install the official MDM binaries locally."""
    spec = asset_spec(version=version)
    if not spec["installable"]:
        raise MdmError(spec["reason"])

    dest_dir = Path(target_dir) if target_dir else default_install_dir()
    dest_dir.mkdir(parents=True, exist_ok=True)
    owns_session = session is None
    http = session or requests.Session()
    tag = spec["tag"]
    asset = spec["asset"]

    try:
        sums_url = f"{MDM_RELEASE_BASE}/{quote(tag)}/SHA256SUMS.txt"
        try:
            sums_resp = http.get(sums_url, timeout=min(30, timeout), verify=True)
            sums_resp.raise_for_status()
        except requests.RequestException as err:
            raise MdmError(f"failed to fetch MDM SHA256SUMS.txt: {err}") from err

        checksums = parse_sha256sums(sums_resp.text)
        expected = checksums.get(asset)
        if not expected:
            raise MdmError(f"{asset} missing from MDM {tag} SHA256SUMS.txt")

        asset_url = f"{MDM_RELEASE_BASE}/{quote(tag)}/{quote(asset)}"
        fd, tmp_raw = tempfile.mkstemp(prefix=".mdm-dl-", dir=str(dest_dir))
        archive_path = Path(tmp_raw)
        stage_dir = Path(tempfile.mkdtemp(prefix=".mdm-stage-", dir=str(dest_dir)))
        try:
            hasher = hashlib.sha256()
            try:
                with os.fdopen(fd, "wb") as handle, http.get(
                    asset_url, stream=True, timeout=timeout, verify=True,
                ) as resp:
                    resp.raise_for_status()
                    for chunk in resp.iter_content(chunk_size=65536):
                        if chunk:
                            handle.write(chunk)
                            hasher.update(chunk)
            except requests.RequestException as err:
                raise MdmError(f"failed to download MDM archive {asset}: {err}") from err

            actual = hasher.hexdigest().lower()
            if not hmac.compare_digest(actual, expected):
                raise MdmError(
                    f"SHA-256 verification failed for {asset}: expected {expected}, got {actual}"
                )

            wanted = set(MDM_CLI_NAMES) | set(MDM_APP_NAMES)
            files = (_safe_extract_tar(archive_path, stage_dir, wanted)
                     if spec["kind"] == "tar.gz"
                     else _safe_extract_zip(archive_path, stage_dir, wanted))
            if not any(p.name in MDM_CLI_NAMES for p in files):
                raise MdmError(f"{asset} did not contain misterdownloadmanager-cli")

            cli_dest = ""
            app_dest = ""
            for item in files:
                target = dest_dir / item.name
                tmp_target = dest_dir / f".{item.name}.{secrets.token_hex(4)}"
                shutil.copyfile(item, tmp_target)
                if item.name in wanted and sys.platform != "win32":
                    os.chmod(tmp_target, 0o755)
                os.replace(tmp_target, target)
                if item.name in MDM_CLI_NAMES:
                    cli_dest = str(target)
                elif item.name in MDM_APP_NAMES:
                    app_dest = str(target)

            probe = runner or probe_cli
            ok, version_str = probe(cli_dest)
            if not ok:
                raise MdmError(f"installed MDM binary could not start: {version_str}")

            return {
                "ok": True,
                "version": version_str,
                "release_tag": tag,
                "asset": asset,
                "sha256": actual,
                "install_dir": str(dest_dir),
                "cli_path": cli_dest,
                "app_path": app_dest,
            }
        finally:
            archive_path.unlink(missing_ok=True)
            shutil.rmtree(stage_dir, ignore_errors=True)
    finally:
        if owns_session:
            http.close()


# ------------------------------------------------------------------ ledgers --


_LEDGER_LOCKS: dict[str, threading.RLock] = {}
_LEDGER_LOCKS_GUARD = threading.Lock()
_JOB_CONTROLS: dict[tuple[str, str], "_JobControl"] = {}
_JOB_CONTROLS_GUARD = threading.Lock()


@dataclass
class _JobControl:
    """Coordination handle shared across reloads for cancelling and stopping a job."""

    cancel: threading.Event
    done: threading.Event
    future: concurrent.futures.Future | None = None
    abort: Callable[[], None] | None = None


def _ledger_key(path: str | Path) -> str:
    return str(Path(path).resolve())


def register_job_control(
    ledger_path: str | Path,
    job_id: str,
    *,
    abort: Callable[[], None] | None = None,
) -> _JobControl:
    """Register cancel and completion events for a background job keyed by ledger path."""
    ctrl = _JobControl(cancel=threading.Event(), done=threading.Event(), abort=abort)
    with _JOB_CONTROLS_GUARD:
        _JOB_CONTROLS[(_ledger_key(ledger_path), job_id)] = ctrl
    return ctrl


def set_job_future(ctrl: _JobControl, future: concurrent.futures.Future) -> None:
    """Attach the executor future to a registered job control handle."""
    ctrl.future = future


def clear_job_control(ledger_path: str | Path, job_id: str, ctrl: _JobControl | None = None) -> None:
    """Mark a background job finished and remove its registry entry if still active."""
    key = (_ledger_key(ledger_path), job_id)
    with _JOB_CONTROLS_GUARD:
        current = _JOB_CONTROLS.get(key)
        if current is None or (ctrl is not None and current is not ctrl):
            return
        current.done.set()
        _JOB_CONTROLS.pop(key, None)


def stop_job(ledger_path: str | Path, job_id: str, *, timeout: float = 10.0) -> bool:
    """Signal a job to cancel and wait until its worker has stopped or queued work cancels.

    Returns True when no worker remains active, or False if the wait timed out.
    """
    key = (_ledger_key(ledger_path), job_id)
    with _JOB_CONTROLS_GUARD:
        ctrl = _JOB_CONTROLS.get(key)
    if ctrl is None:
        return True
    ctrl.cancel.set()
    if ctrl.abort is not None:
        try:
            ctrl.abort()
        except Exception:  # noqa: BLE001
            pass
    if ctrl.future is not None and ctrl.future.cancel():
        ctrl.done.set()
        clear_job_control(ledger_path, job_id, ctrl)
        return True
    return ctrl.done.wait(timeout=timeout)


class _MdmLedger:
    """Thread-safe JSON ledger tracking ROMarr-owned MDM jobs on disk."""

    def __init__(self, path: Path):
        self.path = path
        key = _ledger_key(path)
        with _LEDGER_LOCKS_GUARD:
            self._lock = _LEDGER_LOCKS.setdefault(key, threading.RLock())

    def _read_unlocked(self) -> dict[str, dict]:
        try:
            raw = self.path.read_text(encoding="utf-8")
            data = json.loads(raw)
        except (OSError, ValueError):
            return {}
        return {str(k): v for k, v in data.items() if isinstance(v, dict)} if isinstance(data, dict) else {}

    def _write_unlocked(self, data: dict[str, dict]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".ledger-", dir=str(self.path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle, indent=2)
            os.replace(tmp, self.path)
        finally:
            if os.path.exists(tmp):
                try:
                    os.unlink(tmp)
                except OSError:
                    pass

    def upsert(self, job_id: str, fields: dict) -> dict:
        """Insert or update a job row in the persistent ledger."""
        with self._lock:
            data = self._read_unlocked()
            row = dict(data.get(job_id) or {})
            row.update(fields)
            row["id"] = job_id
            data[job_id] = row
            self._write_unlocked(data)
            return dict(row)

    def update(self, job_id: str, fields: dict) -> dict | None:
        """Update an existing job row; return None without recreating it if removed."""
        with self._lock:
            data = self._read_unlocked()
            if job_id not in data:
                return None
            data[job_id].update(fields)
            data[job_id]["id"] = job_id
            self._write_unlocked(data)
            return dict(data[job_id])

    def get(self, job_id: str) -> dict | None:
        with self._lock:
            row = self._read_unlocked().get(job_id)
            return dict(row) if row is not None else None

    def remove(self, job_id: str) -> dict | None:
        with self._lock:
            data = self._read_unlocked()
            removed = data.pop(job_id, None)
            if removed is not None:
                self._write_unlocked(data)
            return dict(removed) if removed is not None else None

    def rows(self) -> list[dict]:
        with self._lock:
            return [dict(v) for v in self._read_unlocked().values()]


# ----------------------------------------------------------- remote HTTPS --


def _is_loopback_host(host: str | None) -> bool:
    clean = str(host or "").strip().lower().strip("[]")
    if clean in ("127.0.0.1", "localhost", "::1"):
        return True
    try:
        return ipaddress.ip_address(clean).is_loopback
    except ValueError:
        return False


def _is_internal_host(host: str | None) -> bool:
    """Refuse unresolved hosts and any DNS answer outside public addresses."""
    try:
        answers = socket.getaddrinfo(str(host or ""), None, type=socket.SOCK_STREAM)
        return not answers or any(not ipaddress.ip_address(answer[4][0]).is_global
                                  for answer in answers)
    except (OSError, ValueError):
        return True


def _close_response(resp) -> None:
    close = getattr(resp, "close", None)
    if callable(close):
        try:
            close()
        except Exception:  # noqa: BLE001
            pass


def _read_bounded_response(resp, limit: int, label: str) -> bytes:
    """Read an HTTP response payload up to `limit` bytes cleanly."""
    headers = getattr(resp, "headers", None) or {}
    content_length = headers.get("Content-Length")
    if content_length is not None:
        try:
            declared = int(content_length)
        except (TypeError, ValueError) as err:
            raise MdmError(f"{label} returned an invalid Content-Length") from err
        if declared < 0:
            raise MdmError(f"{label} returned an invalid Content-Length")
        if declared > limit:
            raise MdmError(f"{label} exceeds the {limit // (1024 * 1024)} MiB safety limit")

    iter_fn = getattr(resp, "iter_content", None)
    if callable(iter_fn):
        chunks: list[bytes] = []
        total = 0
        for chunk in iter_fn(chunk_size=64 * 1024):
            if not chunk:
                continue
            total += len(chunk)
            if total > limit:
                raise MdmError(f"{label} exceeds the {limit // (1024 * 1024)} MiB safety limit")
            chunks.append(chunk)
        if chunks:
            return b"".join(chunks)

    raw = bytes(getattr(resp, "content", b"") or b"")
    if len(raw) > limit:
        raise MdmError(f"{label} exceeds the {limit // (1024 * 1024)} MiB safety limit")
    return raw


def _bdecode_next(buf: bytes, idx: int, depth: int = 0) -> tuple[object, int, tuple[int, int] | None]:
    """Decode one bencoded value and return `(value, next_idx, info_span)` safely."""
    if depth > 32 or idx >= len(buf):
        raise ValueError("invalid bencode")
    ch = buf[idx:idx + 1]
    if ch == b"i":
        end = buf.index(b"e", idx + 1)
        return int(buf[idx + 1:end]), end + 1, None
    if ch == b"l":
        items = []
        pos = idx + 1
        info_span = None
        while buf[pos:pos + 1] != b"e":
            val, pos, span = _bdecode_next(buf, pos, depth + 1)
            items.append(val)
            if info_span is None and span is not None:
                info_span = span
        return items, pos + 1, info_span
    if ch == b"d":
        mapping: dict[bytes, object] = {}
        pos = idx + 1
        info_span = None
        while buf[pos:pos + 1] != b"e":
            key, pos, _ = _bdecode_next(buf, pos, depth + 1)
            if not isinstance(key, bytes):
                raise ValueError("invalid bencode dict key")
            val_start = pos
            val, pos, child_span = _bdecode_next(buf, pos, depth + 1)
            mapping[key] = val
            if depth == 0 and key == b"info" and isinstance(val, dict):
                info_span = (val_start, pos)
            elif info_span is None and child_span is not None:
                info_span = child_span
        return mapping, pos + 1, info_span
    if b"0" <= ch <= b"9":
        colon = buf.index(b":", idx)
        size = int(buf[idx:colon])
        if size < 0 or colon + 1 + size > len(buf):
            raise ValueError("invalid bencode string")
        start = colon + 1
        return buf[start:start + size], start + size, None
    raise ValueError("unexpected bencode token")


def _extract_torrent_metadata(torrent_bytes: bytes) -> tuple[str, str]:
    """Return `(info_hash_hex, name)` extracted from a `.torrent` payload, if valid."""
    raw = bytes(torrent_bytes or b"")
    try:
        top, _, info_span = _bdecode_next(raw, 0, 0)
    except (ValueError, IndexError):
        return "", ""
    if not isinstance(top, dict):
        return "", ""
    info_hash = ""
    if info_span is not None:
        info_hash = hashlib.sha1(raw[info_span[0]:info_span[1]], usedforsecurity=False).hexdigest().lower()
    info = top.get(b"info")
    name = ""
    if isinstance(info, dict):
        raw_name = info.get(b"name.utf-8") or info.get(b"name")
        if isinstance(raw_name, bytes):
            name = raw_name.decode("utf-8", errors="replace").strip()
    return info_hash, name


class MdmRemoteTransport:
    """Authenticated HTTPS transport for `misterdownloadmanager --headless` or Desktop."""

    def __init__(
        self,
        base_url: str,
        api_token: str = "",
        *,
        verify_tls: bool = False,
        timeout: int = 30,
        session: requests.Session | None = None,
    ):
        raw_url = str(base_url or "").strip().rstrip("/")
        self.rejected_reason = ""
        if raw_url:
            parts = urlsplit(raw_url)
            scheme = parts.scheme.lower()
            host = parts.hostname or ""
            if not host or (scheme != "https" and not (scheme == "http" and _is_loopback_host(host))):
                # Refuse plain http:// off-host so the Bearer token never crosses a network in cleartext.
                self.base_url = ""
                self.rejected_reason = (
                    "Remote MDM URL must use https:// (plain http:// is allowed only for localhost/127.0.0.1)"
                )
            else:
                self.base_url = raw_url
        else:
            self.base_url = ""
        self.api_token = str(api_token or "").strip()
        self.verify_tls = bool(verify_tls)
        self.timeout = max(1, int(timeout or 30))
        self.session = session or requests.Session()

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_token}"} if self.api_token else {}

    @staticmethod
    def _valid_category(category: str) -> str | None:
        clean = str(category or "").strip().lower()
        return clean if clean in MDM_CATEGORIES else None

    def list_downloads(self, *, timeout: float | None = None) -> tuple[bool, list[dict], str]:
        if not self.base_url:
            return False, [], self.rejected_reason or "Remote MDM URL is required"
        req_timeout = self.timeout if timeout is None else max(0.05, min(float(self.timeout), float(timeout)))
        try:
            resp = self.session.get(
                f"{self.base_url}/api/list",
                headers=self._headers(),
                verify=self.verify_tls,
                timeout=req_timeout,
            )
            if resp.status_code == 401:
                return False, [], "MDM Remote API rejected the Bearer token"
            resp.raise_for_status()
            payload = resp.json()
        except (requests.RequestException, ValueError) as err:
            return False, [], f"MDM Remote unreachable: {err.__class__.__name__}"
        if isinstance(payload, dict) and payload.get("error"):
            return False, [], str(payload["error"])
        rows = payload.get("downloads", []) if isinstance(payload, dict) else []
        return True, [r for r in rows if isinstance(r, dict)], ""

    def add_url(self, url: str, *, category: str = "", start: bool = True) -> tuple[bool, str, str]:
        if not self.base_url:
            return False, "", self.rejected_reason or "Remote MDM URL is required"
        body: dict = {"url": url, "start": bool(start)}
        cat = self._valid_category(category)
        if cat:
            body["category"] = cat
        try:
            resp = self.session.post(
                f"{self.base_url}/api/add",
                json=body,
                headers=self._headers(),
                verify=self.verify_tls,
                timeout=self.timeout,
            )
            resp.raise_for_status()
            payload = resp.json()
        except (requests.RequestException, ValueError) as err:
            return False, "", f"MDM Remote add failed: {err.__class__.__name__}"
        if isinstance(payload, dict) and payload.get("error"):
            return False, "", str(payload["error"])
        job_id = str(payload.get("id") or "") if isinstance(payload, dict) else ""
        return True, job_id, ""

    def add_torrent(self, data: bytes, *, category: str = "", start: bool = True) -> tuple[bool, str, str]:
        if not self.base_url:
            return False, "", self.rejected_reason or "Remote MDM URL is required"
        params: dict[str, str] = {"start": "true" if start else "false"}
        cat = self._valid_category(category)
        if cat:
            params["category"] = cat
        headers = {**self._headers(), "Content-Type": "application/octet-stream"}
        try:
            resp = self.session.post(
                f"{self.base_url}/api/torrent",
                params=params,
                data=data,
                headers=headers,
                verify=self.verify_tls,
                timeout=self.timeout,
            )
            resp.raise_for_status()
            payload = resp.json()
        except (requests.RequestException, ValueError) as err:
            return False, "", f"MDM Remote torrent failed: {err.__class__.__name__}"
        if isinstance(payload, dict) and payload.get("error"):
            return False, "", str(payload["error"])
        return True, str(payload.get("id") or "") if isinstance(payload, dict) else "", ""

    def act(self, action: str, ids: list[int]) -> bool:
        if not ids:
            return False
        try:
            resp = self.session.post(
                f"{self.base_url}/api/act",
                json={"action": action, "ids": ids},
                headers=self._headers(),
                verify=self.verify_tls,
                timeout=self.timeout,
            )
            resp.raise_for_status()
            payload = resp.json()
        except (requests.RequestException, ValueError):
            return False
        return not (isinstance(payload, dict) and payload.get("error"))


# ----------------------------------------------------------- shared workers --


def _fetch_torrent_or_magnet(
    url: str,
    session: requests.Session,
    timeout: int,
) -> tuple[str | None, bytes | None, str]:
    """Fetch an HTTP torrent link while catching terminal magnet redirects safely."""
    current = str(url or "").strip()
    if current.lower().startswith("magnet:"):
        return current, None, ""
    if not current.lower().startswith(("http://", "https://")):
        return None, None, f"unsupported torrent link scheme: {urlsplit(current).scheme or 'none'}"
    initial_host = urlsplit(current).hostname
    initial_internal = _is_internal_host(initial_host)
    for _ in range(6):
        resp = None
        try:
            resp = session.get(current, timeout=timeout, stream=True, allow_redirects=False)
        except requests.exceptions.InvalidSchema as err:
            match = re.search(r"(magnet:\?[^\s'\"]+)", str(err), re.I)
            if match:
                return match.group(1), None, ""
            return None, None, f"unsupported redirect target: {err}"
        except requests.RequestException as err:
            return None, None, f"could not fetch .torrent: {err.__class__.__name__}"

        try:
            if resp.status_code in (301, 302, 303, 307, 308):
                location = str(resp.headers.get("Location") or "").strip()
                if location.lower().startswith("magnet:"):
                    return location, None, ""
                if location:
                    next_url = urljoin(current, location)
                    next_parts = urlsplit(next_url)
                    if next_parts.scheme.lower() not in ("http", "https"):
                        return None, None, "unsafe redirect scheme while fetching .torrent"
                    if _is_internal_host(next_parts.hostname) and not initial_internal:
                        return None, None, "unsafe internal redirect while fetching .torrent"
                    current = next_url
                    continue
                return None, None, f".torrent redirect {resp.status_code} missing Location"
            if not getattr(resp, "ok", False) and getattr(resp, "status_code", 200) >= 400:
                return None, None, f".torrent server returned HTTP {resp.status_code}"
            raw = _read_bounded_response(resp, MAX_TORRENT_BYTES, "Torrent download")
        except MdmError as err:
            return None, None, str(err)
        finally:
            _close_response(resp)
        text = raw.lstrip()
        if text.startswith(b"magnet:"):
            return text.decode("utf-8", "ignore").strip(), None, ""
        if not text.startswith(b"d"):
            return None, None, "fetched torrent payload is not valid bencoded data"
        return None, raw, ""
    return None, None, "too many redirects while fetching .torrent"


class _MdmBaseClient:
    """Shared worker lifecycle, NDJSON runner, and ledger state for MDM clients."""

    def __init__(
        self,
        *,
        ledger_name: str,
        save_path: str,
        mode: str,
        cli_path: str,
        remote_url: str,
        api_token: str,
        verify_tls: bool,
        category: str,
        timeout: int,
        max_workers: int,
        data_dir: str,
        session: requests.Session | None = None,
        cli_runner=None,
    ):
        self._save_path = str(save_path or "").strip()
        self._mode = "remote" if str(mode or "").strip().lower() == "remote" else "cli"
        self._cli_path = str(cli_path or "").strip()
        self._data_dir = str(data_dir or "").strip()
        self._timeout = max(5, int(timeout or 30))
        self._category = str(category or "").strip()
        self._session = session or requests.Session()
        self._cli_runner = cli_runner
        self._remote = MdmRemoteTransport(
            remote_url, api_token, verify_tls=verify_tls,
            timeout=self._timeout, session=self._session,
        )
        if self._mode == "remote":
            env_state = os.environ.get("ROMARR_DATA", "").strip()
            if self._data_dir:
                ledger_dir = Path(self._data_dir)
            elif env_state:
                ledger_dir = Path(env_state).parent
            elif self._save_path and Path(self._save_path).is_dir():
                ledger_dir = Path(self._save_path)
            else:
                ledger_dir = Path(tempfile.gettempdir()) / "romarr-mdm"
        else:
            ledger_dir = Path(self._save_path or self._data_dir or ".")
        self._ledger = _MdmLedger(ledger_dir / ledger_name)
        self._pool = concurrent.futures.ThreadPoolExecutor(
            max_workers=max(1, min(8, int(max_workers or 2))),
            thread_name_prefix="romarr-mdm",
        )
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

    def _resolve_cli(self) -> str:
        return find_binary(self._cli_path, data_dir=self._data_dir or None)

    def _run_ndjson(
        self,
        args: list[str],
        job_id: str,
        cancel_event: threading.Event,
    ) -> dict:
        if self._cli_runner is not None:
            return self._cli_runner(args, job_id=job_id, cancel_event=cancel_event)
        cli = self._resolve_cli()
        if not cli:
            raise MdmError("misterdownloadmanager-cli is not installed")
        cmd = [cli, "--json", *args]
        watch_stop = threading.Event()
        with tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace") as err_file:
            popen_kwargs: dict = {
                "stdout": subprocess.PIPE,
                "stderr": err_file,
                "text": True,
                "encoding": "utf-8",
                "errors": "replace",
                "creationflags": _SUBPROCESS_FLAGS,
            }
            if sys.platform != "win32":
                popen_kwargs["start_new_session"] = True
            proc = subprocess.Popen(cmd, **popen_kwargs)

            def _kill_proc_tree(*, force: bool = False) -> None:
                if sys.platform == "win32":
                    subprocess.run(
                        ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        creationflags=_SUBPROCESS_FLAGS,
                        check=False,
                    )
                    if proc.poll() is None:
                        proc.kill()
                    return
                sig = signal.SIGKILL if force else signal.SIGTERM
                try:
                    os.killpg(proc.pid, sig)
                except (ProcessLookupError, OSError):
                    if proc.poll() is None:
                        if force:
                            proc.kill()
                        else:
                            proc.terminate()

            def _watch_cancel() -> None:
                while not watch_stop.is_set() and proc.poll() is None:
                    if cancel_event.wait(0.2):
                        if proc.poll() is None:
                            _kill_proc_tree(force=False)
                            try:
                                proc.wait(timeout=1.5)
                            except subprocess.TimeoutExpired:
                                if proc.poll() is None:
                                    _kill_proc_tree(force=True)
                        return

            watcher = threading.Thread(target=_watch_cancel, daemon=True)
            watcher.start()
            final_event: dict = {}
            try:
                assert proc.stdout is not None
                for raw_line in proc.stdout:
                    if cancel_event.is_set():
                        if proc.poll() is None:
                            _kill_proc_tree(force=True)
                        raise MdmError("cancelled by user")
                    line = raw_line.strip()
                    if not line:
                        continue
                    try:
                        evt = json.loads(line)
                    except ValueError:
                        continue
                    if not isinstance(evt, dict):
                        continue
                    ev_type = str(evt.get("event") or "").lower()
                    if evt.get("error") or ev_type == "error":
                        raise MdmError(str(evt.get("error") or evt.get("message") or "MDM CLI error"))
                    if ev_type == "progress":
                        self._ledger.update(job_id, {
                            "downloaded": int(evt.get("downloaded") or 0),
                            "size": int(evt.get("size") or 0),
                            "speed": int(evt.get("speed") or 0),
                            "phase": str(evt.get("phase") or ""),
                        })
                    elif ev_type in ("done", "paused", "stopped"):
                        final_event = evt
                rc = proc.wait(timeout=10)
                err_file.seek(0)
                stderr_text = err_file.read().strip()
            except Exception:
                if proc.poll() is None:
                    _kill_proc_tree(force=True)
                raise
            finally:
                watch_stop.set()
                watcher.join(timeout=1.0)
        if cancel_event.is_set():
            raise MdmError("cancelled by user")
        if rc != 0:
            raise MdmError(stderr_text or f"MDM CLI exited with status {rc}")
        return final_event

    def _run_json_once(self, args: list[str], *, timeout: float = 60.0) -> dict:
        if self._cli_runner is not None:
            return self._cli_runner(args, job_id="", cancel_event=None)
        cli = self._resolve_cli()
        if not cli:
            raise MdmError("misterdownloadmanager-cli is not installed")
        try:
            proc = subprocess.run(
                [cli, "--json", *args],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                creationflags=_SUBPROCESS_FLAGS,
            )
        except subprocess.TimeoutExpired as err:
            raise MdmError(f"MDM CLI timed out after {int(timeout)}s") from err
        except OSError as err:
            raise MdmError(f"MDM CLI execution failed: {err}") from err
        for line in reversed((proc.stdout or "").splitlines()):
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except ValueError:
                continue
            if isinstance(data, dict):
                if data.get("error"):
                    raise MdmError(str(data["error"]))
                if proc.returncode == 0:
                    return data
        err_msg = (proc.stderr or proc.stdout or "").strip()
        raise MdmError(err_msg or f"MDM CLI exited with code {proc.returncode}")

    def _register_job(self, job_id: str) -> _JobControl:
        return register_job_control(self._ledger.path, job_id)

    def _clear_job(self, job_id: str, ctrl: _JobControl) -> None:
        clear_job_control(self._ledger.path, job_id, ctrl)

    def reachable(self) -> bool:
        if self._mode == "remote":
            ok, _, err = self._remote.list_downloads()
            self.detail = "MDM Remote API connected" if ok else err
            return ok
        if not self._save_path:
            self.detail = "Save Path is required"
            return False
        cli = self._resolve_cli()
        if not cli:
            info = availability(self._cli_path, data_dir=self._data_dir or None)
            self.detail = info["reason"]
            return False
        ok, ver = probe_cli(cli)
        self.detail = ver
        return ok

    def active_downloads(self) -> list[dict]:
        if self._mode == "remote":
            ok, rows, _ = self._remote.list_downloads()
            if not ok:
                return []
            return [
                {"id": f"mdm-remote:{r.get('id')}", "name": r.get("file", ""),
                 "state": str(r.get("status") or "").lower(),
                 "worker_active": r.get("status") in ("Queued", "Downloading"),
                 "size": int(r.get("size") or 0),
                 "downloaded": int(r.get("downloaded") or 0)}
                for r in rows
                if str(r.get("status") or "") in ("Queued", "Downloading", "Stopped")
            ]
        return [
            {"id": r["id"], "name": r.get("name", ""),
             "hash": r.get("hash", ""),
             "state": r.get("status", "downloading"),
             "worker_active": r.get("status") in ("queued", "downloading"),
             "size": int(r.get("size") or 0),
             "downloaded": int(r.get("downloaded") or 0)}
            for r in self._ledger.rows()
            if r.get("status") in ("queued", "downloading")
        ]

    def failed_jobs(self) -> list[dict]:
        if self._mode == "remote":
            ok, rows, _ = self._remote.list_downloads()
            if not ok:
                return []
            return [
                {"id": f"mdm-remote:{r.get('id')}", "name": r.get("file", ""),
                 "detail": str(r.get("error") or "download failed"),
                 "release_fault": False}
                for r in rows if str(r.get("status") or "") == "Error"
            ]
        return [
            {"id": r["id"], "name": r.get("name", ""),
             "detail": str(r.get("error") or "MDM download failed"),
             "release_fault": bool(r.get("release_fault", False))}
            for r in self._ledger.rows()
            if r.get("status") == "error"
        ]

    def _remote_id_for_row(self, row: dict | None, raw_id: str) -> str:
        """Resolve MDM's numeric download id for a remote-mode `download_id`, if present."""
        if raw_id.startswith("mdm-remote:"):
            candidate = raw_id.removeprefix("mdm-remote:")
            if candidate.isdigit():
                return candidate
        if row:
            rid = str(row.get("remote_id") or "")
            if rid.isdigit():
                return rid
            row_hash = str(row.get("hash") or "").lower()
            tname = str(row.get("torrent_name") or row.get("name") or "")
            ok, rows, _ = self._remote.list_downloads()
            if ok:
                for item in rows:
                    item_hash = magnet_hash(str(item.get("url") or ""))
                    if row_hash and item_hash == row_hash:
                        return str(item.get("id") or "")
                    if tname and str(item.get("file") or "").strip() == tname:
                        return str(item.get("id") or "")
        return ""

    def remove_download(self, download_id: str, *, delete_files: bool = False) -> bool:
        raw_id = str(download_id or "").strip()
        if not raw_id:
            return False
        if self._mode == "remote":
            stored = self._ledger.get(raw_id)
            target = self._remote_id_for_row(stored, raw_id)
            if not target.isdigit():
                if stored is not None:
                    self._ledger.remove(raw_id)
                    return True
                return False
            ok = self._remote.act("delete" if delete_files else "remove", [int(target)])
            if ok:
                self._ledger.remove(raw_id)
            return ok
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
        torrent_stage = row.get("torrent_file")
        if torrent_stage and stopped:
            Path(torrent_stage).unlink(missing_ok=True)
        return True


# ----------------------------------------------------------- torrent client --


@dataclass(frozen=True)
class MdmTorrentConfig:
    mode: str = "cli"
    save_path: str = "/downloads/torrents"
    cli_path: str = ""
    remote_url: str = ""
    api_token: str = ""
    verify_tls: bool = False
    category: str = "romarr"
    timeout: int = 30
    max_workers: int = 2
    data_dir: str = ""

    @property
    def base_url(self) -> str:
        return self.remote_url.strip() if self.mode == "remote" else self.save_path.strip()


class MdmTorrentClient(_MdmBaseClient):
    """Standalone BitTorrent client backed by Mister Download Manager."""

    protocol = "torrent"
    name = "Standalone BitTorrent (MDM)"
    TAKES_NAME = True
    supports_torrent_file = True

    def __init__(
        self,
        config: MdmTorrentConfig,
        *,
        session: requests.Session | None = None,
        cli_runner=None,
    ):
        super().__init__(
            ledger_name=".romarr-mdm-torrents.json",
            save_path=config.save_path,
            mode=config.mode,
            cli_path=config.cli_path,
            remote_url=config.remote_url,
            api_token=config.api_token,
            verify_tls=config.verify_tls,
            category=config.category,
            timeout=config.timeout,
            max_workers=config.max_workers,
            data_dir=config.data_dir,
            session=session,
            cli_runner=cli_runner,
        )
        self._config = config
        self._files_lock = threading.Lock()
        self._files_cache: dict[str, list[dict]] = {}

    @property
    def last_selected_file(self) -> dict:
        val = getattr(self._added, "selected_file", None)
        return dict(val) if isinstance(val, dict) else {}

    @last_selected_file.setter
    def last_selected_file(self, value: dict) -> None:
        self._added.selected_file = dict(value) if isinstance(value, dict) else {}

    @property
    def last_retained_hash(self) -> str:
        return str(getattr(self._added, "retained_hash", "") or "")

    @last_retained_hash.setter
    def last_retained_hash(self, value: str) -> None:
        self._added.retained_hash = str(value or "")

    @property
    def configured(self) -> bool:
        if self._mode == "remote":
            return bool(self._remote.base_url)
        return bool(self._save_path)

    @property
    def supports_selective(self) -> bool:
        return self._mode != "remote"

    def _stage_torrent_bytes(self, tag: str, torrent_bytes: bytes) -> Path:
        stage = Path(self._save_path or ".") / ".romarr-mdm-staging"
        stage.mkdir(parents=True, exist_ok=True)
        path = stage / f"{tag}.torrent"
        path.write_bytes(torrent_bytes)
        return path

    def _enqueue_cli_torrent(
        self,
        *,
        source: str,
        name: str,
        torrent_hash: str = "",
        selected_file: dict | None = None,
        file_indices: list[int] | None = None,
        torrent_file_path: str = "",
        release_url: str = "",
    ) -> bool:
        tag = secrets.token_hex(8)
        job_id = f"{TAG_PREFIX}{tag}"
        safe_title = _safe_name(name or torrent_hash or tag, fallback=f"torrent-{tag}")
        job_dir = str(Path(self._save_path) / f"{safe_title}-{tag[:6]}")
        Path(job_dir).mkdir(parents=True, exist_ok=True)
        self._ledger.upsert(job_id, {
            "tag": tag,
            "name": name or safe_title,
            "status": "queued",
            "hash": torrent_hash.lower(),
            "job_dir": job_dir,
            "content_path": "",
            "save_path": job_dir,
            "selected_file": selected_file or {},
            "file_indices": list(file_indices or []),
            "torrent_file": torrent_file_path,
            "error": "",
            "release_fault": False,
            "created_at": time.time(),
        })
        self.last_job_id = job_id
        self.last_error = ""
        ctrl = self._register_job(job_id)
        cancel_ev = ctrl.cancel

        def _worker() -> None:
            src = source
            staged = torrent_file_path
            try:
                if cancel_ev.is_set() or self._ledger.get(job_id) is None:
                    return
                self._ledger.update(job_id, {"status": "downloading"})
                if not src and release_url:
                    mag, tbytes, ferr = _fetch_torrent_or_magnet(
                        release_url, self._session, self._timeout,
                    )
                    if cancel_ev.is_set():
                        return
                    if ferr:
                        self._ledger.update(job_id, {
                            "status": "error", "error": ferr, "release_fault": False,
                        })
                        return
                    if mag:
                        src = mag
                    elif tbytes is not None:
                        staged = str(self._stage_torrent_bytes(tag, tbytes))
                        src = staged
                        self._ledger.update(job_id, {"torrent_file": staged})
                if cancel_ev.is_set():
                    return
                cmd = ["torrent-get", src, "-o", job_dir]
                if file_indices:
                    cmd.extend(["--files", ",".join(str(i) for i in file_indices)])
                done = self._run_ndjson(cmd, job_id, cancel_ev)
                out_path = str(done.get("path") or "").strip()
                if not out_path:
                    raise MdmError("MDM finished torrent without returning output path")
                self._ledger.update(job_id, {
                    "status": "complete",
                    "content_path": out_path,
                    "save_path": out_path,
                    "size": int(done.get("size") or 0),
                })
            except Exception as err:  # noqa: BLE001
                if not cancel_ev.is_set():
                    self._ledger.update(job_id, {
                        "status": "error",
                        "error": str(err),
                        "release_fault": False,
                    })
            finally:
                if staged:
                    Path(staged).unlink(missing_ok=True)
                self._clear_job(job_id, ctrl)

        fut = self._pool.submit(_worker)
        set_job_future(ctrl, fut)
        return True

    def _record_remote_job(
        self,
        *,
        remote_id: str,
        name: str = "",
        torrent_hash: str = "",
        torrent_name: str = "",
    ) -> str:
        """Create a persistent tracking id for a remote-mode torrent (even when pending)."""
        clean_rid = str(remote_id or "").strip()
        tag = secrets.token_hex(8)
        job_id = f"mdm-remote:{clean_rid}" if clean_rid else f"{TAG_PREFIX}{tag}"
        self._ledger.upsert(job_id, {
            "mode": "remote",
            "tag": "" if clean_rid else tag,
            "remote_id": clean_rid,
            "name": name or torrent_name or torrent_hash or clean_rid or tag,
            "torrent_name": torrent_name,
            "hash": torrent_hash.lower(),
            "status": "pending",
            "created_at": time.time(),
        })
        return job_id

    def add(self, url: str, *, save_path: str | None = None, name: str = "") -> bool:
        del save_path
        if not self.configured:
            self.last_error = self._remote.rejected_reason or "Standalone MDM Torrent client is not configured"
            return False
        raw = str(url or "").strip()
        if not raw:
            self.last_error = "missing torrent URL or magnet"
            return False
        if self._mode == "remote":
            ok, job_id, err = self._remote.add_url(raw, category=self._category, start=True)
            if ok:
                thash = magnet_hash(raw) if raw.lower().startswith("magnet:") else ""
                dn_list = parse_qs(urlsplit(raw).query).get("dn", []) if thash else []
                tname = str(dn_list[0]).strip() if dn_list else ""
                if job_id:
                    self.last_job_id = self._record_remote_job(
                        remote_id=job_id, name=name, torrent_hash=thash, torrent_name=tname,
                    )
                elif raw.lower().startswith("magnet:"):
                    self.last_job_id = self._record_remote_job(
                        remote_id="", name=name, torrent_hash=thash, torrent_name=tname,
                    )
                else:
                    self.last_job_id = ""
            else:
                self.last_job_id = ""
            self.last_error = err
            return ok
        if raw.lower().startswith("magnet:"):
            return self._enqueue_cli_torrent(
                source=raw, name=name, torrent_hash=magnet_hash(raw),
            )
        if raw.lower().startswith(("http://", "https://")):
            return self._enqueue_cli_torrent(source="", name=name, release_url=raw)
        self.last_error = "unsupported torrent URL scheme"
        return False

    def add_torrent_file(self, torrent_bytes: bytes, *, name: str = "") -> bool:
        if not self.configured:
            self.last_error = self._remote.rejected_reason or "Standalone MDM Torrent client is not configured"
            return False
        raw = bytes(torrent_bytes or b"")
        if not raw.lstrip().startswith(b"d"):
            self.last_error = "uploaded payload is not a valid .torrent file"
            return False
        if self._mode == "remote":
            info_hash, torrent_name = _extract_torrent_metadata(raw)
            ok, job_id, err = self._remote.add_torrent(raw, category=self._category, start=True)
            if ok:
                self.last_job_id = self._record_remote_job(
                    remote_id=job_id,
                    name=name or torrent_name,
                    torrent_hash=info_hash,
                    torrent_name=torrent_name,
                )
            else:
                self.last_job_id = ""
            self.last_error = err
            return ok
        tag = secrets.token_hex(8)
        staged = str(self._stage_torrent_bytes(tag, raw))
        return self._enqueue_cli_torrent(
            source=staged, name=name, torrent_file_path=staged,
        )

    def add_magnet_selective(
        self,
        magnet: str,
        selected_file: str,
        *,
        owned_selections=(),
    ) -> bool:
        """Fetch metadata via MDM CLI and download ONLY `selected_file`."""
        del owned_selections
        self.last_selected_file = {}
        self.last_retained_hash = ""
        self.last_job_id = ""
        if not self.configured or not self.supports_selective:
            self.last_error = "selective download requires Standalone MDM CLI mode"
            return False
        try:
            wanted = _normalize_rel_path(selected_file)
        except ValueError as err:
            self.last_error = str(err)
            return False
        raw_magnet = str(magnet or "").strip()
        if not raw_magnet.lower().startswith("magnet:"):
            self.last_error = "selective download requires a magnet link"
            return False

        # Every selective MDM job downloads into its own isolated job_dir, so no other job's files are touched.
        info_hash = magnet_hash(raw_magnet)

        try:
            meta = self._run_json_once(["torrent-info", raw_magnet], timeout=120.0)
        except MdmError as err:
            self.last_error = str(err)
            return False

        resolved_hash = str(meta.get("info_hash") or info_hash or "").strip().lower()
        raw_files = meta.get("files") if isinstance(meta.get("files"), list) else []
        normalized: list[dict] = []
        exact_matches: list[dict] = []
        suffix_matches: list[dict] = []
        for entry in raw_files:
            if not isinstance(entry, dict):
                continue
            raw_path = str(entry.get("path") or "").replace("\\", "/").strip("/")
            path_norm = "/".join(p for p in PurePosixPath(raw_path).parts if p and p != ".")
            item = {
                "index": entry.get("index"),
                "name": path_norm,
                "size": int(entry.get("size") or 0),
            }
            normalized.append(item)
            if path_norm == wanted:
                exact_matches.append(item)
            elif path_norm.endswith(f"/{wanted}"):
                suffix_matches.append(item)

        matches = exact_matches or suffix_matches
        if len(matches) != 1 or not isinstance(matches[0].get("index"), int) or matches[0]["size"] <= 0:
            self.last_error = f"exact file {wanted!r} not found uniquely in torrent"
            return False

        chosen = matches[0]
        if resolved_hash:
            with self._files_lock:
                self._files_cache[resolved_hash] = normalized
        identity = {
            "hash": resolved_hash,
            "index": chosen["index"],
            "path": chosen["name"],
            "size": chosen["size"],
        }
        self.last_selected_file = identity
        self.last_retained_hash = resolved_hash
        return self._enqueue_cli_torrent(
            source=raw_magnet,
            name=str(meta.get("name") or PurePosixPath(wanted).name),
            torrent_hash=resolved_hash,
            selected_file=identity,
            file_indices=[chosen["index"]],
        )

    def torrent_files(self, torrent_hash: str) -> list[dict]:
        """Return file rows compatible with `selected_import_path` verification."""
        target = str(torrent_hash or "").strip().lower()
        rows = self._ledger.rows()
        matching = [r for r in rows if r.get("hash") == target]
        selected_idxs = {
            int(idx) for r in matching for idx in (r.get("file_indices") or [])
        }
        any_complete = any(r.get("status") == "complete" for r in matching)
        with self._files_lock:
            cached = list(self._files_cache.get(target, []))
        if not cached:
            for r in matching:
                sel = r.get("selected_file") or {}
                if sel:
                    cached.append({
                        "index": sel.get("index"),
                        "name": sel.get("path"),
                        "size": sel.get("size"),
                    })
        return [
            {
                "index": row["index"],
                "name": row["name"],
                "size": row["size"],
                "progress": 1 if (any_complete and row["index"] in selected_idxs) else 0,
                "priority": 1 if row["index"] in selected_idxs else 0,
            }
            for row in cached
        ]

    def _row_to_completed(self, row: dict) -> dict:
        return {
            "id": row["id"],
            "name": row.get("name", ""),
            "hash": row.get("hash", ""),
            "tags": row.get("tag", ""),
            "content_path": row.get("content_path", ""),
            "save_path": row.get("save_path", ""),
            "state": "complete",
            "size": int(row.get("size") or 0),
        }

    def selective_completed(
        self,
        queue=None,
        *,
        ownership=None,
        update_cache: bool = False,
        timeout: float | None = None,
    ) -> list[dict]:
        del queue, ownership, update_cache, timeout
        if not self.configured or not self.supports_selective:
            return []
        return [
            self._row_to_completed(row)
            for row in self._ledger.rows()
            if row.get("status") == "complete" and row.get("selected_file") and row.get("content_path")
        ]

    def _correlate_remote_row(self, row: dict, tracked: list[dict]) -> dict | None:
        """Match a remote `/api/list` item to a tracked pending/submitted job entry."""
        rid = str(row.get("id") or "").strip()
        row_hash = magnet_hash(str(row.get("url") or ""))
        fname = str(row.get("file") or "").strip()
        for entry in tracked:
            if entry.get("mode") != "remote":
                continue
            if rid and str(entry.get("remote_id") or "") == rid:
                return entry
        for entry in tracked:
            if entry.get("mode") != "remote":
                continue
            ehash = str(entry.get("hash") or "").lower()
            if row_hash and ehash and ehash == row_hash:
                if rid and not entry.get("remote_id"):
                    self._ledger.update(entry["id"], {"remote_id": rid})
                return entry
        for entry in tracked:
            if entry.get("mode") != "remote" or entry.get("remote_id"):
                continue
            tname = str(entry.get("torrent_name") or "").strip()
            rname = str(entry.get("name") or "").strip()
            if fname and ((tname and tname == fname) or (rname and rname == fname)):
                if rid:
                    self._ledger.update(entry["id"], {"remote_id": rid})
                return entry
        return None

    def active_downloads(self) -> list[dict]:
        if self._mode == "remote":
            ok, rows, _ = self._remote.list_downloads()
            if not ok:
                return []
            tracked = self._ledger.rows()
            out = []
            for r in rows:
                if str(r.get("status") or "") not in ("Queued", "Downloading", "Stopped"):
                    continue
                entry = self._correlate_remote_row(r, tracked)
                rhash = magnet_hash(str(r.get("url") or "")) or (entry.get("hash", "") if entry else "")
                out.append({
                    "id": entry["id"] if entry else f"mdm-remote:{r.get('id')}",
                    "name": r.get("file", ""),
                    "hash": rhash,
                    "tags": entry.get("tag", "") if entry else "",
                    "state": str(r.get("status") or "").lower(),
                 "worker_active": r.get("status") in ("Queued", "Downloading"),
                    "size": int(r.get("size") or 0),
                    "downloaded": int(r.get("downloaded") or 0),
                })
            return out
        return super().active_downloads()

    def completed(self, timeout: float | None = None) -> list[dict]:
        if not self.configured:
            return []
        if self._mode == "remote":
            ok, rows, _ = self._remote.list_downloads(timeout=timeout)
            if not ok:
                return []
            tracked = self._ledger.rows()
            out = []
            for row in rows:
                if str(row.get("status") or "") != "Complete":
                    continue
                entry = self._correlate_remote_row(row, tracked)
                fname = str(row.get("file") or "").strip()
                raw_path = str(row.get("path") or "").strip()
                resolved_path = raw_path or (str(Path(self._save_path) / fname) if (self._save_path and fname) else "")
                row_hash = magnet_hash(str(row.get("url") or "")) or (entry.get("hash", "") if entry else "")
                out.append({
                    "id": entry["id"] if entry else f"mdm-remote:{row.get('id')}",
                    "name": fname,
                    "hash": row_hash,
                    "tags": entry.get("tag", "") if entry else "",
                    "content_path": resolved_path,
                    "save_path": self._save_path or resolved_path,
                    "state": "complete",
                    "category": str(row.get("category") or ""),
                    "size": int(row.get("size") or 0),
                })
            return out
        return [
            self._row_to_completed(row)
            for row in self._ledger.rows()
            if row.get("status") == "complete" and not row.get("selected_file") and row.get("content_path")
        ]


# ----------------------------------------------------------- direct client --


@dataclass(frozen=True)
class MdmDirectConfig:
    mode: str = "cli"
    save_path: str = "/downloads/direct"
    connections: int = 8
    cli_path: str = ""
    remote_url: str = ""
    api_token: str = ""
    verify_tls: bool = False
    category: str = ""
    timeout: int = 30
    max_workers: int = 2
    data_dir: str = ""

    @property
    def base_url(self) -> str:
        return self.remote_url.strip() if self.mode == "remote" else self.save_path.strip()


class MdmDirectClient(_MdmBaseClient):
    """Multi-connection direct HTTP(S)/FTP downloader powered by MDM."""

    protocol = "direct"
    name = "Standalone Multi-Connection HTTP (MDM)"
    TAKES_NAME = True

    def __init__(
        self,
        config: MdmDirectConfig,
        *,
        session: requests.Session | None = None,
        cli_runner=None,
    ):
        super().__init__(
            ledger_name=".romarr-mdm-direct.json",
            save_path=config.save_path,
            mode=config.mode,
            cli_path=config.cli_path,
            remote_url=config.remote_url,
            api_token=config.api_token,
            verify_tls=config.verify_tls,
            category=config.category,
            timeout=config.timeout,
            max_workers=config.max_workers,
            data_dir=config.data_dir,
            session=session,
            cli_runner=cli_runner,
        )
        self._config = config
        self._connections = max(1, min(128, int(config.connections or 8)))

    @property
    def configured(self) -> bool:
        if self._mode == "remote":
            return bool(self._remote.base_url)
        return bool(self._save_path)

    def add(self, url: str, *, save_path: str | None = None, name: str = "", headers: dict[str, str] | None = None) -> bool:
        del save_path
        if not self.configured:
            self.last_error = self._remote.rejected_reason or "Standalone MDM Direct client is not configured"
            return False
        raw = str(url or "").strip()
        scheme = urlsplit(raw).scheme.lower()
        if scheme not in ("http", "https", "ftp", "ftps"):
            self.last_error = f"unsupported direct download scheme: {scheme or 'none'}"
            return False

        if self._mode == "remote":
            ok, job_id, err = self._remote.add_url(raw, category=self._category, start=True)
            self.last_job_id = f"mdm-remote:{job_id}" if job_id else ""
            self.last_error = err
            return ok

        tag = secrets.token_hex(8)
        job_id = f"mdm-http:{tag}"
        safe_title = _safe_name(name or tag, fallback=f"direct-{tag}")
        job_dir = str(Path(self._save_path) / f"{safe_title}-{tag[:6]}")
        Path(job_dir).mkdir(parents=True, exist_ok=True)
        self._ledger.upsert(job_id, {
            "tag": tag,
            "name": name or safe_title,
            "status": "queued",
            "job_dir": job_dir,
            "content_path": "",
            "save_path": job_dir,
            "error": "",
            "release_fault": False,
            "created_at": time.time(),
        })
        self.last_job_id = job_id
        self.last_error = ""
        ctrl = self._register_job(job_id)
        cancel_ev = ctrl.cancel

        def _worker() -> None:
            try:
                if cancel_ev.is_set() or self._ledger.get(job_id) is None:
                    return
                self._ledger.update(job_id, {"status": "downloading"})
                args = ["get", raw, "-o", job_dir, "-c", str(self._connections)]
                for hk, hv in (headers or {}).items():
                    clean_k = re.sub(r"[\r\n:]+", "", str(hk or "")).strip()
                    clean_v = re.sub(r"[\r\n]+", "", str(hv or "")).strip()
                    if clean_k and clean_v:
                        args.extend(["-H", f"{clean_k}: {clean_v}"])
                done = self._run_ndjson(args, job_id, cancel_ev)
                out_path = str(done.get("path") or "").strip()
                if not out_path:
                    raise MdmError("MDM HTTP download finished without an output file path")
                self._ledger.update(job_id, {
                    "status": "complete",
                    "content_path": out_path,
                    "save_path": str(Path(out_path).parent),
                    "size": int(done.get("size") or 0),
                })
            except Exception as err:  # noqa: BLE001
                if not cancel_ev.is_set():
                    self._ledger.update(job_id, {
                        "status": "error",
                        "error": str(err),
                        "release_fault": False,
                    })
            finally:
                self._clear_job(job_id, ctrl)

        fut = self._pool.submit(_worker)
        set_job_future(ctrl, fut)
        return True

    def completed(self, timeout: float | None = None) -> list[dict]:
        if not self.configured:
            return []
        if self._mode == "remote":
            ok, rows, _ = self._remote.list_downloads(timeout=timeout)
            if not ok:
                return []
            out = []
            for row in rows:
                if str(row.get("status") or "") != "Complete":
                    continue
                fname = str(row.get("file") or "").strip()
                raw_path = str(row.get("path") or "").strip()
                resolved_path = raw_path or (str(Path(self._save_path) / fname) if (self._save_path and fname) else "")
                out.append({
                    "id": f"mdm-remote:{row.get('id')}",
                    "name": fname,
                    "content_path": resolved_path,
                    "save_path": self._save_path or resolved_path,
                    "state": "complete",
                    "category": str(row.get("category") or ""),
                    "size": int(row.get("size") or 0),
                })
            return out
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
