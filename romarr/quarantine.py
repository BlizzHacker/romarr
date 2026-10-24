"""Reversible quarantine for byte-identical library duplicates.

Quarantine data and its manifest live under the library root, keeping every
move on the same filesystem. A durable manifest precedes each quarantine move;
restores never overwrite an existing name. Interrupted actions can be retried.
"""
from __future__ import annotations

import hashlib
import json
import os
import uuid
from pathlib import Path

ROOT_NAME = ".romarr-quarantine"


def _fsync_dir(folder: Path) -> None:
    # Windows does not expose directory handles through os.open.
    if os.name == "nt":
        return
    fd = os.open(folder, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _atomic_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temp.open("w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
        _fsync_dir(path.parent)
    finally:
        # Preserve the original write failure if cleanup also fails.
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass


def raw_sha1(path: Path) -> str:
    """Hash every byte, including copier headers ignored by DAT matching."""
    digest = hashlib.sha1(usedforsecurity=False)
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _fingerprint(path: Path) -> tuple:
    stat = path.stat()
    return (stat.st_dev, stat.st_ino, stat.st_size,
            stat.st_mtime_ns, stat.st_ctime_ns)


def _verify_unchanged(path: Path, expected: str) -> tuple:
    before = _fingerprint(path)
    if raw_sha1(path) != expected or _fingerprint(path) != before:
        raise ValueError(f"file changed since audit: {path.name}")
    return before


def _safe_file(root: Path, relative: str) -> Path:
    root = root.resolve()
    if not relative or Path(relative).is_absolute():
        raise ValueError("unsafe file path")
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
    _fsync_dir(root)
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
            if path.is_symlink():
                continue
            data = json.loads(path.read_text("utf-8"))
            if not isinstance(data, dict):
                continue
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
    fingerprints = {}
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
        fingerprints[relative] = _verify_unchanged(src, expected)
        _verify_unchanged(keeper, expected)
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
    _fsync_dir(folder)
    _atomic_json(manifest_path, data)
    try:
        for entry in entries:
            src = _safe_file(platform_root, entry["original"])
            dest = folder / entry["object"]
            dest.parent.mkdir(parents=True, exist_ok=True)
            if dest.exists() or dest.is_symlink():
                raise ValueError("quarantine destination already exists")
            # Rehash both immediately before each move: validating an entire
            # selection first leaves a long mutation window on large libraries.
            if _verify_unchanged(src, entry["sha1"]) != fingerprints[
                    platform_root.name + "/" + entry["original"]]:
                raise ValueError(f"file changed since audit: {entry['original']}")
            keeper = _safe_file(platform_root, entry["keeper"])
            _verify_unchanged(keeper, entry["sha1"])
            if _fingerprint(src) != fingerprints[
                    platform_root.name + "/" + entry["original"]]:
                raise ValueError(f"file changed since audit: {entry['original']}")
            os.replace(src, dest)
            _fsync_dir(dest.parent)
            _fsync_dir(src.parent)
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
    if manifest_path.is_symlink():
        raise ValueError("quarantine manifest must not be a symlink")
    data = json.loads(manifest_path.read_text("utf-8"))
    if (not isinstance(data, dict) or not isinstance(data.get("entries"), list)
            or any(not isinstance(e, dict) for e in data["entries"])):
        raise ValueError("invalid quarantine manifest")
    entry = next((e for e in data.get("entries", [])
                  if e.get("id") == entry_id), None)
    if entry is None:
        raise ValueError("no such quarantined file")
    if entry.get("status") == "restored":
        return {"ok": True, "already_restored": True}
    if not isinstance(entry.get("original"), str):
        raise ValueError("unsafe original path in manifest")
    original = Path(entry["original"])
    if original.is_absolute() or ".." in original.parts:
        raise ValueError("unsafe original path in manifest")
    platform_name = str(data.get("platform") or "")
    if (not platform_name or platform_name in (".", "..")
            or Path(platform_name).name != platform_name):
        raise ValueError("unsafe platform in quarantine manifest")
    platform_root = library.resolve() / platform_name
    if platform_root.is_symlink():
        raise ValueError("restore platform must not be a symlink")
    platform_root.mkdir(exist_ok=True)
    _fsync_dir(platform_root.parent)
    destination = _safe_parent(platform_root, original)
    object_path = Path(str(entry.get("object") or ""))
    if (object_path.is_absolute() or len(object_path.parts) != 2
            or object_path.parts[0] != manifest_id
            or not object_path.parts[1].isalnum()):
        raise ValueError("unsafe quarantine object path in manifest")
    source = _safe_parent(folder, object_path)
    if source.is_symlink():
        raise ValueError("quarantine object must not be a symlink")
    if destination.exists():
        _fsync_dir(destination.parent)
        # A crash after linking the restored name can leave both names. Only
        # finish that operation when they still identify the same inode.
        if source.exists() and source.samefile(destination):
            if raw_sha1(destination) != entry.get("sha1"):
                raise ValueError("quarantined file hash changed; refusing restore")
            source.unlink()
            _fsync_dir(source.parent)
        elif not source.exists() and raw_sha1(destination) == entry.get("sha1"):
            pass
        else:
            raise ValueError(f"restore destination already exists: {entry['original']}")
        _record_restored(manifest_path, data, entry)
        return {"ok": True, "already_restored": True}
    if not source.is_file():
        raise ValueError("quarantined file is missing")
    before = _fingerprint(source)
    if raw_sha1(source) != entry.get("sha1"):
        raise ValueError("quarantined file hash changed; refusing restore")
    if _fingerprint(source) != before:
        raise ValueError("quarantined file changed during restore")
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Creating the restored name must fail if another file appears after our
    # collision check. Link then unlink keeps one durable copy throughout.
    try:
        os.link(source, destination)
    except FileExistsError as err:
        raise ValueError(f"restore destination already exists: {entry['original']}") from err
    _fsync_dir(destination.parent)
    source.unlink()
    _fsync_dir(source.parent)
    _record_restored(manifest_path, data, entry)
    return {"ok": True, "file": entry["original"]}


def _record_restored(path: Path, data: dict, entry: dict) -> None:
    entry["status"] = "restored"
    data["status"] = ("restored" if all(e.get("status") == "restored"
                                         for e in data["entries"])
                      else "quarantined")
    _atomic_json(path, data)


def _safe_parent(root: Path, relative: Path) -> Path:
    if (not relative.parts or relative.is_absolute()
            or ".." in relative.parts):
        raise ValueError("unsafe restore path")
    root = root.resolve()
    current = root
    for part in relative.parts[:-1]:
        current = current / part
        if current.is_symlink():
            raise ValueError("restore path crosses a symlink")
        current.mkdir(exist_ok=True)
        _fsync_dir(current.parent)
        current.resolve().relative_to(root)
    destination = current / relative.name
    if destination.is_symlink():
        raise ValueError("restore path crosses a symlink")
    return destination
