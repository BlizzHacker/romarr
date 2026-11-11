"""Queue actions: the read-only list becomes actionable."""

from romarr.app import ROMarr, QueueItem


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
