"""Read-only Switch update/DLC gaps from local filename tags and TitleDB metadata.

Files are never opened,
imported, renamed or extracted. Unknown metadata and incomplete scans stay visible.
"""
from __future__ import annotations

import os
import re
import stat
import time
from collections import defaultdict
from pathlib import Path

from .file_reports import _PinnedLibrary
from .titled import TitleDBIndex, base_tid_of, classify_tid

_EXTENSIONS = {".nsp", ".nsz", ".xci", ".xcz", ".nup"}
_TID = re.compile(r"[\[(]([0-9a-f]{16})[\])]", re.I)
_VERSION = re.compile(r"\[v([0-9]+)\]", re.I)
_SKIP = {"covers", "images", "screenshots", "manuals", "media", "3dboxes", "soundtracks", "soundtrack"}
MAX_ENTRIES = 100_000
MAX_DEPTH = 24
SCAN_SECONDS = 3.0


def _dump(name: str, relative: str) -> dict | None:
    """Parse a supported filename without treating extension words as formats."""
    if Path(name).suffix.lower() not in _EXTENSIONS:
        return None
    matches = _TID.findall(name)
    if len(matches) != 1 or classify_tid(matches[0]) not in ("base", "update", "dlc"):
        return None
    version = _VERSION.search(name)
    value = None
    if version and len(version.group(1)) <= 10:
        number = int(version.group(1))
        if number <= 0xFFFFFFFF:
            value = number
    return {"tid": matches[0].lower(), "kind": classify_tid(matches[0]),
            "version": value, "file": relative}


def scan_folder(root: Path, *, deadline: float | None = None, max_entries: int = MAX_ENTRIES) -> dict:
    """Walk pinned directories without following links, quarantine or internal data."""
    root = Path(root)
    result = {"dumps": [], "examined": 0, "skipped_links": 0, "warnings": [], "complete": True}
    deadline = deadline if deadline is not None else time.monotonic() + SCAN_SECONDS
    if not root.exists() and not root.is_symlink():
        return result
    try:
        with _PinnedLibrary(root) as pins:
            identity = pins.root_id
        pending = [("", 0)]
        while pending:
            if time.monotonic() >= deadline or result["examined"] >= max_entries:
                result["complete"] = False
                result["warnings"].append("scan limit reached")
                break
            relative, depth = pending.pop()
            if depth > MAX_DEPTH:
                result["complete"] = False
                result["warnings"].append("directory depth limit reached")
                continue
            frame, children = [], []
            try:
                # Closing per directory bounds descriptors even on very large libraries.
                with _PinnedLibrary(root, expected=identity) as pins:
                    descriptor, folder, _ = pins.parent((relative + "/" if relative else "") + ".scan")
                    with os.scandir(folder if pins.windows else descriptor) as entries:
                        for entry in entries:
                            if result["examined"] >= max_entries or time.monotonic() >= deadline:
                                result["complete"] = False
                                break
                            result["examined"] += 1
                            if entry.name.startswith(".") or entry.name.casefold() in _SKIP:
                                continue
                            observed = entry.stat(follow_symlinks=False)
                            if stat.S_ISLNK(observed.st_mode) or getattr(observed, "st_file_attributes", 0) & 0x400:
                                result["skipped_links"] += 1
                                continue
                            member = str(Path(relative) / entry.name).replace("\\", "/")
                            if stat.S_ISDIR(observed.st_mode):
                                children.append((member, depth + 1))
                            elif stat.S_ISREG(observed.st_mode):
                                dump = _dump(entry.name, member)
                                if dump:
                                    frame.append(dump)
                    pins.validate()
                result["dumps"].extend(frame)
                pending.extend(children)
            except (OSError, ValueError):
                result["complete"] = False
                result["warnings"].append("directory unavailable or changed during scan")
    except (OSError, ValueError):
        result["complete"] = False
        result["warnings"].append("root unavailable, changed or linked")
    if not result["complete"] and not result["warnings"]:
        result["warnings"].append("scan limit reached")
    result["warnings"] = sorted(set(result["warnings"]))
    return result


