"""Virus scanning before import, through a ClamAV daemon.

ROMarr copies what a download client finished into the library, and a
library is what other machines play from. A release that arrived with
something else inside it -- an `.exe` beside the ROM, a "patcher", a poisoned
archive -- should be stopped before it lands there, not found afterwards.

Off by default. With `clamav_enabled` set, every finished download is sent to
clamd before anything is imported:

  * clean            -> imported as usual;
  * infected         -> refused, recorded with the signature, and counted as
                        the release's fault, so failed download handling
                        blocklists it and grabs another;
  * clamd unreachable -> not imported *yet*: the download waits for the next
                        import sweep rather than going in unscanned.

The bytes are streamed (INSTREAM) rather than handed over as a path, because
clamd usually runs in another container and sees another filesystem. One
limit follows from that: clamd refuses a stream longer than its
`StreamMaxLength`. Such a file is imported unscanned and logged by name --
the same call a scanner on the download folder makes for files over its own
size limit -- because a multi-gigabyte disc image is data, and refusing it
would leave every large game unimportable.

Standard library only: the protocol is a few bytes on a socket.
"""

from __future__ import annotations

import socket
import struct
from dataclasses import dataclass
from pathlib import Path

#: How much of a file goes to clamd per INSTREAM chunk.
CHUNK = 1 << 20

#: Seconds to wait on clamd. A large archive takes it a while.
TIMEOUT = 600


class ScanUnavailable(Exception):
    """clamd could not be asked, so nothing is known about the file."""


@dataclass(frozen=True)
class Finding:
    path: str
    signature: str


def _connect(address: str, timeout: float) -> socket.socket:
    """`tcp://host:port`, `host:port`, `unix:///path` or `/path`."""
    address = address.strip()
    if address.startswith(("/", "unix://")):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        sock.connect(address.removeprefix("unix://"))
        return sock
    host, _, port = address.removeprefix("tcp://").rpartition(":")
    if not host:
        host, port = port, "3310"
    return socket.create_connection((host, int(port)), timeout=timeout)


def _command(address: str, command: bytes, send=None, timeout: float = TIMEOUT) -> str:
    try:
        with _connect(address, timeout) as sock:
            sock.sendall(b"z" + command + b"\0")
            if send is not None:
                try:
                    send(sock)
                except OSError:
                    # clamd closes the stream when a file exceeds its limit;
                    # its reply, read below, says which case this was.
                    pass
            reply = b""
            while chunk := sock.recv(4096):
                reply += chunk
    except (OSError, ValueError) as err:
        raise ScanUnavailable(f"ClamAV at {address} is unreachable: {err}") from err
    text = reply.rstrip(b"\0").decode("utf-8", "replace").strip()
    if not text:
        raise ScanUnavailable(f"ClamAV at {address} closed the connection without answering")
    return text


def version(address: str) -> str:
    """clamd's version and signature date, which doubles as a liveness test."""
    return _command(address, b"VERSION", timeout=10)


def _scan_file(address: str, path: Path) -> str:
    def send(sock):
        with path.open("rb") as fh:
            while chunk := fh.read(CHUNK):
                sock.sendall(struct.pack("!L", len(chunk)) + chunk)
        sock.sendall(struct.pack("!L", 0))
    return _command(address, b"INSTREAM", send)


def scan(address: str, target: Path) -> tuple[list[Finding], list[str]]:
    """(infected files, files too large for clamd to take) under `target`.

    clamd unpacks archives itself, so a zip or 7z is one stream and what is
    inside it is still scanned. Raises ScanUnavailable when a verdict could
    not be had for some file -- never a guess.
    """
    files = ([target] if target.is_file()
             else sorted(p for p in target.rglob("*") if p.is_file()))
    found: list[Finding] = []
    too_large: list[str] = []
    for path in files:
        reply = _scan_file(address, path)
        if reply.endswith("FOUND"):
            signature = reply.removeprefix("stream:").removesuffix("FOUND").strip()
            found.append(Finding(str(path), signature))
        elif "size limit exceeded" in reply.lower():
            too_large.append(str(path))
        elif not reply.endswith("OK"):
            raise ScanUnavailable(f"ClamAV could not scan {path.name}: {reply}")
    return found, too_large
