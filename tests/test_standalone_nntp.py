"""Tests for standalone built-in Usenet NNTP / NZB / yEnc engine."""

from __future__ import annotations

import socketserver
import threading
import time
import zlib
from pathlib import Path

import pytest

from romarr.nntp import (
    ArticleMissingError,
    BuiltinNzbClient,
    BuiltinNzbConfig,
    NntpError,
    decode_yenc,
    parse_nzb,
)


def _encode_yenc_segment(payload: bytes, name: str, *, part: int = 0, total: int = 0, begin: int = 1, corrupt_crc: bool = False) -> list[bytes]:
    crc = zlib.crc32(payload) & 0xFFFFFFFF
    if corrupt_crc:
        crc ^= 0x1
    encoded = bytearray()
    for b in payload:
        c = (b + 42) & 0xFF
        if c in (0x00, 0x0A, 0x0D, 0x3D):  # NULL, LF, CR, '='
            encoded.append(0x3D)
            encoded.append((c + 64) & 0xFF)
        else:
            encoded.append(c)

    if part:
        end = begin + len(payload) - 1
        lines = [
            f"=ybegin part={part} total={total} line=128 size={end} name={name}\r\n".encode(),
            f"=ypart begin={begin} end={end}\r\n".encode(),
            bytes(encoded) + b"\r\n",
            f"=yend size={len(payload)} part={part} pcrc32={crc:08x}\r\n".encode(),
        ]
    else:
        lines = [
            f"=ybegin line=128 size={len(payload)} name={name}\r\n".encode(),
            bytes(encoded) + b"\r\n",
            f"=yend size={len(payload)} crc32={crc:08x}\r\n".encode(),
        ]
    return lines


def _wait_until(predicate, timeout: float = 4.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("condition was not met before timeout")


class _MockNntpServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, articles: dict[str, list[bytes]], *, username="u", password="p"):
        self.articles = articles
        self.expected_user = username
        self.expected_pass = password
        super().__init__(("127.0.0.1", 0), _NntpHandler)


class _NntpHandler(socketserver.StreamRequestHandler):
    def handle(self):
        srv: _MockNntpServer = self.server  # type: ignore[assignment]
        self.wfile.write(b"200 MockNNTP ready\r\n")
        self.wfile.flush()
        authed = not srv.expected_user
        seen_user = ""
        while True:
            raw = self.rfile.readline()
            if not raw:
                break
            cmd = raw.decode("utf-8", "replace").strip()
            upper = cmd.upper()
            if upper.startswith("AUTHINFO USER "):
                seen_user = cmd[14:].strip()
                self.wfile.write(b"381 Enter password\r\n")
            elif upper.startswith("AUTHINFO PASS "):
                pw = cmd[14:].strip()
                if seen_user == srv.expected_user and pw == srv.expected_pass:
                    authed = True
                    self.wfile.write(b"281 Authentication accepted\r\n")
                else:
                    self.wfile.write(b"481 Authentication rejected\r\n")
            elif upper.startswith("BODY "):
                if not authed:
                    self.wfile.write(b"480 Auth required\r\n")
                else:
                    msg_id = cmd[5:].strip()
                    lines = srv.articles.get(msg_id)
                    if lines is None:
                        self.wfile.write(b"430 No such article\r\n")
                    else:
                        self.wfile.write(b"222 Body follows\r\n")
                        for line in lines:
                            if line.startswith(b"."):
                                self.wfile.write(b"." + line)
                            else:
                                self.wfile.write(line)
                        self.wfile.write(b".\r\n")
            elif upper == "QUIT":
                self.wfile.write(b"205 Bye\r\n")
                self.wfile.flush()
                break
            else:
                self.wfile.write(b"500 Unknown command\r\n")
            self.wfile.flush()


