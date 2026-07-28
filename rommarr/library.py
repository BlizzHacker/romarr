"""Getting a finished download into RomM's library.

RomM reads a directory tree: `<library>/<platform-slug>/<rom file>`. Nothing
more clever than that is required, which is why this module is small — but the
details it does handle are the ones that silently corrupt a library:

  * archives (game releases are usually zipped or 7z'd, not bare ROMs)
  * choosing the ROM among the readmes and box art
  * never overwriting an existing ROM without being told to
  * never writing outside the library root, even if an archive contains
    `../../etc/passwd` — a real and old attack against every extractor
"""

from __future__ import annotations

import logging
import shutil
import zipfile
from dataclasses import dataclass
from pathlib import Path

from .platforms import Platform
from .selection import pick_rom_file

log = logging.getLogger(__name__)

ARCHIVE_SUFFIXES = (".zip",)


@dataclass(frozen=True)
class ImportResult:
    ok: bool
    destination: Path | None
    reason: str = ""


def safe_members(archive: zipfile.ZipFile, root: Path) -> list[str]:
    """Archive entries that stay inside `root` when extracted.

    A zip may contain absolute paths or `../` traversal. Extracting those writes
    outside the library — the classic zip-slip. Anything that does not resolve
    inside root is dropped rather than sanitised, because a release that needs
    sanitising is not one to trust.
    """
    keep: list[str] = []
    root_resolved = root.resolve()
    for name in archive.namelist():
        if name.endswith("/"):
            continue
        target = (root_resolved / name).resolve()
        if target.is_relative_to(root_resolved):
            keep.append(name)
        else:
            log.warning("refusing archive entry outside root: %r", name)
    return keep


def list_candidates(download: Path) -> list[str]:
    """Every file a completed download offers, looking inside a zip if needed."""
    if download.is_file() and download.suffix.lower() in ARCHIVE_SUFFIXES:
        with zipfile.ZipFile(download) as archive:
            return safe_members(archive, download.parent)
    if download.is_file():
        return [download.name]
    return [str(p.relative_to(download)) for p in download.rglob("*") if p.is_file()]


def import_rom(download: Path, platform: Platform, library_root: Path, *,
               overwrite: bool = False) -> ImportResult:
    """Place the ROM from a finished download into RomM's library."""
    if not download.exists():
        return ImportResult(False, None, f"download path does not exist: {download}")

    candidates = list_candidates(download)
    chosen = pick_rom_file(candidates, platform)
    if chosen is None:
        return ImportResult(
            False, None,
            f"no {platform.name} ROM among {len(candidates)} file(s)",
        )

    target_dir = library_root / platform.slug
    target_dir.mkdir(parents=True, exist_ok=True)
    destination = target_dir / Path(chosen).name

    if destination.exists() and not overwrite:
        return ImportResult(False, destination, "already in the library")

    if download.is_file() and download.suffix.lower() in ARCHIVE_SUFFIXES:
        with zipfile.ZipFile(download) as archive, \
                archive.open(chosen) as src, \
                open(destination, "wb") as dst:
            shutil.copyfileobj(src, dst)
    else:
        source = download if download.is_file() else download / chosen
        shutil.copy2(source, destination)

    log.info("imported %s -> %s", chosen, destination)
    return ImportResult(True, destination)


def map_remote_path(path, mappings):
    """Translate a download client's path into one this process can open.

    The client reports paths in ITS filesystem. When it runs in a different
    container the same file has a different path here -- or the volume is not
    mounted at all, which is a mount problem a mapping cannot paper over, and
    the caller finds that out because the translated path still does not exist.

    The longest matching prefix wins, so a specific mapping can override a
    broader one rather than depending on which was added first.
    """
    text = str(path)
    best = None
    for entry in mappings or []:
        remote = str(entry.get("remote", "")).rstrip("/\\")
        local = str(entry.get("local", "")).rstrip("/\\")
        if not remote or not local:
            continue
        if text == remote or text.startswith(remote + "/") or text.startswith(remote + "\\"):
            if best is None or len(remote) > len(best[0]):
                best = (remote, local)
    if best is None:
        return Path(text)
    remote, local = best
    rest = text[len(remote):].lstrip("/\\")
    return Path(local) / rest if rest else Path(local)
