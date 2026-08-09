"""What a ROM actually is, read from its bytes rather than its name.

ROMarr trusts the file extension. That is right almost always and wrong in the
case that costs the most: a file renamed by hand, by a scene release, or by a
frontend that guessed. It is then filed under the wrong platform, and the
symptom appears much later as a game that will not boot.

Yarr.It, which plays ROMs in the browser, already reads the header before
choosing an emulator core -- its test suite names the case directly: "a Mega
Drive rom that somebody renamed .nes". It has to, because handing EmulatorJS
the wrong core produces a black screen rather than an error. ROMarr files the
same files and had no equivalent, so this closes that gap on our side.

Only signatures that are genuinely distinctive are here. A format identified by
"it is the right length" is not identification, and guessing confidently is
worse than saying nothing: `identify` returns None when it does not know, and
every caller treats that as "no opinion", never as "wrong".
"""

from __future__ import annotations

import pathlib
from dataclasses import dataclass

#: The Nintendo logo both Game Boy and GBA carry, and the boot ROM checks. Its
#: first bytes are enough to separate them from everything else here.
_GB_LOGO = bytes.fromhex("CEED6666CC0D000B03730083")
_GBA_LOGO = bytes.fromhex("24FFAE51699AA2213D84820A")


@dataclass(frozen=True)
class Sniffed:
    """What the bytes say, and how sure that is."""

    platform: str
    detail: str


def _at(blob: bytes, offset: int, marker: bytes) -> bool:
    return blob[offset:offset + len(marker)] == marker


