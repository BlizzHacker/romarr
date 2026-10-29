"""Safe preview, verified file renames, and rollback for existing libraries."""
from __future__ import annotations

import os
import shlex
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .quarantine import (_atomic_json, _fingerprint, _fsync_dir, _safe_file,
                         _verify_unchanged, raw_sha1)

ROOT_NAME = ".romarr-renames"
MAX_FILES = 50_000
PAGE_SIZE = 5000
_JOURNAL_LOCKS_GUARD = threading.Lock()
_JOURNAL_LOCKS: dict[tuple[str, str], threading.RLock] = {}


def _journal_lock(library: Path, manifest_id: str):
    key = (str(Path(library).resolve()), str(manifest_id))
    with _JOURNAL_LOCKS_GUARD:
        return _JOURNAL_LOCKS.setdefault(key, threading.RLock())


DISC_SUFFIXES = {".cue", ".gdi", ".m3u", ".ccd", ".sub", ".mds",
                 ".mdf", ".iso", ".chd", ".cso", ".pbp", ".pkg",
                 ".nsp", ".xci", ".nca", ".nsz", ".xcz"}
ROM_SUFFIXES = {
    ".rom", ".bin", ".nes", ".fds", ".sfc", ".smc", ".gb", ".gbc",
    ".gba", ".nds", ".3ds", ".cia", ".n64", ".z64", ".v64", ".md",
    ".gen", ".sms", ".gg", ".pce", ".a26", ".a52", ".a78", ".j64",
    ".ws", ".wsc", ".ngp", ".ngc", ".vb", ".adf", ".dsk", ".tap",
    ".prg", ".d64", ".g64", ".t64", ".p", ".xex", ".atr", ".st",
    ".msa", ".ipf", ".z80", ".sna", ".cas", ".jag",
} | DISC_SUFFIXES
_RESERVED = {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$",
             *(f"COM{n}" for n in range(1, 10)),
             *(f"LPT{n}" for n in range(1, 10)),
             *(f"COM{chr(n)}" for n in (185, 178, 179)),
             *(f"LPT{chr(n)}" for n in (185, 178, 179))}


def _valid_slug(slug: str) -> bool:
    return bool(slug) and len(slug) <= 64 and slug[0].isalnum() and all(
        c.isalnum() or c in "_-" for c in slug) and slug == slug.lower()


def _safe_basename(name: str) -> str:
    invalid = set('<>:"/|?*') | {chr(n) for n in range(32)} | {chr(92)}
    device = name.split(".", 1)[0].upper()
    if (not name or name in {".", ".."} or any(c in invalid for c in name)
            or name.endswith((".", " ")) or device in _RESERVED):
        raise ValueError("unsafe canonical filename")
    return name


def _reference_paths(platform_root: Path) -> set[Path]:
    """Files named by CUE/GDI/M3U manifests; never move referenced media."""
    protected: set[Path] = set()
    manifests = [p for p in platform_root.rglob("*")
                 if p.is_file() and p.suffix.lower() in {".cue", ".gdi", ".m3u"}
                 and not any(part.startswith(".romarr-") for part in p.parts)]
    for manifest in manifests:
        try:
            lines = manifest.read_text(encoding="utf-8-sig", errors="replace").splitlines()
        except OSError as err:
            raise ValueError(
                f"Could not read reference manifest {manifest.name}: {err}") from err
        for line in lines:
            line = line.strip()
            candidate = ""
            try:
                fields = shlex.split(line)
            except ValueError:
                fields = []
            suffix = manifest.suffix.lower()
            if suffix == ".cue" and len(fields) >= 2 and fields[0].upper() == "FILE":
                candidate = fields[1]
            elif suffix == ".gdi" and len(fields) >= 2:
                candidate = fields[-2]
            elif suffix == ".m3u" and line and not line.startswith("#"):
                candidate = line.strip('"')
            if not candidate:
                continue
            ref = (manifest.parent / candidate).resolve(strict=False)
            protected.add(ref)
            if suffix == ".m3u" and ref.suffix.lower() in {".cue", ".gdi"}:
                try:
                    nested = ref.read_text(encoding="utf-8-sig",
                                           errors="replace").splitlines()
                except OSError as err:
                    raise ValueError(
                        f"Could not read referenced set manifest {ref.name}: {err}") from err
                for record in nested:
                    try:
                        fields = shlex.split(record.strip())
                    except ValueError:
                        fields = []
                    if (ref.suffix.lower() == ".cue" and len(fields) >= 2
                            and fields[0].upper() == "FILE"):
                        protected.add((ref.parent / fields[1]).resolve(strict=False))
                    elif ref.suffix.lower() == ".gdi" and len(fields) >= 2:
                        protected.add((ref.parent / fields[-2]).resolve(strict=False))
    return protected


