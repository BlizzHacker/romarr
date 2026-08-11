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
import os
import shutil
import subprocess
import zipfile
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from .dat import BAD_DUMP, UNKNOWN, VERIFIED, Match, hash_bytes
from .platforms import Platform
from .selection import pick_all_rom_sets, pick_rom_set

log = logging.getLogger(__name__)

#: Formats a finished download arrives in.
#:
#: `.zip` was the whole list, which was survivable while only cartridges were
#: supported. It is not survivable now: the live library's disc platforms are
#: overwhelmingly `.7z` -- 2,621 PlayStation entries, 2,409 PS2, 1,470 Wii --
#: and an importer that cannot open the format the content ships in supports
#: the platform on paper only.
ARCHIVE_SUFFIXES = (".zip", ".7z", ".rar")

#: Handled by the standard library, with no tool to install.
_STDLIB_ARCHIVES = (".zip",)


def _bsdtar() -> str | None:
    """A libarchive `bsdtar`, which reads 7z, rar and zip uniformly.

    `RommStreamServer` already reaches for exactly this and for the same
    reason, so this is a second user of a proven choice rather than a new
    dependency. Windows ships it as `tar.exe`; Alpine needs
    `libarchive-tools`, which the Dockerfile installs.

    GNU tar is explicitly rejected. It answers to the same name on most Linux
    systems and cannot read a 7z, so accepting it on name alone would turn a
    missing dependency into a corrupt-archive error pointing at the download.

    Every `PATH` entry is searched rather than the first hit per name, because
    a machine with both is the normal case, not a corner: Windows ships
    libarchive as `System32\\tar.exe` while Git for Windows puts GNU tar
    earlier on the same `PATH`, and a Linux box with `libarchive-tools`
    installed alongside GNU tar looks identical. Stopping at the first binary
    called `tar` finds the one that cannot do the job and concludes the job
    cannot be done.
    """
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        if not directory:
            continue
        for name in ("bsdtar", "tar"):
            path = shutil.which(name, path=directory)
            if path and _is_libarchive(path):
                return path
    return None