def identify(head: bytes) -> Sniffed | None:
    """The platform these opening bytes belong to, or None.

    `head` should be at least 1 KiB; more is never needed by anything here, so
    a caller may read a small window rather than a whole 4 GB disc image.
    """
    if not head:
        return None

    # iNES and its successors. The magic is four bytes and unambiguous.
    if _at(head, 0, b"NES\x1a"):
        return Sniffed("nes", "iNES header (NES\\x1a)")

    # Nintendo 64, in all three byte orders. The same word, shuffled -- which
    # is exactly why a .n64/.v64/.z64 mix-up is so common.
    for offset, order in ((b"\x80\x37\x12\x40", "big-endian (.z64)"),
                          (b"\x37\x80\x40\x12", "byte-swapped (.v64)"),
                          (b"\x40\x12\x37\x80", "little-endian (.n64)")):
        if _at(head, 0, offset):
            return Sniffed("n64", f"Nintendo 64 magic, {order}")

    # Game Boy and Game Boy Advance both carry a fixed Nintendo logo, at
    # different offsets. Checking the logo rather than the extension is what
    # separates a .gb that is really a .gba.
    if _at(head, 0x104, _GB_LOGO):
        # The colour flag lives at 0x143; 0x80 and 0xC0 both mean CGB.
        cgb = head[0x143:0x144]
        if cgb in (b"\x80", b"\xc0"):
            return Sniffed("gbc", "Nintendo logo at 0x104, CGB flag set")
        return Sniffed("gb", "Nintendo logo at 0x104")
    if _at(head, 0x04, _GBA_LOGO):
        return Sniffed("gba", "Nintendo logo at 0x04")

    # Mega Drive / Genesis. "SEGA" at 0x100 is the console name field.
    if _at(head, 0x100, b"SEGA"):
        return Sniffed("genesis-slash-megadrive", "SEGA console name at 0x100")

    # Master System and Game Gear share the TMR SEGA signature; which of the
    # three offsets it sits at depends on ROM size, not on the machine, so
    # this cannot tell them apart and says so.
    for offset in (0x1FF0, 0x3FF0, 0x7FF0):
        if _at(head, offset, b"TMR SEGA"):
            return Sniffed("sms", f"TMR SEGA signature at {offset:#06x} "
                                  f"(Master System or Game Gear)")

    if _at(head, 0x01, b"ATARI7800"):
        return Sniffed("atari7800", "ATARI7800 header")
    if _at(head, 0x40, b"LYNX"):
        return Sniffed("lynx", "LYNX header")

    # -- discs --------------------------------------------------------------
    #
    # Most of a disc library arrives as a 7z or rar, and without these the
    # header check has nothing to say about any of it: on the library this was
    # measured against, twenty consecutive real archives were all disc images
    # and every one came back with no opinion.

    # Nintendo optical discs carry a magic word in the boot header. GameCube's
    # sits at 0x1C, Wii's at 0x18, and a Wii disc carries both -- so Wii is
    # tested first or every Wii disc reads as a GameCube one.
    if _at(head, 0x18, b"\x5d\x1c\x9e\xa3"):
        return Sniffed("wii", "Wii disc magic at 0x18")
    if _at(head, 0x1C, b"\xc2\x33\x9f\x3d"):
        return Sniffed("ngc", "GameCube disc magic at 0x1c")

    # Compressed Nintendo disc containers. Which console the image is for
    # lives in the payload, not the wrapper, so these name the container and
    # say the console is undetermined -- the same treatment the Master System
    # signature gets. Reporting the container is still worth it: on a real
    # library these are most of the GameCube shelf, and the alternative is
    # saying nothing about any of them.
    for magic, fmt in ((b"CISO", "CISO"), (b"RVZ\x01", "RVZ"),
                       (b"WIA\x01", "WIA"),
                       (b"\x01\xc0\x0b\xb1", "GCZ")):
        if _at(head, 0x00, magic):
            return Sniffed("ngc", f"{fmt} compressed disc image "
                                  f"(GameCube or Wii)")

    # Sega's optical formats name themselves in the first sector. The offset
    # differs between a 2048-byte user-data rip and a 2352-byte raw one, so
    # both are checked.
    for offset in (0x00, 0x10):
        if _at(head, offset, b"SEGADISCSYSTEM") or \
                _at(head, offset, b"SEGABOOTDISC"):
            return Sniffed("segacd", f"Sega CD boot header at {offset:#04x}")
        if _at(head, offset, b"SEGA SEGASATURN"):
            return Sniffed("saturn", f"Saturn boot header at {offset:#04x}")
        if _at(head, offset, b"SEGA SEGAKATANA"):
            return Sniffed("dc", f"Dreamcast boot header at {offset:#04x}")

    # 3DO's opera filesystem starts with a record type and five 'Z's.
    if _at(head, 0x00, b"\x01\x5a\x5a\x5a\x5a\x5a"):
        return Sniffed("3do", "3DO opera filesystem header")

    # PlayStation and PS2 are ISO9660 with a Sony volume identifier. This
    # cannot separate the two -- both say PLAYSTATION -- so it says so rather
    # than picking one.
    if _at(head, 0x8001, b"CD001"):
        window = head[0x8000:0x9000]
        if b"PLAYSTATION" in window:
            return Sniffed("psx", "ISO9660 with a PLAYSTATION volume "
                                  "identifier (PlayStation or PS2)")

    # Atari Lynx also ships headerless; nothing else distinctive is left, so
    # anything unrecognised is reported as unknown rather than guessed at.
    return None


#: How many bytes `identify` ever looks at. Read this much and no more.
#:
#: 0x9000 rather than 0x8000 because ISO9660's first volume descriptor sits at
#: 0x8001, and that is what identifies a PlayStation or PS2 disc. Still small
#: enough that reading it off a 1.3 GB archive member costs nothing.
HEAD_BYTES = 0x9000


def identify_file(path) -> Sniffed | None:
    """`identify` for something on disk, reading only the opening window.

    An archive is opened and its first ROM-looking member read instead, since
    a zipped ROM is the normal shipping form and its own header says nothing
    about which machine the game is for.
    """
    if pathlib.Path(str(path)).suffix.lower() in _ARCHIVE_SUFFIXES:
        return identify_archive(path)
    try:
        with open(path, "rb") as handle:
            return identify(handle.read(HEAD_BYTES))
    except OSError:
        return None


def disagrees_with(path, platform_slug: str) -> Sniffed | None:
    """The bytes' opinion, when it contradicts the platform claimed.

    Returns None when the bytes agree, have no opinion, or cannot separate the
    two -- the Master System and Game Gear share a signature, so a Game Gear
    file must not be reported as mislabelled just because the header says SMS.
    """
    got = identify_file(path)
    if got is None or not platform_slug:
        return None
    claimed = platform_slug.strip().lower()
    if got.platform == claimed:
        return None
    # Families this cannot separate. Silence beats a false accusation.
    for family in (("sms", "gamegear"), ("gb", "gbc"),
                   ("nes", "famicom", "fds"),
                   ("snes", "sfam"),
                   # Both are ISO9660 with the same Sony identifier.
                   ("psx", "ps2"),
                   # A compressed container does not say which console.
                   ("ngc", "wii")):
        if got.platform in family and claimed in family:
            return None
    return got

