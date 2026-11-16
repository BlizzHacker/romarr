"""Soundtrack ownership, cancellation and restoration stay consistent under failures and overlap.

RomM is the loopback fake from soundtracks_support; nothing leaves 127.0.0.1.
"""

from __future__ import annotations

import zipfile

import pytest

from romarr import soundtracks as st
from soundtracks_support import TOKEN, FakeRomm

A = {"id": "10", "name": "Game A"}
B = {"id": "11", "name": "Game B"}
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


def test_a_token_replaced_by_a_refresh_is_still_redacted(romm):
    tokens = {"current": TOKEN}
    api = st.RommApi(romm.url, lambda refresh=False: tokens["current"])
    api.tracks("42")                                   # sends TOKEN
    tokens["current"] = "ANOTHER-token-refreshed-77"   # another job refreshed the cache
    assert TOKEN not in api._redact(f"upstream echoed Bearer {TOKEN}")


def test_a_cancelled_watch_is_never_revived(store):
    wid = store.add_watch("42", "romarr-sound-abc", "Chrono Trigger OST")
    store.cancel_watch("42")
    assert store._set_watch(wid, state="importing", message="Download finished") is False
    assert store.watch_for("42")["state"] == "cancelled"


def test_a_watch_cancelled_while_resolving_does_not_import(store, api, romm, monkeypatch):
    wid = store.add_watch("42", "romarr-sound-abc", "Chrono Trigger OST")
    imported = []
    monkeypatch.setattr(store, "_import_torrent", lambda *a, **k: imported.append(a) or "added 1")

    def resolve(key):
        store.cancel_watch("42")                       # the user stops watching meanwhile
        return {"id": "42", "name": "Chrono Trigger"}, api

    rows = [{"tags": "romarr-sound-abc", "progress": 1.0, "state": "uploading", "content_path": "/x"}]
    store.process_watches(rows, resolve, lambda path: path)
    assert imported == [] and store.watch_for("42")["state"] == "cancelled"
    assert store.watch_for("42")["id"] == wid


def test_an_interrupted_merge_becomes_restorable_after_a_restart(tmp_path, api, romm):
    db = tmp_path / "romarr-soundtracks.db"
    first = st.Soundtracks(db)
    first._add_merge("11", "10", "B", "A", {"01.mp3": "x"})        # stopped before _finish_merge
    restarted = st.Soundtracks(db)
    assert [(m["drop_id"], m["state"]) for m in restarted.dups()["merged"]] == [("11", "partial")]


def test_undo_holds_the_game_until_it_finishes(store, api, romm, monkeypatch):
    romm.seed(42, {})
    path = st.Path(store.tmp_dir) / "01.mp3"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"track")
    api.upload("42", "01.mp3", str(path), 5)
    store._journal_sent("42", ["01.mp3"], api)
    blocked = []
    real_delete = api.delete_track

    def delete_and_try_to_start_a_merge(rom_id, file_id):
        with pytest.raises(st.JobBusy):
            store._begin_job(["42"], "merge", "merge")
        blocked.append(file_id)
        return real_delete(rom_id, file_id)

    monkeypatch.setattr(api, "delete_track", delete_and_try_to_start_a_merge)
    assert store.undo({"id": "42", "name": "Chrono"}, api)["removed"] == 1
    assert blocked and store.jobs()[0]["kind"] == "undo" and store.jobs()[0]["state"] == "done"
    store._begin_job(["42"], "merge", "merge")         # released afterwards


def test_an_upload_that_could_not_be_verified_is_still_undoable(store, api, romm, monkeypatch):
    romm.seed(42, {})
    path = st.Path(store.tmp_dir) / "01.mp3"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"track")
    api.upload("42", "01.mp3", str(path), 5)
    real_tracks = api.tracks
    monkeypatch.setattr(api, "tracks", lambda key: (_ for _ in ()).throw(st.RommError(503, "busy")))
    with pytest.raises(st.RommError):
        store._journal_sent("42", ["01.mp3"], api)
    assert store._journal_count("42") == 0
    monkeypatch.setattr(api, "tracks", real_tracks)
    fresh = st.RommApi(romm.url, lambda refresh=False: TOKEN)        # receipts in memory are gone
    assert store.undo({"id": "42", "name": "Chrono"}, fresh)["removed"] == 1
    assert romm.tracks[42] == {}


