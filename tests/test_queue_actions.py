"""Queue actions: the read-only list becomes actionable."""

from romarr.app import ROMarr, QueueItem
from types import SimpleNamespace
from unittest.mock import Mock

import pytest


def svc(tmp_path):
    return ROMarr(env={"ROMARR_DATA": str(tmp_path / "s.json")})


def test_remove_forgets_one_row(tmp_path):
    s = svc(tmp_path)
    s.queue = [QueueItem("A", "snes", "", 0, "failed"),
               QueueItem("B", "nes", "", 0, "grabbed")]
    out = s.queue_action(0, "remove")
    assert out["ok"] and out["removed"]
    assert [q.game for q in s.queue] == ["B"]


def test_retry_reruns_the_request_and_drops_the_stale_row(tmp_path, monkeypatch):
    s = svc(tmp_path)
    s.queue = [QueueItem("Chrono Trigger", "snes", "", 0, "failed",
                         "no usable release")]
    called = {}

    def fake_request(g, p):
        called["args"] = (g, p)
        return {"ok": True, "release": "Chrono Trigger (USA)"}

    monkeypatch.setattr(s, "request", fake_request)
    out = s.queue_action(0, "retry")
    assert out["ok"]
    assert called["args"] == ("Chrono Trigger", "snes")
    # The stale failed row is gone; the retry's own outcome replaces it.
    assert all(q.detail != "no usable release" for q in s.queue)


def test_an_out_of_range_index_is_a_clean_error(tmp_path):
    s = svc(tmp_path)
    assert not s.queue_action(9, "remove")["ok"]
    assert not s.queue_action(0, "remove")["ok"]  # empty queue


def test_clear_removes_only_the_named_state(tmp_path):
    s = svc(tmp_path)
    s.queue = [QueueItem("A", "snes", "", 0, "failed"),
               QueueItem("B", "nes", "", 0, "grabbed"),
               QueueItem("C", "gba", "", 0, "failed")]
    out = s.clear_queue("failed")
    assert out["removed"] == 2
    assert [q.game for q in s.queue] == ["B"]


def test_clear_all_empties_it(tmp_path):
    s = svc(tmp_path)
    s.queue = [QueueItem("A", "snes", "", 0, "failed")]
    assert s.clear_queue()["removed"] == 1
    assert s.queue == []


def test_the_queue_page_has_actions_now():
    from romarr.ui import page
    p = page()
    assert "data-qretry" in p and "data-qremove" in p
    assert "q-import" in p and "q-clearfail" in p
    assert "data-calreq" in p, "calendar entries are requestable"


@pytest.mark.parametrize("state,preview,complete,expected", [
    ("grabbed", {}, True, True),
    ("import-review", {"items": []}, True, True),
    ("grabbed", {"items": [{"primary": "Game.gb"}]}, True, True),
    ("import-failed", {}, True, True),
    ("grabbed", {}, False, False),
    ("failed", {}, False, False),
    ("failed", {}, True, False),
    ("imported", {"items": [{"primary": "Game.gb"}]}, True, False),
])
def test_preview_eligibility_follows_download_completion(tmp_path, state, preview, complete, expected):
    """Verify can_preview_import tracks completed downloads across all queue states."""
    s = svc(tmp_path)
    s.queue = [QueueItem("Game", "gb", "release", 1, state, download_id="job", import_preview=preview)]
    s.clients = [SimpleNamespace(completed=lambda: [{"id": "job"}] if complete else [])]
    assert s.queue_rows()[0]["can_preview_import"] is expected


def test_queue_checks_each_client_once_and_survives_client_failures(tmp_path):
    """Verify queue_rows() skips disabled clients, survives client errors, and polls each active client once."""
    s = svc(tmp_path)
    s.queue = [QueueItem("A", "gb", "release", 1, "grabbed", download_id="a"),
               QueueItem("B", "gb", "release", 1, "grabbed", download_id="b")]
    disabled = SimpleNamespace(configured=False, completed=Mock())
    broken = SimpleNamespace(completed=Mock(side_effect=OSError("offline")))
    working = SimpleNamespace(completed=Mock(return_value=[{"id": "b", "name": "release"}]))
    s.clients = [disabled, broken, working]
    assert [r["can_preview_import"] for r in s.queue_rows()] == [False, True]
    disabled.completed.assert_not_called()
    broken.completed.assert_called_once_with()
    working.completed.assert_called_once_with()


@pytest.mark.parametrize("kind", ["direct", "browser", "hub"])
def test_queue_reports_local_downloads_without_fetching_pending_jobs(tmp_path, kind):
    """Verify site and hub downloaders report done files without starting pending jobs."""
    from romarr.downloaders import build_client
    s = svc(tmp_path)
    client = build_client({"type": kind, "save_path": str(tmp_path)})
    path = tmp_path / "Game.gb"
    path.write_bytes(b"rom")
    client._write([{"name": "Pending", "state": "pending"},
                   {"name": "Finished", "state": "done", "file": str(path)}])
    client._run = Mock(side_effect=AssertionError("must not fetch"))
    s.clients = [client]
    s.queue = [QueueItem(name, "gb", name, 1, "grabbed") for name in ("Pending", "Finished")]
    assert [r["can_preview_import"] for r in s.queue_rows()] == [False, True]
    client._run.assert_not_called()


