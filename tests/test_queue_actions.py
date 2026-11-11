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
