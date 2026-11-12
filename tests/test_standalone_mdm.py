"""Tests for companion Mister Download Manager integration and standalone clients."""

from __future__ import annotations

import hashlib
import io
import json
import tarfile
import threading
import time
import zipfile
from pathlib import Path

import pytest
import requests

from romarr import mdm
from romarr.app import ROMarr, QueueItem
from romarr.downloaders import CLIENT_TYPES, build_client, hand_off_with_id, match_queue_item
from romarr.selective_import import selected_import_path


def _wait_until(predicate, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("condition was not met before timeout")


class _FakeResp:
    def __init__(self, *, status_code=200, content=b"", text=None, headers=None, json_data=None):
        self.status_code = status_code
        self.ok = 200 <= status_code < 400
        self.content = content if content else (text.encode("utf-8") if text is not None else b"")
        self.text = text if text is not None else self.content.decode("utf-8", errors="replace")
        self.headers = headers or {}
        self._json = json_data

    def json(self):
        if self._json is not None:
            return self._json
        return json.loads(self.text)

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}", response=self)

    def iter_content(self, chunk_size=65536):
        for i in range(0, len(self.content), chunk_size):
            yield self.content[i:i + chunk_size]

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class _FakeSession:
    def __init__(self, handler):
        self.handler = handler
        self.calls = []

    def get(self, url, **kw):
        self.calls.append(("GET", url, kw))
        return self.handler("GET", url, kw)

    def post(self, url, **kw):
        self.calls.append(("POST", url, kw))
        return self.handler("POST", url, kw)


def _make_archive_bytes(kind: str, binary_name: str, payload: bytes = b"#!/bin/sh\necho mdm 0.5.2\n") -> bytes:
    buf = io.BytesIO()
    if kind == "tar.gz":
        with tarfile.open(fileobj=buf, mode="w:gz") as tf:
            ti = tarfile.TarInfo(f"MisterDownloadManager-0.5.2-linux-x86_64/{binary_name}")
            ti.size = len(payload)
            ti.mode = 0o755
            tf.addfile(ti, io.BytesIO(payload))
            bad = tarfile.TarInfo("../../evil")
            bad.size = 3
            tf.addfile(bad, io.BytesIO(b"bad"))
    else:
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr(f"MisterDownloadManager-0.5.2-portable/{binary_name}", payload)
            zf.writestr("../../evil.txt", b"bad")
    return buf.getvalue()


def test_asset_spec_platforms_and_musl():
    g = mdm.asset_spec("0.5.2", sys_platform="linux", machine="x86_64", libc="glibc")
    assert g["installable"] is True
    assert g["asset"] == "MisterDownloadManager-0.5.2-linux-x86_64.tar.gz"

    m = mdm.asset_spec("v0.5.2", sys_platform="linux", machine="x86_64", libc="musl")
    assert m["installable"] is False
    assert "musl" in m["reason"].lower()

    w = mdm.asset_spec("v0.5.2", sys_platform="win32", machine="AMD64")
    assert w["installable"] is True
    assert w["asset"] == "MisterDownloadManager-0.5.2-portable.zip"

    mac = mdm.asset_spec("v0.5.2", sys_platform="darwin", machine="arm64")
    assert mac["installable"] is False
    assert ".dmg" in mac["asset"]


def test_parse_sha256sums_ignores_comments_and_invalid_lines():
    h = "a" * 64
    text = f"# comment\n{h}  *MisterDownloadManager-0.5.2-linux-x86_64.tar.gz\nbadline\n"
    parsed = mdm.parse_sha256sums(text)
    assert parsed == {"MisterDownloadManager-0.5.2-linux-x86_64.tar.gz": h}


def test_install_mdm_verifies_sha256_and_extracts_safely(tmp_path, monkeypatch):
    monkeypatch.setattr(mdm, "asset_spec", lambda version=mdm.MDM_VERSION: {
        "tag": "v0.5.2", "version": "0.5.2", "platform": "windows-x86_64",
        "installable": True, "kind": "zip", "asset": "MisterDownloadManager-0.5.2-portable.zip",
        "reason": "",
    })
    archive = _make_archive_bytes("zip", "misterdownloadmanager-cli.exe", b"MZtest")
    digest = hashlib.sha256(archive).hexdigest()
    sums_text = f"{digest}  MisterDownloadManager-0.5.2-portable.zip\n"

    session = _FakeSession(
        lambda method, url, kw: (
            _FakeResp(text=sums_text) if url.endswith("SHA256SUMS.txt")
            else _FakeResp(content=archive)
        )
    )
    out = mdm.install_mdm(
        tmp_path, session=session,
        runner=lambda p: (True, "misterdownloadmanager-cli 0.5.2"),
    )
    assert out["ok"] is True
    assert out["sha256"] == digest
    installed = tmp_path / "misterdownloadmanager-cli.exe"
    assert installed.read_bytes() == b"MZtest"
    assert not (tmp_path.parent / "evil.txt").exists()


