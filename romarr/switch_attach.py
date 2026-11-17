"""File Switch updates and DLC inside their game's folder, where RomM looks for them.

RomM treats ``update/`` and ``dlc/`` folders inside a game's own folder as that
game's extra content. Dropped loose in the platform folder, the same file shows
up as a second game. So when the base game already lives in a folder of its
own, an update goes to ``<game>/update/`` and DLC to ``<game>/dlc/``.

The decision is deliberately narrow: exactly one bracketed title ID in the file
name, exactly one game folder holding that base title, and no link or reparse
point anywhere between the platform folder and the destination. Anything else
keeps the normal destination. The base-game search reuses the Switch report's
bounded scan, which never follows links and skips media folders.
"""
from __future__ import annotations

import os
import stat
from pathlib import Path

from .switch_report import _TID, scan_folder
from .titled import base_tid_of, classify_tid

#: Platforms whose title IDs follow the application family layout used here.
ATTACH_PLATFORMS = ("switch",)
_REPARSE_POINT = 0x400


def title_id_of(file_name: str) -> str:
    """The single bracketed 16-digit title ID in a file name, or ""."""
    found = _TID.findall(str(file_name or ""))
    return found[0].lower() if len(found) == 1 else ""


def is_real_directory_chain(root: Path, target: Path) -> bool:
    """Whether every existing step from `root` down to `target` is a plain directory.

    Missing trailing components are fine; the importer creates them. A link,
    reparse point or file anywhere on the way makes the answer False.
    """
    try:
        relative = Path(target).relative_to(Path(root))
    except ValueError:
        return False
    if not relative.parts or ".." in relative.parts or relative.is_absolute():
        return False
    current = Path(root)
    for part in relative.parts:
        current = current / part
        try:
            observed = os.lstat(current)
        except FileNotFoundError:
            return True
        except OSError:
            return False
        if (stat.S_ISLNK(observed.st_mode) or not stat.S_ISDIR(observed.st_mode)
                or getattr(observed, "st_file_attributes", 0) & _REPARSE_POINT):
            return False
    return True


def attach_dir(file_name: str, platform_dir: Path) -> Path | None:
    """Where an update or DLC belongs, or None to keep the normal destination."""
    tid = title_id_of(file_name)
    kind = classify_tid(tid) if tid else ""
    if kind not in ("update", "dlc"):
        return None
    base = base_tid_of(tid)
    scan = scan_folder(Path(platform_dir))
    folders = {Path(dump["file"]).parts[0] for dump in scan["dumps"]
               if dump["kind"] == "base" and dump["tid"] == base
               and len(Path(dump["file"]).parts) > 1}
    if len(folders) != 1:
        # None found, or the same game in two folders: guessing would scatter it.
        return None
    target = Path(platform_dir) / folders.pop() / kind
    return target if is_real_directory_chain(Path(platform_dir), target) else None
