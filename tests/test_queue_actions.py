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


def test_preview_import_is_offered_only_for_rows_with_a_saved_preview():
    from dataclasses import asdict
    import json
    import shutil
    import subprocess

    import pytest

    from romarr import ui

    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js needed for the queue render check")
    preview = {"items": [{"primary": "Game.nsp"}]}
    rows = [asdict(QueueItem("Needs review", "switch", "", 0, "import-review", import_preview=preview)),
            asdict(QueueItem("Selection saved", "switch", "", 0, "grabbed",
                             import_preview=preview, import_intent={"token": "t"})),
            asdict(QueueItem("Downloading", "snes", "", 0, "grabbed")),
            asdict(QueueItem("Search failed", "snes", "", 0, "failed")),
            asdict(QueueItem("Import failed", "snes", "", 0, "import-failed")),
            asdict(QueueItem("Nothing to review", "switch", "", 0, "import-review",
                             import_preview={"items": []})),
            asdict(QueueItem("Imported", "switch", "", 0, "imported", import_preview=preview))]
    start = ui.JS.index("RENDER.queue=")
    end = ui.JS.index("RENDER.history=", start)
    harness = r"""
const assert=require('assert');
const RENDER={},nodes={},buttons={},calls=[];
const $=selector=>nodes[selector]||(nodes[selector]={innerHTML:''});
const document={querySelectorAll:selector=>buttons[selector]??=(()=>{
  const attr=selector.slice(1,-1);
  return [...$('#page').innerHTML.matchAll(new RegExp(attr+'="(\\d+)"','g'))]
    .map(m=>({dataset:{[attr.slice(5)]:m[1]}}));
})()};
const j=async url=>{calls.push(url);return url==='/api/v1/queue'?{items:ROWS}:{error:'not complete'};};
const esc=x=>String(x??''); const toast=()=>{}; const go=()=>{}; const refreshCounts=()=>{};
""".replace("ROWS", json.dumps(rows))
    checks = r"""
(async()=>{
 await RENDER.queue();
 const actions=Object.fromEntries($('#page').innerHTML.split('<tr>').slice(2)
   .map(row=>[row.match(/<td>([^<]*)<\/td>/)[1],[...row.matchAll(/<button[^>]*>([^<]*)<\/button>/g)].map(m=>m[1])]));
 const previewable=Object.keys(actions).filter(game=>actions[game].includes('Preview import'));
 assert.deepEqual(previewable,['Needs review','Selection saved']);
 assert.deepEqual(Object.keys(actions).filter(game=>actions[game].includes('Retry')),['Search failed','Import failed']);
 await document.querySelectorAll('[data-qplan]')[1].onclick();
 assert.equal(calls.at(-1),'/api/v1/queue/import-plan?index=1');
})().catch(error=>{console.error(error);process.exitCode=1;});
"""
    result = subprocess.run([node, "-e", harness + ui.JS[start:end] + checks],
                            text=True, capture_output=True, timeout=30)
    assert result.returncode == 0, result.stderr
