"""Scanning finished downloads with ClamAV before they are imported.

The daemon here is a fake that speaks clamd's real wire protocol -- `z`
commands, INSTREAM's length-prefixed chunks, and the replies clamd sends,
including the one for a stream over its size limit -- so the client is tested
against the protocol rather than against itself.
"""

from __future__ import annotations

import socket
import socketserver
import struct
import threading

import pytest

from romarr import clamav
from romarr.app import QueueItem, ROMarr

EICAR = b"X5O!P%@AP[4\\PZX54(P^)7CC)7}$EICAR-STANDARD-ANTIVIRUS-TEST-FILE!$H+H*"


def fake_clamd(limit=1 << 20):
    class Handler(socketserver.BaseRequestHandler):
        def handle(self):
            buf = b""
            while not buf.endswith(b"\0"):
                buf += self.request.recv(1)
            command = buf[1:-1]
            if command == b"VERSION":
                self.request.sendall(b"ClamAV 1.4.3/27800/Fri Oct 16 2026\0")
                return
            data = b""
            while True:
                (size,) = struct.unpack("!L", self._exactly(4))
                if not size:
                    break
                data += self._exactly(size)
                if len(data) > limit:
                    self.request.sendall(b"INSTREAM size limit exceeded. ERROR\0")
                    return
            verdict = b"Eicar-Test-Signature FOUND" if EICAR in data else b"OK"
            self.request.sendall(b"stream: " + verdict + b"\0")

        def _exactly(self, n):
            out = b""
            while len(out) < n:
                chunk = self.request.recv(n - len(out))
                if not chunk:
                    raise ConnectionError
                out += chunk
            return out

    server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"tcp://127.0.0.1:{server.server_address[1]}"


@pytest.fixture
def clamd():
    server, address = fake_clamd()
    yield address
    server.shutdown()


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_version_answers(clamd):
    assert clamav.version(clamd).startswith("ClamAV 1.4.3")


def test_a_download_is_scanned_file_by_file(clamd, tmp_path):
    download = tmp_path / "Release"
    (download / "sub").mkdir(parents=True)
    (download / "game.sfc").write_bytes(b"\0" * 300_000)            # several chunks
    (download / "sub" / "setup.exe").write_bytes(b"MZ" + EICAR)
    found, too_large = clamav.scan(clamd, download)
    assert [(f.signature, f.path.endswith("setup.exe")) for f in found] == [
        ("Eicar-Test-Signature", True)]
    assert too_large == []


def test_a_file_over_the_stream_limit_is_reported_not_failed(tmp_path):
    server, address = fake_clamd(limit=1000)
    big = tmp_path / "disc.iso"
    big.write_bytes(b"\0" * (2 * clamav.CHUNK))
    assert clamav.scan(address, big) == ([], [str(big)])
    server.shutdown()


def test_an_unreachable_daemon_is_never_a_clean_verdict(tmp_path):
    (tmp_path / "a.sfc").write_bytes(b"x")
    with pytest.raises(clamav.ScanUnavailable):
        clamav.scan(f"127.0.0.1:{free_port()}", tmp_path / "a.sfc")


# -- the import sweep ----------------------------------------------------------

class FakeClient:
    name = "fake"
    protocol = "torrent"
    configured = True

    def __init__(self, rows):
        self._rows = rows

    def completed(self):
        return list(self._rows)


def sweep(tmp_path, payload: bytes, **settings):
    root = tmp_path / "roms"
    root.mkdir()
    service = ROMarr({"ROMARR_DATA": str(tmp_path / "s.json"),
                      "LIBRARY_KIND": "folder", "LIBRARY_PATH": str(root)})
    service.store.settings.update(settings)
    download = tmp_path / "dl" / "Super Metroid (USA)"
    download.mkdir(parents=True)
    (download / "Super Metroid (USA).sfc").write_bytes(payload)
    row = QueueItem("Super Metroid", "snes", "Super Metroid (USA)", 5, "grabbed",
                    release_id="btih:" + "c" * 40)
    service.queue.append(row)
    service.clients = [FakeClient([{"name": "Super Metroid (USA)",
                                    "content_path": str(download)}])]
    results = service.import_finished(retry_failed=False)
    return service, row, results, root / "snes" / "Super Metroid (USA).sfc"


def test_a_clean_download_is_imported(clamd, tmp_path):
    _, row, results, landed = sweep(tmp_path, b"\0" * 1024,
                                    clamav_enabled=True, clamav_address=clamd)
    assert results[0]["ok"] and landed.exists() and row.state == "imported"


def test_an_infected_download_is_refused_and_blamed_on_the_release(clamd, tmp_path):
    service, row, results, landed = sweep(tmp_path, EICAR,
                                          clamav_enabled=True, clamav_address=clamd)
    assert not landed.exists()
    assert "Eicar-Test-Signature" in results[0]["reason"]
    assert (row.state, row.release_fault) == ("import-failed", True)
    # So failed download handling blocklists it and looks for another.
    assert service.retire_dead_downloads()["blocklisted"] == 1


def test_while_clamav_is_down_downloads_wait(tmp_path):
    _, row, results, landed = sweep(tmp_path, b"\0" * 1024, clamav_enabled=True,
                                    clamav_address=f"127.0.0.1:{free_port()}")
    assert not landed.exists()
    assert row.state == "grabbed"            # untouched: the next sweep tries again
    assert results[0]["reason"].startswith("waiting for ClamAV")


def test_enabled_without_an_address_waits_rather_than_importing_unscanned(tmp_path):
    _, row, _, landed = sweep(tmp_path, b"\0" * 1024, clamav_enabled=True)
    assert not landed.exists() and row.state == "grabbed"


def test_off_by_default(tmp_path):
    _, row, _, landed = sweep(tmp_path, EICAR)
    assert landed.exists() and row.state == "imported"


def test_status_says_whether_clamd_answers(clamd, tmp_path):
    service = ROMarr({"ROMARR_DATA": str(tmp_path / "s.json")})
    assert service.clamav_status()["configured"] is False
    service.store.settings.update(clamav_enabled=True, clamav_address=clamd)
    status = service.clamav_status()
    assert status["ok"] and status["version"].startswith("ClamAV")
