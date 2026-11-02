"""Regression tests for Issues #107, #108, #109, #110, #111, #112, and #93."""

from __future__ import annotations

import json
import pathlib
import threading
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer

import pytest

from romarr.app import ROMarr, make_handler
from romarr.library import import_rom, platform_dir
from romarr.platforms import resolve
from romarr.selection import Release, infer_platform
from romarr.store import QueueItem
from romarr.ui import FAVICON_LINK, favicon_svg, link_page, login_page, page


def _start_server(service: ROMarr):
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(service))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{server.server_port}"


# ----------------------------------------------------------------- #112 ---

def test_issue_112_favicon_routes_and_head_links(tmp_path):
    """#112: /favicon.ico and /favicon.svg serve the cartridge SVG unauthenticated,
    and every HTML shell links it in <head>."""
    service = ROMarr({
        "ROMARR_DATA": str(tmp_path / "state.json"),
        "ROMARR_API_KEY": "secret-key",
    })
    server, base = _start_server(service)
    try:
        for path in ("/favicon.ico", "/favicon.svg"):
            with urllib.request.urlopen(base + path, timeout=5) as resp:
                assert resp.status == 200
                assert "image/svg+xml" in resp.headers.get("Content-Type", "")
                body = resp.read().decode("utf-8")
                assert "<svg" in body
                assert "#f0a92c" in body
    finally:
        server.shutdown()

    assert FAVICON_LINK in page()
    assert FAVICON_LINK in login_page(claimed=True)
    assert FAVICON_LINK in login_page(claimed=False)
    assert FAVICON_LINK in link_page()
    assert favicon_svg().startswith("<svg")


# ----------------------------------------------------------------- #111 ---

