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
    ("imported", {"items": [{"primary": "Game.gb"}]}, True, False),
])
def test_preview_eligibility_follows_download_completion(tmp_path, state, preview, complete, expected):
    s = svc(tmp_path)
    s.queue = [QueueItem("Game", "gb", "release", 1, state, download_id="job", import_preview=preview)]
    s.clients = [SimpleNamespace(completed=lambda: [{"id": "job"}] if complete else [])]
    assert s.queue_rows()[0]["can_preview_import"] is expected


def test_queue_checks_each_client_once_and_survives_client_failures(tmp_path):
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
    from romarr.downloaders import DebridClient, DebridConfig
    s = svc(tmp_path)
    client = DebridClient(DebridConfig(api_token="test", save_path=str(tmp_path)))
    client._finished = lambda: [(name, ["https://example.com/Game.gb"]) for name in ("Pending", "Finished")]
    client._download = Mock(side_effect=AssertionError("must not fetch"))
    path = tmp_path / "Finished" / "Game.gb"
    path.parent.mkdir()
    path.write_bytes(b"rom")
    s.clients = [client]
    s.queue = [QueueItem(name, "gb", name, 1, "grabbed") for name in ("Pending", "Finished")]
    assert [r["can_preview_import"] for r in s.queue_rows()] == [False, True]
    client._download.assert_not_called()
