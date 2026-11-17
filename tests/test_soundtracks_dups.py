"""Soundtracks phase 2: duplicate groups verified on real bytes, merge, undo, and the download skip.

RomM is the loopback fake from soundtracks_support; nothing leaves 127.0.0.1.
"""

from __future__ import annotations

import json
import logging

import pytest

from romarr import soundtracks as st
from soundtracks_support import TOKEN, FakeRomm

A = {"id": "10", "name": "Game A"}
B = {"id": "11", "name": "Game B"}
C = {"id": "12", "name": "Game C"}
KEEP = {"01.mp3": b"one", "02.mp3": b"two"}


@pytest.fixture
def romm():
    r = FakeRomm()
    r.rom = {"fs_name": "Some Game", "has_multiple_files": True}
    yield r
    r.close()


@pytest.fixture
def api(romm):
    return st.RommApi(romm.url, lambda refresh=False: TOKEN)


@pytest.fixture
def store(tmp_path):
    return st.Soundtracks(tmp_path / "romarr-soundtracks.db")


def scan(store, api):
    store.start_dup_scan(api, run_async=False)
    return store.dups()


def test_identical_bytes_form_a_group_and_the_scan_reads_them_from_romm(store, api, romm):
    romm.seed(10, KEEP, "Game A")
    romm.seed(11, KEEP, "Game B")
    out = scan(store, api)
    assert out["scan"]["state"] == "done" and len(out["groups"]) == 1
    g = out["groups"][0]
    assert sorted(x["id"] for x in g["games"]) == ["10", "11"] and g["tracks"] == 2 and g["bytes"] == 6
    assert "files" not in g                                           # hashes stay server side
    assert any("/files/" in r[1] and "/content/" in r[1] for r in romm.requests)


def test_same_names_and_sizes_but_different_bytes_are_not_offered(store, api, romm):
    romm.seed(10, KEEP, "Game A")
    romm.seed(11, {"01.mp3": b"one", "02.mp3": b"TWO"}, "Game B")    # same sizes, one byte differs
    out = scan(store, api)
    assert out["groups"] == [] and out["scan"]["state"] == "done"


def test_three_games_where_one_differs_group_only_the_identical_pair(store, api, romm):
    romm.seed(10, KEEP, "A")
    romm.seed(11, KEEP, "B")
    romm.seed(12, {"01.mp3": b"xxx", "02.mp3": b"two"}, "C")
    out = scan(store, api)
    assert [sorted(x["id"] for x in g["games"]) for g in out["groups"]] == [["10", "11"]]


def test_a_single_game_or_no_soundtrack_is_never_a_group(store, api, romm):
    romm.seed(10, KEEP, "A")
    romm.tracks[11] = {}
    assert scan(store, api)["groups"] == []


def merge(store, api, keep=A, drops=(B,)):
    return store.start_merge(keep, list(drops), api, run_async=False)


def test_merge_deletes_only_the_non_kept_copies_and_journals_them(store, api, romm):
    romm.seed(10, KEEP, "A")
    romm.seed(11, KEEP, "B")
    romm.seed(12, KEEP, "C")
    scan(store, api)
    merge(store, api, A, (B,))
    job = store.jobs()[0]
    assert job["state"] == "done" and job["made"] == 1
    assert len(romm.tracks[10]) == 2 and romm.tracks[11] == {} and len(romm.tracks[12]) == 2      # C was not named
    deletes = [r[1] for r in romm.requests if r[0] == "DELETE"]
    assert len(deletes) == 2 and all(d.startswith("/api/roms/11/soundtracks/") for d in deletes)
    out = store.dups()
    assert [(m["drop_id"], m["keep_id"], m["state"]) for m in out["merged"]] == [("11", "10", "merged")]
    assert out["groups"] and [x["id"] for x in out["groups"][0]["games"]] == ["10", "12"]       # B left the set, the rest stays
    assert store.status(B, api)["state"] == "merged"


def test_merge_refuses_a_copy_whose_bytes_differ_and_leaves_it_alone(store, api, romm):
    romm.seed(10, KEEP, "A")
    romm.seed(11, {"01.mp3": b"one", "02.mp3": b"TWO"}, "B")
    merge(store, api)
    job = store.jobs()[0]
    assert job["state"] == "done" and job["made"] == 0 and "not identical" in job["message"]
    assert len(romm.tracks[11]) == 2 and not [r for r in romm.requests if r[0] == "DELETE"]
    assert store.dups()["merged"] == []


def test_merge_refuses_a_copy_with_different_names(store, api, romm):
    romm.seed(10, KEEP, "A")
    romm.seed(11, {"track1.mp3": b"one", "track2.mp3": b"two"}, "B")
    merge(store, api)
    assert store.jobs()[0]["made"] == 0 and len(romm.tracks[11]) == 2


def test_merge_with_nothing_to_keep_or_nothing_to_drop_is_refused(store, api, romm):
    romm.seed(10, {}, "A")
    romm.seed(11, KEEP, "B")
    with pytest.raises(ValueError):
        store.start_merge(A, [A], api, run_async=False)               # keeping a game "into itself"
    merge(store, api)
    assert store.jobs()[0]["state"] == "error" and len(romm.tracks[11]) == 2