def test_queue_waits_for_debrid_files_to_be_downloaded_locally(tmp_path):
    """Verify DebridClient.completed(fetch=False) checks local files without resolving or downloading links."""
    from romarr.downloaders import DebridClient, DebridConfig
    s = svc(tmp_path)
    client = DebridClient(DebridConfig(api_token="test", save_path=str(tmp_path)))
    client._finished = lambda: [(name, ["https://example.com/Game.gb"]) for name in ("Pending", "Finished")]
    client._resolve = Mock(side_effect=AssertionError("must not resolve"))
    client._download = Mock(side_effect=AssertionError("must not fetch"))
    path = tmp_path / "Finished" / "Game.gb"
    path.parent.mkdir()
    path.write_bytes(b"rom")
    s.clients = [client]
    s.queue = [QueueItem(name, "gb", name, 1, "grabbed") for name in ("Pending", "Finished")]
    assert [r["can_preview_import"] for r in s.queue_rows()] == [False, True]
    client._resolve.assert_not_called()
    client._download.assert_not_called()


def test_queue_skips_clients_when_only_imported_or_failed_rows_exist(tmp_path):
    """Verify queue_rows() makes zero client calls when no preview-eligible rows exist."""
    s = svc(tmp_path)
    probe = Mock()
    s.clients = [SimpleNamespace(configured=True, completed=probe)]
    s.queue = [
        QueueItem("Imported", "gb", "Imported", 1, "imported"),
        QueueItem("Failed", "gb", "Failed", 0, "failed", detail="no release"),
    ]
    rows = s.queue_rows()
    assert [r["can_preview_import"] for r in rows] == [False, False]
    probe.assert_not_called()


def test_queue_import_plan_stops_after_first_matching_client(tmp_path):
    """Verify queue_import_plan stops polling remaining clients once one matches the item."""
    import zipfile
    s = svc(tmp_path)
    archive = tmp_path / "Target Release.zip"
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr("Game.gb", b"rom")
    first = SimpleNamespace(
        configured=True,
        completed=lambda: [{"name": "Target Release", "content_path": str(archive)}],
    )
    second = SimpleNamespace(configured=True, completed=Mock())
    s.clients = [first, second]
    s.queue = [QueueItem("Game", "gb", "Target Release", 1, "grabbed")]
    s.library_for = lambda slug: ({"path": str(tmp_path / "lib")}, SimpleNamespace(rescan=lambda slug: None))

    out = s.queue_import_plan(0, {})
    assert out["ok"] and len(out["preview"]["items"]) == 1
    second.completed.assert_not_called()


def test_queue_renderer_shows_preview_button_only_when_can_preview_import_is_true():
    """Verify RENDER.queue in romarr/ui.py renders Preview import only when can_preview_import is truthy."""
    import json
    import shutil
    import subprocess
    from romarr import ui

    assert "i.can_preview_import?" in ui.JS
    node = shutil.which("node")
    if not node:
        return
    start = ui.JS.index("RENDER.queue=async()=>{")
    end = ui.JS.index("RENDER.history=async()=>{", start)
    queue_fn = ui.JS[start:end]
    rows = [
        {"game": "Downloading", "platform": "snes", "release": "Dl", "seeders": 2, "state": "grabbed", "can_preview_import": False},
        {"game": "Completed", "platform": "snes", "release": "Ready", "seeders": 3, "state": "grabbed", "can_preview_import": True},
        {"game": "Empty Review", "platform": "switch", "release": "Bundle", "seeders": 1, "state": "import-review", "import_preview": {"items": []}, "can_preview_import": True},
        {"game": "Imported", "platform": "snes", "release": "Done", "seeders": 5, "state": "imported", "can_preview_import": False},
    ]
    script = f"""
const assert = require('assert');
const pageEl = {{ innerHTML: '' }};
const qImportEl = {{ onclick: null }};
const $ = sel => sel === '#page' ? pageEl : sel === '#q-import' ? qImportEl : null;
const esc = s => String(s ?? '');
const document = {{ querySelectorAll: () => [] }};
const RENDER = {{}};
const j = async () => ({{ items: {json.dumps(rows)} }});
{queue_fn}
(async () => {{
  await RENDER.queue();
  const html = pageEl.innerHTML;
  assert(!html.includes('data-qplan="0"'), 'downloading row must not offer Preview import');
  assert(html.includes('data-qplan="1"'), 'completed row must offer Preview import');
  assert(html.includes('data-qplan="2"'), 'review row with can_preview_import must offer Preview import');
  assert(!html.includes('data-qplan="3"'), 'imported row must not offer Preview import');
}})().catch(err => {{ console.error(err); process.exit(1); }});
"""
    res = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=15)
    assert res.returncode == 0, res.stderr


def test_queue_bounds_slow_debrid_probe_and_preserves_cached_completion(tmp_path):
    """Verify slow debrid probes stop at QUEUE_PROBE_TIMEOUT while still surfacing cached local completions."""
    import time
    from romarr.downloaders import RealDebrid, RealDebridConfig

    save_dir = tmp_path / "rd"
    save_dir.mkdir()
    rd = RealDebrid(RealDebridConfig(api_token="tok", save_path=str(save_dir), timeout=30))
    rd._remember("cached-id")
    for idx in range(4):
        rd._remember(f"slow-{idx}")

    class Session:
        """Fake HTTP session toggling between fast initial reads and slow polls."""
        def __init__(self):
            """Initialize recorded request log and speed toggle."""
            self.calls = []
            self.slow = False

        def get(self, url, **kwargs):
            """Simulate fast initial state or slow Real-Debrid polling."""
            self.calls.append((url, kwargs.get("timeout")))
            if self.slow:
                time.sleep(0.06)
                return SimpleNamespace(raise_for_status=lambda: None,
                                       json=lambda: {"status": "downloading"})
            if url.endswith("/torrents/info/cached-id"):
                return SimpleNamespace(raise_for_status=lambda: None,
                                       json=lambda: {"status": "downloaded",
                                                     "filename": "Cached Game",
                                                     "links": ["https://rd.test/opaque-1"],
                                                     "hash": "a" * 40})
            return SimpleNamespace(raise_for_status=lambda: None,
                                   json=lambda: {"status": "downloading"})

        def post(self, url, **kwargs):
            """Simulate Real-Debrid link unrestriction response."""
            return SimpleNamespace(raise_for_status=lambda: None,
                                   text="ok",
                                   json=lambda: {"filename": "Cached.gb", "download": "https://cdn.test/Cached.gb"})

    sess = Session()
    rd._session = sess
    rd._download = lambda url, dest: dest.parent.mkdir(parents=True, exist_ok=True) or dest.write_bytes(b"rom") or True
    assert len(rd.completed(fetch=True)) == 1

    # Add an uncached directory on disk so queue probe must query _finished(),
    # then make provider calls slow so the probe must cut off cleanly.
    uncached = save_dir / "Uncached Release"
    uncached.mkdir()
    (uncached / "part.bin").write_bytes(b"partial-or-other")
    sess.calls.clear()
    sess.slow = True

    s = svc(tmp_path)
    s.QUEUE_PROBE_TIMEOUT = 0.05
    s.clients = [rd]
    s.queue = [QueueItem("Cached Game", "gb", "Cached Game", 1, "grabbed", release_id="btih:" + "a" * 40)]

    started = time.monotonic()
    rows = s.queue_rows()
    elapsed = time.monotonic() - started

    assert rows[0]["can_preview_import"] is True
    assert elapsed < 0.5
    assert len(sess.calls) < 5
    assert all((t or 0) <= 0.05 + 1e-6 for _, t in sess.calls)


