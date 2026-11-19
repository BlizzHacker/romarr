"""What kind of disc a CHD holds, read from its own header.

chdman decompresses a CHD with a different command per media type:
`extractcd` for CD and GD-ROM images, `extractdvd` for DVDs, `extracthd` for
hard disks, `extractld` for LaserDiscs and `extractraw` for a CHD with no
media metadata. Asking for the wrong one fails the job, so the batch has to
know which kind it holds before it submits anything.

The answer is in the file. Every CHD (MAME's format, versions 3 to 5) starts
with a header that gives the offset of a chain of metadata entries, and each
entry carries a four-character tag naming the media: `CHT2` for a CD track,
`DVD ` for a DVD, `GDDD` for hard-disk geometry and so on. Only the header
and the entry headers are read, a few hundred bytes from a file that may be
tens of gigabytes.
"""

from __future__ import annotations

import struct
from pathlib import Path

#: The eight bytes every CHD starts with.
CHD_MAGIC = b"MComprHD"

#: Media kinds `chd_media` reports.
CD = "cd"
GDROM = "gd"
DVD = "dvd"
HARD_DISK = "hd"
LASERDISC = "ld"
RAW = "raw"

#: Metadata tag -> media kind. Tags not listed here (hard-disk ident, PCMCIA
#: CIS and the like) describe a disc further but do not name its kind.
_KIND_BY_TAG: dict[bytes, str] = {
    b"CHCD": CD, b"CHTR": CD, b"CHT2": CD,
    b"CHGT": GDROM, b"CHGD": GDROM,
    b"DVD ": DVD,
    b"GDDD": HARD_DISK,
    b"AVAV": LASERDISC, b"AVLD": LASERDISC,
}

#: Offset of the 64-bit metadata offset in the header, by CHD version.
_META_OFFSET_AT = {3: 36, 4: 36, 5: 48}

#: The metadata chain is a linked list inside the file. A real CHD has a
#: handful of entries (one per CD track, 99 at most); anything longer is a
#: damaged or hostile file, and the walk stops rather than loop.
_MAX_ENTRIES = 256

#: chdman's decompression mode for each media kind.
_EXTRACT_MODE = {
    CD: "extractcd",
    GDROM: "extractcd",
    DVD: "extractdvd",
    HARD_DISK: "extracthd",
    LASERDISC: "extractld",
    RAW: "extractraw",
}

#: What a CHD that cannot be read is sent as: the mode used before media
#: kinds were read, so an unreadable file fails at the service with the
#: service's own error rather than being dropped here without a word.
DEFAULT_EXTRACT_MODE = "extractcd"


def chd_media(path: str | Path) -> str:
    """The media kind of the CHD at `path`, or "" when it cannot be told.

    "" covers a file that is not a CHD, a version this does not know, a
    metadata chain that runs off the end of the file, and metadata whose tags
    name no kind. A CHD with a valid header and no metadata at all is `raw`:
    that is what `chdman createraw` writes.
    """
    try:
        with open(path, "rb") as handle:
            return _read_media(handle)
    except OSError:
        return ""


def extract_mode_for_chd(path: str | Path) -> str:
    """The chdman mode that decompresses the CHD at `path`."""
    return _EXTRACT_MODE.get(chd_media(path), DEFAULT_EXTRACT_MODE)


def _read_media(handle) -> str:
    """Walk the header and metadata chain of an open CHD."""
    header = handle.read(64)
    if len(header) < 16 or not header.startswith(CHD_MAGIC):
        return ""
    version = struct.unpack(">I", header[12:16])[0]
    at = _META_OFFSET_AT.get(version)
    if at is None or len(header) < at + 8:
        return ""
    offset = struct.unpack(">Q", header[at:at + 8])[0]
    if offset == 0:
        return RAW
    handle.seek(0, 2)
    size = handle.tell()
    seen: set[int] = set()
    for _ in range(_MAX_ENTRIES):
        if offset == 0:
            return ""
        if offset in seen or offset + 16 > size:
            return ""
        seen.add(offset)
        handle.seek(offset)
        entry = handle.read(16)
        if len(entry) < 16:
            return ""
        kind = _KIND_BY_TAG.get(entry[:4])
        if kind:
            return kind
        offset = struct.unpack(">Q", entry[8:16])[0]
    return ""