def _sha1_index(dat_index) -> dict[str, list]:
    """Index every DAT match once; uniqueness still considers all DATs."""
    index: dict[str, list] = {}
    for dat in getattr(dat_index, "dats", ()):
        for game in getattr(dat, "games", {}).values():
            for rom in getattr(game, "roms", ()):
                key = str(getattr(rom, "sha1", "") or "").lower()
                if key:
                    index.setdefault(key, []).append((dat, game, rom))
    return index


def plan(library: Path, platform_slug: str, dat_index, *,
         scope: str = "", offset: int = 0,
         page_size: int = 500, layout: str = "flat") -> dict:
    """Hash one platform subtree and propose safe, paged single-file renames."""
    library = Path(library).resolve()
    if not _valid_slug(str(platform_slug or "")):
        raise ValueError("invalid platform")
    if offset < 0 or page_size < 1 or page_size > PAGE_SIZE:
        raise ValueError("invalid preview page")
    if str(layout or "flat").strip().lower() != "flat":
        raise ValueError(
            "Safe renames currently support flat library layouts. "
            "Switch this library to flat or keep its current names.")
    root = library / platform_slug
    if root.is_symlink() or not root.is_dir() or root.resolve().parent != library:
        raise ValueError("platform directory is unavailable or unsafe")
    scan_root = root
    scope = str(scope or "").strip()
    if scope:
        rel_scope = Path(scope)
        if (rel_scope.is_absolute() or chr(92) in scope
                or any(part in ("", ".", "..") for part in rel_scope.parts)):
            raise ValueError("scope must be a relative folder under the platform")
        scan_root = root
        for part in rel_scope.parts:
            scan_root = scan_root / part
            if scan_root.is_symlink():
                raise ValueError("scope must not pass through a symlink")
        if not scan_root.is_dir() or not scan_root.resolve().is_relative_to(root.resolve()):
            raise ValueError("scope folder is unavailable or outside the platform")
    from .dat import hash_file, VERIFIED

    protected = _reference_paths(root)
    disk_files = []
    truncated = False
    for current, dirs, files in os.walk(scan_root, followlinks=False):
        current_path = Path(current)
        dirs[:] = [d for d in dirs if not d.startswith(".romarr-")
                   and not (current_path / d).is_symlink()]
        for name in files:
            path = current_path / name
            if path.is_symlink() or path.suffix.lower() not in ROM_SUFFIXES:
                continue
            if len(disk_files) >= MAX_FILES:
                truncated = True
                break
            disk_files.append(path)
        if truncated:
            break
    disk_files.sort()
    page = disk_files[offset:offset + page_size]
    # Destinations are always direct children of the chosen platform folder.
    # Check every current top-level name, including folders, case-insensitively
    # so a library moved to Windows cannot acquire a hidden collision.
    existing_names = {child.name.casefold() for child in root.iterdir()}

    entries = []
    claims: dict[str, list[dict]] = {}
    exact = _sha1_index(dat_index)
    for path in page:
        rel = path.relative_to(library).as_posix()
        try:
            digest = hash_file(path)
            sha1 = str(digest.get("sha1") or "").lower()
            fingerprint = list(_fingerprint(path))
            size = path.stat().st_size
        except OSError as err:
            entries.append({"file": rel, "target": "", "eligible": False,
                            "reason": f"Could not read file: {err}",
                            "sha1": "", "size": 0})
            continue

        reason = ""
        target_name = ""
        raw_checksum = ""
        lookup = dat_index.lookup(**digest)
        if path.suffix.lower() in DISC_SUFFIXES:
            reason = "Disc images and their sets are left in place."
        elif path.resolve() in protected:
            reason = "A CUE, GDI or M3U file refers to this file."
        elif lookup.status != VERIFIED or not lookup.rom:
            reason = "DAT could not verify this file exactly."
        else:
            matches = exact.get(sha1, [])
            if (len(matches) != 1 or len(getattr(matches[0][1], "roms", ())) != 1
                    or matches[0][2] != lookup.rom):
                reason = "The DAT identity is ambiguous or belongs to a multi-file set."
            else:
                try:
                    raw_checksum = raw_sha1(path)
                except OSError as err:
                    reason = f"Could not read file: {err}"
                canonical = str(lookup.rom.name or "")
                try:
                    if Path(canonical).name != canonical or "/" in canonical or chr(92) in canonical:
                        raise ValueError
                    target_name = _safe_basename(Path(canonical).stem + path.suffix)
                except ValueError:
                    reason = "The DAT filename is not safe to use."
        target = root / target_name if target_name else None
        if not reason and target is not None:
            if (path.parent == root and target.name.casefold() == path.name.casefold()
                    and target.name != path.name):
                reason = "This is a case-only rename; it is skipped to avoid Windows collisions."
            elif target == path:
                reason = "Already uses the canonical filename."
            elif target.name.casefold() in existing_names:
                reason = "The destination already exists (including a case-insensitive path match)."
        row = {"file": rel,
               "target": target.relative_to(library).as_posix() if target else "",
               "sha1": raw_checksum or sha1, "size": size, "fingerprint": fingerprint,
               "eligible": not reason, "reason": reason}
        entries.append(row)
        if row["eligible"]:
            claims.setdefault(row["target"].casefold(), []).append(row)

    for duplicate_claims in claims.values():
        if len(duplicate_claims) > 1:
            for row in duplicate_claims:
                row["eligible"] = False
                row["reason"] = "More than one file proposes this destination."
    eligible = sum(bool(row["eligible"]) for row in entries)
    next_offset = offset + len(page)
    has_more = next_offset < len(disk_files) or truncated
    return {"platform": platform_slug, "scope": scope, "status": "done",
            "scanned": len(page), "candidate_count": len(disk_files),
            "eligible": eligible, "blocked": len(entries) - eligible,
            "offset": offset, "next_offset": next_offset,
            "has_more": has_more, "truncated": truncated, "entries": entries,
            "message": ("Scan stopped at the 50,000-file limit. Files beyond "
                        "this preview remain untouched; a folder scope only "
                        "scans that folder and cannot reach omitted files "
                        "elsewhere." if truncated else "")}


