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

import json
import logging
import errno
import stat
import os
import re
import shutil
import subprocess
import threading
import tempfile
import time
import zipfile
from contextlib import ExitStack
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from .dat import BAD_DUMP, UNKNOWN, VERIFIED, Match, hash_bytes, hash_stream
from .platforms import Platform, resolve, romm_fs_slug
from .nested_zip import (MAX_OUTER_ZIP_MEMBERS, NestedZipIndex, StagingBudget,
                         check_zip_central_directory)
from .selection import (_basename, _cue_and_gdi_names, _names_in_sheet,
                        _parent, pick_all_rom_sets, pick_rom_set)

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
class RarTool:
    """An installed program that reads RAR archives, split sets included.

    `kind` is `"7z"` (7-Zip; `path` is the program) or `"unar"` (`path` is
    `unar`, `lister` is its companion `lsar`).
    """

    kind: str
    path: str
    lister: str = ""


def _reads_rar(path: str) -> bool:
    """Whether this 7-Zip build carries the RAR decoders.

    Some distributions build 7-Zip without its RAR code because the RAR
    licence is not free: Alpine's `7zip` is built with `DISABLE_RAR`, and
    Debian's lists the RAR format but ships the decoders separately in
    `7zip-rar` (non-free). Such a build answers to the same name, and the
    format line alone says nothing, so the `Rar2`/`Rar3`/`Rar5` codecs in
    `7z i` are what is checked.
    """
    try:
        out = subprocess.run([path, "i"], capture_output=True, text=True,
                             timeout=10, stdin=subprocess.DEVNULL, check=False)
    except (OSError, subprocess.SubprocessError):
        return False
    codecs = set(re.findall(r"\bRar\d\b", out.stdout))
    return {"Rar2", "Rar3", "Rar5"} <= codecs


def rar_tools() -> list[RarTool]:
    """Every installed RAR reader, best first: 7-Zip with RAR support, then unar."""
    tools = []
    candidates = [shutil.which(name) for name in ("7zz", "7z")]
    if os.name == "nt":
        for env_var in ("ProgramFiles", "ProgramFiles(x86)"):
            base = os.environ.get(env_var)
            if base:
                candidates.append(os.path.join(base, "7-Zip", "7z.exe"))
    seen: set[str] = set()
    for path in candidates:
        if not path or not os.path.isfile(path):
            continue
        key = os.path.normcase(os.path.abspath(path))
        if key in seen:
            continue
        seen.add(key)
        if _reads_rar(path):
            tools.append(RarTool("7z", path))
            break
    unar, lsar = shutil.which("unar"), shutil.which("lsar")
    if unar and lsar:
        tools.append(RarTool("unar", unar, lsar))
    return tools


def _rar_tool() -> RarTool | None:
    """The best installed RAR reader, or None."""
    tools = rar_tools()
    return tools[0] if tools else None


@lru_cache(maxsize=1)
def rar_tool() -> RarTool | None:
    """`_rar_tool()`, resolved once. `None` when only bsdtar (or nothing) can read RAR.

    libarchive reads one RAR volume at a time, so it cannot join a release
    split into `.part1.rar`/`.r00`/`.001` volumes, and it refuses some RAR
    data (some solid archives, filters inside PPMd blocks, some older data).
    7-Zip and unar read split sets, so a `.rar` goes to one of them whenever
    one is installed; 7-Zip is preferred.
    """
    return _rar_tool()


_RAR4_SIGNATURE = b"Rar!\x1a\x07\x00"
_RAR5_SIGNATURE = b"Rar!\x1a\x07\x01\x00"


def _rar_signature(path: Path) -> bytes:
    """The RAR signature `path` starts with, or `b""` if it is not a RAR."""
    try:
        with open(path, "rb") as handle:
            head = handle.read(8)
    except OSError:
        return b""
    for signature in (_RAR5_SIGNATURE, _RAR4_SIGNATURE):
        if head.startswith(signature):
            return signature
    return b""


def _rar_volume_key(path: Path) -> tuple[tuple[str, str], int] | None:
    """`((scheme, stem), number)` when `path` is named like a RAR volume.

    Volume numbers count from 1 in archive order. The three schemes scene
    releases use are `name.part01.rar`, the older `name.rar` + `name.r00` ..
    `name.r99` + `name.s00` .., and `name.001` (a RAR renamed to the generic
    split-file naming). A `.001` is only a RAR volume if it starts with a RAR
    signature, which `rar_sets` checks; the name alone says nothing.
    """
    name = path.name.lower()
    match = re.fullmatch(r"(.+)\.part(\d+)\.rar", name)
    if match:
        return ("part", match[1]), int(match[2])
    if name.endswith(".rar"):
        return ("old", name[:-4]), 1
    match = re.fullmatch(r"(.+)\.([r-z])(\d\d)", name)
    if match:
        return ("old", match[1]), 2 + (ord(match[2]) - ord("r")) * 100 + int(match[3])
    match = re.fullmatch(r"(.+)\.(\d{3})", name)
    if match and int(match[2]) > 0:
        return ("num", match[1]), int(match[2])
    return None


def rar_sets(files) -> tuple[dict[Path, list[Path]], set[Path]]:
    """Group RAR volumes among `files` (paths in one directory tree).

    Returns `(sets, later)`: `sets` maps each set's first volume to every
    volume in order (a lone `.rar` maps to itself), and `later` holds the
    volumes after the first, which must be read through the first, never on
    their own: bsdtar lists a later RAR 4 volume as holding the same file
    again, and a later RAR 5 volume as empty. A numbered set counts only when
    its `.001` starts with a RAR signature and its numbers run from 1 without
    a gap; a set with a gap is left alone, so the import fails on the missing
    part as before.
    """
    groups: dict[tuple[Path, tuple[str, str]], dict[int, Path]] = {}
    for path in files:
        key = _rar_volume_key(path)
        if key is None:
            continue
        (scheme, stem), number = key
        groups.setdefault((path.parent, (scheme, stem)), {})[number] = path
    sets: dict[Path, list[Path]] = {}
    later: set[Path] = set()
    for (_folder, (scheme, _stem)), volumes in groups.items():
        numbers = sorted(volumes)
        if numbers != list(range(1, len(numbers) + 1)):
            continue
        ordered = [volumes[n] for n in numbers]
        if scheme == "num" and not _rar_signature(ordered[0]):
            continue
        if scheme == "old" and ordered[0].suffix.lower() != ".rar":
            continue
        sets[ordered[0]] = ordered
        later.update(ordered[1:])
    return sets, later


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
    import_method: str = ""
    fallback_reason: str = ""


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

    def close(self) -> None:
        """Release temporary archive state when this source owns any."""
        return None

    def names(self) -> list[str]:
        raise NotImplementedError

    def rom_sets(self, platform):
        """Select ROM sets from the members this source exposes."""
        return pick_all_rom_sets(self.names(), platform, read=self.read)

    def read(self, name: str) -> bytes | None:
        raise NotImplementedError

    def copy(self, name: str, destination: Path) -> None:
        raise NotImplementedError

    def hash(self, name: str, suffix: str = "") -> dict | None:
        """`hash_bytes` for one member, streamed rather than read whole.

        Verifying a disc set used to `read` every member into memory: a PS3
        image is tens of gigabytes, and a multi-disc archive took a 92 GB host
        to its OOM killer. The shapes below stream; this fallback keeps any
        other `_Source` working as before.
        """
        data = self.read(name)
        return None if data is None else hash_bytes(data, suffix=suffix)