def test_unmerge_refuses_to_report_success_over_a_different_existing_track(store, api, romm):
    romm.seed(10, KEEP, "A")
    romm.seed(11, KEEP, "B")
    store.start_merge(A, [B], api, run_async=False)
    romm.seed(11, {"01.mp3": b"someone else's"}, "B")
    store.start_unmerge(B, api, run_async=False)
    job = store.jobs()[0]
    assert job["state"] == "error" and "differ" in job["message"]
    assert [m["state"] for m in store.dups()["merged"]] == ["merged"]
    assert romm.blobs[(11, next(iter(romm.tracks[11])))] == b"someone else's"


def test_unmerge_staging_stops_at_the_transfer_limit(store, api, romm, monkeypatch):
    romm.seed(10, KEEP, "A")
    romm.seed(11, KEEP, "B")
    store.start_merge(A, [B], api, run_async=False)
    monkeypatch.setattr(st, "MAX_ARCHIVE_BYTES", 2)
    store.start_unmerge(B, api, run_async=False)
    job = store.jobs()[0]
    assert job["state"] == "error" and "size limit" in job["message"]


def _zip(tmp_path):
    archive = tmp_path / "ost.zip"
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr("01 Intro.mp3", b"intro")
    return [("01 Intro.mp3", str(archive), "01 Intro.mp3")]


def test_zip_upload_failing_at_start_releases_the_game(store, api, romm, tmp_path, monkeypatch):
    monkeypatch.setattr(api, "tracks", lambda key: (_ for _ in ()).throw(st.RommError(503, "busy")))
    with pytest.raises(st.RommError):
        store._import_zip_members({"id": "42", "name": "Chrono"}, api, _zip(tmp_path), "OST")
    job = store.jobs()[0]
    assert job["state"] == "error" and job["finished"]
    store._begin_job(["42"], "again", "torrent")


def test_zip_upload_applies_the_rom_name_guard(store, api, romm, tmp_path):
    romm.rom = {"fs_name": "Game v1.2.sfc", "has_multiple_files": False}
    with pytest.raises(ValueError, match="dotted number"):
        store._import_zip_members({"id": "42", "name": "Chrono"}, api, _zip(tmp_path), "OST")
    assert romm.uploads() == []


def test_native_script_album_names_are_searchable():
    albums = [{"id": "Koji Kondo/ゼルダの伝説"}, {"id": "Junichi Masuda/Pokémon Red"}]
    assert [a["id"] for a in st.search_albums(albums, "ゼルダ")] == ["Koji Kondo/ゼルダの伝説"]
    assert [a["id"] for a in st.search_albums(albums, "pokemon")] == ["Junichi Masuda/Pokémon Red"]


def test_a_failed_album_holds_the_game_until_its_uploads_are_journaled(store, api, romm, tmp_path, monkeypatch):
    romm.seed(42, {})
    album = tmp_path / "album"
    album.mkdir()
    (album / "01.mp3").write_bytes(b"one")
    files = st.AudioFiles([("01.mp3", str(album / "01.mp3"), 3), ("02.mp3", str(album / "missing.mp3"), 3)])
    seen = []
    real = store._journal_sent

    def journal(key, names, api_):
        seen.append(store.jobs()[0]["state"])
        return real(key, names, api_)

    monkeypatch.setattr(store, "_journal_sent", journal)
    job = store._begin_job(["42"], "Chrono", "album")
    store._run_album(job, {"id": "42", "name": "Chrono"}, api, "Artist/Album", files)
    assert seen == ["running"] and job["state"] == "error"
    assert store._journal_count("42") == 1


def test_undo_reports_uploads_it_still_cannot_verify(store, api, romm, monkeypatch):
    romm.seed(42, {})
    path = st.Path(store.tmp_dir) / "01.mp3"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"track")
    api.upload("42", "01.mp3", str(path), 5)
    monkeypatch.setattr(api, "tracks", lambda key: (_ for _ in ()).throw(st.RommError(503, "busy")))
    with pytest.raises(st.RommError):
        store._journal_sent("42", ["01.mp3"], api)
    out = store.undo({"id": "42", "name": "Chrono"}, api)
    assert out == {"removed": 0, "remaining": 1, "unverified": 1}
    assert store.status({"id": "42"}, None)["state"] != "unsearched"


def test_unmerge_that_cannot_make_its_work_folder_releases_both_games(store, api, romm, monkeypatch):
    romm.seed(10, KEEP, "A")
    romm.seed(11, KEEP, "B")
    store.start_merge(A, [B], api, run_async=False)
    monkeypatch.setattr(st.tempfile, "mkdtemp", lambda **kw: (_ for _ in ()).throw(OSError("disk full")))
    store.start_unmerge(B, api, run_async=False)
    job = store.jobs()[0]
    assert job["state"] == "error" and job["finished"]
    store._begin_job(["10", "11"], "again", "merge")
