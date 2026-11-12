"""Bounded inspection of ZIP files stored inside ZIP downloads.

Only inner ZIP containers are staged. Playable members remain in their owning
archive and use ROMarr's existing streaming hash/copy implementation.
"""

from __future__ import annotations

import stat
import struct
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Callable

MAX_ZIP_LAYERS = 3
MAX_INNER_ARCHIVES = 16
MAX_ZIP_MEMBERS = 10_000
MAX_INNER_BYTES = 256 * 1024 * 1024
MAX_STAGED_BYTES = 512 * 1024 * 1024
MAX_FINAL_MEMBER_BYTES = 64 * 1024 * 1024 * 1024
MAX_EXPANDED_BYTES = 128 * 1024 * 1024 * 1024
MAX_COMPRESSION_RATIO = 10_000
MAX_DESCRIPTOR_BYTES = 2 * 1024 * 1024
MAX_CENTRAL_DIRECTORY_BYTES = 16 * 1024 * 1024
COPY_CHUNK = 1024 * 1024


def check_zip_central_directory(path: Path) -> None:
    """Bound ZIP directory size and entries before ZipFile allocates ZipInfo.

    Verify the end record (including ZIP64 when needed), cap directory bytes,
    and count central headers independently of the untrusted declared count.
    """
    with path.open("rb") as stream:
        stream.seek(0, 2)
        size = stream.tell()
        tail_size = min(size, 65_557)
        stream.seek(size - tail_size)
        tail = stream.read(tail_size)
        # ZipFile uses the last EOCD signature even when its declared ZIP
        # comment length does not reach EOF. Select the same signature, but
        # refuse inconsistent comments rather than preflight a different EOCD.
        offset = tail.rfind(b"PK\x05\x06")
        if offset < 0 or offset + 22 > tail_size:
            raise ValueError("ZIP end-of-central-directory record missing")
        comment_length = struct.unpack_from("<H", tail, offset + 20)[0]
        if offset + 22 + comment_length != tail_size:
            raise ValueError("ambiguous ZIP end-of-central-directory record")
        end = size - tail_size + offset
        disk, directory_disk, disk_entries, entries, central_bytes, central_offset, _ = struct.unpack_from(
            "<HHHHIIH", tail, offset + 4)
        if disk or directory_disk:
            raise ValueError("multi-volume ZIP is not supported")
        # CPython follows a ZIP64 locator even when classic EOCD fields do
        # not carry ZIP64 sentinel values. Mirror that selection here or a
        # forged small classic entry count could bypass the preflight.
        locator = b""
        if end >= 20:
            stream.seek(end - 20)
            locator = stream.read(20)
        zip64_required = (disk_entries == 0xFFFF or entries == 0xFFFF
                          or central_bytes == 0xFFFFFFFF
                          or central_offset == 0xFFFFFFFF)
        if locator[:4] == b"PK\x06\x07" or zip64_required:
            if len(locator) != 20 or locator[:4] != b"PK\x06\x07":
                raise ValueError("ZIP64 locator missing")
            locator_disk, record_at, disk_count = struct.unpack_from("<IQI", locator, 4)
            if locator_disk or disk_count != 1 or end < 76:
                raise ValueError("invalid ZIP64 locator")
            # Recent Python may first consult the ZIP64 locator's offset,
            # then fall back to the adjacent record for prepended ZIPs.
            # Older versions read the adjacent record directly. Only allow
            # prepend-only offsets when both readers necessarily agree.
            effective_record_at = end - 20 - 56
            prefixed = record_at != effective_record_at
            if prefixed:
                if (record_at > effective_record_at
                        or central_offset + central_bytes != record_at):
                    raise ValueError("ZIP64 locator offset does not match adjacent record")
                stream.seek(record_at)
                pointed_record = stream.read(56)
                if (len(pointed_record) != 56
                        or pointed_record.startswith(b"PK\x06\x06")):
                    raise ValueError("ambiguous ZIP64 locator offset")
            stream.seek(effective_record_at)
            record = stream.read(56)
            if len(record) != 56 or record[:4] != b"PK\x06\x06":
                raise ValueError("invalid ZIP64 end-of-central-directory record")
            record_bytes, _, _, disk, directory_disk, disk_entries, entries, central_bytes, _ = struct.unpack_from(
                "<QHHIIQQQQ", record, 4)
            if record_bytes < 44 or disk or directory_disk:
                raise ValueError("unsupported ZIP64 directory")
            if prefixed and record_bytes != 44:
                raise ValueError("unsupported prefixed ZIP64 extended record")
            directory_end = effective_record_at
        else:
            directory_end = end
        if disk_entries != entries:
            raise ValueError("inconsistent ZIP directory entry count")
        if entries > MAX_ZIP_MEMBERS:
            raise ValueError("nested ZIP member-count limit exceeded")
        if central_bytes > MAX_CENTRAL_DIRECTORY_BYTES:
            raise ValueError("ZIP central-directory byte limit exceeded")
        if central_bytes > directory_end or directory_end > size:
            raise ValueError("invalid ZIP central-directory bounds")
        stream.seek(directory_end - central_bytes)
        directory = stream.read(central_bytes)
        if len(directory) != central_bytes:
            raise ValueError("truncated ZIP central directory")
        # ZIP headers have 46 fixed bytes followed by three length-prefixed
        # fields. Count actual headers, not a forged EOCD count or signatures
        # inside filenames/comments, before ZipFile allocates ZipInfo objects.
        position = 0
        observed = 0
        while position < len(directory):
            if (len(directory) - position < 46
                    or directory[position:position + 4] != b"PK\x01\x02"):
                raise ValueError("invalid ZIP central-directory header")
            filename_len, extra_len, comment_len = struct.unpack_from(
                "<HHH", directory, position + 28)
            position += 46 + filename_len + extra_len + comment_len
            if position > len(directory):
                raise ValueError("truncated ZIP central-directory entry")
            observed += 1
            if observed > MAX_ZIP_MEMBERS:
                raise ValueError("ZIP central-directory entry limit exceeded")
        if observed != entries:
            raise ValueError("inconsistent ZIP central-directory entry count")