class _ZipSource(_Source):
    def __init__(self, path: Path):
        self.path = path
        self.prefer_hardlinks = False
        self.last_transfer = ("copy", "")
        self.transfers = {}

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

    def hash(self, name, suffix=""):
        """Hash one member as it decompresses, a chunk at a time."""
        with zipfile.ZipFile(self.path) as archive, archive.open(name) as src:
            return hash_stream(src, suffix=suffix)


#: bsdtar treats every member argument as a shell-style glob: `[` opens a
#: character class, `*`/`?` are wildcards, `\` escapes. Scene names are full of
#: brackets -- every Switch release ships as
#: `Title [010012101468C000][v0][US].nsp` -- and the literal member name can
#: never match the pattern it parses as, so extraction reports "Not found in
#: archive" for a member that `-tf` lists fine. Backslash-escaping every
#: metacharacter makes the pattern mean exactly the name.
_GLOB_METACHARS = re.compile(r"([*?[\]\\])")


def bsdtar_pattern(name: str) -> str:
    """Escape glob metacharacters so bsdtar matches `name` literally."""
    return _GLOB_METACHARS.sub(r"\\\1", name)


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
        return subprocess.run(args, capture_output=True, check=True,
                              timeout=600, stdin=subprocess.DEVNULL, **kw)

    def _list_command(self) -> list[str]:
        """The command that lists the members, one per line."""
        return [self.tool, "-tf", str(self.path)]

    def _list(self, stdout: str) -> list[str]:
        """Member names from the list command's output."""
        return [line.strip() for line in stdout.splitlines()]

    def _extract_command(self, name: str) -> list[str]:
        """The command that writes member `name`, and nothing else, to stdout."""
        return [self.tool, "-xOf", str(self.path), bsdtar_pattern(name)]

    def _check_size(self, name: str, written: int) -> None:
        """Raise if `written` bytes cannot be the whole of member `name`.

        bsdtar exits non-zero when it cannot finish a member, so there is
        nothing to check here; tools that exit 0 on a short read override it.
        """

    #: Text encoding of the list command's output; None is the locale's.
    _list_encoding: str | None = None

    def names(self):
        out = self._run(self._list_command(), text=True, encoding=self._list_encoding)
        return [n for n in self._list(out.stdout)
                if n and not n.endswith("/") and is_safe_name(n, self.path.parent)]

    def read(self, name):
        data = self._run(self._extract_command(name)).stdout
        self._check_size(name, len(data))
        return data

    def copy(self, name, destination):
        with open(destination, "wb") as dst:
            subprocess.run(self._extract_command(name), stdout=dst, check=True,
                           timeout=600, stdin=subprocess.DEVNULL)
        self._check_size(name, Path(destination).stat().st_size)

    #: Seconds `bsdtar` may go without producing a byte before `hash` gives
    #: up on the member. Not a deadline on the whole read: a 40 GB disc off a
    #: spinning array legitimately takes longer than `_run`'s 600 seconds, and
    #: failing it would undo the point of streaming. A stall is what hangs.
    STALL_TIMEOUT = 600

    def hash(self, name, suffix=""):
        """Hash one member as `bsdtar` streams it out of the archive.

        A watchdog kills `bsdtar` if it produces nothing for `STALL_TIMEOUT`
        seconds; the stall surfaces as `TimeoutError`, which `verify_set`
        reports as UNKNOWN rather than blocking the import forever.
        """
        stalled = threading.Event()
        finished = threading.Event()
        last_byte = [time.monotonic()]
        written = [0]
        with subprocess.Popen(self._extract_command(name),
                              stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL,
                              stdin=subprocess.DEVNULL) as proc:

            class _Progress:
                """`proc.stdout` that records when data last arrived."""

                def read(self, size):
                    block = proc.stdout.read1(size)
                    last_byte[0] = time.monotonic()
                    written[0] += len(block)
                    return block

            def watchdog():
                while not finished.wait(1):
                    if time.monotonic() - last_byte[0] > self.STALL_TIMEOUT:
                        stalled.set()
                        if proc.poll() is None:
                            try:
                                proc.kill()
                            except OSError:
                                # It may have exited between poll() and kill().
                                pass
                        return

            guard = threading.Thread(target=watchdog, daemon=True)
            guard.start()
            exited_after_eof = False
            try:
                hashes = hash_stream(_Progress(), suffix=suffix)
                try:
                    # EOF only means stdout closed. Keep the child bounded too:
                    # a broken helper can close stdout and then hang forever.
                    proc.wait(timeout=self.STALL_TIMEOUT)
                except subprocess.TimeoutExpired:
                    exited_after_eof = True
                    if proc.poll() is None:
                        proc.kill()
                    proc.wait()
            except BaseException:
                if proc.poll() is None:
                    proc.kill()
                proc.wait()
                raise
            finally:
                finished.set()
                guard.join()
        if exited_after_eof:
            raise TimeoutError(f"{Path(self.tool).name} did not exit within {self.STALL_TIMEOUT}s "
                               f"after finishing {name!r}")
        if stalled.is_set():
            raise TimeoutError(f"{Path(self.tool).name} produced nothing for {self.STALL_TIMEOUT}s "
                               f"while extracting {name!r}")
        if proc.returncode:
            raise subprocess.CalledProcessError(proc.returncode, proc.args)
        self._check_size(name, written[0])
        return hashes