def test_parse_nzb_sorts_segments_sanitizes_paths_and_blocks_entities():
    xml = """<?xml version="1.0" encoding="UTF-8"?>
    <nzb xmlns="http://www.newzbin.com/DTD/2003/nzb">
      <file subject='Release "../../etc/passwd.sfc" yEnc (1/2)'>
        <groups><group>alt.binaries.emulators</group></groups>
        <segments>
          <segment bytes="5" number="2">seg2@example.com</segment>
          <segment bytes="6" number="1">&lt;seg1@example.com&gt;</segment>
        </segments>
      </file>
      <file subject='Release "passwd.sfc" yEnc (1/1)'>
        <groups><group>alt.binaries.emulators</group></groups>
        <segments><segment bytes="4" number="1">seg3@example.com</segment></segments>
      </file>
    </nzb>"""
    files = parse_nzb(xml)
    assert len(files) == 2
    assert files[0].filename == "passwd.sfc"
    assert files[1].filename == "passwd_2.sfc"
    assert [s.message_id for s in files[0].segments] == ["<seg1@example.com>", "<seg2@example.com>"]

    with pytest.raises(NntpError, match="ENTITY"):
        parse_nzb('<!DOCTYPE nzb [<!ENTITY x "boom">]><nzb/>')


def test_decode_yenc_roundtrip_escapes_and_crc_check():
    raw = bytes(range(256)) + b"\r\n.=yend fake"
    lines = _encode_yenc_segment(raw, "test.bin")
    decoded = decode_yenc(lines)
    assert decoded.data == raw

    bad_lines = _encode_yenc_segment(raw, "test.bin", corrupt_crc=True)
    with pytest.raises(ArticleMissingError, match="CRC32"):
        decode_yenc(bad_lines)


def test_builtin_nzb_end_to_end_loopback_nntp(tmp_path):
    part1 = b"SUPER-METROID-" * 10
    part2 = b"END-OF-ROM\x00\x0d\x0a." * 5
    par2 = b"PAR2-RECOVERY"
    articles = {
        "<m1@test>": _encode_yenc_segment(part1, "Super Metroid (USA).sfc", part=1, total=2, begin=1),
        "<m2@test>": _encode_yenc_segment(part2, "Super Metroid (USA).sfc", part=2, total=2, begin=len(part1) + 1),
        "<par@test>": _encode_yenc_segment(par2, "Super Metroid.par2"),
    }
    nzb = f"""<nzb xmlns="http://www.newzbin.com/DTD/2003/nzb">
      <file subject='"Super Metroid (USA).sfc" yEnc (1/2)'>
        <segments>
          <segment bytes="{len(part2)}" number="2">m2@test</segment>
          <segment bytes="{len(part1)}" number="1">m1@test</segment>
        </segments>
      </file>
      <file subject='"Super Metroid.par2" yEnc (1/1)'>
        <segments><segment bytes="{len(par2)}" number="1">missing-par@test</segment></segments>
      </file>
    </nzb>"""

    srv = _MockNntpServer(articles, username="newsuser", password="secret")
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    port = srv.server_address[1]
    try:
        bad = BuiltinNzbClient(BuiltinNzbConfig(
            host="127.0.0.1", port=port, use_ssl=False,
            username="newsuser", password="wrong", save_path=str(tmp_path / "bad"),
        ))
        assert bad.reachable() is False
        assert "rejected" in bad.detail.lower()

        good = BuiltinNzbClient(BuiltinNzbConfig(
            host="127.0.0.1", port=port, use_ssl=False,
            username="newsuser", password="secret", connections=2,
            save_path=str(tmp_path / "dl"),
        ))
        assert good.reachable() is True
        assert good.add(nzb, name="Super Metroid (USA)") is True
        assert good.last_job_id.startswith("nzb-")
        _wait_until(lambda: len(good.completed()) == 1)

        item = good.completed()[0]
        assembled = Path(item["content_path"]) / "Super Metroid (USA).sfc"
        assert assembled.read_bytes() == part1 + part2

        # Missing primary segment must report release_fault=True in failed_jobs()
        broken_nzb = """<nzb><file subject='"Broken.sfc" yEnc'><segments>
          <segment bytes="10" number="1">does-not-exist@test</segment>
        </segments></file></nzb>"""
        assert good.add(broken_nzb, name="Broken Release") is True
        _wait_until(lambda: len(good.failed_jobs()) == 1)
        fault = good.failed_jobs()[0]
        assert fault["release_fault"] is True
        assert "430" in fault["detail"]
        assert not list(Path(item["content_path"]).parent.glob("*Broken*/*.partial"))
    finally:
        srv.shutdown()
        srv.server_close()