def test_install_mdm_tar_and_rejects_bad_checksum(tmp_path, monkeypatch):
    monkeypatch.setattr(mdm, "asset_spec", lambda version=mdm.MDM_VERSION: {
        "tag": "v0.5.2", "version": "0.5.2", "platform": "linux-x86_64 (glibc)",
        "installable": True, "kind": "tar.gz", "asset": "MisterDownloadManager-0.5.2-linux-x86_64.tar.gz",
        "reason": "",
    })
    archive = _make_archive_bytes("tar.gz", "misterdownloadmanager-cli", b"ELF-mock")
    good = _FakeSession(
        lambda m, url, kw: _FakeResp(
            text=f"{hashlib.sha256(archive).hexdigest()}  MisterDownloadManager-0.5.2-linux-x86_64.tar.gz\n"
        ) if url.endswith("SHA256SUMS.txt") else _FakeResp(content=archive)
    )
    out = mdm.install_mdm(tmp_path / "ok", session=good, runner=lambda p: (True, "0.5.2"))
    assert Path(out["cli_path"]).read_bytes() == b"ELF-mock"

    bad = _FakeSession(
        lambda m, url, kw: _FakeResp(
            text=f"{'0' * 64}  MisterDownloadManager-0.5.2-linux-x86_64.tar.gz\n"
        ) if url.endswith("SHA256SUMS.txt") else _FakeResp(content=archive)
    )
    with pytest.raises(mdm.MdmError, match="SHA-256 verification failed"):
        mdm.install_mdm(tmp_path / "bad", session=bad, runner=lambda p: (True, "0.5.2"))
    assert not list((tmp_path / "bad").glob("misterdownloadmanager*"))


def test_mdm_torrent_selective_end_to_end_and_selected_import_path(tmp_path):
    rom_bytes = b"ROMDATA!" * 16
    btih = "a" * 40
    target_rel = "SNES/Super Metroid (USA).sfc"

    def fake_runner(args, *, job_id="", cancel_event=None):
        cmd = args[0]
        if cmd == "torrent-info":
            return {
                "name": "SNES Set",
                "info_hash": btih,
                "size": len(rom_bytes) + 10,
                "files": [
                    {"index": 0, "size": 10, "path": "SNES/Other.sfc"},
                    {"index": 1, "size": len(rom_bytes), "path": target_rel},
                ],
            }
        if cmd == "torrent-get":
            assert "--files" in args and args[args.index("--files") + 1] == "1"
            out_dir = Path(args[args.index("-o") + 1]) / "SNES Set"
            dest = out_dir / "SNES" / "Super Metroid (USA).sfc"
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(rom_bytes)
            return {"event": "done", "path": str(out_dir), "size": len(rom_bytes)}
        raise AssertionError(f"unexpected args {args}")

    client = mdm.MdmTorrentClient(
        mdm.MdmTorrentConfig(mode="cli", save_path=str(tmp_path / "dl")),
        cli_runner=fake_runner,
    )
    client.romarr_id = "c1"
    magnet = f"magnet:?xt=urn:btih:{btih}&dn=SNES+Set"
    assert client.add_magnet_selective(magnet, target_rel) is True
    assert client.last_job_id.startswith("mdm-tag:")
    assert client.last_selected_file == {
        "hash": btih, "index": 1, "path": target_rel, "size": len(rom_bytes),
    }

    _wait_until(lambda: len(client.selective_completed()) == 1)
    done = client.selective_completed()[0]
    done["_romarr_client"] = client

    q_item = QueueItem(
        "Super Metroid", "snes", "SNES Set", 10, "grabbed",
        release_id=f"btih:{btih}",
        download_id=client.last_job_id,
        download_client="c1",
        file_selection_required=True,
        selected_file=client.last_selected_file,
    )
    assert match_queue_item([q_item], done, client_id="c1", client_protocol="torrent") is q_item
    resolved = selected_import_path(q_item, done, client)
    assert resolved.read_bytes() == rom_bytes
    assert client.remove_download(client.last_job_id, delete_files=True) is True
    assert client.selective_completed() == []