class _RarToolSource(_BsdtarSource):
    """A RAR archive, split or not, read with 7-Zip or unar.

    `volumes` is every volume in order, the first being `path`; both tools
    find the later volumes themselves from the first one's name. unar does
    not recognise a RAR named `name.001`, so for that naming it is handed
    symlinks under the names RAR itself would have used, in a temporary
    directory this source removes on `close`.

    Both tools exit 0 for some short reads (unar when a later volume is
    missing, 7-Zip when no member matched), so every extraction is checked
    against the size the listing reported.
    """

    #: 7-Zip is told to list in UTF-8 (-sccUTF-8) and lsar writes JSON in UTF-8.
    _list_encoding = "utf-8"

    def __init__(self, path: Path, tool: RarTool, volumes: list[Path] | None = None):
        super().__init__(path, tool.path)
        self.rar = tool
        self.volumes = volumes or [path]
        self._sizes: dict[str, int] = {}
        self._indexes: dict[str, int] = {}
        self._staged: str | None = None
        self.archive = path
        key = _rar_volume_key(path)
        # unar opens a lone `.001` by its contents; only a set needs RAR's names.
        if tool.kind == "unar" and len(self.volumes) > 1 and key and key[0][0] == "num":
            try:
                self.archive = self._stage_rar_names()
            except OSError as exc:
                self.close()
                raise RuntimeError(
                    f"{path.name}: unar needs the volumes under RAR's own names, and "
                    f"they could not be linked in a temporary folder ({exc})") from exc

    def _stage_rar_names(self) -> Path:
        """Symlink each `.NNN` volume under its RAR name; return the first.

        Raises OSError when symlinks cannot be made (Windows without the
        privilege); the caller removes the folder.
        """
        self._staged = tempfile.mkdtemp(prefix="romarr-rar-")
        first = self.volumes[0]
        signature = _rar_signature(first)
        try:
            with open(first, "rb") as handle:
                head = handle.read(13)
        except OSError:
            head = b""
        # RAR 4 main header flag 0x0010 says the volumes are named name.partN.rar;
        # without it RAR expects name.rar, name.r00, ... RAR 5 always uses partN.
        part_names = (signature == _RAR5_SIGNATURE
                      or (len(head) >= 12 and head[7 + 2] == 0x73
                          and int.from_bytes(head[10:12], "little") & 0x0010))
        width = max(2, len(str(len(self.volumes))))
        for number, volume in enumerate(self.volumes, start=1):
            if part_names:
                staged = f"set.part{number:0{width}d}.rar"
            elif number == 1:
                staged = "set.rar"
            else:
                letter = chr(ord("r") + (number - 2) // 100)
                staged = f"set.{letter}{(number - 2) % 100:02d}"
            os.symlink(volume.resolve(), os.path.join(self._staged, staged))
        return Path(self._staged) / (f"set.part{1:0{width}d}.rar" if part_names else "set.rar")

    def close(self) -> None:
        """Remove the staged symlinks, if any."""
        if self._staged:
            shutil.rmtree(self._staged, ignore_errors=True)
            self._staged = None

    def _type_switch(self) -> list[str]:
        """7-Zip reads a `.001` as a generic split file unless told which RAR it is.

        `-trar` selects only the RAR 4 reader, so a RAR 5 set needs `-trar5`.
        """
        if self.path.suffix.lower() == ".rar":
            return []
        return ["-trar5"] if _rar_signature(self.path) == _RAR5_SIGNATURE else ["-trar"]

    def _list_command(self) -> list[str]:
        """7-Zip's technical listing, or lsar's JSON listing, of the whole set."""
        if self.rar.kind == "7z":
            return [self.rar.path, "l", "-slt", "-ba", "-sccUTF-8", *self._type_switch(),
                    "--", str(self.archive)]
        return [self.rar.lister, "-j", str(self.archive)]

    def _list(self, stdout: str) -> list[str]:
        """Parse 7-Zip's technical listing or lsar's JSON, keeping sizes and indexes."""
        names = []
        if self.rar.kind == "7z":
            entry: dict[str, str] = {}
            for line in [*stdout.splitlines(), ""]:
                if line.strip():
                    key, _, value = line.partition(" = ")
                    entry[key.strip()] = value
                    continue
                if entry.get("Path") and entry.get("Folder") != "+":
                    names.append(entry["Path"])
                    if entry.get("Size", "").isdigit():
                        self._sizes[entry["Path"]] = int(entry["Size"])
                entry = {}
            return names
        listing = json.loads(stdout or "{}")
        for item in listing.get("lsarContents", []):
            name = item.get("XADFileName")
            if not name or item.get("XADIsDirectory"):
                continue
            if not isinstance(item.get("XADIndex"), int):
                continue  # unar extracts by index; an entry without one cannot be read
            names.append(name)
            self._indexes[name] = item["XADIndex"]
            if isinstance(item.get("XADFileSize"), int):
                self._sizes[name] = item["XADFileSize"]
        return names

    def _extract_command(self, name: str) -> list[str]:
        """The command that writes member `name` to stdout.

        7-Zip gets the name with `-spd`, so brackets and `*` are literal; unar
        gets the member's index from the listing, so no name is parsed at all.
        """
        if not self._sizes and not self._indexes:
            self.names()
        if self.rar.kind == "7z":
            # -spd: match the name literally, never as a wildcard.
            return [self.rar.path, "e", "-so", "-spd", "-bd", "-y", *self._type_switch(),
                    "--", str(self.archive), name]
        if name not in self._indexes:
            raise ValueError(f"{name!r} is not in {self.path.name}")
        return [self.rar.path, "-q", "-o", "-", "-i", str(self.archive),
                str(self._indexes[name])]

    def _check_size(self, name: str, written: int) -> None:
        """Raise when the bytes written differ from the size the listing gave."""
        expected = self._sizes.get(name)
        if expected is not None and written != expected:
            raise RuntimeError(
                f"{self.path.name}: {name} came out as {written} bytes, but the "
                f"archive lists {expected}; a volume of the set may be missing "
                "or damaged")


class _PathSource(_Source):
    """A bare ROM file, or a directory the download client already unpacked."""

    def __init__(self, path: Path):
        self.path = path
        self.prefer_hardlinks = False
        self.last_transfer = ("copy", "")
        self.transfers = {}

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

    def hash(self, name, suffix=""):
        """Hash the file a chunk at a time; None if it can't be opened."""
        target = self.path if self.path.is_file() else self.path / name
        try:
            with open(target, "rb") as handle:
                return hash_stream(handle, suffix=suffix)
        except OSError:
            return None

    def copy(self, name, destination):
        source = self.path if self.path.is_file() else self.path / name
        # The source belongs to the download client.  Copy its bytes, not its
        # permission bits: propagating a client's private 0700/0600 mode made
        # the imported library file unreadable to the shared media group.
        # A new destination therefore follows the process umask, while the
        # source inode, owner, group and mode remain untouched.
        self.last_transfer = ("copy", "")
        if self.prefer_hardlinks:
            original = source.lstat()
            mode = original.st_mode
            # Shared libraries need readable files. Never chmod a seeded inode.
            if not stat.S_ISREG(mode):
                reason = "source is not an unchanged regular file"
            elif mode & 0o444 != 0o444:
                reason = "source permissions do not allow shared library reads"
            else:
                try:
                    os.link(source, destination, follow_symlinks=False)
                except OSError as exc:
                    if exc.errno not in {errno.EXDEV, errno.EPERM, errno.EACCES,
                                         errno.ENOTSUP, errno.EOPNOTSUPP, errno.ENOSYS,
                                         errno.EMLINK}:
                        raise
                    reason = f"hardlink unavailable: {exc.strerror or exc}"
                else:
                    linked = Path(destination).lstat()
                    current = source.lstat()
                    def identity(value):
                        return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns)
                    if (not stat.S_ISREG(linked.st_mode)
                            or identity(linked) != identity(original)
                            or identity(current) != identity(original)):
                        Path(destination).unlink()
                        raise OSError(errno.ESTALE, "source changed while creating hardlink")
                    self.last_transfer = ("hardlink", "")
                    self.transfers[name] = self.last_transfer
                    return
            self.last_transfer = ("copy", reason)
        shutil.copyfile(source, destination)
        self.transfers[name] = self.last_transfer