class StagingBudget:
    """Bound the combined staged ZIP bytes for one download inspection."""

    def __init__(self, limit=None):
        self.limit = MAX_STAGED_BYTES if limit is None else limit
        self.used = 0

    def reserve(self, size: int) -> None:
        """Reject an archive before it can exceed the shared disk allowance."""
        if size < 0 or self.used + size > self.limit:
            raise ValueError("nested ZIP download staging size limit exceeded")
        self.used += size

    def release(self, size: int) -> None:
        """Return reservations when the owning scratch archives are removed."""
        if size < 0 or size > self.used:
            raise RuntimeError("nested ZIP staging reservation imbalance")
        self.used -= size


@dataclass(frozen=True)
class ZipLeaf:
    """A playable candidate's original member name and owning ZIP container."""

    archive_path: Path
    member: str
    size: int


class NestedZipIndex:
    """Inspect nested ZIPs with explicit bounds and temporary-file ownership.

    The caller retains this object during preview/import and thereby retains
    temporary inner-archive containers. Temporary data is removed by close()
    or when the underlying TemporaryDirectory object is garbage-collected.
    """

    def __init__(self, outer: Path, safe_name: Callable[[str, Path], bool], staging_budget=None):
        self.members: dict[str, ZipLeaf] = {}
        self._safe_name = safe_name
        self._temp = tempfile.TemporaryDirectory(prefix="romarr-nested-zip-")
        self._inner_count = 0
        self._staged_bytes = 0
        self._staging_budget = staging_budget
        self._reserved_bytes = 0
        self._examined = 0
        self._seen: set[str] = set()
        self._expanded_bytes = 0
        try:
            self._scan(outer, "", 1)
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        """Release scratch data and the shared budget reservation exactly once."""
        self._temp.cleanup()
        if self._staging_budget is not None and self._reserved_bytes:
            self._staging_budget.release(self._reserved_bytes)
            self._reserved_bytes = 0

    def _scan(self, path: Path, prefix: str, depth: int) -> None:
        """Index one ZIP without mixing leaf members from different ZIPs."""
        check_zip_central_directory(path)
        with zipfile.ZipFile(path) as archive:
            for info in archive.infolist():
                self._examined += 1
                if self._examined > MAX_ZIP_MEMBERS:
                    raise ValueError("nested ZIP member-count limit exceeded")
                if info.is_dir():
                    continue
                name = info.filename
                parts = PurePosixPath(name)
                if (not name or "\\" in name or ":" in name or "\x00" in name
                        or parts.is_absolute() or ".." in parts.parts
                        or not self._safe_name(name, path.parent)):
                    raise ValueError(f"unsafe nested ZIP member: {name!r}")
                mode = info.external_attr >> 16
                if stat.S_IFMT(mode) == stat.S_IFLNK:
                    raise ValueError(f"nested ZIP symlink is not allowed: {name!r}")
                if info.flag_bits & 0x1:
                    raise ValueError(f"encrypted nested ZIP member: {name!r}")
                qualified = prefix + name
                if info.file_size < 0 or info.file_size > MAX_FINAL_MEMBER_BYTES:
                    raise ValueError(f"nested ZIP member exceeds size limit: {qualified!r}")
                if (name.lower().endswith((".cue", ".gdi", ".m3u"))
                        and info.file_size > MAX_DESCRIPTOR_BYTES):
                    raise ValueError(f"oversized nested ZIP descriptor: {qualified!r}")
                if (info.file_size and info.compress_size == 0) or (
                        info.compress_size and info.file_size > MAX_COMPRESSION_RATIO * info.compress_size):
                    raise ValueError(f"nested ZIP compression ratio limit exceeded: {qualified!r}")
                if qualified in self._seen:
                    raise ValueError(f"duplicate nested ZIP member: {qualified!r}")
                self._seen.add(qualified)
                if name.lower().endswith(".zip"):
                    if depth >= MAX_ZIP_LAYERS:
                        raise ValueError(f"nested ZIP depth limit exceeded: {qualified!r}")
                    if self._inner_count >= MAX_INNER_ARCHIVES:
                        raise ValueError("nested ZIP archive-count limit exceeded")
                    if info.file_size > MAX_INNER_BYTES or self._staged_bytes + info.file_size > MAX_STAGED_BYTES:
                        raise ValueError(f"nested ZIP staging size limit exceeded: {qualified!r}")
                    if self._staging_budget is not None:
                        self._staging_budget.reserve(info.file_size)
                        self._reserved_bytes += info.file_size
                    self._inner_count += 1
                    staged = Path(self._temp.name) / f"{self._inner_count}.zip"
                    with archive.open(info) as reader, staged.open("xb") as writer:
                        copied = 0
                        while chunk := reader.read(COPY_CHUNK):
                            copied += len(chunk)
                            if copied > info.file_size or copied > MAX_INNER_BYTES:
                                raise ValueError(f"nested ZIP member expanded beyond size declaration: {qualified!r}")
                            writer.write(chunk)
                    if copied != info.file_size:
                        raise ValueError(f"nested ZIP member size changed: {qualified!r}")
                    self._staged_bytes += copied
                    self._scan(staged, qualified + "/", depth + 1)
                else:
                    self._expanded_bytes += info.file_size
                    if self._expanded_bytes > MAX_EXPANDED_BYTES:
                        raise ValueError("nested ZIP expanded-file budget exceeded")
                    self.members[qualified] = ZipLeaf(path, name, info.file_size)

    def names(self) -> list[str]:
        """Return distinct, qualified ROM candidate names across ZIP layers."""
        return list(self.members)

    def member(self, qualified: str) -> ZipLeaf | None:
        """Look up an archive leaf without accepting unknown or aliased names."""
        return self.members.get(qualified)