def test_merge_drops_the_phase_one_journal_rows_of_the_files_it_removed(store, api, romm):
    romm.seed(10, KEEP, "A")
    romm.seed(11, KEEP, "B")
    fid = next(iter(romm.tracks[11]))
    c = store._db()
    c.execute("INSERT INTO journal(key,path,size,ts) VALUES('11',?,0,0)", (f"romm://11/{fid}/{romm.tracks[11][fid]}",))
    c.commit()
    c.close()
    merge(store, api)
    assert store._journal_count("11") == 0


def test_a_failed_delete_is_reported_recorded_as_partial_and_can_be_undone(store, api, romm):
    romm.seed(10, KEEP, "A")
    romm.seed(11, KEEP, "B")
    second = sorted(romm.tracks[11])[1]
    romm.fail_delete = {second}
    merge(store, api)
    job = store.jobs()[0]
    assert job["state"] == "done" and job["made"] == 0 and "could not remove" in job["message"]
    assert TOKEN not in job["message"]
    assert [m["state"] for m in store.dups()["merged"]] == ["partial"]
    romm.fail_delete = set()
    store.start_unmerge(B, api, run_async=False)
    assert sorted(romm.tracks[11].values()) == ["01.mp3", "02.mp3"]


def test_undo_re_uploads_the_kept_files_verified_and_journals_them(store, api, romm):
    romm.seed(10, KEEP, "A")
    romm.seed(11, KEEP, "B")
    merge(store, api)
    assert romm.tracks[11] == {}
    store.start_unmerge(B, api, run_async=False)
    assert store.jobs()[0]["state"] == "done"
    assert sorted(romm.tracks[11].values()) == ["01.mp3", "02.mp3"]
    assert sorted(romm.blobs[(11, f)] for f in romm.tracks[11]) == [b"one", b"two"]
    ups = [r for r in romm.uploads() if r[1] == "/api/roms/11/soundtracks"]
    assert {r[2]["x-upload-filename"] for r in ups} == {"01.mp3", "02.mp3"}
    assert store.dups()["merged"] == [] and store._journal_count("11") == 2
    assert len(romm.tracks[10]) == 2                                   # the keeper is untouched
    with pytest.raises(ValueError):
        store.start_unmerge(B, api, run_async=False)                   # nothing left to undo


def test_undo_refuses_when_the_kept_copy_changed_since_the_merge(store, api, romm):
    romm.seed(10, KEEP, "A")
    romm.seed(11, KEEP, "B")
    merge(store, api)
    fid = sorted(romm.tracks[10])[0]
    romm.blobs[(10, fid)] = b"edited by hand"
    store.start_unmerge(B, api, run_async=False)
    job = store.jobs()[0]
    assert job["state"] == "error" and "changed since the merge" in job["message"]
    assert romm.tracks[11] == {} and not [r for r in romm.uploads() if r[1] == "/api/roms/11/soundtracks"]
    assert [m["state"] for m in store.dups()["merged"]] == ["merged"]  # still undoable once the keeper is back


def test_one_job_per_game_covers_every_game_in_a_merge(store, api, romm):
    romm.seed(10, KEEP, "A")
    romm.seed(11, KEEP, "B")
    job = store._begin_job(["10", "11"], "x", "merge")
    with pytest.raises(st.JobBusy):
        store.start_merge(C, [B], api, run_async=False)                # B is busy
    with pytest.raises(st.JobBusy):
        store.undo(A, api)
    assert store.job_for("11")["id"] == job["id"]


def test_no_credential_reaches_a_job_a_listing_or_the_log(store, api, romm, caplog):
    caplog.set_level(logging.DEBUG)
    romm.seed(10, KEEP, "A")
    romm.seed(11, KEEP, "B")
    romm.fail_delete = {sorted(romm.tracks[11])[0]}
    scan(store, api)
    merge(store, api)
    text = json.dumps([store.dups(), store.jobs()]) + caplog.text
    assert TOKEN not in text


def test_download_skips_a_soundtrack_another_game_already_holds(store, api, romm, monkeypatch):
    monkeypatch.setattr(st, "stream", lambda url, on_total=None: iter([b"x"]))
    plan = lambda cid: {"downloads": [{"kind": "files", "files": [{"url": "https://archive.org/download/x/01 A.mp3", "name": "01 A.mp3"}]}], "tracks": 1}
    s = st.Soundtracks(store.db_path.parent / "other.db", providers={"fake": (lambda t, p: [], plan)})
    romm.rom = {"fs_name": "G", "has_multiple_files": True}
    cand = {"provider": "fake", "id": "item-1"}
    s.start_download({"id": "42", "name": "Chrono Trigger"}, api, cand, "files", run_async=False)
    with pytest.raises(st.AlreadyStored) as ex:
        s.start_download({"id": "43", "name": "Chrono Trigger (Rev A)"}, api, cand, "files", run_async=False)
    assert ex.value.holders == ["Chrono Trigger"] and not any(j["game_id"] == "43" for j in s.jobs())
    s.start_download({"id": "43", "name": "Chrono Trigger (Rev A)"}, api, cand, "files", run_async=False, force=True)
    assert s.jobs()[0]["game_id"] == "43" and s.jobs()[0]["state"] == "done"


def test_a_merged_games_soundtrack_still_counts_as_held(store, api, romm):
    romm.seed(10, KEEP, "A")
    romm.seed(11, KEEP, "B")
    store._set_sound("11", status="downloaded", chosen=json.dumps({"provider": "zophar", "id": "/m/x"}), title="Game B")
    merge(store, api)
    assert store.held_elsewhere("99", "zophar", "/m/x") == ["Game B"]
    assert store.held_elsewhere("11", "zophar", "/m/x") == []           # but not against itself
