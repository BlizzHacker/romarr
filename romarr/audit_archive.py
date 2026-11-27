"""Safe, bounded DAT verification for ZIPs already in a folder library."""
from __future__ import annotations

import stat
import struct
import posixpath
import zipfile
from pathlib import Path, PurePosixPath

from .dat import BAD_DUMP, UNKNOWN, VERIFIED, Match, hash_stream
from .selection import (_cue_and_gdi_names, _names_in_sheet, _playlist_names,
                       pick_all_rom_sets)

MAX_ARCHIVE_MEMBERS = 10_000
MAX_MEMBER_BYTES = 2 * 1024 * 1024 * 1024
MAX_ARCHIVE_BYTES = 8 * 1024 * 1024 * 1024
MAX_CENTRAL_BYTES = 4 * 1024 * 1024
MAX_SHEET_BYTES = 1024 * 1024


def _bounded_directory(source):
    """Inspect bounded EOCD metadata before zipfile allocates its member index.

    ZIP64 and split archives stay unknown rather than allocating unbounded metadata.
    """
    size = source.seek(0, 2)
    if size < 22:
        return False
    # Most ZIPs have no comment. Read the minimum footer first, especially
    # for libraries where every archive read goes across a network mount.
    source.seek(-22, 2)
    tail = source.read(22)
    at = 0
    if not tail.startswith(b"PK\x05\x06") or tail[-2:] != b"\0\0":
        source.seek(max(0, size - 65557))
        tail = source.read(65557)
        at = tail.rfind(b"PK\x05\x06")
    if at < 0 or len(tail) - at < 22:
        return False
    _, disk, start, local_count, count, central_size, offset, comment = struct.unpack_from("<4s4H2LH", tail, at)
    return (disk == start == 0 and local_count == count and count <= MAX_ARCHIVE_MEMBERS
            and central_size <= MAX_CENTRAL_BYTES and offset != 0xffffffff
            and at + 22 + comment == len(tail)
            and offset + central_size <= size - len(tail) + at)



def _safe_member(name: str) -> bool:
    path = name.replace(chr(92), "/")
    return (bool(path) and not path.startswith("/")
            and not any(part in ("", ".", "..") for part in path.split("/")))


def single_zip_fingerprint(path: Path) -> tuple[int, str] | None:
    """Read the sole file's size and CRC from bounded ZIP metadata."""
    try:
        if path.is_symlink():
            return None
        with path.open("rb") as source:
            if not _bounded_directory(source):
                return None
            with zipfile.ZipFile(source) as archive:
                members = [info for info in archive.infolist() if not info.is_dir()]
                if len(members) != 1:
                    return None
                info = members[0]
                if (not _safe_member(info.filename) or info.flag_bits & 1
                        or stat.S_ISLNK(info.external_attr >> 16)
                        or not 0 < info.file_size <= MAX_MEMBER_BYTES):
                    return None
                return info.file_size, f"{info.CRC:08x}"
    except (OSError, zipfile.BadZipFile, ValueError, NotImplementedError):
        return None


def audit_zip(path: Path, platform, dats):
    """Return the DAT verdict; unsafe or incomplete archives are unknown.

    Member reads reach EOF so zipfile checks CRC, and hash_stream keeps RAM bounded.
    """
    try:
        with path.open("rb") as source:
            if not _bounded_directory(source):
                return Match(UNKNOWN, detail="archive directory limit or unsupported ZIP layout")
        with zipfile.ZipFile(path) as archive:
            all_infos = archive.infolist()
            infos = [info for info in all_infos if not info.is_dir()]
            if len(all_infos) > MAX_ARCHIVE_MEMBERS:
                return Match(UNKNOWN, detail="archive member limit exceeded")
            total, by_name, seen = 0, {}, set()
            for info in infos:
                mode = info.external_attr >> 16
                if (not _safe_member(info.filename) or stat.S_ISLNK(mode)
                        or info.filename.replace(chr(92), "/").casefold() in seen
                        or info.file_size < 0 or info.file_size > MAX_MEMBER_BYTES):
                    return Match(UNKNOWN, detail="unsafe or ambiguous archive member")
                total += info.file_size
                if total > MAX_ARCHIVE_BYTES:
                    return Match(UNKNOWN, detail="archive size limit exceeded")
                by_name[info.filename] = info
                seen.add(info.filename.replace(chr(92), "/").casefold())

            def read_sheet(name):
                """Bound decompressed sheet reads before selection or reference parsing."""
                info = by_name.get(name)
                if info is None or info.file_size > MAX_SHEET_BYTES:
                    raise ValueError("archive sheet size limit exceeded")
                with archive.open(info) as stream:
                    data = stream.read(MAX_SHEET_BYTES + 1)
                if len(data) > MAX_SHEET_BYTES:
                    raise ValueError("archive sheet size limit exceeded")
                return data
            sets = pick_all_rom_sets(list(by_name), platform, read=read_sheet)
            if not sets:
                return Match(UNKNOWN)
            outcomes = []
            for rom_set in sets:
                for sheet in rom_set.members:
                    suffix = Path(sheet).suffix.lower()
                    parser = (_playlist_names if suffix == ".m3u" else
                              _cue_and_gdi_names if suffix in (".cue", ".gdi") else None)
                    if parser:
                        referenced = _names_in_sheet(sheet, read_sheet, parser)
                        parent = PurePosixPath(sheet.replace(chr(92), "/")).parent
                        selected = {n.replace(chr(92), "/").casefold() for n in rom_set.members}
                        def exact_reference(name, parent=parent):
                            """Keep sheet-relative directories instead of matching just basenames."""
                            name = name.replace(chr(92), "/")
                            if name.startswith("/"):
                                return ""
                            normalized = posixpath.normpath(str(parent / name))
                            return "" if normalized.startswith("../") else normalized.casefold()
                        if referenced is None or any(exact_reference(name) not in selected for name in referenced):
                            return Match(UNKNOWN, detail="incomplete archive set")
                verdicts = []
                for member in rom_set.members:
                    info = by_name.get(member)
                    if info is None:
                        return Match(UNKNOWN, detail="incomplete archive set")
                    with archive.open(info) as stream:
                        hashes = hash_stream(stream, suffix=Path(member).suffix)
                    verdicts.append(dats.lookup(**hashes))
                if verdicts and all(v.status == VERIFIED for v in verdicts):
                    outcomes.append(verdicts[0])
                else:
                    bad = next((v for v in verdicts if v.status == BAD_DUMP), None)
                    outcomes.append(bad or Match(UNKNOWN))
            if outcomes and all(v.status == VERIFIED for v in outcomes):
                return outcomes[0]
            bad = next((v for v in outcomes if v.status == BAD_DUMP), None)
            return bad or Match(UNKNOWN)
    except Exception:
        return Match(UNKNOWN, detail="could not safely read ZIP archive")