def test_debrid_opaque_link_ignores_unrelated_file_and_tracks_fetched_name(tmp_path):
    """Verify RealDebrid(fetch=False) ignores unrelated files and tracks the actual fetched filename."""
    from romarr.downloaders import RealDebrid, RealDebridConfig

    save_dir = tmp_path / "rd"
    target = save_dir / "Mystery Release"
    target.mkdir(parents=True)
    (target / "unrelated.txt").write_bytes(b"not the rom")

    rd = RealDebrid(RealDebridConfig(api_token="tok", save_path=str(save_dir)))
    rd._finished = lambda: [("Mystery Release", ["https://rd.test/d/opaque123"], "b" * 40)]
    resolve_mock = Mock(return_value=("Real Game.gb", "https://cdn.test/dl"))
    rd._resolve = resolve_mock
    rd._download = lambda url, dest: dest.write_bytes(b"rom") or True

    s = svc(tmp_path)
    s.clients = [rd]
    s.queue = [QueueItem("Real Game", "gb", "Mystery Release", 1, "grabbed", release_id="btih:" + "b" * 40)]

    assert rd.completed(fetch=False) == []
    assert s.queue_rows()[0]["can_preview_import"] is False
    resolve_mock.assert_not_called()

    fetched = rd.completed(fetch=True)
    assert len(fetched) == 1
    assert (target / "Real Game.gb").is_file()

    # A fresh RealDebrid instance reads the persisted fetched filename record without calling _resolve.
    fresh = RealDebrid(RealDebridConfig(api_token="tok", save_path=str(save_dir)))
    fresh._finished = lambda: [("Mystery Release", ["https://rd.test/d/opaque123"], "b" * 40)]
    fresh._resolve = Mock(side_effect=AssertionError("must not resolve during fetch=False"))
    assert len(fresh.completed(fetch=False)) == 1
    s.clients = [fresh]
    assert s.queue_rows()[0]["can_preview_import"] is True

    # Removing the real fetched file leaves only unrelated.txt, so fetch=False reports incomplete again.
    (target / "Real Game.gb").unlink()
    assert fresh.completed(fetch=False) == []
    assert s.queue_rows()[0]["can_preview_import"] is False


def test_queue_preview_and_plan_match_against_full_queue(tmp_path):
    """Verify duplicate titles and tagged downloads cannot offer or apply preview on the wrong queue row."""
    import zipfile
    archive = tmp_path / "Shared Title.zip"
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr("Game.gb", b"rom")

    s = svc(tmp_path)
    s.library_for = lambda slug: ({"path": str(tmp_path / "lib")}, SimpleNamespace(rescan=lambda slug: None))
    s.clients = [SimpleNamespace(configured=True, completed=lambda: [{"name": "Shared Title", "content_path": str(archive)}])]
    s.queue = [
        QueueItem("Game", "gb", "Shared Title", 1, "import-review"),
        QueueItem("Game", "gb", "Shared Title", 2, "grabbed"),
    ]

    assert [r["can_preview_import"] for r in s.queue_rows()] == [False, True]
    wrong = s.queue_import_plan(0, {})
    assert not wrong.get("ok") and "error" in wrong
    right = s.queue_import_plan(1, {})
    assert right["ok"] and len(right["preview"]["items"]) == 1

    # Also verify a tagged row owns the download cleanly over an untagged duplicate title.
    s.clients = [SimpleNamespace(configured=True, completed=lambda: [
        {"name": "Shared Title", "tags": "romarr-owned", "content_path": str(archive)}
    ])]
    s.queue = [
        QueueItem("Game", "gb", "Shared Title", 1, "grabbed"),
        QueueItem("Game", "gb", "Shared Title", 2, "grabbed", download_id="qbit-tag:romarr-owned"),
    ]
    assert [r["can_preview_import"] for r in s.queue_rows()] == [False, True]
    assert not s.queue_import_plan(0, {}).get("ok")
    assert s.queue_import_plan(1, {})["ok"]