def test_issue_111_wanted_missing_remove_empty_platform(tmp_path):
    """#111: Removing a Wanted entry whose platform is empty ('') succeeds via
    both query string and JSON body, while omitting the platform parameter still
    returns 400."""
    service = ROMarr({
        "ROMARR_DATA": str(tmp_path / "state.json"),
        "ROMARR_AUTH": "disabled",
    })
    service.store.want("Chrono Trigger", "")
    service.store.want("EarthBound", "")
    assert len(service.store.wanted) == 2

    server, base = _start_server(service)
    try:
        # 1. Query string with empty platform: ?game=Chrono+Trigger&platform=
        req = urllib.request.Request(
            f"{base}/api/v1/wanted/missing?game=Chrono+Trigger&platform=",
            method="DELETE",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            assert resp.status == 200
            assert json.loads(resp.read()) == {"deleted": True}

        # 2. JSON body with empty platform: {"game": "EarthBound", "platform": ""}
        req2 = urllib.request.Request(
            f"{base}/api/v1/wanted/missing",
            data=json.dumps({"game": "EarthBound", "platform": ""}).encode(),
            headers={"Content-Type": "application/json"},
            method="DELETE",
        )
        with urllib.request.urlopen(req2, timeout=5) as resp2:
            assert resp2.status == 200
            assert json.loads(resp2.read()) == {"deleted": True}

        assert len(service.store.wanted) == 0

        # 3. Missing platform parameter altogether still returns 400
        req3 = urllib.request.Request(
            f"{base}/api/v1/wanted/missing?game=Anything",
            method="DELETE",
        )
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(req3, timeout=5)
        assert exc.value.code == 400
    finally:
        server.shutdown()


# ----------------------------------------------------------------- #110 ---

def test_issue_110_configurable_platform_folders_and_dotted_digital_dirs(tmp_path, caplog):
    """#110:
    1. Custom platform folder overrides (e.g. win -> Windows) file imports into
       <library_root>/Windows instead of <library_root>/win.
    2. Multi-file digital game folders with dots in their names (e.g.
       Call.of.Duty.Modern.Warfare.4.[DODI.Repack]) are not truncated by Path.stem.
    3. Already-imported downloads in import_finished do not re-trigger remote
       path mapping or spam unmatched warnings on every sweep.
    """
    lib_root = tmp_path / "library"
    dl_root = tmp_path / "downloads"
    repack_dir = dl_root / "Call.of.Duty.Modern.Warfare.4.[DODI.Repack]"
    repack_dir.mkdir(parents=True)
    (repack_dir / "setup.exe").write_bytes(b"MZ" + b"\x00" * 1024)
    (repack_dir / "data1.doi").write_bytes(b"DATA" * 256)

    win = resolve("win")
    assert win is not None
    custom = {"win": "Windows", "ps2": "PlayStation 2"}

    assert platform_dir(lib_root, win, platform_folders=custom) == lib_root / "Windows"
    assert platform_dir(lib_root, "ps2", layout="nested", platform_folders=custom) == (
        lib_root / "PlayStation 2" / "roms"
    )
    # Path traversal in override is ignored safely
    assert platform_dir(lib_root, win, platform_folders={"win": "../escape"}) == lib_root / "win"

    outcomes = import_rom(repack_dir, win, lib_root, platform_folders=custom)
    assert len(outcomes) == 1 and outcomes[0].ok
    expected_dest = lib_root / "Windows" / "Call.of.Duty.Modern.Warfare.4.[DODI.Repack]"
    assert outcomes[0].destination == expected_dest
    assert (expected_dest / "setup.exe").is_file()

    # Observation 2: Unmatched download warning is logged once, not every sweep
    service = ROMarr({"ROMARR_DATA": str(tmp_path / "state.json"), "LIBRARY_PATH": str(lib_root)})

    class _DummyClient:
        name = "qBittorrent"
        configured = True
        _config = type("Cfg", (), {"category": "romarr"})()

        def completed(self):
            return [{
                "name": "Foreign.Release",
                "hash": "abc12345",
                "category": "romarr",
                "content_path": "/downloads/Foreign.Release",
            }]

    service.clients = [_DummyClient()]
    caplog.clear()
    service.import_finished(retry_failed=False)
    service.import_finished(retry_failed=False)
    warnings = [r for r in caplog.records if "could not be matched to a ROMarr queue row" in r.message]
    assert len(warnings) == 1


# ----------------------------------------------------------------- #109 ---

def test_issue_109_any_platform_search_infers_platform_and_imports(tmp_path):
    """#109: Interactive Search with Any platform infers the platform for
    known extensions/markers, refuses ambiguous grabs unless a platform is
    chosen, and imports legacy empty-platform queue rows by inferring from the
    completed torrent."""
    r_snes = Release(
        title="Chrono Trigger (USA).sfc",
        download_url="http://example.invalid/ct.torrent",
        size=4 * 1024 * 1024,
        seeders=12,
        indexer="TestIdx",
        categories=(),
        protocol="torrent",
    )
    r_pc = Release(
        title="Cyberpunk.2077.v2.1-FitGirl.Repack",
        download_url="http://example.invalid/cp.torrent",
        size=50 * 1024 * 1024 * 1024,
        seeders=40,
        indexer="TestIdx",
        categories=(),
        protocol="torrent",
    )
    r_ambiguous = Release(
        title="Mystery Game Collection Archive",
        download_url="http://example.invalid/mystery.torrent",
        size=50 * 1024 * 1024,
        seeders=5,
        indexer="TestIdx",
        categories=(),
        protocol="torrent",
    )
    assert infer_platform(r_snes).slug == "snes"
    assert infer_platform(r_pc).slug == "win"
    assert infer_platform(r_ambiguous) is None

    service = ROMarr({
        "ROMARR_DATA": str(tmp_path / "state.json"),
        "LIBRARY_PATH": str(tmp_path / "lib"),
        "ROMM_URL": "http://romm.invalid",
    })
    # Hold candidates from an Any-platform search
    service._candidates["Mystery|"] = {
        "ambig01": (r_ambiguous, "Mystery Game", ""),
    }
    res = service.grab_candidate("ambig01")
    assert res["ok"] is False
    assert res.get("needs_platform") is True

    # Legacy queue item with platform="" still imports when download name reveals platform
    rom_file = tmp_path / "dl" / "Chrono Trigger (USA).sfc"
    rom_file.parent.mkdir(parents=True)
    rom_file.write_bytes(b"SNESROMDATA12345" * 4096)

    service.queue.append(QueueItem(
        game="Chrono Trigger",
        platform="",
        release="Chrono Trigger (USA).sfc",
        indexer="TestIdx",
        size=65536,
        seeders=10,
        state="grabbed",
        download_id="qbit-tag:romarr-ct123",
    ))

    class _CompletedClient:
        name = "qBittorrent"
        configured = True
        _config = type("Cfg", (), {"category": "romarr"})()

        def completed(self):
            return [{
                "name": "Chrono Trigger (USA).sfc",
                "hash": "deadbeef",
                "tags": "romarr-ct123",
                "category": "romarr",
                "content_path": str(rom_file),
                "size": 65536,
            }]

    service.clients = [_CompletedClient()]
    imported = service.import_finished(retry_failed=False)
    assert len(imported) == 1
    assert service.queue[0].platform == "snes"
    assert service.queue[0].state == "imported"


# ----------------------------------------------------------------- #108 ---

def test_issue_108_stall_detection_respects_active_or_completed_and_removes_dead(tmp_path):
    """#108:
    1. A download that is actively transferring bytes or already completed in
       qBittorrent is NOT retired as stalled even when grab age >= stall_minutes.
    2. A download that genuinely stalled IS retired and removed from qBittorrent.
    """
    service = ROMarr({"ROMARR_DATA": str(tmp_path / "state.json")})
    service.store.settings["stalled_timeout_minutes"] = 60
    old_stamp = (datetime.now(timezone.utc) - timedelta(minutes=120)).isoformat(timespec="seconds")

    active_row = QueueItem(
        game="Active Big Game",
        platform="win",
        release="Active.Big.Game-Repack",
        indexer="Idx",
        size=50_000_000_000,
        seeders=20,
        state="grabbed",
        at=old_stamp,
        release_id="rel-active",
        download_id="qbit-tag:romarr-active",
    )
    stalled_row = QueueItem(
        game="Stalled Dead Game",
        platform="snes",
        release="Stalled.Dead.Game.sfc",
        indexer="Idx",
        size=4_000_000,
        seeders=0,
        state="grabbed",
        at=old_stamp,
        release_id="rel-stalled",
        download_id="qbit-tag:romarr-stalled",
    )
    service.queue = [active_row, stalled_row]

    removed_ids: list[str] = []

    class _FakeQbit:
        name = "qBittorrent"
        configured = True
        romarr_id = ""

        def completed(self):
            return []

        def active_downloads(self):
            return [
                {
                    "name": "Active.Big.Game-Repack",
                    "hash": "1111",
                    "tags": "romarr-active",
                    "state": "downloading",
                    "dlspeed": 8_500_000,
                    "progress": 0.45,
                    "num_seeds": 15,
                },
                {
                    "name": "Stalled.Dead.Game.sfc",
                    "hash": "2222",
                    "tags": "romarr-stalled",
                    "state": "stalledDL",
                    "dlspeed": 0,
                    "progress": 0.0,
                    "num_seeds": 0,
                },
            ]

        def remove_download(self, download_id: str, *, delete_files: bool = False) -> bool:
            removed_ids.append(download_id)
            return True

    service.clients = [_FakeQbit()]
    service._automatic_request = lambda *a, **kw: {"ok": False}

    outcome = service.retire_dead_downloads()
    assert outcome["blocklisted"] == 1
    assert active_row.state == "grabbed"
    assert not active_row.blocklisted
    assert stalled_row.state == "failed"
    assert stalled_row.blocklisted
    assert removed_ids == ["qbit-tag:romarr-stalled"]


# ----------------------------------------------------------------- #107 ---

def test_issue_107_ui_local_timezone_helpers():
    """#107: UI defines fmtDate and fmtDateTime helpers converting UTC ISO-8601
    timestamps to local time, with a UTC toggle on the Live log page."""
    html = page()
    assert "const fmtDate=" in html
    assert "const fmtDateTime=" in html
    assert "fmtDate(w.added)" in html
    assert "fmtDateTime(e.at)" in html
    assert "fmtDateTime(t.last_run)" in html
    assert 'id="lg-utc"' in html


# ------------------------------------------------------------------ #93 ---

def test_issue_93_rom_hub_pin_updated():
    """#93: Dockerfile, Dockerfile.browser, and docs/PLUGINS.md pin the updated
    rom-hub commit containing the catalog & Igir v5.8.0 updates."""
    root = pathlib.Path(__file__).resolve().parents[1]
    expected_sha = "f30123ff9113c2a8067e76e177a2405ca66561ab"
    for rel in ("Dockerfile", "Dockerfile.browser", "docs/PLUGINS.md"):
        content = (root / rel).read_text(encoding="utf-8")
        assert expected_sha in content, f"{rel} does not pin {expected_sha}"
