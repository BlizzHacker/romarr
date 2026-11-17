"""Read bounded tar-family containers without extracting archive paths to disk."""
from __future__ import annotations

import bz2
import contextlib
import gzip
import io
import lzma
import os
from pathlib import Path, PurePosixPath
import re
import tarfile
import zlib
from contextlib import ExitStack

from . import nested_zip as limits
from .dat import hash_stream
from .file_reports import _PinnedLibrary, _fd_identity, _object_id

TAR_SUFFIXES = (".tar.gz", ".tar.bz2", ".tar.xz", ".tar", ".tgz", ".tbz2", ".txz")
MAX_METADATA_BYTES = 16 * 1024 * 1024
MAX_LZMA_MEMORY = 128 * 1024 * 1024
#: Records in one PAX header. Global headers are copied into every member.
MAX_PAX_RECORDS = 256


def is_tar_name(name):
    return str(name).lower().endswith(TAR_SUFFIXES)


class _Replay:
    """Replay checked PAX bytes once without seeking a compressed stream backward."""
    def __init__(self, data, original):
        self.data, self.original = data, original
    def read(self, size):
        if self.data:
            result, self.data = self.data[:size], self.data[size:]
            if len(result) == size:
                return result
            return result + self.original.read(size - len(result))
        return self.original.read(size)
    def tell(self):
        return self.original.tell() - len(self.data)
    def __getattr__(self, name):
        return getattr(self.original, name)


class _BoundedInfo(tarfile.TarInfo):
    def _proc_member(self, archive):
        archive._romarr_headers = getattr(archive, "_romarr_headers", 0) + 1
        if archive._romarr_headers > 3 * limits.MAX_ZIP_MEMBERS:
            raise ValueError("TAR header-count limit exceeded")
        if self.issym() or self.islnk() or self.isdev() or self.type == tarfile.GNUTYPE_SPARSE:
            raise ValueError("TAR links, devices and sparse files are not supported")
        if self.size < 0 or self.size > limits.MAX_FINAL_MEMBER_BYTES:
            raise ValueError("TAR member size limit exceeded")
        if self.type in (tarfile.XHDTYPE, tarfile.XGLTYPE, tarfile.SOLARIS_XHDTYPE,
                         tarfile.GNUTYPE_LONGNAME, tarfile.GNUTYPE_LONGLINK):
            total = getattr(archive, "_romarr_metadata", 0) + self.size
            if self.size > limits.MAX_DESCRIPTOR_BYTES or total > MAX_METADATA_BYTES:
                raise ValueError("TAR extended-metadata size limit exceeded")
            archive._romarr_metadata = total
        return super()._proc_member(archive)

    def _proc_pax(self, archive):
        original = archive.fileobj
        data = original.read(self._block(self.size))
        # Refuse sparse extensions before tarfile parses an attacker-sized sparse map.
        if re.search(rb"(?:^|\n)[0-9]+ (?:GNU\.sparse[.=]|SCHILY\.realsize=)", data):
            raise ValueError("Sparse PAX files are not supported")
        if data.count(b"\n") > MAX_PAX_RECORDS:
            raise ValueError("TAR extended-metadata record limit exceeded")
        archive.fileobj = _Replay(data, original)
        try:
            return super()._proc_pax(archive)
        finally:
            archive.fileobj = original


class _LzmaReader:
    """Bound both XZ decoder memory and bytes emitted by one read."""
    def __init__(self, source):
        self.source = source
        self.decoder = lzma.LZMADecompressor(memlimit=MAX_LZMA_MEMORY)
        self.pending = b""
        self.done = False
    def read(self, size):
        if self.done:
            return b""
        size = min(size, limits.COPY_CHUNK)
        while True:
            if self.decoder.eof:
                # XZ allows zero-byte stream padding in multiples of four,
                # between concatenated streams and after the last one.
                data, padding = self.decoder.unused_data, 0
                while True:
                    if not data:
                        data = self.source.read(65536)
                        if not data:
                            if padding % 4:
                                raise ValueError("Invalid XZ stream padding")
                            self.done = True
                            return b""
                    stripped = data.lstrip(b"\0")
                    padding += len(data) - len(stripped)
                    data = stripped
                    if data:
                        break
                if padding % 4:
                    raise ValueError("Invalid XZ stream padding")
                self.pending = data
                self.decoder = lzma.LZMADecompressor(memlimit=MAX_LZMA_MEMORY)
            if self.decoder.needs_input:
                data, self.pending = self.pending or self.source.read(65536), b""
                if not data:
                    raise ValueError("Truncated XZ stream")
            else:
                data = b""
            result = self.decoder.decompress(data, max_length=size)
            if result:
                return result
    def close(self):
        self.done = True


class _CountingReader:
    """Cap decompressed bytes independently of TAR declarations."""
    def __init__(self, source, physical_size):
        self.source, self.total = source, 0
        self.limit = min(limits.MAX_EXPANDED_BYTES + MAX_METADATA_BYTES,
                         max(512, physical_size) * limits.MAX_COMPRESSION_RATIO)
    def read(self, size):
        data = self.source.read(min(size, limits.COPY_CHUNK))
        self.total += len(data)
        if self.total > self.limit:
            raise ValueError("TAR decompressed-byte/compression-ratio limit exceeded")
        return data


