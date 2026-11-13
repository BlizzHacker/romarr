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


def _encode_yenc_segment(
    payload: bytes,
    name: str,
    *,
    part: int = 0,
    total: int = 0,
    begin: int = 1,
    total_size: int | None = None,
    corrupt_crc: bool = False,
) -> list[bytes]:
    """Encode raw bytes into CRLF-terminated yEnc article body lines."""
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
        ybegin_size = total_size if total_size is not None else end
        lines = [
            f"=ybegin part={part} total={total} line=128 size={ybegin_size} name={name}\r\n".encode(),
            f"=ypart begin={begin} end={end}\r\n".encode(),
            bytes(encoded) + b"\r\n",
            f"=yend size={len(payload)} part={part} pcrc32={crc:08x}\r\n".encode(),
        ]
    else:
        lines = [
            f"=ybegin line=128 size={total_size if total_size is not None else len(payload)} name={name}\r\n".encode(),
            bytes(encoded) + b"\r\n",
            f"=yend size={len(payload)} crc32={crc:08x}\r\n".encode(),
        ]
    return lines


def _wait_until(predicate, timeout: float = 4.0) -> None:
    """Poll predicate until truthy or raise AssertionError after timeout."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("condition was not met before timeout")


class _MockNntpServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    """Minimal loopback NNTP server for testing auth, pipelining, and yEnc fetches."""

    allow_reuse_address = True
    daemon_threads = True

    def __init__(
        self,
        articles: dict[str, list[bytes]],
        *,
        username="u",
        password="p",
        block_msg_id: str = "",
        started_event: threading.Event | None = None,
        block_event: threading.Event | None = None,
    ):
        self.articles = articles
        self.expected_user = username
        self.expected_pass = password
        self.block_msg_id = block_msg_id
        self.started_event = started_event
        self.block_event = block_event
        self.conn_lock = threading.Lock()
        self.active_conns = 0
        self.max_conns = 0
        super().__init__(("127.0.0.1", 0), _NntpHandler)


class _NntpHandler(socketserver.StreamRequestHandler):
    """Single-connection NNTP handler implementing AUTHINFO, BODY, and QUIT."""

    def handle(self):
        """Process NNTP commands on this client connection."""
        srv: _MockNntpServer = self.server  # type: ignore[assignment]
        with srv.conn_lock:
            srv.active_conns += 1
            srv.max_conns = max(srv.max_conns, srv.active_conns)
        try:
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
                        if srv.block_msg_id and msg_id == srv.block_msg_id:
                            if srv.started_event is not None:
                                srv.started_event.set()
                            if srv.block_event is not None:
                                srv.block_event.wait(timeout=10.0)
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
        finally:
            with srv.conn_lock:
                srv.active_conns = max(0, srv.active_conns - 1)


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
    total_len = len(part1) + len(part2)
    articles = {
        "<m1@test>": _encode_yenc_segment(
            part1, "Super Metroid (USA).sfc", part=1, total=2, begin=1, total_size=total_len
        ),
        "<m2@test>": _encode_yenc_segment(
            part2, "Super Metroid (USA).sfc", part=2, total=2, begin=len(part1) + 1, total_size=total_len
        ),
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


def test_parse_nzb_preserves_unquoted_split_volume_extensions():
    xml = """<nzb xmlns="http://www.newzbin.com/DTD/2003/nzb">
      <file subject="Release.Name Game.zip.001 yEnc (1/2)">
        <segments><segment bytes="10" number="1">a1@test</segment></segments>
      </file>
      <file subject="Release.Name Game.zip.002 yEnc (2/2)">
        <segments><segment bytes="10" number="1">a2@test</segment></segments>
      </file>
      <file subject="Archive Game.part01.rar yEnc (1/1)">
        <segments><segment bytes="10" number="1">a3@test</segment></segments>
      </file>
    </nzb>"""
    files = parse_nzb(xml)
    assert [f.filename for f in files] == ["Game.zip.001", "Game.zip.002", "Game.part01.rar"]


def test_builtin_nzb_url_origin_redirect_and_size_caps(tmp_path, monkeypatch):
    import romarr.nntp as nntp_mod
    from romarr.app import ROMarr

    class _FakeResp:
        def __init__(self, status_code=200, content=b"", headers=None):
            self.status_code = status_code
            self.content = content
            self.headers = headers or {}
            self.closed = False

        def raise_for_status(self):
            if self.status_code >= 400:
                raise RuntimeError(f"HTTP {self.status_code}")

        def iter_content(self, chunk_size=65536):
            for i in range(0, len(self.content), chunk_size):
                yield self.content[i:i + chunk_size]

        def close(self):
            self.closed = True

    seen_calls = []
    valid_nzb = b'<nzb><file subject=\'"OK.sfc" yEnc\'><segments><segment bytes="4" number="1">m1@t</segment></segments></file></nzb>'
    created_resps = []

    class _FakeSession:
        def get(self, url, **kw):
            seen_calls.append((url, kw))
            if "redir.nzb" in url:
                r = _FakeResp(302, headers={"Location": "http://127.0.0.1:8080/evil.nzb"})
            elif "huge.nzb" in url:
                r = _FakeResp(200, content=b"x" * 128, headers={"Content-Length": "128"})
            else:
                r = _FakeResp(200, content=valid_nzb)
            created_resps.append(r)
            return r

    cfg = BuiltinNzbConfig(host="news.local", port=119, use_ssl=False, save_path=str(tmp_path / "dl"))
    unconfigured = BuiltinNzbClient(cfg, session=_FakeSession())
    assert unconfigured.add("http://127.0.0.1:9999/test.nzb") is False
    assert "allowed" in unconfigured.last_error.lower() or "origin" in unconfigured.last_error.lower()
    assert seen_calls == []

    scoped = BuiltinNzbClient(
        cfg,
        session=_FakeSession(),
        allowed_origins=("http://prowlarr.local:9696",),
    )
    assert scoped.add("http://prowlarr.local:9696/redir.nzb") is False
    assert seen_calls[-1][1]["allow_redirects"] is False
    assert "127.0.0.1" in scoped.last_error or "redirect" in scoped.last_error.lower() or "origin" in scoped.last_error.lower()

    monkeypatch.setattr(nntp_mod, "MAX_NZB_BYTES", 64)
    assert scoped.add("http://prowlarr.local:9696/huge.nzb") is False
    assert "limit" in scoped.last_error.lower() or "exceeds" in scoped.last_error.lower()
    assert created_resps[-1].closed is True

    # App wiring exposes configured Prowlarr and enabled direct Newznab indexer origins dynamically.
    svc = ROMarr(env={"ROMARR_DATA": str(tmp_path / "romarr.json"), "PROWLARR_URL": "http://prowlarr.local:9696"})
    svc.store.put_item("indexers", {"id": "nzb1", "type": "newznab", "url": "https://nzb.allowed:8443/api", "enable": True})
    svc.store.put_item("indexers", {"id": "nzb2", "type": "newznab", "url": "https://disabled.invalid/api", "enable": False})
    built = svc._build_client({
        "type": "builtin_nzb",
        "host": "news.local",
        "port": 119,
        "use_ssl": False,
        "save_path": str(tmp_path / "dl2"),
    })
    assert built is not None and callable(built.allowed_origins_provider)
    origins = svc._nzb_allowed_origins()
    assert "http://prowlarr.local:9696" in origins
    assert "https://nzb.allowed:8443/api" in origins
    assert "https://disabled.invalid/api" not in origins
    # Smoke-check valid fetch via fake session on the wired client without spinning live NNTP.
    built._session = _FakeSession()
    monkeypatch.setattr(built._pool, "submit", lambda *a, **kw: type("F", (), {"done": lambda self: True})())
    monkeypatch.setattr(nntp_mod, "MAX_NZB_BYTES", 1024 * 1024)
    assert built.add("https://nzb.allowed:8443/getnzb?id=1", name="Test") is True


def test_builtin_nzb_thread_local_last_job_id_and_last_error(tmp_path):
    cfg = BuiltinNzbConfig(host="127.0.0.1", port=119, use_ssl=False, save_path=str(tmp_path / "dl"))
    client = BuiltinNzbClient(cfg)
    client._pool.shutdown(wait=False)

    valid_nzb = '<nzb><file subject=\'"A.sfc" yEnc\'><segments><segment bytes="4" number="1">m@t</segment></segments></file></nzb>'
    invalid_nzb = "<nzb/>"
    barrier = threading.Barrier(2)
    results = {}

    def _ok_worker():
        client._pool = type("P", (), {"submit": lambda self, *a, **k: type("F", (), {"done": lambda s: True})()})()
        assert client.add(valid_nzb, name="Good") is True
        barrier.wait(timeout=3.0)
        results["ok"] = (client.last_job_id, client.last_error)

    def _err_worker():
        assert client.add(invalid_nzb, name="Empty") is False
        barrier.wait(timeout=3.0)
        results["err"] = (client.last_job_id, client.last_error)

    t1 = threading.Thread(target=_ok_worker)
    t2 = threading.Thread(target=_err_worker)
    t1.start()
    t2.start()
    t1.join(timeout=5.0)
    t2.join(timeout=5.0)

    assert results["ok"][0].startswith("nzb-")
    assert results["ok"][1] == ""
    assert results["err"][0] == ""
    assert results["err"][1] != ""


def test_builtin_nzb_shared_connection_cap_across_concurrent_jobs(tmp_path):
    part = b"ABCD" * 8
    articles = {}
    nzb_template = []
    for label in ("j1", "j2"):
        segs = []
        total_len = len(part) * 4
        for idx in range(1, 5):
            mid = f"<{label}-{idx}@test>"
            articles[mid] = _encode_yenc_segment(
                part, f"{label}.sfc", part=idx, total=4, begin=(idx - 1) * len(part) + 1, total_size=total_len
            )
            segs.append(f'<segment bytes="{len(part)}" number="{idx}">{label}-{idx}@test</segment>')
        nzb_template.append(
            f'<nzb><file subject=\'"{label}.sfc" yEnc (1/4)\'><segments>{"".join(segs)}</segments></file></nzb>'
        )

    started = threading.Event()
    release = threading.Event()
    srv = _MockNntpServer(articles, username="", password="",
                          block_msg_id="<j1-1@test>", started_event=started,
                          block_event=release)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        client = BuiltinNzbClient(BuiltinNzbConfig(
            host="127.0.0.1", port=srv.server_address[1], use_ssl=False,
            connections=2, save_path=str(tmp_path / "dl"),
        ))
        assert client.add(nzb_template[0], name="Job1") is True
        assert started.wait(3), "First job did not enter the mock NNTP server"
        assert client.add(nzb_template[1], name="Job2") is True
        assert len(client.active_downloads()) == 2
        release.set()
        _wait_until(lambda: len(client.completed()) == 2, timeout=6.0)
        assert srv.max_conns <= 2
    finally:
        release.set()
        srv.shutdown()
        srv.server_close()


def test_builtin_nzb_rejects_gap_overlap_and_size_mismatch_before_commit(tmp_path):
    p1 = b"AAAAA"
    p2 = b"BBBBB"
    articles = {
        # Gap: begins at 1..5 and 8..12 (missing 6..7)
        "<gap1@t>": _encode_yenc_segment(p1, "Gap.sfc", part=1, total=2, begin=1, total_size=12),
        "<gap2@t>": _encode_yenc_segment(p2, "Gap.sfc", part=2, total=2, begin=8, total_size=12),
        # Overlap: begins at 1..5 and 4..8
        "<ov1@t>": _encode_yenc_segment(p1, "Overlap.sfc", part=1, total=2, begin=1, total_size=8),
        "<ov2@t>": _encode_yenc_segment(p2, "Overlap.sfc", part=2, total=2, begin=4, total_size=8),
        # Wrong declared total size: only 5 bytes delivered for declared size=20
        "<sz1@t>": _encode_yenc_segment(p1, "SizeMismatch.sfc", part=1, total=1, begin=1, total_size=20),
    }

    srv = _MockNntpServer(articles, username="", password="")
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        client = BuiltinNzbClient(BuiltinNzbConfig(
            host="127.0.0.1", port=srv.server_address[1], use_ssl=False,
            connections=2, save_path=str(tmp_path / "dl"),
        ))
        for label, fname, seg_xml, expected_word in [
            ("Gap", "Gap.sfc", '<segment bytes="5" number="1">gap1@t</segment><segment bytes="5" number="2">gap2@t</segment>', "gap"),
            ("Overlap", "Overlap.sfc", '<segment bytes="5" number="1">ov1@t</segment><segment bytes="5" number="2">ov2@t</segment>', "overlap"),
            ("SizeMismatch", "SizeMismatch.sfc", '<segment bytes="5" number="1">sz1@t</segment>', "size"),
        ]:
            nzb = f'<nzb><file subject=\'"{fname}" yEnc\'><segments>{seg_xml}</segments></file></nzb>'
            assert client.add(nzb, name=label) is True
            jid = client.last_job_id
            _wait_until(lambda j=jid: any(f["id"] == j for f in client.failed_jobs()), timeout=4.0)
            fault = [f for f in client.failed_jobs() if f["id"] == jid][0]
            assert fault["release_fault"] is True
            assert expected_word in fault["detail"].lower()
            assert not any(p.name == fname for p in (tmp_path / "dl").rglob(fname))
    finally:
        srv.shutdown()
        srv.server_close()


def test_builtin_nzb_remove_download_cancels_in_flight_job_cleanly(tmp_path):
    started = threading.Event()
    release = threading.Event()
    srv = _MockNntpServer(
        {"<slow@test>": _encode_yenc_segment(b"HELLO", "Slow.sfc")},
        username="",
        password="",
        block_msg_id="<slow@test>",
        started_event=started,
        block_event=release,
    )
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        client = BuiltinNzbClient(BuiltinNzbConfig(
            host="127.0.0.1", port=srv.server_address[1], use_ssl=False,
            connections=1, save_path=str(tmp_path / "dl"),
        ))
        nzb = '<nzb><file subject=\'"Slow.sfc" yEnc\'><segments><segment bytes="5" number="1">slow@test</segment></segments></file></nzb>'
        assert client.add(nzb, name="Slow Release") is True
        job_id = client.last_job_id
        assert started.wait(timeout=3.0) is True

        job_dirs = [p for p in (tmp_path / "dl").iterdir() if p.is_dir()]
        assert len(job_dirs) == 1

        timer = threading.Timer(0.15, release.set)
        timer.start()
        assert client.remove_download(job_id, delete_files=True) is True
        timer.join(timeout=2.0)

        assert not job_dirs[0].exists()
        assert client.active_downloads() == []
        assert client.completed() == []
        assert client.failed_jobs() == []
    finally:
        release.set()
        srv.shutdown()
        srv.server_close()


def test_remove_timeout_retains_nzb_ledger_and_files(tmp_path, monkeypatch):
    """Leave a timed-out worker recoverable for a later removal attempt."""
    from romarr import nntp
    root = tmp_path / "dl"
    job_dir = root / "active"
    job_dir.mkdir(parents=True)
    payload = job_dir / "game.sfc"
    payload.write_bytes(b"partial")
    client = BuiltinNzbClient(BuiltinNzbConfig(host="localhost", save_path=str(root)))
    client._ledger.upsert("active", { "status": "downloading", "job_dir": str(job_dir)})
    monkeypatch.setattr(nntp, "stop_job", lambda *a, **kw: False)
    assert not client.remove_download("active", delete_files=True)
    assert client.active_downloads()[0]["id"] == "active"
    assert payload.read_bytes() == b"partial"
