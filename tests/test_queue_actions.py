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
