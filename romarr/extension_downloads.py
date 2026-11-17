"""Server-side multi-file download and completed-capture handling for the
ROMarr browser extension.
"""
from __future__ import annotations

import ipaddress
import logging
import os
import re
import socket
import tempfile
import threading
import uuid
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit

import requests

log = logging.getLogger(__name__)

MAX_BATCH_ITEMS = 500
MAX_JOBS = 40
MAX_COMPLETED = 100
MAX_WORKERS = 2
MAX_REDIRECTS = 5
MAX_FILE_BYTES = 16 * 1024 * 1024 * 1024  # 16 GiB per file safety ceiling

_UNSAFE_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def sanitize_filename(raw: str, fallback: str = "download.bin") -> str:
    """Return a single safe basename with traversal and control chars removed."""
    base = Path(str(raw or "").replace("\\", "/")).name
    clean = _UNSAFE_CHARS.sub("_", base).strip(". ")
    return clean[:180] or fallback


def sanitize_rel_subdir(raw: str) -> str:
    """Normalise an optional relative subfolder, rejecting traversal."""
    parts: list[str] = []
    for seg in str(raw or "").replace("\\", "/").split("/"):
        s = _UNSAFE_CHARS.sub("_", seg).strip(". ")
        if not s or s in (".", ".."):
            continue
        parts.append(s[:64])
    return "/".join(parts)


def _check_ip(ip_obj: ipaddress.IPv4Address | ipaddress.IPv6Address, label: str) -> None:
    if (
        ip_obj.is_loopback
        or ip_obj.is_link_local
        or ip_obj.is_unspecified
        or ip_obj.is_private
        or ip_obj.is_reserved
        or ip_obj.is_multicast
    ):
        raise ValueError(f"refusing non-public address: {label!r}")


def check_remote_url(raw_url: str, *, resolve_dns: bool = True) -> str:
    """Validate an http(s) URL and refuse loopback/private/link-local SSRF targets."""
    url = str(raw_url or "").strip()
    parts = urlsplit(url)
    if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
        raise ValueError(f"unsupported URL: {raw_url!r}")
    host = parts.hostname.lower().strip("[]")
    if host in ("localhost", "localhost.localdomain") or host.endswith(".local"):
        raise ValueError(f"refusing local host: {host!r}")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip is not None:
        _check_ip(ip, host)
        return url
    if resolve_dns:
        try:
            infos = socket.getaddrinfo(host, parts.port or (443 if parts.scheme.lower() == "https" else 80), type=socket.SOCK_STREAM)
        except OSError:
            infos = []
        for info in infos:
            addr = info[4][0].split("%")[0]
            try:
                _check_ip(ipaddress.ip_address(addr), f"{host} ({addr})")
            except ValueError as exc:
                if "refusing" in str(exc):
                    raise
    return url


def resolve_dest_dir(service: Any, platform: str = "", destination: str = "") -> Path:
    """Choose a safe destination folder inside the library or store directory."""
    lib = getattr(service, "library", None)
    base = Path(lib) if lib else (service.store.path.parent / "extension_downloads")
    sub = sanitize_rel_subdir(destination)
    plat = sanitize_rel_subdir(platform)
    if sub:
        target = base / sub
    elif plat:
        target = base / plat
    else:
        target = base / "_extension_downloads"
    from .path_guard import confined
    try:
        safe = Path(confined(str(target), [str(base)]))
    except ValueError:
        raise ValueError("destination escapes library root") from None
    safe.mkdir(parents=True, exist_ok=True)
    return safe


def _non_colliding_path(target: Path) -> Path:
    """Pick a unique filename inside `target.parent` without overwriting existing files."""
    if not target.exists():
        return target
    stem = target.stem
    suffix = target.suffix
    for idx in range(1, 10000):
        cand = target.with_name(f"{stem}_{idx}{suffix}")
        if not cand.exists():
            return cand
    raise ValueError(f"too many existing files named {target.name!r}")


