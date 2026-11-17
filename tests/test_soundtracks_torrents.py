"""Watched soundtrack torrents: wait for the download, pick one audio format, upload to RomM, journal it."""

from __future__ import annotations

import os

import pytest

from romarr import soundtracks as st
from soundtracks_support import TOKEN, FakeRomm, make_zip

GAME = {"id": "42", "name": "Chrono Trigger", "platform_slug": "snes", "platform_name": "SNES"}


@pytest.fixture
def romm():
    r = FakeRomm()
    r.rom = {"fs_name": "Chrono Trigger", "has_multiple_files": True}
    r.tracks[42] = {}
    yield r
    r.close()


@pytest.fixture
def api(romm):
    return st.RommApi(romm.url, lambda refresh=False: TOKEN)


@pytest.fixture
def store(tmp_path):
    return st.Soundtracks(tmp_path / "s.db")


def put(path, data=b"x"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)


def row(tag, progress, path="", state="downloading"):
    return {"tags": f"foo, {tag}", "progress": progress, "content_path": path, "state": state, "name": "CT OST"}


def run(store, api, rows):
    return store.process_watches(rows, lambda k: (GAME, api), lambda p: p)


def test_one_format_is_kept_and_the_upload_is_journaled(store, api, romm, tmp_path):
    root = tmp_path / "CT OST"
    put(str(root / "MP3" / "01 Opening.mp3"))
    put(str(root / "FLAC" / "01 Opening.flac"))
    put(str(root / "cover.jpg"))
    wid = store.add_watch("42", "romarr-abc", "Chrono Trigger OST")
    run(store, api, [row("romarr-abc", 0.4)])
    w = store.watch_for("42")
    assert w["state"] == "downloading" and w["pct"] == pytest.approx(0.4)
    assert not romm.tracks[42]

    run(store, api, [row("romarr-abc", 1.0, str(root))])
    w = store.watch_for("42")
    assert w["state"] == "added", w["message"]
    names = sorted(romm.tracks[42].values())
    assert names == ["MP3 - 01 Opening.mp3"]
    assert store._journal_count("42") == 1
    assert store.active_watches() == [] and wid


def test_nothing_playable_fails_with_a_reason(store, api, tmp_path):
    root = tmp_path / "x"
    put(str(root / "notes.txt"))
    put(str(root / "album.rar"))
    store.add_watch("42", "romarr-abc", "Thing")
    run(store, api, [row("romarr-abc", 1.0, str(root))])
    w = store.watch_for("42")
    assert w["state"] == "failed" and "archives" in w["message"]


def test_a_zip_in_the_torrent_is_opened(store, api, romm, tmp_path):
    root = tmp_path / "x"
    os.makedirs(root)
    (root / "ost.zip").write_bytes(make_zip({"01 A.mp3": b"a", "02 B.mp3": b"b"}))
    store.add_watch("42", "romarr-abc", "Thing")
    run(store, api, [row("romarr-abc", 1.0, str(root))])
    assert store.watch_for("42")["state"] == "added"
    assert {"01 A.mp3", "02 B.mp3"} <= set(romm.tracks[42].values())


def test_a_torrent_that_vanishes_is_given_up_on_and_cancel_stops_watching(store, api, monkeypatch):
    store.add_watch("42", "romarr-abc", "Thing")
    run(store, api, [])
    assert store.watch_for("42")["state"] == "downloading"
    monkeypatch.setattr(st, "WATCH_LOST_AFTER", -1)
    run(store, api, [])
    assert store.watch_for("42")["state"] == "lost"
    store.add_watch("42", "romarr-def", "Other")
    store.cancel_watch("42")
    assert store.active_watches() == []


def test_choose_audio_prefers_mp3_then_flac_then_chiptune():
    f = lambda n: (n, "/p/" + n, 1)
    assert st.choose_audio([f("a.flac"), f("a.mp3")])[1] == ".mp3"
    assert st.choose_audio([f("a.flac"), f("b.spc")])[1] == ".flac"
    assert st.choose_audio([f("b.spc")])[1] == ".spc"
    assert st.choose_audio([]) == ([], "")


def test_zip_members_and_loose_files_share_one_format_and_discs_do_not_collide(store, api, romm, tmp_path):
    root = tmp_path / "x"
    put(str(root / "bonus.flac"))
    os.makedirs(root, exist_ok=True)
    (root / "ost.zip").write_bytes(make_zip({"CD1/01 A.mp3": b"a", "CD2/01 A.mp3": b"b", "CD1/01 A.flac": b"f", "cover.jpg": b"j"}))
    store.add_watch("42", "romarr-abc", "Thing")
    run(store, api, [row("romarr-abc", 1.0, str(root))])
    w = store.watch_for("42")
    assert w["state"] == "added", w["message"]
    assert sorted(romm.tracks[42].values()) == ["CD1 - 01 A.mp3", "CD2 - 01 A.mp3"]
    assert store._journal_count("42") == 2
