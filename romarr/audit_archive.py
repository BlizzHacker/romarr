"""Safe, bounded DAT verification for ZIPs already in a folder library."""
from __future__ import annotations

import stat
import zipfile
from pathlib import Path, PurePosixPath

from .dat import BAD_DUMP, UNKNOWN, VERIFIED, Match, hash_stream
from .selection import (_cue_and_gdi_names, _names_in_sheet, _playlist_names,
                       pick_all_rom_sets)

MAX_ARCHIVE_MEMBERS = 10_000
MAX_MEMBER_BYTES = 2 * 1024 * 1024 * 1024
MAX_ARCHIVE_BYTES = 8 * 1024 * 1024 * 1024


def _safe_member(name: str) -> bool:
    path = name.replace(chr(92), "/")
    return (bool(path) and not path.startswith("/")
            and not any(part in ("", ".", "..") for part in path.split("/")))


def audit_zip(path: Path, platform, dats):
    """Return the DAT verdict; unsafe or incomplete archives are unknown.

    Member reads reach EOF so zipfile checks CRC, and hash_stream keeps RAM bounded.
    """
    try:
        with zipfile.ZipFile(path) as archive:
            infos = [info for info in archive.infolist() if not info.is_dir()]
            if len(infos) > MAX_ARCHIVE_MEMBERS:
                return Match(UNKNOWN, detail="archive member limit exceeded")
            total, by_name = 0, {}
            for info in infos:
                mode = info.external_attr >> 16
                if (not _safe_member(info.filename) or stat.S_ISLNK(mode)
                        or info.filename.casefold() in by_name
                        or info.file_size < 0 or info.file_size > MAX_MEMBER_BYTES):
                    return Match(UNKNOWN, detail="unsafe or ambiguous archive member")
                total += info.file_size
                if total > MAX_ARCHIVE_BYTES:
                    return Match(UNKNOWN, detail="archive size limit exceeded")
                by_name[info.filename] = info

            from .library import _ZipSource
            reader = _ZipSource(path)
            sets = pick_all_rom_sets(list(by_name), platform, read=reader.read)
            if not sets:
                return Match(UNKNOWN)
            outcomes = []
            for rom_set in sets:
                for sheet in rom_set.members:
                    suffix = Path(sheet).suffix.lower()
                    parser = (_playlist_names if suffix == ".m3u" else
                              _cue_and_gdi_names if suffix in (".cue", ".gdi") else None)
                    if parser:
                        referenced = _names_in_sheet(sheet, reader.read, parser)
                        parent = PurePosixPath(sheet).parent
                        nearby = {(PurePosixPath(n).parent,
                                   PurePosixPath(n).name.casefold()) for n in by_name}
                        if referenced is None or any(
                                (parent, PurePosixPath(name.replace(chr(92), "/")).name.casefold())
                                not in nearby for name in referenced):
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