class ExtensionDownloadManager:
    """Tracks bounded background multi-file downloads and completed local captures."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._jobs: dict[str, dict[str, Any]] = {}
        self._job_order: list[str] = []
        self._completed: deque[dict[str, Any]] = deque(maxlen=MAX_COMPLETED)
        self._pool = ThreadPoolExecutor(max_workers=MAX_WORKERS, thread_name_prefix="ext-dl")

    def _evict_terminal_if_needed(self) -> None:
        while len(self._job_order) >= MAX_JOBS:
            evicted = False
            for idx, jid in enumerate(self._job_order):
                job = self._jobs.get(jid)
                if not job or job.get("status") in ("done", "partial", "failed"):
                    self._job_order.pop(idx)
                    self._jobs.pop(jid, None)
                    evicted = True
                    break
            if not evicted:
                break

    def start_batch(
        self,
        service: Any,
        items: list[dict[str, Any]],
        *,
        page_url: str = "",
        page_title: str = "",
        platform: str = "",
        destination: str = "",
    ) -> dict[str, Any]:
        if not isinstance(items, list) or not items:
            raise ValueError("items must be a non-empty list")
        if len(items) > MAX_BATCH_ITEMS:
            raise ValueError(f"too many items (max {MAX_BATCH_ITEMS})")

        dest_dir = resolve_dest_dir(service, platform=platform, destination=destination)
        normalized: list[dict[str, Any]] = []
        seen_names: set[str] = set()
        for idx, raw in enumerate(items, 1):
            if not isinstance(raw, dict):
                continue
            url = check_remote_url(str(raw.get("url") or ""))
            guess = urlsplit(url).path.rsplit("/", 1)[-1] or f"file_{idx}.bin"
            fname = sanitize_filename(str(raw.get("filename") or guess), fallback=f"file_{idx}.bin")
            stem, dot, ext = fname.partition(".")
            cand = fname
            n = 1
            while cand.lower() in seen_names:
                cand = f"{stem}_{n}{dot}{ext}" if dot else f"{stem}_{n}"
                n += 1
            seen_names.add(cand.lower())
            normalized.append({
                "url": url,
                "filename": cand,
                "status": "queued",
                "bytes": 0,
                "path": str(dest_dir / cand),
                "error": "",
            })

        if not normalized:
            raise ValueError("no valid downloadable URLs in batch")

        job_id = uuid.uuid4().hex[:12]
        job = {
            "job_id": job_id,
            "status": "queued",
            "page_url": str(page_url or ""),
            "page_title": str(page_title or ""),
            "platform": str(platform or ""),
            "dest_dir": str(dest_dir),
            "total": len(normalized),
            "completed": 0,
            "failed": 0,
            "files": normalized,
        }
        with self._lock:
            self._evict_terminal_if_needed()
            self._job_order.append(job_id)
            self._jobs[job_id] = job

        self._pool.submit(self._run_job, job_id, str(page_url or ""))
        return {
            "ok": True,
            "job_id": job_id,
            "queued": len(normalized),
            "dest_dir": str(dest_dir),
        }

    def _open_validated_stream(self, url: str, headers: dict[str, str]):
        current = url
        for _ in range(MAX_REDIRECTS + 1):
            check_remote_url(current)
            resp = requests.get(current, headers=headers, stream=True, timeout=60, allow_redirects=False)
            if resp.status_code in (301, 302, 303, 307, 308):
                loc = resp.headers.get("Location") or resp.headers.get("location") or ""
                resp.close()
                if not loc:
                    raise ValueError(f"redirect {resp.status_code} missing Location")
                current = urljoin(current, loc)
                continue
            return resp
        raise ValueError("too many redirects")

    def _run_job(self, job_id: str, referer: str) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return
            job["status"] = "downloading"
            files = list(job["files"])

        headers = {"User-Agent": "ROMarr-Extension-Downloader/1.0"}
        if referer.startswith(("http://", "https://")):
            headers["Referer"] = referer

        for entry in files:
            requested_target = Path(entry["path"])
            tmp_path: Path | None = None
            try:
                entry["status"] = "downloading"
                written = 0
                with self._open_validated_stream(entry["url"], headers) as resp:
                    resp.raise_for_status()
                    with tempfile.NamedTemporaryFile(
                        dir=requested_target.parent,
                        prefix=".extdl-",
                        suffix=".part",
                        delete=False,
                    ) as fh:
                        tmp_path = Path(fh.name)
                        for chunk in resp.iter_content(chunk_size=65536):
                            if chunk:
                                written += len(chunk)
                                if written > MAX_FILE_BYTES:
                                    raise ValueError("file exceeds 16 GiB safety limit")
                                fh.write(chunk)
                final_target = _non_colliding_path(requested_target)
                os.replace(tmp_path, final_target)
                tmp_path = None
                entry["path"] = str(final_target)
                entry["filename"] = final_target.name
                entry["status"] = "done"
                entry["bytes"] = written
                with self._lock:
                    job["completed"] += 1
            except Exception as exc:
                if tmp_path is not None:
                    try:
                        tmp_path.unlink(missing_ok=True)
                    except OSError:
                        pass
                entry["status"] = "failed"
                entry["error"] = f"{type(exc).__name__}: {exc}"
                with self._lock:
                    job["failed"] += 1

        with self._lock:
            job["status"] = "done" if job["failed"] == 0 else (
                "partial" if job["completed"] > 0 else "failed"
            )

    def get_status(self, job_id: str = "") -> dict[str, Any]:
        with self._lock:
            if job_id:
                job = self._jobs.get(job_id)
                return dict(job) if job else {"error": "job not found"}
            jobs = [dict(self._jobs[jid]) for jid in reversed(self._job_order) if jid in self._jobs]
            completed = list(self._completed)
        return {"jobs": jobs, "completed_local": completed}

    def record_completed(
        self,
        service: Any,
        items: list[dict[str, Any]],
        *,
        platform: str = "",
        adopt: bool = False,
    ) -> dict[str, Any]:
        if not isinstance(items, list):
            raise ValueError("items must be a list")
        results: list[dict[str, Any]] = []
        adopted = 0
        for raw in items[:MAX_BATCH_ITEMS]:
            if not isinstance(raw, dict):
                continue
            local_path = str(raw.get("local_path") or "").strip()
            rec = {
                "url": str(raw.get("url") or ""),
                "filename": sanitize_filename(str(raw.get("filename") or ""), fallback="file"),
                "local_path": local_path,
                "file_size": int(raw.get("file_size") or 0),
                "page_url": str(raw.get("page_url") or ""),
                "platform": str(platform or ""),
                "adopted": False,
            }
            safe_path = ""
            if adopt and platform and local_path:
                # Only files in folders the operator configured (library,
                # downloads, ~/Downloads, ROMARR_IMPORT_ROOTS) are imported.
                from .path_guard import confined
                roots_of = getattr(service, "extension_import_roots", None)
                try:
                    safe_path = confined(local_path, roots_of() if callable(roots_of) else [])
                except ValueError:
                    rec["adopt_error"] = "file is outside the folders ROMarr may import from"
            if safe_path and os.path.isfile(safe_path):
                try:
                    res = service.adopt(safe_path, platform)
                    rec["adopted"] = bool(res and res.get("ok"))
                    if rec["adopted"]:
                        adopted += 1
                except Exception as exc:
                    rec["adopt_error"] = f"{type(exc).__name__}: {exc}"
            with self._lock:
                self._completed.appendleft(rec)
            results.append(rec)
        return {"ok": True, "recorded": len(results), "adopted": adopted, "results": results}