def test_exhausted_network_budget_still_probes_local_and_cached_debrid_clients(tmp_path):
    """Verify exhausting the network probe deadline never skips local or cached debrid clients."""
    import os
    import time
    from romarr.clients import QBittorrent, QbitConfig
    from romarr.downloaders import (
        Blackhole, BlackholeConfig, RealDebrid, RealDebridConfig, SiteConfig, SiteDownloader,
    )

    class SlowQbitSession:
        """Session that consumes the probe budget on the first torrent check."""

        def __init__(self):
            """Initialize call counter."""
            self.calls = 0

        def get(self, url, **kwargs):
            """Sleep longer than QUEUE_PROBE_TIMEOUT then return empty rows."""
            self.calls += 1
            time.sleep(0.06)
            return SimpleNamespace(status_code=200, ok=True, text="[]", json=lambda: [], raise_for_status=lambda: None)

        def post(self, url, **kwargs):
            """Accept qBittorrent login."""
            return SimpleNamespace(status_code=200, ok=True, text="Ok.", json=lambda: {}, raise_for_status=lambda: None)

    class TrailingSabSession:
        """Session that must not be called once network budget is exhausted."""

        def __init__(self):
            """Track HTTP GET attempts."""
            self.called = False

        def get(self, url, **kwargs):
            """Record unexpected network probe after budget exhaustion."""
            self.called = True
            raise AssertionError("trailing network client must be skipped")

    from romarr.downloaders import SabConfig, SABnzbd

    slow_qbit = QBittorrent(QbitConfig(base_url="http://qbit", category="romarr", timeout=30), session=SlowQbitSession())
    slow_qbit._authed = True

    site_dir = tmp_path / "site"
    site_dir.mkdir()
    site_file = site_dir / "Site Game.gb"
    site_file.write_bytes(b"rom")
    site_client = SiteDownloader(SiteConfig(save_path=str(site_dir)))
    site_client._write([{"name": "Site Game", "state": "done", "file": str(site_file)}])

    bh_drop = tmp_path / "drop"
    bh_watch = tmp_path / "watch"
    bh_drop.mkdir()
    bh_watch.mkdir()
    bh_file = bh_watch / "Blackhole Game.gb"
    bh_file.write_bytes(b"rom")
    old = bh_file.stat().st_mtime - 600
    os.utime(bh_file, (old, old))
    bh_client = Blackhole(BlackholeConfig(drop_path=str(bh_drop), watch_path=str(bh_watch), settle=0))

    rd_dir = tmp_path / "rd"
    rd_dir.mkdir()
    rd = RealDebrid(RealDebridConfig(api_token="tok", save_path=str(rd_dir), timeout=30))
    target = rd_dir / "Debrid Cached"
    target.mkdir()
    (target / "Debrid Cached.gb").write_bytes(b"rom")
    cached_row = {"name": "Debrid Cached", "content_path": str(target), "save_path": str(rd_dir), "state": "downloaded", "hash": "d" * 40}
    rd._update_fetched_cache(target, resolved={"str:https://rd/1": "Debrid Cached.gb"}, completed_row=cached_row, files=["Debrid Cached.gb"])
    rd._session = TrailingSabSession()

    sab_sess = TrailingSabSession()
    sab = SABnzbd(SabConfig(base_url="http://sab", api_key="key"), session=sab_sess)

    s = svc(tmp_path)
    s.QUEUE_PROBE_TIMEOUT = 0.04
    s.clients = [slow_qbit, site_client, bh_client, rd, sab]
    s.queue = [
        QueueItem("Site", "gb", "Site Game", 1, "grabbed"),
        QueueItem("BH", "gb", "Blackhole Game", 1, "grabbed"),
        QueueItem("RD", "gb", "Debrid Cached", 1, "grabbed", release_id="btih:" + "d" * 40),
        QueueItem("SAB", "gb", "Trailing Sab", 1, "grabbed", download_id="sab-1"),
    ]

    rows = s.queue_rows()
    assert [r["can_preview_import"] for r in rows] == [True, True, True, False]
    assert not sab_sess.called