def test_mdm_torrent_upload_bytes_and_magnet_redirect(tmp_path):
    seen_sources = []

    def fake_runner(args, *, job_id="", cancel_event=None):
        seen_sources.append(list(args))
        out_dir = Path(args[args.index("-o") + 1]) / "Game"
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "game.sfc").write_bytes(b"1234")
        return {"event": "done", "path": str(out_dir), "size": 4}

    session = _FakeSession(
        lambda m, url, kw: _FakeResp(
            status_code=302, headers={"Location": f"magnet:?xt=urn:btih:{'b' * 40}"}
        )
    )
    client = mdm.MdmTorrentClient(
        mdm.MdmTorrentConfig(mode="cli", save_path=str(tmp_path)),
        session=session, cli_runner=fake_runner,
    )
    assert client.add_torrent_file(b"d8:announce1:x4:infod4:name4:Gameee", name="Game")
    _wait_until(lambda: len(client.completed()) == 1)
    assert seen_sources[0][0] == "torrent-get"

    assert client.add("https://tracker.example/get/1.torrent", name="Redirected")
    _wait_until(lambda: len(client.completed()) == 2)
    assert seen_sources[1][1].startswith("magnet:?xt=urn:btih:")


def test_mdm_direct_cli_headers_connections_and_errors(tmp_path):
    recorded = []

    def fake_runner(args, *, job_id="", cancel_event=None):
        recorded.append(list(args))
        if "fail" in args[1]:
            raise mdm.MdmError("HTTP 404 Not Found")
        out_dir = Path(args[args.index("-o") + 1])
        out_file = out_dir / "rom.zip"
        out_file.write_bytes(b"PK")
        return {"event": "done", "path": str(out_file), "size": 2}

    client = mdm.MdmDirectClient(
        mdm.MdmDirectConfig(mode="cli", save_path=str(tmp_path), connections=16),
        cli_runner=fake_runner,
    )
    ok, jid = hand_off_with_id(client, "https://cdn.example.com/rom.zip", name="My ROM")
    assert ok and jid.startswith("mdm-http:")
    _wait_until(lambda: len(client.completed()) == 1)
    assert "-c" in recorded[0] and recorded[0][recorded[0].index("-c") + 1] == "16"

    assert client.add("https://cdn.example.com/fail.zip", name="Missing ROM")
    _wait_until(lambda: len(client.failed_jobs()) == 1)
    assert "404" in client.failed_jobs()[0]["detail"]


def test_mdm_remote_transport_and_category_filtering(tmp_path):
    posted = []

    def handler(method, url, kw):
        if url.endswith("/api/list"):
            assert kw["headers"]["Authorization"] == "Bearer tok123"
            return _FakeResp(json_data={"downloads": [
                {"id": 7, "file": "Super Metroid.zip", "path": "", "status": "Complete", "size": 100, "category": "compressed"},
                {"id": 8, "file": "Working.zip", "path": "", "status": "Downloading", "size": 200, "downloaded": 50},
                {"id": 9, "file": "Broken.zip", "path": "", "status": "Error", "error": "timeout"},
            ]})
        if url.endswith("/api/add"):
            posted.append(kw["json"])
            return _FakeResp(json_data={"id": 42, "pending": False})
        if "/api/torrent" in url:
            posted.append({"params": kw["params"], "data": kw["data"]})
            return _FakeResp(json_data={"id": None, "pending": True})
        if url.endswith("/api/act"):
            posted.append(kw["json"])
            return _FakeResp(json_data={})
        raise AssertionError(url)

    session = _FakeSession(handler)
    client = mdm.MdmTorrentClient(
        mdm.MdmTorrentConfig(
            mode="remote",
            save_path=str(tmp_path),
            remote_url="https://127.0.0.1:8463",
            api_token="tok123",
            category="romarr",
        ),
        session=session,
    )
    assert client.reachable() is True
    assert client.supports_selective is False
    done = client.completed()
    assert len(done) == 1
    assert done[0]["content_path"] == str(tmp_path / "Super Metroid.zip")
    assert len(client.active_downloads()) == 1
    assert len(client.failed_jobs()) == 1

    assert client.add("magnet:?xt=urn:btih:1234", name="Test")
    # Non-MDM category 'romarr' must be omitted rather than triggering unknown-category.
    assert "category" not in posted[-1]
    assert client.add_torrent_file(b"d8:announce1:xe")
    assert client.remove_download("mdm-remote:7", delete_files=True) is True
    assert posted[-1] == {"action": "delete", "ids": [7]}