def _is_libarchive(path: str) -> bool:
    try:
        out = subprocess.run([path, "--version"], capture_output=True,
                             text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return False
    return "libarchive" in (out.stdout + out.stderr).lower()


@lru_cache(maxsize=1)
def bsdtar_path() -> str | None:
    """`_bsdtar()`, resolved once. `None` when no usable tool is installed."""
    return _bsdtar()


@dataclass(frozen=True)
class ImportResult:
    ok: bool
    destination: Path | None
    reason: str = ""
    #: What the DAT said about the bytes that actually landed. `unknown` when
    #: no DAT is loaded, which is the default and is not a failure.
    verification: Match = field(default_factory=lambda: Match(UNKNOWN))
    #: What the game is, once a metadata provider has been asked. Empty
    #: when none is configured -- metadata is an enhancement and its
    #: absence must never look like a failed import.
    info: dict = field(default_factory=dict)


def is_safe_name(name: str, root: Path) -> bool:
    """Whether extracting `name` under `root` stays under `root`.

    Applied to every format, not only zip. The check used to live inside the
    zip reader, so adding 7z and rar would have added two formats with no
    zip-slip protection at all -- and the format an attacker chooses is the one
    with the gap.
    """
    if not name or name.endswith(("/", "\\")):
        return False
    root_resolved = root.resolve()
    return (root_resolved / name).resolve().is_relative_to(root_resolved)


def safe_members(archive: zipfile.ZipFile, root: Path) -> list[str]:
    """Archive entries that stay inside `root` when extracted.

    A zip may contain absolute paths or `../` traversal. Extracting those writes
    outside the library -- the classic zip-slip. Anything that does not resolve
    inside root is dropped rather than sanitised, because a release that needs
    sanitising is not one to trust.
    """
    keep = []
    for name in archive.namelist():
        if name.endswith("/"):
            continue
        if is_safe_name(name, root):
            keep.append(name)
        else:
            log.warning("refusing archive entry outside root: %r", name)
    return keep


class _Source:
    """Where the files of a finished download are, and how to get at them.

    Three shapes exist -- a zip, an archive only `bsdtar` can read, and a plain
    file or directory the client already unpacked -- and each answers the same
    three questions. `pick_rom_set` needs `read` to parse a `.cue`, and only
    something that knows the shape can provide it.
    """

    def names(self) -> list[str]:
        raise NotImplementedError

    def read(self, name: str) -> bytes | None:
        raise NotImplementedError

    def copy(self, name: str, destination: Path) -> None:
        raise NotImplementedError


class _ZipSource(_Source):
    def __init__(self, path: Path):
        self.path = path

    def names(self):
        with zipfile.ZipFile(self.path) as archive:
            return safe_members(archive, self.path.parent)

    def read(self, name):
        with zipfile.ZipFile(self.path) as archive:
            return archive.read(name)

    def copy(self, name, destination):
        with zipfile.ZipFile(self.path) as archive, \
                archive.open(name) as src, open(destination, "wb") as dst:
            shutil.copyfileobj(src, dst)


class _BsdtarSource(_Source):
    """7z and rar, via libarchive.

    Every member is read by re-invoking `bsdtar` rather than unpacking the
    archive once. A disc release is a handful of files and the sheet is a few
    hundred bytes, so the cost is small -- and unpacking the whole thing means
    materialising up to twelve gigabytes to keep four of them.
    """

    def __init__(self, path: Path, tool: str):
        self.path = path
        self.tool = tool

    def _run(self, args: list[str], **kw):
        return subprocess.run([self.tool, *args], capture_output=True,
                              check=True, timeout=600, **kw)

    def names(self):
        out = self._run(["-tf", str(self.path)], text=True)
        return [n for n in (line.strip() for line in out.stdout.splitlines())
                if n and not n.endswith("/") and is_safe_name(n, self.path.parent)]

    def read(self, name):
        return self._run(["-xOf", str(self.path), name]).stdout

    def copy(self, name, destination):
        with open(destination, "wb") as dst:
            subprocess.run([self.tool, "-xOf", str(self.path), name],
                           stdout=dst, check=True, timeout=600)


class _PathSource(_Source):
    """A bare ROM file, or a directory the download client already unpacked."""

    def __init__(self, path: Path):
        self.path = path

    def names(self):
        if self.path.is_file():
            return [self.path.name]
        return [str(p.relative_to(self.path)).replace("\\", "/")
                for p in self.path.rglob("*") if p.is_file()]

    def read(self, name):
        target = self.path if self.path.is_file() else self.path / name
        try:
            return target.read_bytes()
        except OSError:
            return None

    def copy(self, name, destination):
        source = self.path if self.path.is_file() else self.path / name
        shutil.copy2(source, destination)


class MissingArchiveTool(Exception):
    """The download is in a format no installed tool can open."""


def _source_for(download: Path, platform: Platform | None = None) -> _Source:
    # For MAME, FBNeo and DOSBox the archive IS the ROM: the core opens it and
    # expects its internal layout. Looking inside would pick one chip dump out
    # of a romset and import that, which succeeds and leaves an entry no core
    # can load. Treat it as a plain file.
    if (platform is not None and platform.archive_is_the_rom
            and download.is_file()):
        return _PathSource(download)

    suffix = download.suffix.lower()
    if download.is_file() and suffix in _STDLIB_ARCHIVES:
        return _ZipSource(download)
    if download.is_file() and suffix in ARCHIVE_SUFFIXES:
        tool = bsdtar_path()
        if tool is None:
            raise MissingArchiveTool(
                f"{download.name} is a {suffix} archive and bsdtar is not "
                "installed, so its contents cannot be read. Install "
                "libarchive-tools (Debian/Alpine) or use the official ROMarr "
                "image, which ships it.")
        return _BsdtarSource(download, tool)
    return _PathSource(download)


def list_candidates(download: Path) -> list[str]:
    """Every file a completed download offers, looking inside an archive if needed."""
    return _source_for(download).names()


def verify_set(source, members, dats) -> Match:
    """What a DAT says about the files that are about to be imported.

    Verified means **every** member matched. Redump lists each track of a disc
    as its own `<rom>`, so a cue with a good checksum beside a corrupt track
    is not a good import -- and reporting the set on the strength of its first
    member is how that would be missed.

    Read from the source rather than the destination on purpose: it is the
    same bytes, and doing it here means a refusal can happen before anything
    is written.
    """
    if dats is None:
        return Match(UNKNOWN)
    verdicts = []
    for member in members:
        try:
            data = source.read(member)
        except Exception:
            return Match(UNKNOWN, detail=f"could not read {member!r} to verify")
        if data is None:
            return Match(UNKNOWN, detail=f"could not read {member!r} to verify")
        suffix = Path(member.replace("\\", "/")).suffix
        verdicts.append(dats.lookup(**hash_bytes(data, suffix=suffix)))

    if not verdicts:
        return Match(UNKNOWN)
    bad = next((v for v in verdicts if v.status == BAD_DUMP), None)
    if bad is not None:
        return bad
    if all(v.status == VERIFIED for v in verdicts):
        # Every member matched; name the game they all belong to.
        return verdicts[0]
    return Match(UNKNOWN)


#: The two directory layouts RomM understands, and every folder-based
#: frontend after it. Named for what RomM calls them in its own docs so an
#: operator can match this to the setting they already know.
#:
#:   flat  -- <root>/<platform>/<rom>            (RomM "Structure A")
#:   nested -- <root>/<platform>/roms/<rom>      (RomM "Structure B")
#:
#: A translation, when the policy keeps it beside the original, goes one
#: level deeper in a folder RomM treats as a variant rather than a second
#: game. That subfolder is the same in both layouts.
TRANSLATION_SUBDIR = "Translations"


def platform_dir(library_root: Path, platform: "Platform | str", *,
                 layout: str = "flat", translation: bool = False) -> Path:
    """Where a ROM for this platform is filed, under the chosen layout."""
    slug = getattr(platform, "slug", platform)
    base = library_root / slug
    if str(layout).lower() in ("nested", "romm_b", "b"):
        base = base / "roms"
    if translation:
        base = base / TRANSLATION_SUBDIR
    return base


def import_rom(download: Path, platform: Platform, library_root: Path, *,
               overwrite: bool = False, dats=None,
               require_verified: bool = False,
               layout: str = "flat", translation: bool = False) -> list[ImportResult]:
    """Place every ROM from a finished download into the library.

    A cartridge zip may hold several games (a 3-in-1 collection, a bundle)
    and every one is imported.  A disc lands as a directory holding every
    file of the set, which is the layout the live library already uses for
    multi-track rips.

    `layout` picks the directory shape (see `platform_dir`); `translation`
    files the set in the platform's Translations subfolder, for the
    keep-both case where a T-En patch sits beside the original dump.
    """
    if not download.exists():
        msg = (
            f"download path does not exist in this container: {download}. "
            "Mount the download client's completed directory at that exact "
            "path, or add a remote path mapping under Settings -> Media "
            "Management.")
        return [ImportResult(False, None, msg)]

    try:
        source = _source_for(download, platform)
        candidates = source.names()
    except MissingArchiveTool as exc:
        return [ImportResult(False, None, str(exc))]

    chosen_sets = pick_all_rom_sets(candidates, platform, read=source.read)
    if not chosen_sets:
        return [ImportResult(
            False, None,
            f"no {platform.name} ROM among {len(candidates)} file(s)")]

    results: list[ImportResult] = []
    for chosen in chosen_sets:
        verdict = verify_set(source, chosen.members, dats)
        if require_verified and verdict.status != VERIFIED:
            results.append(ImportResult(
                False, None,
                f"refused: {verdict.detail or verdict}",
                verification=verdict))
            continue

        target_dir = platform_dir(library_root, platform,
                                   layout=layout, translation=translation)

        if not chosen.is_multi_file:
            target_dir.mkdir(parents=True, exist_ok=True)
            destination = target_dir / Path(chosen.primary).name
            if destination.exists() and not overwrite:
                results.append(
                    ImportResult(False, destination, "already in the library"))
                continue
            source.copy(chosen.primary, destination)
            log.info("imported %s -> %s", chosen.primary, destination)
            results.append(ImportResult(True, destination,
                                        verification=verdict))
            continue

        # A digital set is named after the download, not its primary: every
        # repack's installer is called setup.exe, and a library of
        # directories all named "setup" is a library of nothing.
        set_name = (download.stem if platform.media == "digital"
                    else _set_name(chosen.primary))
        destination = target_dir / set_name
        if destination.exists() and any(destination.iterdir()) and not overwrite:
            results.append(
                ImportResult(False, destination, "already in the library"))
            continue
        destination.mkdir(parents=True, exist_ok=True)

        written: dict[str, str] = {}
        for member in chosen.members:
            name = Path(member.replace("\\", "/")).name
            if not is_safe_name(name, destination):
                log.warning("refusing set member with an unusable name: %r",
                            member)
                continue
            if name in written:
                results.append(ImportResult(
                    False, destination,
                    f"two files in this download are both called {name!r} "
                    f"({written[name]} and {member}); refusing to guess which "
                    "one the game needs"))
                break
            source.copy(member, destination / name)
            written[name] = member
        else:
            log.info("imported %d files -> %s", len(written), destination)
            results.append(ImportResult(True, destination,
                                        verification=verdict))

    return results


def _set_name(primary: str) -> str:
    """The directory name a multi-file game gets.

    The primary's stem, because that is the name the sheet carries and the one
    a person recognises. `102 Dalmatians ….gdi` and its five `trackNN.bin`
    files become `102 Dalmatians …/`.
    """
    return Path(primary.replace("\\", "/")).stem


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
        return _checked(Path(text), text, mapped=False)
    remote, local = best
    rest = text[len(remote):].lstrip("/\\")
    return _checked(Path(local) / rest if rest else Path(local), text, mapped=True)


def _checked(result: Path, reported: str, *, mapped: bool) -> Path:
    """Warn, once translation is done, if the result is not openable here.

    This is the only point that knows both paths, and the difference between
    them is the whole diagnosis. Without it the operator sees a download that
    completed and never imported, and nothing that names the container path
    ROMarr actually tried -- which is the one string that makes a wrong volume
    mount obvious.

    A warning rather than a raise: the caller reports the failure per download,
    and one unopenable path must not stop the others importing.
    """
    if not reported or result.exists():
        return result
    if mapped:
        log.warning(
            "download client reported %s, which a remote path mapping turns "
            "into %s -- and that does not exist here. Check the mapping's local "
            "side against what is really mounted.", reported, result)
    else:
        log.warning(
            "download client reported %s, which does not exist here and no "
            "remote path mapping covers it. Mount the client's completed "
            "directory at that exact path, or add a mapping under Settings -> "
            "Media Management.", reported)
    return result