def _safe_destination(root: Path, relative: str) -> Path:
    root = root.resolve()
    rel = Path(relative)
    if not relative or rel.is_absolute() or any(p in ("", ".", "..") for p in rel.parts):
        raise ValueError("unsafe rename destination")
    parent = root
    for part in rel.parts[:-1]:
        parent = parent / part
        if parent.is_symlink():
            raise ValueError("rename destination traverses a symlink")
    parent.mkdir(parents=True, exist_ok=True)
    target = parent / rel.name
    if target.is_symlink():
        raise ValueError("rename destination is a symlink")
    target.resolve(strict=False).relative_to(root)
    return target


def _link_then_unlink(source: Path, target: Path, expected: str,
                      expected_fingerprint: list) -> None:
    if target.exists() or target.is_symlink():
        raise ValueError(f"destination already exists: {target.name}")
    before = list(_fingerprint(source))
    if before != list(expected_fingerprint) or raw_sha1(source) != expected:
        raise ValueError(f"file changed since preview: {source.name}")
    # link() creates the destination exclusively; unlike os.replace(), it
    # cannot silently overwrite a file created after preview.
    os.link(source, target)
    _fsync_dir(target.parent)
    # A watcher can replace or rewrite a source between preview and link.
    # Verify both names still reference the approved bytes before unlinking
    # anything; on failure, leave the source path untouched.
    # Creating a hard link updates ctime/nlink on the original inode, so
    # compare stable identity/content fields and verify the bytes again.
    if (list(_fingerprint(source))[:4] != before[:4]
            or list(_fingerprint(target))[:4] != before[:4]
            or raw_sha1(target) != expected):
        target.unlink()
        _fsync_dir(target.parent)
        raise ValueError(f"file changed during rename: {source.name}")
    # Move the source path to a unique sibling atomically, then verify the
    # object that actually left that path. A pathname check followed by
    # unlink() can delete a concurrent atomic replacement between the two.
    recovered = source.with_name(f"ROMarr-Recovery-{uuid.uuid4().hex}")
    try:
        os.rename(source, recovered)
    except OSError as err:
        raise ValueError(
            f"could not safely remove source path {source.name}: {err}") from err
    recovered_fingerprint = list(_fingerprint(recovered))
    if (recovered_fingerprint[:4] != before[:4]
            or recovered.is_symlink() or raw_sha1(recovered) != expected):
        if not recovered.is_symlink() and not source.exists() and not source.is_symlink():
            try:
                os.link(recovered, source)
                recovered.unlink()
                _fsync_dir(source.parent)
            except OSError as err:
                raise ValueError(
                    f"source changed; preserved the replacement at {recovered}") from err
            raise ValueError(
                f"source changed during rename: {source.name}; "
                "the replacement was restored and the verified target was kept")
        raise ValueError(
            f"source changed during rename: preserved the replacement at {recovered}")
    staged_target = list(_fingerprint(target))
    if (staged_target[:4] != before[:4] or raw_sha1(target) != expected):
        if not source.exists() and not source.is_symlink():
            try:
                os.link(recovered, source)
                recovered.unlink()
                _fsync_dir(source.parent)
            except OSError as err:
                raise ValueError(
                    f"destination changed; preserved source at {recovered}") from err
        raise ValueError(
            f"destination changed during rename: preserved source at {recovered}")
    recovered.unlink()
    _fsync_dir(source.parent)