def test_app_grab_and_mdm_http_endpoints(tmp_path, monkeypatch):
    import urllib.error
    import urllib.request
    from http.server import ThreadingHTTPServer
    from romarr.app import make_handler

    installed_calls = []

    def fake_install(*, target_dir=None, version=mdm.MDM_VERSION):
        installed_calls.append((target_dir, version))
        return {
            "ok": True, "version": "misterdownloadmanager-cli 0.5.2",
            "release_tag": version, "asset": "mock.zip",
            "sha256": "a" * 64, "install_dir": str(target_dir),
            "cli_path": str(Path(target_dir) / "misterdownloadmanager-cli"),
            "app_path": "",
        }

    monkeypatch.setattr(mdm, "install_mdm", fake_install)
    svc = ROMarr(env={"ROMARR_DATA": str(tmp_path / "romarr.json"), "ROMARR_API_KEY": "k1"})
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(svc))
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        # Unauthenticated requests must be rejected
        try:
            urllib.request.urlopen(f"{base}/api/v1/downloadclient/mdm", timeout=5)
            raise AssertionError("expected 401")
        except urllib.error.HTTPError as err:
            assert err.code == 401

        req = urllib.request.Request(
            f"{base}/api/v1/downloadclient/mdm",
            headers={"X-Api-Key": "k1"},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            cap = json.loads(resp.read())
        assert "platform" in cap and "release_tag" in cap

        req = urllib.request.Request(
            f"{base}/api/v1/downloadclient/mdm/install",
            method="POST",
            headers={"X-Api-Key": "k1", "Content-Type": "application/json"},
            data=json.dumps({}).encode(),
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            inst = json.loads(resp.read())
        assert inst["ok"] is True
        assert installed_calls and installed_calls[0][1] == mdm.MDM_VERSION

        # Bad version rejected cleanly
        req_bad = urllib.request.Request(
            f"{base}/api/v1/downloadclient/mdm/install",
            method="POST",
            headers={"X-Api-Key": "k1", "Content-Type": "application/json"},
            data=json.dumps({"version": "bad;cmd"}).encode(),
        )
        try:
            urllib.request.urlopen(req_bad, timeout=5)
            raise AssertionError("expected 400")
        except urllib.error.HTTPError as err:
            assert err.code == 400
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_app_grab_allows_mdm_torrent_selective(tmp_path):
    from romarr.selection import Release

    rom_bytes = b"ROM" * 16
    btih = "c" * 40
    target_rel = "./SNES/Chrono Trigger (USA).sfc"

    def fake_runner(args, *, job_id="", cancel_event=None):
        if args[0] == "torrent-info":
            return {
                "name": "SNES Set",
                "info_hash": btih,
                "size": len(rom_bytes),
                "files": [{"index": 0, "size": len(rom_bytes), "path": "SNES/Chrono Trigger (USA).sfc"}],
            }
        out_dir = Path(args[args.index("-o") + 1]) / "SNES Set"
        (out_dir / "SNES").mkdir(parents=True, exist_ok=True)
        (out_dir / "SNES" / "Chrono Trigger (USA).sfc").write_bytes(rom_bytes)
        return {"event": "done", "path": str(out_dir), "size": len(rom_bytes)}

    svc = ROMarr(env={"ROMARR_DATA": str(tmp_path / "romarr.json")})
    client = mdm.MdmTorrentClient(
        mdm.MdmTorrentConfig(mode="cli", save_path=str(tmp_path / "torrents")),
        cli_runner=fake_runner,
    )
    client.romarr_id = "mdm1"
    svc.clients = [client]

    pick = Release(
        title="Chrono Trigger (USA)",
        download_url=f"magnet:?xt=urn:btih:{btih}&dn=SNES+Set",
        size=len(rom_bytes),
        seeders=10,
        categories=(1000,),
        indexer="hub:minerva-archive",
        protocol="torrent",
        file_selection_required=True,
        torrent_file_path=target_rel,
    )
    res = svc.grab(pick, "Chrono Trigger", "snes", manual=True)
    assert res["ok"] is True
    assert svc.queue[-1].download_id.startswith("mdm-tag:")
    assert svc.queue[-1].selected_file["path"] == "SNES/Chrono Trigger (USA).sfc"
    assert "mdm_torrent" in CLIENT_TYPES and "mdm_http" in CLIENT_TYPES


def test_build_client_helper_preserves_legacy_single_arg_factory(tmp_path, monkeypatch):
    from types import SimpleNamespace
    import romarr.app as app_module

    seen = []

    def legacy_factory(cfg):
        seen.append(cfg["type"])
        return SimpleNamespace(name=cfg.get("name", "legacy"), reachable=lambda: True)

    svc = ROMarr(env={"ROMARR_DATA": str(tmp_path / "romarr.json")})
    svc.store.put_item("download_clients", {"id": "c1", "type": "qbittorrent", "name": "qbit", "enable": True})
    monkeypatch.setattr(app_module, "build_client", legacy_factory)

    svc.reload_clients()
    tested = svc.test_client({"type": "qbittorrent", "name": "qbit"})

    assert seen == ["qbittorrent", "qbittorrent"]
    assert len(svc.clients) == 1
    assert tested["ok"] is True