def test_generic_network_clients_receive_bounded_probe_timeout(tmp_path):
    """Verify generic network clients and unparameterized clients honor QUEUE_PROBE_TIMEOUT."""
    import time
    from romarr.downloaders import (
        Aria2, Aria2Config, Flood, FloodConfig, FreeboxConfig, FreeboxDownload,
        Hadouken, HadoukenConfig, NZBGet, NZBVortex, NzbVortexConfig, NzbgetConfig,
        Porla, PorlaConfig, SABnzbd, SabConfig, SynologyConfig, SynologyDownloadStation,
        Transmission, TransmissionConfig, UTorrent, UTorrentConfig,
    )

    timeouts: list[float | None] = []

    class RecordingSession:
        """Fake requests.Session capturing every call timeout."""

        def get(self, url, **kwargs):
            """Record GET timeout and satisfy handshake/list endpoints."""
            timeouts.append(kwargs.get("timeout"))
            if url.endswith("token.html"):
                body = "<div id='token'>TOK</div>"
                return SimpleNamespace(status_code=200, text=body, json=lambda: {}, raise_for_status=lambda: None)
            if url.endswith("/auth/nonce"):
                return SimpleNamespace(status_code=200, text="{}", json=lambda: {"authNonce": "n"}, raise_for_status=lambda: None)
            if url.endswith("/auth/login"):
                return SimpleNamespace(status_code=200, text="{}", json=lambda: {"sessionID": "s"}, raise_for_status=lambda: None)
            if url.endswith("/login"):
                return SimpleNamespace(status_code=200, text="{}", json=lambda: {"result": {"challenge": "c"}}, raise_for_status=lambda: None)
            if url.endswith("auth.cgi"):
                return SimpleNamespace(status_code=200, text="{}", json=lambda: {"success": True, "data": {"sid": "s"}}, raise_for_status=lambda: None)
            return SimpleNamespace(status_code=200, text="{}", json=lambda: {"history": {"slots": []}, "result": [], "torrents": {}, "nzbs": [], "data": {"tasks": []}}, raise_for_status=lambda: None)

        def post(self, url, **kwargs):
            """Record POST timeout and satisfy auth/list RPC endpoints."""
            timeouts.append(kwargs.get("timeout"))
            if url.endswith("/auth/authenticate"):
                return SimpleNamespace(status_code=200, text="{}", json=lambda: {"success": True}, raise_for_status=lambda: None)
            if url.endswith("/login/session"):
                return SimpleNamespace(status_code=200, text="{}", json=lambda: {"result": {"session_token": "t"}}, raise_for_status=lambda: None)
            return SimpleNamespace(status_code=200, text="{}", json=lambda: {"result": "success", "arguments": {"torrents": []}, "torrents": {}}, raise_for_status=lambda: None)

    sess = RecordingSession()
    clients = [
        SABnzbd(SabConfig(base_url="http://sab", api_key="k", timeout=30), session=sess),
        NZBGet(NzbgetConfig(base_url="http://nzbget", timeout=30), session=sess),
        NZBVortex(NzbVortexConfig(base_url="https://nzbvortex", api_key="k", timeout=30), session=sess),
        Transmission(TransmissionConfig(base_url="http://tx", timeout=30), session=sess),
        SynologyDownloadStation(SynologyConfig(base_url="http://syno", username="u", password="p", timeout=30), session=sess),
        Aria2(Aria2Config(base_url="http://aria2", timeout=30), session=sess),
        Flood(FloodConfig(base_url="http://flood", username="u", password="p", timeout=30), session=sess),
        FreeboxDownload(FreeboxConfig(base_url="http://fbx", app_id="a", app_token="t", timeout=30), session=sess),
        Hadouken(HadoukenConfig(base_url="http://hdk", timeout=30), session=sess),
        UTorrent(UTorrentConfig(base_url="http://ut", timeout=30), session=sess),
        Porla(PorlaConfig(base_url="http://porla", api_token="t", timeout=30), session=sess),
    ]

    for idx, client in enumerate(clients):
        timeouts.clear()
        s = svc(tmp_path / f"c{idx}")
        s.QUEUE_PROBE_TIMEOUT = 0.05
        s.clients = [client]
        s.queue = [QueueItem("Game", "gb", "Rel", 1, "grabbed")]
        s.queue_rows()
        assert timeouts, f"{client.name} made no HTTP calls"
        assert all(isinstance(t, float) and t <= 0.05 + 1e-6 for t in timeouts), f"{client.name} exceeded probe timeout: {timeouts}"

    # Verify a slow custom client whose completed() accepts no timeout parameter is still bounded.
    s = svc(tmp_path / "slow-custom")
    s.QUEUE_PROBE_TIMEOUT = 0.05
    s.clients = [SimpleNamespace(configured=True, name="Custom", completed=lambda: (time.sleep(0.35), [])[1])]
    s.queue = [QueueItem("Game", "gb", "Rel", 1, "grabbed")]
    started = time.monotonic()
    assert s.queue_rows()[0]["can_preview_import"] is False
    assert (time.monotonic() - started) < 0.25


def test_qbittorrent_selective_completed_shares_single_deadline():
    """Verify selective_completed shares one deadline and stops probing further hashes cleanly."""
    import time
    from romarr.clients import QBittorrent, QbitConfig

    calls: list[tuple[str, float | None]] = []

    class SlowQbitSession:
        """Fake qBittorrent session recording per-request timeouts across collection hashes."""

        def get(self, url, **kwargs):
            """Delay slightly on torrents/files so the second hash sees an expired deadline."""
            calls.append((url.rsplit("/", 1)[-1], kwargs.get("timeout")))
            if url.endswith("/torrents/info"):
                return SimpleNamespace(
                    ok=True, status_code=200,
                    json=lambda: [
                        {"hash": "h1", "category": "romarr", "tags": "romarr-collection-1"},
                        {"hash": "h2", "category": "romarr", "tags": "romarr-collection-2"},
                    ],
                    raise_for_status=lambda: None,
                )
            if url.endswith("/torrents/files"):
                time.sleep(0.06)
                return SimpleNamespace(
                    ok=True, status_code=200,
                    json=lambda: [{"index": 0, "name": "a.gb", "size": 10, "priority": 1, "progress": 1.0}],
                    raise_for_status=lambda: None,
                )
            raise AssertionError(f"unexpected URL {url}")

    qbit = QBittorrent(QbitConfig(base_url="http://qbit", category="romarr", timeout=30), session=SlowQbitSession())
    qbit.romarr_id = "q1"
    qbit._authed = True
    owners = [
        QueueItem("G1", "gb", "C1", 1, "grabbed", download_client="q1", download_id="qbit-tag:romarr-collection-1", selected_file={"hash": "h1", "index": 0, "path": "a.gb", "size": 10}),
        QueueItem("G2", "gb", "C2", 1, "grabbed", download_client="q1", download_id="qbit-tag:romarr-collection-2", selected_file={"hash": "h2", "index": 0, "path": "b.gb", "size": 10}),
    ]
    rows = qbit.selective_completed(owners, ownership=owners, update_cache=False, timeout=0.05)
    assert not any(r.get("_romarr_selection_error") for r in rows)
    assert all(t is not None and t <= 0.05 + 1e-6 for _, t in calls)
    assert [u for u, _ in calls] == ["info", "files"]