def _complete_nested_zip_sets(sets, read):
    """Reject incomplete nested playlists and readable CUE/GDI disc sheets.

    An unreadable disc sheet retains the existing companion-file fallback.
    """
    accepted = []
    for game in sets:
        if game.primary.lower().endswith(".m3u"):
            try:
                raw = read(game.primary)
            except (OSError, ValueError, RuntimeError, zipfile.BadZipFile):
                raw = None
            if raw is None:
                log.warning("skipping unreadable nested ZIP playlist %r", game.primary)
                continue
            contents = raw.decode("utf-8-sig", errors="replace") if isinstance(raw, bytes) else str(raw)
            requested = [line.strip() for line in contents.splitlines()
                         if line.strip() and not line.lstrip().startswith("#")]
            present = {Path(name.replace("\\", "/")).name.casefold()
                       for name in game.members if name != game.primary}
            normalized = [name[2:] if name.startswith("./") else name
                          for name in requested]
            valid = bool(normalized) and all(
                name not in ("", ".", "..") and "/" not in name
                and "\\" not in name and ":" not in name
                and name.casefold() in present for name in normalized)
            if not valid:
                log.warning("skipping incomplete nested ZIP playlist %r", game.primary)
                continue

        # Playlists may reference CUE/GDI files; verify their track lists too.
        members = list(game.members)
        sheets = [member for member in members
                  if member.lower().endswith((".cue", ".gdi"))]
        # Index once for this set, preserving _resolve_beside's first-match,
        # case-insensitive, same-directory companion resolution.
        by_directory = {}
        if sheets:
            for member in members:
                by_directory.setdefault(_parent(member), {}).setdefault(
                    _basename(member).lower(), member)
        incomplete_sheet = None
        for sheet in sheets:
            named = _names_in_sheet(sheet, read, _cue_and_gdi_names)
            if named is None:
                continue
            by_key = by_directory[_parent(sheet)]
            # Multi-file disc imports flatten companions on publication. A
            # reference to another directory cannot safely resolve after that.
            if not named or any(
                not (reference := name.replace("\\", "/").removeprefix("./"))
                or "/" in reference or ":" in reference
                or reference in (".", "..")
                or (match := by_key.get(reference.lower())) is None
                or match == sheet for name in named
            ):
                incomplete_sheet = sheet
                break
        if incomplete_sheet is not None:
            log.warning("skipping incomplete nested ZIP disc sheet %r", incomplete_sheet)
            continue
        accepted.append(game)
    return accepted

class _NestedZipSource(_Source):
    """Present nested ZIP members through the existing streaming ZIP readers.

    Each inner archive owns its own ROM sets, so matching CUE/BIN names in
    different inner archives cannot be combined into an invalid disc.
    """

    def __init__(self, path: Path, staging_budget=None):
        self.path = path
        self._index = NestedZipIndex(path, is_safe_name, staging_budget=staging_budget)
        self._sources = {}

    def close(self):
        """Close the staged inner ZIP containers used by this source."""
        self._index.close()

    def names(self):
        """Expose members with their full nested archive provenance."""
        return self._index.names()

    def _member_source(self, name):
        """Resolve a qualified name to its owning ZIP reader and member."""
        leaf = self._index.member(name)
        if leaf is None:
            return None, None
        if leaf.archive_path not in self._sources:
            self._sources[leaf.archive_path] = _ZipSource(leaf.archive_path)
        return self._sources[leaf.archive_path], leaf

    def group_key(self, name):
        """Keep CUE/BIN sidecars inside the ZIP that actually owns them."""
        leaf = self._index.member(name)
        return leaf.archive_path if leaf else None

    def member_size(self, name):
        """Return a ZIP member's declared size for a review-only preview."""
        leaf = self._index.member(name)
        return leaf.size if leaf else None

    def rom_sets(self, platform):
        """Keep complete digital installers, but isolate each disc archive."""
        if platform.media == "digital":
            # A PC installer can span outer and inner ZIPs: setup.exe may
            # depend on .bin files in either container. Preserve one set.
            return pick_all_rom_sets(self.names(), platform, read=self.read)
        groups = {}
        for name in self.names():
            groups.setdefault(self.group_key(name), []).append(name)
        selected = [game for names in groups.values()
                    for game in pick_all_rom_sets(names, platform, read=self.read)]
        return _complete_nested_zip_sets(selected, self.read)

    def read(self, name):
        """Read a small descriptor using the member's original ZIP source."""
        source, leaf = self._member_source(name)
        return source.read(leaf.member) if source else None

    def hash(self, name, suffix=""):
        """Reuse streamed ZIP hashing; never materialize a disc image."""
        source, leaf = self._member_source(name)
        return source.hash(leaf.member, suffix) if source else None

    def copy(self, name, destination):
        """Copy one selected leaf without changing its original download."""
        source, leaf = self._member_source(name)
        if source is None:
            raise ValueError(f"nested ZIP member no longer exists: {name!r}")
        source.copy(leaf.member, destination)