#: A zip is the normal shipping form for a cartridge ROM, and `.zip` is the
#: most ambiguous extension ROMarr has -- twenty platforms claim it. Reading
#: the archive's first member is the difference between the header check being
#: useful on a handful of loose files and useful on a real library: on the one
#: this was measured against, 3,319 of 3,347 NES files and 3,833 of 3,873 GBA
#: files are zipped.
_ARCHIVE_SUFFIXES = (".zip", ".7z", ".rar")

#: Handled by the stdlib. Everything else in _ARCHIVE_SUFFIXES needs bsdtar,
#: which ROMarr already requires to import those formats at all.
_ZIP_SUFFIXES = (".zip",)

#: Members that are never the ROM.
_NOT_A_ROM = (".txt", ".nfo", ".diz", ".sfv", ".md5", ".sha1", ".jpg",
              ".png", ".gif", ".pdf", ".doc", ".dat", ".xml", ".cue")


def identify_archive(path) -> Sniffed | None:
    """`identify`, applied to the first plausible member of an archive.

    Zip is read with the standard library. 7z and rar go through the same
    `bsdtar` ROMarr already requires to import them, and when no libarchive
    binary is installed this returns None rather than guessing -- the same
    answer it gives for a format it cannot read.

    Only the first ROM-looking member is opened, and only its opening window,
    so a scan never becomes a decompression.
    """
    import zipfile

    path = pathlib.Path(str(path))
    suffix = path.suffix.lower()
    if suffix not in _ARCHIVE_SUFFIXES:
        return None
    if suffix not in _ZIP_SUFFIXES:
        return _identify_via_bsdtar(path)
    try:
        with zipfile.ZipFile(path) as archive:
            for info in archive.infolist():
                if info.is_dir():
                    continue
                name = info.filename.lower()
                if name.endswith(_NOT_A_ROM):
                    continue
                with archive.open(info) as member:
                    got = identify(member.read(HEAD_BYTES))
                if got is not None:
                    return Sniffed(got.platform,
                                   f"{got.detail}, inside {path.suffix}")
                # Only the first plausible member is worth reading: a
                # multi-ROM archive is one platform's worth of games, and
                # walking all of them would turn a scan into a decompression.
                return None
    except (OSError, zipfile.BadZipFile, RuntimeError):
        return None
    return None

def _plausible_member(names: list[str]) -> str | None:
    """The first entry that could be the ROM rather than release furniture."""
    for name in names:
        if name.endswith("/"):
            continue
        if name.lower().endswith(_NOT_A_ROM):
            continue
        return name
    return None


def _identify_via_bsdtar(path) -> Sniffed | None:
    """7z and rar, through the libarchive tool ROMarr already depends on.

    The member is streamed and the pipe closed after the opening window, so a
    two-gigabyte entry costs the same as a small one. bsdtar is killed rather
    than waited on: it has more to write and nobody is going to read it.
    """
    import subprocess

    from .library import bsdtar_path

    tool = bsdtar_path()
    if tool is None:
        return None

    try:
        listed = subprocess.run([tool, "-tf", str(path)], capture_output=True,
                                text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    if listed.returncode != 0:
        return None

    member = _plausible_member([line.strip()
                                for line in listed.stdout.splitlines()
                                if line.strip()])
    if member is None:
        return None

    proc = None
    try:
        proc = subprocess.Popen([tool, "-xOf", str(path), member],
                                stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL)
        head = proc.stdout.read(HEAD_BYTES)
    except (OSError, subprocess.SubprocessError):
        return None
    finally:
        if proc is not None:
            try:
                proc.stdout.close()
            except OSError:
                pass
            proc.kill()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass

    got = identify(head or b"")
    if got is None:
        return None
    return Sniffed(got.platform,
                   f"{got.detail}, inside {pathlib.Path(str(path)).suffix}")