def test_qbittorrent_login_and_session_refresh_respect_deadline():
    """Verify qBittorrent initial login and 403 re-login retry share the active probe timeout."""
    from romarr.clients import QBittorrent, QbitConfig

    recorded: list[tuple[str, float | None]] = []
    state = {"get_count": 0}

    class RefreshSession:
        """Fake session requiring login on start and returning 403 once to force re-login."""

        def post(self, url, **kwargs):
            """Record auth/login timeout."""
            recorded.append(("post:" + url.rsplit("/", 1)[-1], kwargs.get("timeout")))
            return SimpleNamespace(ok=True, status_code=200, text="Ok.", json=lambda: {}, raise_for_status=lambda: None)

        def get(self, url, **kwargs):
            """Force 403 on first GET, succeed on retry."""
            recorded.append(("get:" + url.rsplit("/", 1)[-1], kwargs.get("timeout")))
            state["get_count"] += 1
            if state["get_count"] == 1:
                return SimpleNamespace(ok=False, status_code=403, text="Forbidden", json=lambda: [], raise_for_status=lambda: None)
            return SimpleNamespace(ok=True, status_code=200, text="[]", json=lambda: [], raise_for_status=lambda: None)

    qbit = QBittorrent(QbitConfig(base_url="http://qbit", username="admin", password="pw", timeout=30), session=RefreshSession())
    assert qbit.completed(repair=False, timeout=0.05) == []
    assert len(recorded) == 4  # initial login + initial 403 GET + refresh login + retry GET
    assert all(t is not None and t <= 0.05 + 1e-6 for _, t in recorded)


def test_debrid_concurrent_completed_calls_isolate_thread_deadlines(tmp_path):
    """Verify concurrent DebridClient.completed() calls keep probe deadlines isolated per thread."""
    import threading
    from romarr.downloaders import DebridClient, DebridConfig

    rd = DebridClient(DebridConfig(api_token="tok", save_path=str(tmp_path)))
    rd.configured  # verify property works
    entered = threading.Barrier(2)
    finished_short = threading.Event()
    observed: list[float | None] = []

    def fake_finished():
        """Synchronize two overlapping probes so short finishes while long still executes."""
        if threading.current_thread().name == "long":
            entered.wait(timeout=2)
            assert finished_short.wait(timeout=2)
            observed.append(rd._get_probe_deadline())
            return []
        entered.wait(timeout=2)
        return []

    rd._finished = fake_finished
    # Force fallback to _finished() by adding one uncached folder on disk.
    uncached = tmp_path / "Uncached"
    uncached.mkdir()
    (uncached / "f.bin").write_bytes(b"1")

    t_long = threading.Thread(name="long", target=lambda: rd.completed(fetch=False, timeout=2.0))
    t_long.start()
    rd.completed(fetch=False, timeout=0.02)
    finished_short.set()
    t_long.join(timeout=3)
    assert observed and observed[0] is not None and not rd._probe_timed_out()
    assert rd._get_probe_deadline() is None


def test_debrid_non_timeout_scan_failure_still_merges_cached_rows(tmp_path):
    """Verify DebridClient.completed(fetch=False) merges verified cache rows after HTTP errors."""
    import requests as _req
    from romarr.downloaders import RealDebrid, RealDebridConfig

    save_dir = tmp_path / "rd"
    save_dir.mkdir()
    target = save_dir / "Cached Game"
    target.mkdir()
    (target / "Cached Game.gb").write_bytes(b"rom")

    rd = RealDebrid(RealDebridConfig(api_token="tok", save_path=str(save_dir), timeout=30))
    cached_row = {"name": "Cached Game", "content_path": str(target), "save_path": str(save_dir), "state": "downloaded"}
    rd._update_fetched_cache(target, resolved={"str:https://rd/1": "Cached Game.gb"}, completed_row=cached_row, files=["Cached Game.gb"])
    rd._remember("failing-id")
    # Uncached dir forces live scan instead of fast-pathing from cache.
    other = save_dir / "Other Dir"
    other.mkdir()
    (other / "a.bin").write_bytes(b"1")

    class ErrorSession:
        """Session failing immediately so _probe_timed_out() remains False."""

        def get(self, url, **kwargs):
            """Raise connection error before timeout expires."""
            raise _req.ConnectionError("transient offline")

    rd._session = ErrorSession()
    rows = rd.completed(fetch=False, timeout=5.0)
    assert [r["name"] for r in rows] == ["Cached Game"]


def test_debrid_rejects_filename_collisions_before_caching(tmp_path):
    """Verify DebridClient rejects releases whose resolved links collide on filename."""
    from romarr.downloaders import RealDebrid, RealDebridConfig

    save_dir = tmp_path / "rd"
    save_dir.mkdir()
    rd = RealDebrid(RealDebridConfig(api_token="tok", save_path=str(save_dir)))
    rd._finished = lambda: [("Colliding Release", ["https://rd/1", "https://rd/2"])]
    rd._resolve = lambda link: ("SameName.gb", f"https://cdn/{link.rsplit('/', 1)[-1]}")
    rd._download = lambda url, dest: dest.parent.mkdir(parents=True, exist_ok=True) or dest.write_bytes(b"rom") or True

    assert rd.completed(fetch=True) == []
    assert rd._cached_completed_rows() == {}
    assert rd.completed(fetch=False) == []
    # Also ensure _expected_filenames rejects duplicate tuple filenames even on fetch=False.
    dup_dir = save_dir / "Dup"
    dup_dir.mkdir()
    (dup_dir / "rom.gb").write_bytes(b"rom")
    assert rd._expected_filenames([("rom.gb", "1"), ("rom.gb", "2")], dup_dir, fetch=False) is None


