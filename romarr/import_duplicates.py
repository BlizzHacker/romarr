"""Confirm indexed import duplicates against retained raw or ZIP-member bytes."""
from __future__ import annotations

import hashlib
import lzma
import zlib
import os
import stat
import struct
import zipfile
from pathlib import Path

from .audit_archive import MAX_ARCHIVE_MEMBERS, MAX_MEMBER_BYTES, MAX_ARCHIVE_BYTES, MAX_CENTRAL_BYTES
from .file_reports import _open_source
from .platforms import resolve


def stream_sha1(source):
    """Hash physical bytes, retaining copier headers and bounded memory."""
    digest = hashlib.sha1(usedforsecurity=False)
    for block in iter(lambda: source.read(1024 * 1024), b""):
        digest.update(block)
    return digest.hexdigest()


def _identity(source):
    """Compare metadata from the same opened object before and after hashing."""
    item = os.fstat(source.fileno())
    return item.st_dev, item.st_ino, item.st_size, item.st_mtime_ns, item.st_ctime_ns


def _zip_contains(source, wanted, platform_slug, suffix):
    """Confirm a retained ROM member without extraction or unbounded ZIP metadata."""
    platform = resolve(platform_slug)
    if platform is None or platform.archive_is_the_rom:
        return False
    source.seek(0, 2)
    size = source.tell()
    source.seek(max(0, size - 65557))
    tail = source.read(65557)
    at = tail.rfind(b"PK\x05\x06")
    if at < 0 or len(tail) - at < 22:
        return False
    _, disk, start, local_count, count, central_size, offset, comment = struct.unpack_from("<4s4H2LH", tail, at)
    if not (disk == start == 0 and local_count == count and count <= MAX_ARCHIVE_MEMBERS
            and central_size <= MAX_CENTRAL_BYTES and offset != 0xffffffff
            and at + 22 + comment == len(tail)
            and offset + central_size <= size - len(tail) + at):
        return False
    eocd_offset = size - len(tail) + at
    if eocd_offset >= 20:
        source.seek(eocd_offset - 20)
        if source.read(4) == b"PK\x06\x07":
            return False
    source.seek(0)
    with zipfile.ZipFile(source) as archive:
        infos = archive.infolist()
        if len(infos) > MAX_ARCHIVE_MEMBERS:
            return False
        seen, total = set(), 0
        for info in infos:
            if info.is_dir():
                continue
            name = info.filename.replace(chr(92), "/")
            if (name.startswith("/") or ":" in name or any(part in ("", ".", "..") for part in name.split("/"))
                    or stat.S_ISLNK(info.external_attr >> 16) or name.casefold() in seen
                    or not 0 <= info.file_size <= MAX_MEMBER_BYTES):
                return False
            seen.add(name.casefold())
            total += info.file_size
            if total > MAX_ARCHIVE_BYTES:
                return False
        extensions = set(platform.extensions) | {str(suffix).lower()}
        for info in infos:
            if not info.is_dir() and Path(info.filename).suffix.lower() in extensions:
                with archive.open(info) as member:
                    if stream_sha1(member) == wanted:
                        return True
    return False


def duplicate_reason(index, sha1, roots, *, suffix="", published=None):
    """Report only indexed copies whose retained bytes still match inside known roots."""
    if index is None or not sha1:
        return ""
    entry = (published or {}).get(sha1) or index.by_sha1(sha1)
    if entry is None or not entry.path:
        return ""
    for root in roots:
        root = Path(root).absolute()
        try:
            path = Path(str(entry.path).replace(chr(92), "/"))
            relative = path.relative_to(root) if path.is_absolute() else path
            with _open_source(root, relative.as_posix()) as source:
                before = _identity(source)
                same = stream_sha1(source) == sha1
                if not same and relative.suffix.lower() == ".zip":
                    same = _zip_contains(source, sha1, entry.platform, suffix)
                if not same or before != _identity(source):
                    continue
            return f"already in the library ({entry.platform or 'unknown'}: {relative.as_posix()})"
        except (OSError, ValueError, RuntimeError, zipfile.BadZipFile, EOFError, zlib.error, lzma.LZMAError):
            continue
    return ""
