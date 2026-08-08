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

    # Atari Lynx also ships headerless; nothing else distinctive is left, so
    # anything unrecognised is reported as unknown rather than guessed at.
    return None


#: How many bytes `identify` ever looks at. Read this much and no more.
HEAD_BYTES = 0x8000


def identify_file(path) -> Sniffed | None:
    """`identify` for something on disk, reading only the opening window."""
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
                   ("snes", "sfam")):
        if got.platform in family and claimed in family:
            return None
    return got