def test_putio_withholds_transfer_when_subfolder_traversal_is_incomplete(tmp_path):
    """Verify PutIo withholds a multi-folder transfer when a subfolder GET fails or times out."""
    from romarr.downloaders import DebridConfig, PutIo

    class PartialPutIoSession:
        """Fake put.io API failing only on the nested subfolder listing."""

        def get(self, url, params=None, **kwargs):
            """Serve transfer and top folder, fail nested subfolder listing."""
            if url.endswith("/transfers/1"):
                return SimpleNamespace(raise_for_status=lambda: None, json=lambda: {"transfer": {"id": 1, "name": "Multi", "status": "COMPLETED", "file_id": 10}})
            if url.endswith("/files/list") and (params or {}).get("parent_id") == 10:
                return SimpleNamespace(raise_for_status=lambda: None, json=lambda: {"files": [
                    {"id": 11, "name": "disc1.bin", "file_type": "FILE"},
                    {"id": 12, "name": "sub", "file_type": "FOLDER"},
                ]})
            raise OSError("subfolder lookup failed")

    # Wrap OSError cleanly as RequestException via session error
    import requests as _req

    class TimeoutSubfolderSession(PartialPutIoSession):
        """Raise RequestException when querying the nested subfolder."""

        def __init__(self):
            """Track API requests to prove the nested folder lookup executes."""
            self.calls = []

        def get(self, url, params=None, **kwargs):
            """Fail nested subfolder listing via requests.Timeout."""
            self.calls.append((url, params))
            if url.endswith("/files/list") and (params or {}).get("parent_id") == 12:
                raise _req.Timeout("subfolder timeout")
            return super().get(url, params=params, **kwargs)

    session = TimeoutSubfolderSession()
    client = PutIo(DebridConfig(api_token="tok", save_path=str(tmp_path), base_url="https://api.put.io/v2"), session=session)
    client._remember("1")
    (tmp_path / "Multi").mkdir()
    (tmp_path / "Multi" / "disc1.bin").write_bytes(b"rom")
    assert client.completed(fetch=False) == []
    assert ("https://api.put.io/v2/files/list", {"parent_id": 12}) in session.calls


def test_queue_renderer_includes_refresh_button_and_live_poll_in_browser_mode():
    """Verify RENDER.queue provides manual Refresh and auto-refreshes pending rows when eligible."""
    import json
    import shutil
    import subprocess
    from romarr import ui

    node = shutil.which("node")
    if not node:
        return
    start = ui.JS.index("RENDER.queue=async()=>{")
    end = ui.JS.index("RENDER.history=async()=>{", start)
    queue_fn = ui.JS[start:end]
    pending = [{"game": "Game", "platform": "snes", "release": "Rel", "seeders": 2, "state": "grabbed", "can_preview_import": False}]
    ready = [{"game": "Game", "platform": "snes", "release": "Rel", "seeders": 2, "state": "grabbed", "can_preview_import": True}]
    script = f"""
const assert = require('assert');
const pageEl = {{ innerHTML: '' }};
const qImportEl = {{ onclick: null }};
const qRefreshEl = {{ disabled: false, onclick: null }};
const $ = sel => sel === '#page' ? pageEl : sel === '#q-import' ? qImportEl : sel === '#q-refresh' ? qRefreshEl : null;
const esc = s => String(s ?? '');
const document = {{ hidden: false, querySelectorAll: () => [] }};
const window = {{}};
const activePage = 'queue';
let pageVersion = 1;
const RENDER = {{}};
let goes = [];
const go = p => goes.push(p);
let pollCount = 0;
let intervalCb = null;
let cleared = 0;
global.setInterval = (fn) => {{ intervalCb = fn; return 99; }};
global.clearInterval = (id) => {{ if (id === 99) {{ cleared++; intervalCb = null; }} }};
const j = async () => ({{ items: pollCount++ === 0 ? {json.dumps(pending)} : {json.dumps(ready)} }});
{queue_fn}
(async () => {{
  await RENDER.queue();
  assert(pageEl.innerHTML.includes('id="q-refresh"'), 'Refresh button must render');
  assert(!pageEl.innerHTML.includes('data-qplan="0"'), 'initial pending row has no Preview button');
  assert(typeof qRefreshEl.onclick === 'function', 'Refresh click handler attached');
  qRefreshEl.onclick();
  assert.deepStrictEqual(goes, ['queue']);
  assert(typeof intervalCb === 'function', 'live poll interval scheduled for pending rows');
  await intervalCb();
  await new Promise(r => setTimeout(r, 20));
  assert(cleared >= 1, 'timer cleared once queue snapshot changed');
  assert(pageEl.innerHTML.includes('data-qplan="0"'), 'Preview button appears after completion update');
}})().catch(err => {{ console.error(err); process.exit(1); }});
"""
    res = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=15)
    assert res.returncode == 0, res.stderr


def test_rtorrent_completed_builds_fresh_bounded_proxy_after_cached_reachable():
    """Rtorrent.completed(timeout=...) must not reuse an unbounded cached _proxy."""
    from romarr.downloaders import Rtorrent, RtorrentConfig

    built = []

    class FakeProxy:
        """Minimal stub mimicking an XML-RPC ServerProxy."""

        def __init__(self, timeout_val):
            """Record the socket timeout bound to this proxy instance."""
            self.timeout_val = timeout_val
            self.system = SimpleNamespace(client_version=lambda: "0.9.8")
            self.d = SimpleNamespace(multicall2=lambda *a: [["Game", 1, "/dl", "romarr"]])

    rt = Rtorrent(RtorrentConfig(base_url="http://rt.test/RPC2", timeout=30))
    rt._build_proxy = lambda base_url, timeout: built.append(timeout) or FakeProxy(timeout)
    assert rt.reachable() is True
    assert built == [30.0]
    rows = rt.completed(timeout=0.05)
    assert len(rows) == 1
    assert built == [30.0, 0.05]
    # Zero or non-positive timeout skips XML-RPC altogether.
    assert rt.completed(timeout=0.0) == []
    assert len(built) == 2