def _latest(title) -> int | None:
    """Keep missing or malformed version histories unknown instead of guessing zero."""
    if title is None or not isinstance(title.versions, dict):
        return None
    versions = [int(value) for value in title.versions
                if re.fullmatch(r"[0-9]{1,10}", str(value)) and int(value) <= 0xFFFFFFFF]
    return max(versions) if versions else None


def report(roots: dict[str, Path], index: TitleDBIndex | object | None) -> dict:
    """Group loose/foldered files by application family and compare known metadata."""
    deadline = time.monotonic() + SCAN_SECONDS
    scans = {slug: scan_folder(path, deadline=deadline) for slug, path in roots.items()}
    groups = defaultdict(list)
    for slug, scan in scans.items():
        for dump in scan["dumps"]:
            groups[(slug, base_tid_of(dump["tid"]))].append(dump)
    # An empty local shelf never triggers a large metadata download.
    index = index() if groups and callable(index) else (None if callable(index) else index)
    families = defaultdict(list)
    if index is not None:
        for title in index.by_id.values():
            if title.kind == "dlc":
                families[base_tid_of(title.title_id)].append(title)
    items = []
    for (slug, base), dumps in sorted(groups.items()):
        bases = [dump for dump in dumps if dump["kind"] == "base"]
        updates = [dump for dump in dumps if dump["kind"] == "update"]
        addons = {dump["tid"] for dump in dumps if dump["kind"] == "dlc"}
        known_versions = [dump["version"] for dump in updates + bases if dump["version"] is not None]
        installed = max(known_versions) if known_versions else None
        base_title = index.lookup_by_id(base) if index is not None and slug != "switch-2" else None
        update_title = index.lookup_by_id(f"{int(base, 16) + 0x800:016x}") if index is not None and slug != "switch-2" else None
        latest = _latest(update_title)
        if latest is None:
            latest = _latest(base_title)
        state = "unknown"
        if latest is not None:
            if installed is not None:
                state = "current" if installed >= latest else ("outdated" if updates else "missing")
            elif not updates and latest > 0:
                state = "missing"
        catalog = families.get(base, []) if slug != "switch-2" else []
        missing = [{"tid": title.title_id.upper(), "name": title.name}
                   for title in sorted(catalog, key=lambda title: title.title_id) if title.title_id.lower() not in addons]
        named_scope = bool(base_title and base_title.region and index and index.names_loaded)
        first = (bases or dumps)[0]["file"]
        items.append({"platform": slug, "base_tid": base.upper(),
                      "game": Path(first).parts[0] if len(Path(first).parts) > 1 else Path(first).stem,
                      "name": base_title.name if base_title else "", "base_present": bool(bases),
                      "installed_version": installed, "latest_version": latest, "update": state,
                      "files": sorted(dump["file"] for dump in dumps),
                      "dlc_installed": len(addons), "dlc_missing": missing,
                      "dlc_total": len({title.title_id.lower() for title in catalog} | addons) if named_scope else None,
                      "dlc_complete_list": False,
                      "catalog_region": index.region if index and index.names_loaded and slug != "switch-2" else ""})
    return {"items": items, "scans": {slug: {key: value for key, value in scan.items() if key != "dumps"} for slug, scan in scans.items()},
            "summary": {"games": len(items),
                        "update_gaps": sum(row["update"] in ("missing", "outdated") for row in items),
                        "dlc_gaps": sum(len(row["dlc_missing"]) for row in items),
                        "games_with_gaps": sum(row["update"] in ("missing", "outdated") or bool(row["dlc_missing"]) for row in items),
                        "unknown_to_titledb": sum(row["update"] == "unknown" for row in items),
                        "dlc_lists_complete": False, "titledb_titles": len(index) if index is not None else 0,
                        "scan_complete": all(scan["complete"] for scan in scans.values())}}