class _DirectorySource(_PathSource):
    """Index loose files and archives, keeping ROM sets within each source."""

    def __init__(self, path):
        """Index each archive once and keep its members tied to their source."""
        super().__init__(path)
        self.members = {}
        self._cleanup = ExitStack()
        self._staging_budget = StagingBudget()
        try:
            self._index_sources(path)
        except BaseException:
            self.close()
            raise

    def _index_sources(self, path):
        """Index a directory while sharing one temporary-staging budget."""
        root = path.resolve()
        files = [entry for entry in sorted(path.rglob("*")) if entry.is_file()]
        # Every file passes the escape check before any is opened: grouping
        # reads the first bytes of a .001, and a set's tool reads all its volumes.
        for entry in files:
            if not entry.resolve().is_relative_to(root):
                raise ValueError(f"source escapes download directory: {entry}")
        volume_sets, later_volumes = rar_sets(files)
        for entry in files:
            if entry in later_volumes:
                # Read through the set's first volume, never on its own.
                continue
            relative = entry.relative_to(path).as_posix()
            if entry not in volume_sets and entry.suffix.lower() not in ARCHIVE_SUFFIXES:
                if relative in self.members:
                    raise ValueError(f"duplicate source member: {relative}")
                self.members[relative] = (self, relative)
                continue
            try:
                archive = (_rar_source(entry, volume_sets[entry]) if entry in volume_sets
                           else _source_for(entry, staging_budget=self._staging_budget))
                self._cleanup.callback(archive.close)
                for member in archive.names():
                    name = relative + "/" + member
                    if name in self.members:
                        raise ValueError(f"duplicate archive member: {member}")
                    self.members[name] = (archive, member)
            except (OSError, ValueError, RuntimeError, zipfile.BadZipFile,
                    MissingArchiveTool, subprocess.SubprocessError) as exc:
                detail = getattr(exc, "stderr", None) or str(exc)
                if isinstance(detail, bytes):
                    detail = detail.decode(errors="replace")
                raise ValueError(f"cannot inspect {relative}: {detail}") from exc

    def close(self):
        """Release every owned source, even if one cleanup callback fails."""
        self._cleanup.close()

    def names(self):
        """List loose paths and archive members using distinct source paths."""
        return list(self.members)

    def rom_sets(self, platform):
        """Keep companions together without joining files from different archives."""
        groups = {}
        for name, (source, member) in self.members.items():
            key = ((source, source.group_key(member))
                   if isinstance(source, _NestedZipSource) else source)
            groups.setdefault(key, []).append(name)
        selected = []
        for key, names in groups.items():
            games = pick_all_rom_sets(names, platform, read=self.read)
            if isinstance(key, tuple) and isinstance(key[0], _NestedZipSource):
                games = _complete_nested_zip_sets(games, self.read)
            selected.extend(games)
        return selected

    def member_size(self, name):
        """Expose nested ZIP member sizes without reading their payloads."""
        entry = self.members.get(name)
        if entry is None:
            return None
        source, member = entry
        return source.member_size(member) if isinstance(source, _NestedZipSource) else None

    def read(self, name):
        """Read a loose file or delegate an archive member to its archive."""
        if name not in self.members:
            return None
        source, member = self.members[name]
        return super().read(member) if source is self else source.read(member)

    def hash(self, name, suffix=""):
        """Hash through the owning source so large members can be streamed."""
        if name not in self.members:
            return None
        source, member = self.members[name]
        return super().hash(member, suffix) if source is self else source.hash(member, suffix)

    def copy(self, name, destination):
        """Extract archive members and record that they cannot be hardlinked."""
        source, member = self.members[name]
        if source is self:
            super().copy(member, destination)
        else:
            source.copy(member, destination)
            self.last_transfer = ("extracted", "archive members require extraction")
            self.transfers[name] = self.last_transfer


class MissingArchiveTool(Exception):
    """The download is in a format no installed tool can open."""


def _rar_source(first: Path, volumes: list[Path]) -> _Source:
    """A source for a RAR archive or split set, given its volumes in order.

    7-Zip or unar when installed (`rar_tool`); otherwise bsdtar for a single
    archive, as before. A split set with neither raises MissingArchiveTool
    naming the fix, because bsdtar would open the first volume alone and fail
    partway through the data.
    """
    tool = rar_tool()
    if tool is not None:
        return _RarToolSource(first, tool, volumes)
    if len(volumes) > 1:
        raise MissingArchiveTool(
            f"{first.name} is the first of {len(volumes)} parts of a split RAR "
            "archive. bsdtar reads one part at a time, so it cannot join them. "
            "Install 7-Zip with RAR support (the official 7zz build from "
            "https://github.com/ip7z/7zip/releases; Alpine's and Debian's 7zip "
            "packages leave RAR out) or unar (apt install unar), or use "
            "the ROMarr browser image, which ships unar.")
    tool_path = bsdtar_path()
    if tool_path is None:
        raise MissingArchiveTool(
            f"{first.name} is a .rar archive and no tool that reads RAR is "
            "installed. Install libarchive-tools (bsdtar), 7-Zip with RAR "
            "support, or unar, or use the official ROMarr image.")
    return _BsdtarSource(first, tool_path)


def _source_for(download: Path, platform: Platform | None = None, *,
                staging_budget: StagingBudget | None = None) -> _Source:
    """Choose an archive, directory, or plain-file source for this platform."""
    # When the download is a directory whose only non-hidden file is an archive,
    # unwrap it so the import looks inside the selected archive (#115).
    if download.is_dir():
        non_hidden = [p for p in download.iterdir() if not p.name.startswith(".")]
        if len(non_hidden) == 1 and non_hidden[0].is_file():
            single_file = non_hidden[0]
            if not single_file.resolve().is_relative_to(download.resolve()):
                raise ValueError(f"source escapes download directory: {single_file}")
            single_suffix = single_file.suffix.lower()
            if single_suffix in _STDLIB_ARCHIVES or single_suffix in ARCHIVE_SUFFIXES:
                return _source_for(single_file, platform, staging_budget=staging_budget)

    # For MAME, FBNeo and DOSBox the archive IS the ROM: the core opens it and
    # expects its internal layout. Looking inside would pick one chip dump out
    # of a romset and import that, which succeeds and leaves an entry no core
    # can load. Treat it as a plain file.
    if (platform is not None and platform.archive_is_the_rom
            and download.is_file()):
        return _PathSource(download)

    if (download.is_dir()
            and (platform is None or (not platform.archive_is_the_rom
                                      and platform.media != "digital"))):
        return _DirectorySource(download)

    suffix = download.suffix.lower()
    if download.is_file() and suffix in _STDLIB_ARCHIVES:
        # ZipFile allocates ZipInfo entries during construction. Apply the
        # bounded outer-ZIP metadata preflight before classifying its contents;
        # nested indexes reapply the stricter per-tree cap when opened.
        check_zip_central_directory(download, max_members=MAX_OUTER_ZIP_MEMBERS)
        with zipfile.ZipFile(download) as archive:
            # Flat ZIPs retain their existing reader, including streaming
            # verification. Only ZIPs containing another ZIP need an index.
            nested = any(info.filename.lower().endswith(".zip")
                         for info in archive.infolist() if not info.is_dir())
        # A nested ZIP may be a required Windows installer payload. Keep it
        # intact, rather than silently converting its contents into unrelated
        # files. Cartridge and disc platforms still inspect nested ZIP layers.
        return (_NestedZipSource(download, staging_budget=staging_budget)
                if nested and (platform is None or platform.media != "digital")
                else _ZipSource(download))
    key = _rar_volume_key(download) if download.is_file() else None
    if key and key[0][0] == "num" and key[1] == 1 and _rar_signature(download):
        return _rar_source(download, [download])
    if download.is_file() and suffix == ".rar" and rar_tool() is not None:
        return _RarToolSource(download, rar_tool())
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
    source = _source_for(download)
    try:
        return source.names()
    finally:
        source.close()


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
        suffix = Path(member.replace("\\", "/")).suffix
        try:
            hashes = source.hash(member, suffix)
        except Exception:
            return Match(UNKNOWN, detail=f"could not read {member!r} to verify")
        if hashes is None:
            return Match(UNKNOWN, detail=f"could not read {member!r} to verify")
        verdicts.append(dats.lookup(**hashes))

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


