"""Server-side multi-file download and completed-capture handling for the
ROMarr browser extension.
"""
from __future__ import annotations

import ipaddress
import logging
import os
import re
import threading
import uuid
from collections import deque
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import requests

log = logging.getLogger(__name__)

MAX_BATCH_ITEMS = 500
MAX_JOBS = 40
MAX_COMPLETED = 100

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


def check_remote_url(raw_url: str) -> str:
    """Validate an http(s) URL and refuse loopback/link-local SSRF targets."""
    url = str(raw_url or "").strip()
    parts = urlsplit(url)
    if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
        raise ValueError(f"unsupported URL: {raw_url!r}")
    host = parts.hostname.lower().strip("[]")
    if host in ("localhost", "localhost.localdomain"):
        raise ValueError(f"refusing local host: {host!r}")
    try:
        ip = ipaddress.ip_address(host)
        if ip.is_loopback or ip.is_link_local or ip.is_unspecified:
            raise ValueError(f"refusing local address: {host!r}")
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
    resolved_base = base.resolve()
    resolved_target = target.resolve()
    if resolved_target != resolved_base and resolved_base not in resolved_target.parents:
        raise ValueError("destination escapes library root")
    resolved_target.mkdir(parents=True, exist_ok=True)
    return resolved_target


class ExtensionDownloadManager:
    """Tracks background multi-file downloads and completed local captures."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._jobs: dict[str, dict[str, Any]] = {}
        self._job_order: deque[str] = deque(maxlen=MAX_JOBS)
        self._completed: deque[dict[str, Any]] = deque(maxlen=MAX_COMPLETED)

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
            if len(self._job_order) == self._job_order.maxlen:
                oldest = self._job_order.popleft()
                self._jobs.pop(oldest, None)
            self._job_order.append(job_id)
            self._jobs[job_id] = job

        t = threading.Thread(
            target=self._run_job,
            args=(job_id, str(page_url or "")),
            daemon=True,
        )
        t.start()
        return {
            "ok": True,
            "job_id": job_id,
            "queued": len(normalized),
            "dest_dir": str(dest_dir),
        }

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
            target = Path(entry["path"])
            tmp = target.with_name(target.name + ".part")
            try:
                entry["status"] = "downloading"
                written = 0
                with requests.get(entry["url"], headers=headers, stream=True, timeout=60) as resp:
                    resp.raise_for_status()
                    with open(tmp, "wb") as fh:
                        for chunk in resp.iter_content(chunk_size=65536):
                            if chunk:
                                fh.write(chunk)
                                written += len(chunk)
                os.replace(tmp, target)
                entry["status"] = "done"
                entry["bytes"] = written
                with self._lock:
                    job["completed"] += 1
            except Exception as exc:
                try:
                    tmp.unlink(missing_ok=True)
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
            if adopt and platform and local_path and os.path.isfile(local_path):
                try:
                    res = service.adopt(local_path, platform)
                    rec["adopted"] = bool(res and not res.get("error"))
                    if rec["adopted"]:
                        adopted += 1
                except Exception as exc:
                    rec["adopt_error"] = f"{type(exc).__name__}: {exc}"
            with self._lock:
                self._completed.appendleft(rec)
            results.append(rec)
        return {"ok": True, "recorded": len(results), "adopted": adopted, "results": results}