def manifest_for(library: Path, manifest_id: str) -> dict | None:
    """Read one valid journal without following links."""
    import json
    if not str(manifest_id).isalnum():
        return None
    folder = Path(library) / ROOT_NAME
    path = folder / f"manifest-{manifest_id}.json"
    if folder.is_symlink() or path.is_symlink() or not path.is_file():
        return None
    try:
        data = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    data["manifest_id"] = str(manifest_id)
    return data


def manifests(library: Path) -> list[dict]:
    folder = Path(library) / ROOT_NAME
    if folder.is_symlink() or not folder.is_dir():
        return []
    out = []
    for path in folder.glob("manifest-*.json"):
        if path.is_symlink():
            continue
        ident = path.stem.removeprefix("manifest-")
        data = manifest_for(library, ident)
        if data is None:
            continue
        # Older journals did not record a creation time; retain a stable,
        # useful ordering by using their filesystem timestamp.
        if not data.get("created_at"):
            try:
                data["created_at"] = datetime.fromtimestamp(
                    path.stat().st_mtime, timezone.utc).isoformat()
            except OSError:
                data["created_at"] = ""
        out.append(data)
    out.sort(key=lambda item: str(item.get("created_at") or
                                  item.get("timestamp") or ""), reverse=True)
    return out[:10]


def apply(library: Path, platform_slug: str, selected: list[str],
          planned_entries: list[dict], *, manifest_id: str | None = None) -> dict:
    library = Path(library).resolve()
    ident = str(manifest_id or uuid.uuid4().hex)
    if not ident.isalnum():
        raise ValueError("invalid rename journal id")
    with _journal_lock(library, ident):
        return _apply_locked(library, platform_slug, selected,
                             planned_entries, ident)