def _custom_platform_folder(slug: str, platform_folders) -> str | None:
    """Resolve an operator's custom folder name for `slug`, if configured.

    Accepts either a dict (`{"win": "Windows", "ps2": "PlayStation 2"}`) or a
    list of `{"platform": ..., "folder": ...}` rows. Keys are normalised via
    `resolve()` so `"pc"`, `"windows"`, and `"win"` all map to the `win` slug.
    Rejects path separators and traversal segments so a folder override stays a
    single directory name under `library_root`.
    """
    if not platform_folders:
        return None
    target_plat = resolve(slug)
    canonical = target_plat.slug if target_plat is not None else str(slug or "").strip().lower()
    raw_map: dict[str, str] = {}
    if isinstance(platform_folders, dict):
        items = list(platform_folders.items())
    elif isinstance(platform_folders, list):
        items = [
            (row.get("platform") or row.get("slug") or "",
             row.get("folder") or row.get("name") or "")
            for row in platform_folders if isinstance(row, dict)
        ]
    else:
        return None
    for k, v in items:
        key_str = str(k or "").strip()
        val_str = str(v or "").strip()
        if not key_str or not val_str:
            continue
        if "/" in val_str or "\\" in val_str or val_str in (".", ".."):
            continue
        resolved = resolve(key_str)
        norm_key = resolved.slug if resolved is not None else key_str.lower()
        raw_map[norm_key] = val_str
        raw_map[key_str.lower()] = val_str
    return raw_map.get(canonical) or raw_map.get(str(slug or "").strip().lower())


def _digital_set_name(download: Path) -> str:
    """Folder name for a multi-file digital (PC) import.

    `Path.stem` strips the last dot-separated segment even on directories, so a
    scene or repack folder like `Call.of.Duty.Modern.Warfare.4.[DODI.Repack]`
    was truncated to `Call.of.Duty.Modern.Warfare.4.[DODI`. Only strip the
    suffix when `download` is an archive file.
    """
    if download.is_file() and download.suffix.lower() in ARCHIVE_SUFFIXES:
        return download.stem
    return download.name


def platform_dir(library_root: Path, platform: "Platform | str", *,
                 layout: str = "flat", translation: bool = False,
                 platform_folders=None) -> Path:
    """Where a ROM for this platform is filed, under the chosen layout."""
    slug = getattr(platform, "slug", platform)
    folder = _custom_platform_folder(slug, platform_folders) or romm_fs_slug(slug)
    base = library_root / folder
    if str(layout).lower() in ("nested", "romm_b", "b"):
        base = base / "roms"
    if translation:
        base = base / TRANSLATION_SUBDIR
    return base


def _publish_staged(staged: Path, destination: Path, overwrite: bool) -> None:
    """Publish complete bytes; link gives atomic no-clobber on a shared filesystem."""
    if overwrite:
        os.replace(staged, destination)
    else:
        try:
            os.link(staged, destination)
        except OSError as exc:
            if exc.errno not in (errno.EPERM, errno.EOPNOTSUPP, errno.ENOSYS):
                raise
            if os.name == "nt":
                # Windows rename refuses an existing destination atomically.
                os.rename(staged, destination)
                return
            # Some library filesystems cannot hardlink even within one mount.
            # Linux provides the same no-clobber guarantee without a hardlink.
            import ctypes
            libc = ctypes.CDLL(None, use_errno=True)
            rename = getattr(libc, "renameat2", None)
            if rename is None:
                raise exc
            if rename(-100, os.fsencode(staged), -100, os.fsencode(destination), 1) != 0:
                error = ctypes.get_errno()
                raise OSError(error, os.strerror(error), str(destination))


def _existing_destination(destination: Path) -> str:
    if destination.is_file() and destination.stat().st_size == 0:
        return "existing zero-byte destination needs inspection; refusing to overwrite"
    return "already in the library; reconcile existing files before retrying"


