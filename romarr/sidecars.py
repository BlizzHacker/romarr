"""Read bounded NFO sidecars for cached local folder games, without extracting archives."""
from __future__ import annotations

import os
import stat
from pathlib import Path

MAX_BYTES = 128 * 1024


def read_nfo(root: Path, game_path: str) -> dict:
    """Read one same-stem NFO through pinned, symlink-free directory descriptors.

    Platforms without descriptor-relative no-follow opens fail closed. Neither
    a filename from an HTTP caller nor arbitrary neighbouring files are accepted.
    """
    if os.open not in os.supports_dir_fd or not hasattr(os, "O_NOFOLLOW"):
        return {"available": False, "reason": "Secure sidecar reading is unavailable on this platform."}
    try:
        root = Path(root).absolute()
        relative = Path(game_path).relative_to(root)
        if not relative.parts or any(x in ("", ".", "..") for x in relative.parts):
            return {"available": False}
        # Pin every component, including the configured root's ancestors.
        directory = os.open(root.anchor, os.O_RDONLY | os.O_DIRECTORY)
        try:
            for component in (*root.parts[1:], *relative.parts[:-1]):
                child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                dir_fd=directory)
                os.close(directory)
                directory = child
            # A stale catalogue row must not turn into a neighbouring-file reader.
            game_fd = os.open(relative.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                              dir_fd=directory)
            try:
                if not stat.S_ISREG(os.fstat(game_fd).st_mode):
                    return {"available": False}
            finally:
                os.close(game_fd)
            # Explicit casing variants avoid an unbounded directory scan.
            for suffix in (".nfo", ".NFO"):
                filename = relative.with_suffix(suffix).name
                try:
                    fd = os.open(filename, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                                 dir_fd=directory)
                except FileNotFoundError:
                    continue
                with os.fdopen(fd, "rb") as stream:
                    if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                        return {"available": False}
                    raw = stream.read(MAX_BYTES + 1)
                truncated = len(raw) > MAX_BYTES
                raw = raw[:MAX_BYTES]
                try:
                    text = raw.decode("utf-8-sig")
                    encoding = "utf-8"
                except UnicodeDecodeError:
                    text = raw.decode("cp437")
                    encoding = "cp437"
                # Preserve ASCII art and line breaks, remove terminal controls.
                text = "".join(c for c in text if c in "\n\r\t" or (ord(c) >= 32 and ord(c) != 127))
                return {"available": True, "filename": filename, "text": text,
                        "encoding": encoding, "truncated": truncated}
        finally:
            os.close(directory)
    except (OSError, ValueError):
        pass
    return {"available": False}
