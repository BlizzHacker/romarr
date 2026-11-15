"""Read bounded NFO sidecars for cached local folder games, without extracting archives."""
from __future__ import annotations

import os
import codecs
import sys
from pathlib import Path

from .file_reports import _PinnedLibrary

MAX_BYTES = 128 * 1024


def read_nfo(root: Path, game_path: str) -> dict:
    """Read a same-stem NFO with shared secure POSIX/Windows filesystem pins."""
    if sys.platform != "win32" and (os.open not in os.supports_dir_fd or not hasattr(os, "O_NOFOLLOW")):
        return {"available": False, "reason": "Secure sidecar reading is unavailable on this platform."}
    try:
        root = Path(root).absolute()
        relative = Path(game_path).absolute().relative_to(root)
        if not relative.parts or any(x in ("", ".", "..") for x in relative.parts):
            return {"available": False}
        with _PinnedLibrary(root) as pins:
            # A stale catalogue row must not turn into a neighbouring-file reader.
            with pins.open(pins.parent(relative.as_posix())):
                pass
            # Explicit casing variants avoid an unbounded directory scan.
            for suffix in (".nfo", ".NFO"):
                sidecar = relative.with_suffix(suffix)
                try:
                    with pins.open(pins.parent(sidecar.as_posix())) as stream:
                        raw = stream.read(MAX_BYTES + 1)
                        pins.validate()
                except FileNotFoundError:
                    continue
                truncated = len(raw) > MAX_BYTES
                raw = raw[:MAX_BYTES]
                try:
                    text = codecs.getincrementaldecoder("utf-8-sig")().decode(raw, final=not truncated)
                    encoding = "utf-8"
                except UnicodeDecodeError:
                    text = raw.decode("cp437")
                    encoding = "cp437"
                # Preserve ASCII art and line breaks, remove terminal controls.
                text = "".join(c for c in text if c in "\n\r\t" or (ord(c) >= 32 and ord(c) != 127))
                return {"available": True, "filename": sidecar.name, "text": text,
                        "encoding": encoding, "truncated": truncated}
    except (OSError, ValueError):
        pass
    return {"available": False}