def import_rom(download: Path, platform: Platform, library_root: Path, *,
               overwrite: bool = False, dats=None,
               require_verified: bool = False,
               layout: str = "flat", translation: bool = False,
               rename: bool = False,
               platform_folders=None, prefer_hardlinks: bool = False, selected_primaries=None, bundle_routes=None,
               requested_game: str | None = None, catalog_platforms=None) -> list[ImportResult]:
    """Place every ROM from a finished download into the library.

    A cartridge zip may hold several games (a 3-in-1 collection, a bundle)
    and every one is imported.  A disc lands as a directory holding every
    file of the set, which is the layout the live library already uses for
    multi-track rips.

    `layout` picks the directory shape (see `platform_dir`); `translation`
    files the set in the platform's Translations subfolder, for the
    keep-both case where a T-En patch sits beside the original dump.
    The rename option uses verified DAT names for single-file ROMs, retaining
    the physical extension. Multi-file members keep their reference names.
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
        if isinstance(source, _PathSource):
            # A request-gated import must copy into private staging; a hardlink
            # would let another process mutate already-verified staged bytes.
            source.prefer_hardlinks = prefer_hardlinks and requested_game is None
        candidates = source.names()
        chosen_sets = source.rom_sets(platform)
    except (MissingArchiveTool, OSError, ValueError, zipfile.BadZipFile, RuntimeError, subprocess.SubprocessError) as exc:
        if "source" in locals():
            source.close()
        return [ImportResult(False, None, f"source inspection failed: {exc}")]
    except BaseException:
        # Preserve unexpected errors and cancellation, without leaking staged ZIPs.
        if "source" in locals():
            source.close()
        raise

    try:
        return _import_rom_from_source(
            source, candidates, chosen_sets, download, platform, library_root,
            overwrite=overwrite, dats=dats, require_verified=require_verified,
            layout=layout, translation=translation, rename=rename,
            platform_folders=platform_folders, prefer_hardlinks=prefer_hardlinks,
            selected_primaries=selected_primaries, bundle_routes=bundle_routes,
            requested_game=requested_game, catalog_platforms=catalog_platforms)
    finally:
        source.close()


def _import_rom_from_source(source, candidates, chosen_sets, download, platform,
                            library_root, *, overwrite=False, dats=None,
                            require_verified=False, layout="flat",
                            translation=False, rename=False,
                            platform_folders=None, prefer_hardlinks=False,
                            selected_primaries=None, bundle_routes=None,
                            requested_game=None, catalog_platforms=None):
    """Finish import while the validated source and its scratch ZIPs remain open."""
    if selected_primaries is not None:
        selected = set(selected_primaries)
        available = {group.primary for group in chosen_sets}
        if not selected or not selected <= available:
            return [ImportResult(False, None, "selected import members changed; review the bundle again")]
        chosen_sets = [group for group in chosen_sets if group.primary in selected]
    if not chosen_sets:
        return [ImportResult(
            False, None,
            f"no {platform.name} ROM among {len(candidates)} file(s)")]

    # Optional request-aware gate: deliberately NOT activated by scheduled
    # imports until app.py binds it to a durable queue/source identity. Manual
    # and legacy import callers retain their existing behavior by default.
    # A held/rejected result returns before any destination is created.
    assessment = None
    if requested_game is not None:
        if overwrite:
            return [ImportResult(False, None, "request identity REVIEW_REQUIRED: overwrites need separate review")]
        if any(group.is_multi_file for group in chosen_sets):
            return [ImportResult(False, None, "request identity REVIEW_REQUIRED: multi-file publication needs atomic staging")]
        from .request_evidence import evaluate_source_request

        assessment = evaluate_source_request(
            source, chosen_sets, dats, requested_game, platform.slug,
            catalog_platforms=catalog_platforms,
        )
        if assessment.decision.action != "import":
            decision = assessment.decision
            return [ImportResult(
                False, None, f"request identity {decision.code}: {decision.message}")]

    results: list[ImportResult] = []
    planned = []
    destinations = {}
    file_destinations = {}
    destination_parents = {}
    for chosen in chosen_sets:
        try:
            # Preserve the exact catalog/game selected by request assessment.
            # Legacy imports retain their historic first-match DAT behavior.
            verdict = (assessment.verification if assessment is not None
                       else verify_set(source, chosen.members, dats))
        except (OSError, ValueError, zipfile.BadZipFile, RuntimeError, subprocess.SubprocessError) as exc:
            results.append(ImportResult(False, None, f"verification failed: {exc}"))
            continue
        if require_verified and verdict.status != VERIFIED:
            results.append(ImportResult(
                False, None,
                f"refused: {verdict.detail or verdict}",
                verification=verdict))
            continue

        target_dir = platform_dir(library_root, platform,
                                  layout=layout, translation=translation,
                                  platform_folders=platform_folders)

        if chosen.is_multi_file:
            name = (_digital_set_name(download) if platform.media == "digital"
                    else _set_name(chosen.primary))
            destination = target_dir / name
        else:
            destination = target_dir / _import_name(chosen.primary, verdict, rename=rename)
            if bundle_routes is not None:
                relative = Path(bundle_routes.get(chosen.primary, ""))
                if (not str(relative) or str(relative) == "." or relative.is_absolute()
                        or ".." in relative.parts
                        or not (target_dir / relative).resolve().is_relative_to(target_dir.resolve())):
                    results.append(ImportResult(False, None, "invalid reviewed destination; review the bundle again"))
                    continue
                destination = target_dir / relative
        destination_keys = {Path(str(destination).casefold())}
        if not chosen.is_multi_file and bundle_routes is not None:
            destination_keys.add(Path(str(destination.parent.resolve() / destination.name).casefold()))
        for key in destination_keys:
            if key in destinations:
                return [ImportResult(False, destination,
                                     f"selected ROMs map to the same destination: {destinations[key]} and {chosen.primary}; select one or give the sources distinct names")]
            conflict = destination_parents.get(key) if not chosen.is_multi_file else None
            for parent in key.parents:
                conflict = conflict or file_destinations.get(parent)
            if conflict is not None:
                return [ImportResult(False, destination,
                                     f"selected ROMs have conflicting destinations: {conflict} and {chosen.primary}; a file cannot also be a destination directory")]
        for key in destination_keys:
            destinations[key] = chosen.primary
            if not chosen.is_multi_file:
                file_destinations[key] = chosen.primary
            for parent in key.parents:
                destination_parents[parent] = chosen.primary
        planned.append((chosen, verdict, destination))

    for chosen, verdict, destination in planned:
        if isinstance(source, _PathSource):
            source.transfers = {}
        target_dir = destination.parent
        if not chosen.is_multi_file:
            try:
                target_dir.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                results.append(ImportResult(False, target_dir, f"destination preparation failed: {exc}"))
                continue
            name = destination.name
            if destination.exists() and (not overwrite or destination.stat().st_size == 0):
                results.append(ImportResult(False, destination, _existing_destination(destination)))
                continue
            try:
                with tempfile.TemporaryDirectory(prefix=".romarr-import-", dir=target_dir) as staging:
                    staged = Path(staging) / name
                    source.copy(chosen.primary, staged)
                    actual_size = staged.stat().st_size
                    if actual_size == 0:
                        raise ValueError("empty ROM payload")
                    if platform.slug in ("ps2", "ngc", "xbox") and actual_size < 8 * 1024 * 1024:
                        raise ValueError(f"too small to be a {platform.name} disc image ({actual_size // (1024*1024)}MB < 8MB)")
                    if assessment is not None:
                        from .request_evidence import evaluate_source_request

                        # Re-hash the copied bytes that will actually be
                        # published. The earlier source approval is invalid if
                        # another process changed the loose download meanwhile.
                        staged_evidence = evaluate_source_request(
                            _PathSource(staged), (chosen,), dats,
                            requested_game, platform.slug,
                            catalog_platforms=catalog_platforms,
                        )
                        if (staged_evidence.decision.action != "import"
                                or staged_evidence.verification != verdict):
                            raise ValueError("request identity changed during staging; nothing published")
                    _publish_staged(staged, destination, overwrite)
            except (OSError, ValueError, zipfile.BadZipFile, RuntimeError, subprocess.SubprocessError) as exc:
                results.append(ImportResult(False, destination, f"copy/extraction or publication failed: {exc}"))
                continue
            log.info("imported %s -> %s", chosen.primary, destination)
            results.append(ImportResult(True, destination,
                                        verification=verdict, **_transfer_summary(source)))
            continue

        if destination.exists() and any(destination.iterdir()) and not overwrite:
            results.append(ImportResult(False, destination, _existing_destination(destination)))
            continue
        target_dir.mkdir(parents=True, exist_ok=True)
        try:
            with tempfile.TemporaryDirectory(prefix=".romarr-import-", dir=target_dir) as staging:
                staged_dir = Path(staging)
                # Digital installers must retain their internal path layout,
                # including subdirectories with data/configuration files.
                # Other multi-file ROMs retain their established flat layout.
                written: dict[str, str] = {}
                folded: dict[str, str] = {}
                folded_dirs: set[str] = set()
                for member in chosen.members:
                    relative = (member.replace("\\", "/") if platform.media == "digital"
                                else Path(member.replace("\\", "/")).name)
                    path = Path(relative)
                    if (not is_safe_name(relative, staged_dir)
                            or path.is_absolute() or ".." in path.parts
                            or path.as_posix() == "."):
                        raise ValueError(f"unsafe set member: {member!r}")
                    # Normalize ./ and repeated separators BEFORE checking
                    # aliases, so different ZIP names cannot target one file.
                    # Keep the original member for source.copy() lookup.
                    normalized = path.as_posix()
                    key = normalized.casefold()
                    parents = {parent.as_posix().casefold() for parent in path.parents
                               if parent.as_posix() != "."}
                    if (key in folded or key in folded_dirs
                            or any(parent in folded for parent in parents)):
                        raise ValueError(
                            f"colliding set member paths: {folded.get(key, normalized)!r} and {member!r}")
                    folded[key] = member
                    folded_dirs.update(parents)
                    written[normalized] = member
                for name, member in written.items():
                    staged_member = staged_dir / name
                    staged_member.parent.mkdir(parents=True, exist_ok=True)
                    source.copy(member, staged_member)
                    if staged_member.stat().st_size == 0:
                        raise ValueError(f"empty set member: {member!r}")
                total_set_size = sum((staged_dir / name).stat().st_size for name in written)
                if platform.slug in ("ps2", "ngc", "xbox") and total_set_size < 8 * 1024 * 1024:
                    raise ValueError(f"too small to be a {platform.name} disc image ({total_set_size // (1024*1024)}MB < 8MB)")
                destination.mkdir(parents=True, exist_ok=True)
                resolved_targets: dict[Path, str] = {}
                resolved_ancestors: dict[Path, str] = {}
                for name in written:
                    final = destination / name
                    if not is_safe_name(name, destination):
                        raise ValueError(f"unsafe destination path: {name!r}")
                    # Detect existing file ancestors before publishing any member.
                    for parent in Path(name).parents:
                        if parent == Path("."):
                            continue
                        ancestor = destination / parent
                        if ancestor.exists() and not ancestor.is_dir():
                            raise ValueError(
                                f"destination ancestor is not a directory: {ancestor}")
                    if final.exists() and final.is_dir():
                        raise ValueError(f"destination requires a file but is a directory: {final}")
                    if final.exists() and final.stat().st_size == 0:
                        raise ValueError(f"existing zero-byte destination needs inspection: {final}")
                    # Symlinked directories may make separate member names
                    # resolve to the same file, or to a file and its child.
                    # Validate the full set before publishing any member.
                    target = final.resolve(strict=False)
                    conflict = resolved_targets.get(target) or resolved_ancestors.get(target)
                    if conflict is None:
                        conflict = next((resolved_targets[parent] for parent in target.parents
                                         if parent in resolved_targets), None)
                    if conflict is not None:
                        raise ValueError(
                            f"colliding destination paths: {conflict!r} and {name!r}")
                    resolved_targets[target] = name
                    for parent in target.parents:
                        resolved_ancestors.setdefault(parent, name)
                for name in written:
                    final = destination / name
                    final.parent.mkdir(parents=True, exist_ok=True)
                    _publish_staged(staged_dir / name, final, overwrite)
        except (OSError, ValueError, zipfile.BadZipFile, RuntimeError, subprocess.SubprocessError) as exc:
            results.append(ImportResult(False, destination, f"set copy/extraction or publication failed: {exc}; reconcile any completed members before retrying"))
            continue
        log.info("imported %d files -> %s", len(written), destination)
        results.append(ImportResult(True, destination, verification=verdict, **_transfer_summary(source)))

    return results



def _transfer_summary(source):
    if not isinstance(source, _PathSource):
        return {"import_method": "extracted", "fallback_reason": "archive members require extraction"}
    transfers = list(source.transfers.values())
    methods = {method for method, _ in transfers}
    return {"import_method": next(iter(methods)) if len(methods) == 1 else "mixed",
            "fallback_reason": "; ".join(sorted({reason for _, reason in transfers if reason}))}


def _import_name(primary: str, verdict: Match, *, rename: bool) -> str:
    """Use a verified DAT name without changing the source format or disc refs."""
    original = Path(primary.replace(chr(92), "/")).name
    if not rename or verdict.status != VERIFIED or verdict.rom is None:
        return original
    canonical = str(verdict.rom.name or "")
    if (not canonical or re.search(r'[<>:"/\\|?*\x00-\x1f]', canonical)
            or canonical.endswith((" ", "."))):
        return original
    stem = Path(canonical).stem
    # Windows reserves device names even before multiple suffixes (NUL.tar.gz)
    # and recognizes superscript digits in COM/LPT device names as well.
    device = canonical.split(".", 1)[0].rstrip()
    if (not stem or stem in (".", "..")
            or re.fullmatch(r"(?i:CON|PRN|AUX|NUL|CONIN\$|CONOUT\$|COM[1-9¹²³]|LPT[1-9¹²³])", device)):
        return original
    # Hash normalization can equate .smc/.sfc dumps. A title change must not
    # relabel the physical format, so retain the source file extension.
    return stem + Path(original).suffix


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