class TarSource:
    """Keep a no-follow source handle and expose only regular, bounded members."""
    def __init__(self, path):
        self.path = Path(path).absolute()
        self._cleanup = ExitStack()
        self.members = {}
        try:
            self.pins = self._cleanup.enter_context(_PinnedLibrary(self.path.parent))
            self.location = self.pins.parent(self.path.name)
            self.source = self._cleanup.enter_context(self.pins.open(self.location))
            self.identity = _fd_identity(self.source)
            total = 0
            seen = set()
            with self._archive() as archive:
                for count, info in enumerate(archive, 1):
                    archive.members.clear()
                    if count > limits.MAX_ZIP_MEMBERS:
                        raise ValueError("TAR member-count limit exceeded")
                    if info.sparse is not None or not (info.isfile() or info.isdir()):
                        raise ValueError("Unsupported TAR member type")
                    if info.size < 0 or info.size > limits.MAX_FINAL_MEMBER_BYTES:
                        raise ValueError("TAR member size limit exceeded")
                    total += info.size
                    if total > limits.MAX_EXPANDED_BYTES or total > max(512, self.identity[2]) * limits.MAX_COMPRESSION_RATIO:
                        raise ValueError("TAR expanded-size/compression-ratio limit exceeded")
                    name = info.name
                    parts = PurePosixPath(name)
                    if parts.is_absolute() or ".." in parts.parts:
                        continue
                    if (not name or "\\" in name or ":" in name
                            or any(ord(c) < 32 or 0xD800 <= ord(c) <= 0xDFFF for c in name)):
                        raise ValueError("Unsafe TAR member name")
                    name = parts.as_posix()
                    if name in ("", ".") or info.isdir():
                        continue
                    if name.casefold() in seen:
                        raise ValueError("Duplicate/aliased TAR member")
                    seen.add(name.casefold())
                    if name.lower().endswith((".cue", ".gdi", ".m3u")) and info.size > limits.MAX_DESCRIPTOR_BYTES:
                        raise ValueError("TAR descriptor size limit exceeded")
                    self.members[name] = (info.name, info.size)
            self._validate()
        except BaseException:
            self.close()
            raise

    def _validate(self):
        self.pins.validate()
        current = self.pins.entry_stat(self.location)
        if (_object_id(current) != _object_id(os.fstat(self.source.fileno()))
                or _fd_identity(self.source) != self.identity):
            raise ValueError("TAR source changed after inspection")

    @contextlib.contextmanager
    def _archive(self):
        self._validate()
        self.source.seek(0)
        name = self.path.name.lower()
        if name.endswith((".tar.gz", ".tgz")):
            decoded = gzip.GzipFile(fileobj=self.source, mode="rb")
        elif name.endswith((".tar.bz2", ".tbz2")):
            decoded = bz2.BZ2File(self.source, mode="rb")
        elif name.endswith((".tar.xz", ".txz")):
            decoded = _LzmaReader(self.source)
        else:
            decoded = self.source
        counted = _CountingReader(decoded, self.identity[2])
        try:
            with tarfile.open(fileobj=counted, mode="r|", tarinfo=_BoundedInfo) as archive:
                yield archive
            if decoded is not self.source:
                # tarfile stops at the end-of-archive marker; the gzip CRC and
                # the bz2/xz checks only run once the stream is read to its end.
                while counted.read(limits.COPY_CHUNK):
                    pass
            self._validate()
        except (tarfile.TarError, lzma.LZMAError, zlib.error, EOFError, OSError) as exc:
            raise ValueError("Invalid or unsupported TAR stream") from exc
        finally:
            if decoded is not self.source:
                decoded.close()

    def names(self):
        return list(self.members)

    def member_size(self, name):
        entry = self.members.get(name)
        return entry[1] if entry else None

    def rom_sets(self, platform):
        from .selection import pick_all_rom_sets
        return pick_all_rom_sets(self.names(), platform, read=self.read)

    def _consume(self, name, consume):
        wanted = self.members.get(name)
        if wanted is None:
            raise ValueError("Unknown TAR member")
        with self._archive() as archive:
            for info in archive:
                archive.members.clear()
                if info.name == wanted[0]:
                    if not info.isfile() or info.size != wanted[1]:
                        raise ValueError("TAR member changed after inspection")
                    with archive.extractfile(info) as source:
                        return consume(source)
        raise ValueError("TAR member disappeared")

    def head(self, name, size):
        return self._consume(name, lambda source: source.read(min(size, limits.MAX_DESCRIPTOR_BYTES)))

    def read(self, name):
        if self.member_size(name) is None:
            return None
        if self.member_size(name) > limits.MAX_DESCRIPTOR_BYTES:
            raise ValueError("TAR descriptor read exceeds size limit")
        return self._consume(name, lambda source: source.read(limits.MAX_DESCRIPTOR_BYTES + 1))

    def hash(self, name, suffix=""):
        return self._consume(name, lambda source: hash_stream(source, suffix=suffix))

    def copy(self, name, destination):
        def write(source):
            size = 0
            with open(destination, "wb") as output:
                while block := source.read(limits.COPY_CHUNK):
                    size += len(block)
                    if size > self.member_size(name):
                        raise ValueError("TAR member expanded beyond its declaration")
                    output.write(block)
            if size != self.member_size(name):
                raise ValueError("Truncated TAR member")
        self._consume(name, write)

    def close(self):
        self._cleanup.close()
