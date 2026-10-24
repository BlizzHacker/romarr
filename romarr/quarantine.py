"""Reversible quarantine for byte-identical library duplicates.

Quarantine data and its manifest live under the library root, keeping every
move on the same filesystem. Each move is preceded by an atomic manifest write
and uses os.replace; interrupted actions can be inspected and safely retried.
"""
from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

from .dat import hash_file

ROOT_NAME = ".romarr-quarantine"


def _atomic_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    with temp.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, path)


def _safe_file(root: Path, relative: str) -> Path:
    root = root.resolve()
    raw = root / str(relative)
    current = root
    for part in Path(str(relative)).parts:
        if part in ("", ".", ".."):
            raise ValueError("unsafe file path")
        current = current / part
        if current.is_symlink():
            raise ValueError("duplicate paths through symlinks are not movable")
    candidate = raw.resolve(strict=True)
    candidate.relative_to(root)
    if not candidate.is_file():
        raise ValueError("path is not a regular file")
    return candidate


def _quarantine_root(library: Path) -> Path:
    root = library.resolve()
    folder = root / ROOT_NAME
    if folder.is_symlink():
        raise ValueError("quarantine directory must not be a symlink")
    folder.mkdir(parents=True, exist_ok=True)
    if folder.resolve().parent != root:
        raise ValueError("quarantine directory escaped the library")
    return folder


def preview(duplicates: list[dict]) -> dict:
    groups: dict[str, list[dict]] = {}
    for row in duplicates or []:
        keeper = str(row.get("same_as") or "")
        candidate = str(row.get("file") or "")
        sha1 = str(row.get("sha1") or "").lower()
        if keeper and candidate and sha1:
            groups.setdefault(keeper, []).append({
                "file": candidate, "sha1": sha1,
                "size": int(row.get("size") or 0)})
    return {"groups": [
        {"keeper": keeper, "files": files,
         "recoverable_bytes": sum(f["size"] for f in files)}
        for keeper, files in groups.items()]}


def manifests(library: Path) -> list[dict]:
    folder = library / ROOT_NAME
    if not folder.is_dir() or folder.is_symlink():
        return []
    results = []
    for path in sorted(folder.glob("manifest-*.json")):
        try:
            data = json.loads(path.read_text("utf-8"))
        except (OSError, ValueError):
            continue
        data["manifest_id"] = path.stem.removeprefix("manifest-")
        results.append(data)
    return results


def quarantine(library: Path, platform_root: Path, candidates: list[dict],
               allowed: list[dict]) -> dict:
    if not candidates:
        raise ValueError("select at least one duplicate")
    approved = {(str(r.get("file")), str(r.get("same_as")),
                 str(r.get("sha1") or "").lower()): r for r in allowed}
    ident = uuid.uuid4().hex
    folder = _quarantine_root(library)
    object_dir = folder / ident
    manifest_path = folder / f"manifest-{ident}.json"
    entries = []
    seen = set()
    for item in candidates:
        relative = str(item.get("file") or "")
        match = next((r for (f, _, _), r in approved.items() if f == relative), None)
        if match is None:
            raise ValueError(f"duplicate is not in the latest audit: {relative}")
        keeper_rel = str(match.get("same_as") or "")
        expected = str(match.get("sha1") or "").lower()
        if relative in seen or relative == keeper_rel:
            raise ValueError(f"invalid or repeated duplicate: {relative}")
        seen.add(relative)
        prefix = platform_root.name + "/"
        if not relative.startswith(prefix) or not keeper_rel.startswith(prefix):
            raise ValueError("duplicate path is outside the audited platform")
        src = _safe_file(library, relative)
        keeper = _safe_file(library, keeper_rel)
        digest = hash_file(src).get("sha1", "").lower()
        keeper_digest = hash_file(keeper).get("sha1", "").lower()
        if digest != expected or keeper_digest != expected:
            raise ValueError(f"file changed since audit: {relative}")
        entries.append({
            "id": uuid.uuid4().hex,
            "original": str(Path(relative).relative_to(platform_root.name)),
            "keeper": str(Path(keeper_rel).relative_to(platform_root.name)),
            "sha1": expected,
            "size": src.stat().st_size, "status": "pending",
            "object": f"{ident}/{uuid.uuid4().hex}",
        })
    data = {"version": 1, "platform": platform_root.name,
            "status": "moving", "entries": entries}
    object_dir.mkdir()
    _atomic_json(manifest_path, data)
    try:
        for entry in entries:
            src = _safe_file(platform_root, entry["original"])
            dest = folder / entry["object"]
            dest.parent.mkdir(parents=True, exist_ok=True)
            if dest.exists():
                raise ValueError("quarantine destination already exists")
            os.replace(src, dest)
            entry["status"] = "quarantined"
            _atomic_json(manifest_path, data)
        data["status"] = "quarantined"
        _atomic_json(manifest_path, data)
    except Exception:
        data["status"] = "interrupted"
        _atomic_json(manifest_path, data)
        raise
    return {"ok": True, "manifest_id": ident, "entries": entries}


def restore(library: Path, manifest_id: str, entry_id: str) -> dict:
    if not manifest_id.isalnum():
        raise ValueError("invalid manifest id")
    folder = _quarantine_root(library)
    manifest_path = folder / f"manifest-{manifest_id}.json"
    data = json.loads(manifest_path.read_text("utf-8"))
    entry = next((e for e in data.get("entries", [])
                  if e.get("id") == entry_id), None)
    if entry is None:
        raise ValueError("no such quarantined file")
    if entry.get("status") == "restored":
        return {"ok": True, "already_restored": True}
    original = Path(entry["original"])
    if original.is_absolute() or ".." in original.parts:
        raise ValueError("unsafe original path in manifest")
    platform_name = str(data.get("platform") or "")
    if not platform_name or Path(platform_name).name != platform_name:
        raise ValueError("unsafe platform in quarantine manifest")
    platform_root = library / platform_name
    destination = _safe_parent(platform_root, original)
    source = folder / str(entry.get("object") or "")
    if destination.exists():
        if (not source.exists()
                and hash_file(destination).get("sha1", "").lower()
                == entry.get("sha1")):
            entry["status"] = "restored"
            _atomic_json(manifest_path, data)
            return {"ok": True, "already_restored": True}
        raise ValueError(f"restore destination already exists: {entry['original']}")
    if not source.is_file():
        raise ValueError("quarantined file is missing")
    if hash_file(source).get("sha1", "").lower() != entry.get("sha1"):
        raise ValueError("quarantined file hash changed; refusing restore")
    destination.parent.mkdir(parents=True, exist_ok=True)
    os.replace(source, destination)
    entry["status"] = "restored"
    _atomic_json(manifest_path, data)
    data["status"] = ("restored" if all(e.get("status") == "restored"
                                         for e in data["entries"])
                      else "quarantined")
    _atomic_json(manifest_path, data)
    return {"ok": True, "file": entry["original"]}


def _safe_parent(root: Path, relative: Path) -> Path:
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("unsafe restore path")
    root = root.resolve()
    current = root
    for part in relative.parts[:-1]:
        current = current / part
        if current.is_symlink():
            raise ValueError("restore path crosses a symlink")
        current.mkdir(exist_ok=True)
        current.resolve().relative_to(root)
    return current / relative.name