def test_timeout_aware_probe_enforces_wall_clock_and_caps_inflight_per_client(tmp_path):
    """Slow trickle probes accepting `timeout` are wall-clock bounded and deduplicated per client."""
    import threading
    import time
    from romarr.downloaders import Blackhole, BlackholeConfig

    release_probe = threading.Event()
    calls = 0

    class TrickleClient:
        """Timeout-aware client whose completed() ignores its timeout arg until released."""

        configured = True
        name = "Trickle"
        protocol = "torrent"
        romarr_id = "trickle-1"

        def completed(self, timeout: float | None = None):
            """Block past the queue probe timeout until released by the test."""
            nonlocal calls
            calls += 1
            release_probe.wait(timeout=2.0)
            return []

    watch = tmp_path / "watch"
    watch.mkdir()
    done_dir = watch / "Ready Game (USA)"
    done_dir.mkdir()
    (done_dir / "Ready Game (USA).sfc").write_bytes(b"rom")

    s = svc(tmp_path)
    s.QUEUE_PROBE_TIMEOUT = 0.05
    s.clients = [
        TrickleClient(),
        Blackhole(BlackholeConfig(drop_path=str(watch), watch_path=str(watch), settle=0.0)),
    ]
    s.queue = [
        QueueItem("Pending", "gb", "Pending Rel", 1, "grabbed", download_client="trickle-1"),
        QueueItem("Ready", "snes", "Ready Game (USA)", 1, "grabbed"),
    ]

    try:
        started = time.monotonic()
        rows_first = s.queue_rows()
        elapsed = time.monotonic() - started
        assert elapsed < 0.3
        assert rows_first[0]["can_preview_import"] is False
        assert rows_first[1]["can_preview_import"] is True
        # A second poll while the first TrickleClient probe is still hung must not spawn a 2nd thread.
        rows_second = s.queue_rows()
        assert rows_second[1]["can_preview_import"] is True
        assert calls == 1
    finally:
        release_probe.set()


def test_preview_import_invalidates_inflight_poll_and_stale_render():
    """Clicking Preview import must cancel in-flight queue polls so stale renders never clobber the form."""
    import json
    import shutil
    import subprocess
    from romarr import ui

    node = shutil.which("node")
    if not node:
        return
    start = ui.JS.index("RENDER.queue=async()=>{")
    end = ui.JS.index("RENDER.history=async()=>{", start)
    queue_fn = ui.JS[start:end]
    ready = [{"game": "Game", "platform": "snes", "release": "Rel", "seeders": 2, "state": "grabbed", "can_preview_import": True}]
    updated = [{"game": "Game", "platform": "snes", "release": "Rel2", "seeders": 3, "state": "grabbed", "can_preview_import": True}]
    script = f"""
const assert = require('assert');
const pageEl = {{ innerHTML: '' }};
const qImportEl = {{ onclick: null }};
const planBtn = {{ dataset: {{ qplan: '0' }}, onclick: null }};
const dummyEl = {{ value: '', onchange: null, onclick: null, checked: false }};
const $ = sel => sel === '#page' ? pageEl : sel === '#q-import' ? qImportEl : dummyEl;
const esc = s => String(s ?? '');
const document = {{
  hidden: false,
  querySelectorAll: sel => sel === '[data-qplan]' ? [planBtn] : [],
  querySelector: () => dummyEl,
}};
const window = {{}};
const activePage = 'queue';
let pageVersion = 1;
const RENDER = {{}};
const toast = () => {{}};
const staleQueueResolvers = [];
let pollFetches = 0;
let intervalCb = null;
let verified = false;
process.on('exit', () => {{ if (!verified) process.exitCode = 1; }});
global.setInterval = fn => {{ intervalCb = fn; return 77; }};
global.clearInterval = () => {{}};
const j = (url) => {{
  if (url === '/api/v1/queue') {{
    pollFetches++;
    if (pollFetches === 1) return Promise.resolve({{ items: {json.dumps(ready)} }});
    // Retain every deferred /api/v1/queue resolver (triggered by an in-flight poll or stale RENDER.queue)
    return new Promise(res => {{
      staleQueueResolvers.push(() => res({{ items: {json.dumps(updated)} }}));
    }});
  }}
  if (url.startsWith('/api/v1/queue/import-plan')) {{
    return Promise.resolve({{
      preview: {{
        note: 'preview ok',
        token: 'tok1',
        romm_routing: false,
        items: [{{ primary: 'Game.sfc', evidence: 'ok', confidence: 'high', bytes: 10, members: ['Game.sfc'], role: 'base', destination: 'snes/Game.sfc', selected: true }}],
        extras: [],
      }}
    }});
  }}
  return Promise.resolve({{}});
}};
{queue_fn}
(async () => {{
  await RENDER.queue();
  assert(typeof planBtn.onclick === 'function');
  // Start a background poll fetch that hangs until after Preview import starts.
  const pollPromise = intervalCb();
  await new Promise(r => setTimeout(r, 10));
  const staleRenderPromise = RENDER.queue();
  await new Promise(r => setTimeout(r, 10));
  assert.strictEqual(staleQueueResolvers.length, 2, 'expected both deferred queue reads to be pending');
  // User clicks Preview import while both pollPromise and staleRenderPromise are still awaiting /api/v1/queue.
  await planBtn.onclick();
  assert(pageEl.innerHTML.includes('Choose what enters the library'), 'preview form should render');
  // Resolve both deferred queue fetches and await both promises cleanly before checking the preview form.
  while (staleQueueResolvers.length) staleQueueResolvers.shift()();
  await Promise.all([pollPromise, staleRenderPromise]);
  assert(pageEl.innerHTML.includes('Choose what enters the library'), 'stale render must NOT overwrite preview form');
  verified = true;
}})().catch(err => {{ console.error(err); process.exit(1); }});
"""
    res = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=15)
    assert res.returncode == 0, res.stderr