def _apply_locked(library: Path, platform_slug: str, selected: list[str],
                  planned_entries: list[dict], ident: str) -> dict:
    if not selected or len(selected) != len(set(map(str, selected))):
        raise ValueError("select one or more unique files")
    library = Path(library).resolve()
    approved = {str(row.get("file")): row for row in planned_entries
                if row.get("eligible") is True}
    chosen = []
    targets = set()
    for relative in map(str, selected):
        row = approved.get(relative)
        if row is None or not row.get("target"):
            raise ValueError(f"file is not in the latest preview: {relative}")
        if str(row.get("target")).casefold() in targets:
            raise ValueError("selected files share a destination")
        targets.add(str(row["target"]).casefold())
        source = _safe_file(library, relative)
        target = _safe_destination(library, str(row["target"]))
        if target.exists() or target.is_symlink():
            raise ValueError(f"destination already exists: {target.name}")
        if list(_fingerprint(source)) != list(row.get("fingerprint") or []):
            raise ValueError(f"file changed since preview: {source.name}")
        _verify_unchanged(source, str(row.get("sha1") or ""))
        chosen.append({"id": uuid.uuid4().hex, "original": relative,
                       "target": str(row["target"]), "sha1": row["sha1"],
                       "size": int(row.get("size") or 0),
                       "fingerprint": row["fingerprint"], "status": "pending"})
    if any(not item["original"].startswith(platform_slug + "/")
           or not item["target"].startswith(platform_slug + "/") for item in chosen):
        raise ValueError("selected paths are outside the chosen platform")
    folder = library / ROOT_NAME
    if folder.is_symlink():
        raise ValueError("rename journal directory must not be a symlink")
    folder.mkdir(parents=True, exist_ok=True)
    _fsync_dir(library)
    manifest_path = folder / f"manifest-{ident}.json"
    if manifest_path.exists() or manifest_path.is_symlink():
        raise ValueError("rename journal already exists")
    data = {"version": 1, "platform": platform_slug, "status": "moving",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "entries": chosen}
    _atomic_json(manifest_path, data)
    try:
        for entry in chosen:
            source = _safe_file(library, entry["original"])
            target = _safe_destination(library, entry["target"])
            if list(_fingerprint(source)) != entry["fingerprint"]:
                raise ValueError(f"file changed since preview: {source.name}")
            _verify_unchanged(source, entry["sha1"])
            _link_then_unlink(source, target, entry["sha1"], entry["fingerprint"])
            entry["status"] = "moved"
            _atomic_json(manifest_path, data)
        data["status"] = "complete"
        _atomic_json(manifest_path, data)
    except Exception:
        data["status"] = "interrupted"
        _atomic_json(manifest_path, data)
        raise
    return {"ok": True, "manifest_id": ident, "entries": chosen}


def rollback(library: Path, manifest_id: str) -> dict:
    library = Path(library).resolve()
    if not str(manifest_id).isalnum():
        raise ValueError("invalid rename journal id")
    with _journal_lock(library, manifest_id):
        return _rollback_locked(library, manifest_id)


def _rollback_locked(library: Path, manifest_id: str) -> dict:
    import json
    if not str(manifest_id).isalnum():
        raise ValueError("invalid rename journal id")
    library = Path(library).resolve()
    folder = library / ROOT_NAME
    manifest_path = folder / f"manifest-{manifest_id}.json"
    if folder.is_symlink() or manifest_path.is_symlink() or not manifest_path.is_file():
        raise ValueError("rename journal is unavailable")
    data = json.loads(manifest_path.read_text("utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("entries"), list):
        raise ValueError("invalid rename journal")
    for entry in reversed(data["entries"]):
        if not isinstance(entry, dict):
            raise ValueError("invalid rename journal entry")
        original = _safe_destination(library, str(entry.get("original") or ""))
        target = _safe_destination(library, str(entry.get("target") or ""))
        expected = str(entry.get("sha1") or "")
        orig_exists = original.exists()
        target_exists = target.exists()
        if orig_exists and target_exists:
            expected_fingerprint = list(entry.get("fingerprint") or [])
            if (raw_sha1(original) != expected or raw_sha1(target) != expected
                    or len(expected_fingerprint) < 4
                    or list(_fingerprint(target))[:4] != expected_fingerprint[:4]):
                raise ValueError("restore collision: a file changed or appeared")
            target.unlink()
            _fsync_dir(target.parent)
        elif orig_exists:
            if raw_sha1(original) != expected:
                raise ValueError("original file changed; refusing rollback")
        elif target_exists:
            if raw_sha1(target) != expected:
                raise ValueError("renamed file changed; refusing rollback")
            os.link(target, original)
            _fsync_dir(original.parent)
            target.unlink()
            _fsync_dir(target.parent)
        else:
            raise ValueError("both original and renamed files are missing")
        entry["status"] = "rolled_back"
        _atomic_json(manifest_path, data)
    data["status"] = "rolled_back"
    _atomic_json(manifest_path, data)
    return {"ok": True, "manifest_id": manifest_id, "entries": data["entries"]}
