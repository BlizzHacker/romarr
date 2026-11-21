"""Extracting the archives Compressatorium has no mode for.

Compressatorium's only archive mode, `romz_extract`, takes a .7z/.zip holding
one cartridge ROM. Everything else a ROM library is full of -- a zipped
cue/bin, a multi-disc .7z, a usenet release split into `.part01.rar` volumes
(#224) -- has no service mode at all, so the batch extracts those here, with
the archive readers the importer already uses (`library.open_archive`: the
standard library for zip, bsdtar for 7z, 7-Zip or unar for RAR and split RAR
sets).

What is written follows the same rules the service follows for its own
outputs:

  * Nothing is overwritten. An output that already exists fails the item
    and the existing file is left exactly as it was.
  * Nothing half-written ever sits under a final name. Members are written
    into a hidden staging folder beside the destination and moved into place
    only once every member is out; a failure removes the staging folder.
  * OS and NAS clutter (`__MACOSX/`, `.DS_Store`, `Thumbs.db`...) is not
    extracted; it is not part of the game.

One payload file lands beside the archive, the way the service writes a
`romz_extract` output; several land in a folder named after the archive, so a
multi-disc set stays together and never mixes with its neighbours.
"""

from __future__ import annotations

import contextlib
import errno
import logging
import re
import shutil
import subprocess
import tempfile
import zipfile
import zlib
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from . import library

log = logging.getLogger(__name__)

#: Prefix of the staging folder; hidden so a library scan never lists it.
STAGING_PREFIX = ".romarr-partial-"

#: Archive name endings stripped to name the folder a multi-file set goes in.
_ARCHIVE_ENDINGS = re.compile(
    r"(\.part\d+\.rar|\.rar|\.\d{3}|\.7z|\.zip)$", re.IGNORECASE)

#: Everything opening, listing or reading an archive can raise.
#: A ZIP with a sound directory can still hold a corrupt (zlib.error) or
#: truncated (EOFError) member stream; neither is an OSError.
_ARCHIVE_ERRORS = (OSError, ValueError, RuntimeError, zipfile.BadZipFile,
                   zlib.error, EOFError,
                   library.MissingArchiveTool, subprocess.SubprocessError)


@dataclass
class LocalExtraction:
    """What extracting one archive produced."""

    output_files: list[str] = field(default_factory=list)
    error: str = ""
    #: Members left out because they could not be extracted safely. The
    #: output is then incomplete, so the original must never be deleted.
    skipped_members: list[str] = field(default_factory=list)


def archive_stem(name: str) -> str:
    """`Game (USA).part01.rar` -> `Game (USA)`: the set's own name."""
    stem = _ARCHIVE_ENDINGS.sub("", name)
    return stem or name


def archive_members(path: Path) -> list[str] | None:
    """The members of a .7z/.zip, or None when they cannot be listed.

    None is "do not know", never "empty": an archive no installed tool can
    list is left to the service, which gives its own answer.
    """
    try:
        source = library.open_archive(path)
        try:
            return source.names()
        finally:
            source.close()
    except _ARCHIVE_ERRORS as exc:
        log.info("decompress: cannot list %s: %s", path.name, exc)
        return None


def extract_archive(first: Path, volumes: list[Path] | None,
                    destination_root: Path) -> LocalExtraction:
    """Extract every payload member of one archive or split set.

    `volumes` is the set's volumes in order (a lone archive is its own only
    volume). Outputs go under `destination_root`; see the module docstring
    for where and on what terms.
    """
    try:
        destination_root.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=STAGING_PREFIX,
                                        dir=destination_root))
    except OSError as exc:
        return LocalExtraction(error=f"cannot write to {destination_root}: {exc}")
    try:
        staged, skipped = _extract_into(first, volumes, staging)
        if not staged:
            return LocalExtraction(error="the archive holds no game files")
        placed = _place_all(staged, staging, destination_root,
                            archive_stem(first.name))
        return LocalExtraction(output_files=placed, skipped_members=skipped)
    except FileExistsError as exc:
        return LocalExtraction(
            error=f"output already exists, nothing overwritten: {exc.filename or exc}")
    except _ARCHIVE_ERRORS as exc:
        detail = getattr(exc, "stderr", None) or str(exc)
        if isinstance(detail, bytes):
            detail = detail.decode(errors="replace")
        return LocalExtraction(error=f"extraction failed: {str(detail).strip()}")
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _extract_into(first: Path, volumes: list[Path] | None,
                  staging: Path) -> tuple[list[str], list[str]]:
    """Write every payload member into `staging`.

    Returns (written, skipped): the members written, and the payload members
    `names()` refused as unsafe -- which make the output incomplete.
    """
    source = library.open_archive(first, volumes)
    try:
        # Not every source keeps a reader to reuse; those without a session
        # simply open per call.
        session = getattr(source, "session", None)
        with session() if session else contextlib.nullcontext():
            names = source.names()
            listed = getattr(source, "all_member_names", None)
            # names() drops members it will not extract safely. The safe ones are
            # still extracted, but the caller is told what was left out, because
            # an incomplete output can still verify -- and must not then cost
            # the operator the original.
            kept = set(names)
            skipped = ([n for n in listed() if n not in kept and not library.is_clutter(n)]
                       if listed is not None else [])
            written = []
            for name in names:
                if library.is_clutter(name):
                    continue
                if not library.is_safe_name(name, staging):
                    raise ValueError(f"refusing member outside the archive: {name!r}")
                target = staging / name
                target.parent.mkdir(parents=True, exist_ok=True)
                source.copy(name, target)
                written.append(name)
            return written, skipped
    finally:
        source.close()


def _place_all(names: list[str], staging: Path, root: Path, stem: str) -> list[str]:
    """Move staged members to their final names; return the final paths.

    One member keeps its own file name beside the archive. Several keep their
    relative layout inside `root/stem`, a folder created here and never
    reused: an existing folder of that name is an existing output.
    """
    if len(names) == 1:
        target = root / PurePosixPath(names[0]).name
        _place(staging / names[0], target)
        return [str(target)]
    folder = root / stem
    folder.mkdir()  # FileExistsError when it is already there: nothing reused
    placed: list[Path] = []
    made: list[Path] = [folder]
    try:
        for name in names:
            target = folder / name
            for parent in reversed(target.relative_to(folder).parents[:-1]):
                directory = folder / parent
                if not directory.exists():
                    directory.mkdir()
                    made.append(directory)
            _place(staging / name, target)
            placed.append(target)
    except BaseException:
        # Undo only what this run put there: its own files, then its own
        # folders if they are empty. Anything another writer added since the
        # folder appeared stays, and so do the folders holding it.
        for path in reversed(placed):
            try:
                path.unlink()
            except OSError:
                pass
        for directory in reversed(made):
            try:
                directory.rmdir()
            except OSError:
                pass
        raise
    return sorted(str(p) for p in placed)


def _place(staged: Path, target: Path) -> None:
    """Move one staged file to `target`, refusing to replace anything.

    The importer's own no-clobber publish: a hard link (or renameat2 with
    RENAME_NOREPLACE where links are refused) fails when the target exists,
    so the refusal is atomic. The staging folder is removed afterwards.
    """
    try:
        library.publish_staged(staged, target, overwrite=False)
    except FileExistsError as exc:
        raise FileExistsError(errno.EEXIST, "exists", str(target)) from exc
