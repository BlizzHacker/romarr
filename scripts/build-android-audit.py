#!/usr/bin/env python3
"""Audit native ELF alignment, including modules inside Chaquopy archives."""
import io
import json
import struct
import sys
import zipfile
from pathlib import Path


def inspect_archive(data, prefix=""):
    """Read every ELF shared library without extracting or executing it."""
    rows = []
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        for name in archive.namelist():
            if not name.endswith((".so", ".imy")):
                continue
            content = archive.read(name)
            if content.startswith(b"PK\x03\x04"):
                rows.extend(inspect_archive(content, prefix + name + "!/"))
            elif content.startswith(b"\x7fELF"):
                if content[5] != 1:
                    raise ValueError("Only little-endian ELF is supported")
                if content[4] == 2:
                    offset = struct.unpack_from("<Q", content, 32)[0]
                    size, count = struct.unpack_from("<HH", content, 54)
                    alignment_offset, fmt = 48, "<Q"
                else:
                    offset = struct.unpack_from("<I", content, 28)[0]
                    size, count = struct.unpack_from("<HH", content, 42)
                    alignment_offset, fmt = 28, "<I"
                alignments = [
                    struct.unpack_from(fmt, content, offset + i * size + alignment_offset)[0]
                    for i in range(count)
                    if struct.unpack_from("<I", content, offset + i * size)[0] == 1
                ]
                rows.append({"path": prefix + name,
                             "load_alignment": min(alignments) if alignments else None})
    return rows


def main():
    """Report all packaged ELF PT_LOAD segments and fail below 16 KB."""
    path = Path(sys.argv[1])
    rows = inspect_archive(path.read_bytes())
    passed = bool(rows) and all(row["load_alignment"] is None
                               or row["load_alignment"] >= 16384 for row in rows)
    print(json.dumps({"artifact": path.name, "elf_files": len(rows),
                      "pass": passed, "libraries": rows}, indent=2))
    raise SystemExit(0 if passed else 1)


if __name__ == "__main__":
    main()
