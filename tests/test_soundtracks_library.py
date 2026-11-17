"""Soundtracks phase 3: albums from a read-only music library, uploaded through RomM.

The "library" is a temp directory; RomM is the loopback fake.
"""

from __future__ import annotations

import os

import pytest

from romarr import soundtracks as st
from soundtracks_support import TOKEN, FakeRomm

GAME = {"id": "42", "name": "Chrono Trigger"}


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
    return st.Soundtracks(tmp_path / "data" / "romarr-soundtracks.db")


def put(path, data=b"x"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)


@pytest.fixture
def lib(tmp_path):
    root = tmp_path / "music"
    put(str(root / "Yasunori Mitsuda" / "Chrono Cross" / "01 Scars.flac"), b"scars")
    put(str(root / "Yasunori Mitsuda" / "Chrono Cross" / "02 Time.mp3"), b"time")
    put(str(root / "Yasunori Mitsuda" / "Chrono Cross" / "cover.jpg"), b"img")
    put(str(root / "Yasunori Mitsuda" / "Chrono Cross" / "rip.log"), b"log")
    put(str(root / "Yasunori Mitsuda" / "Chrono Cross" / "album.cue"), b"cue")
    put(str(root / "Yasunori Mitsuda" / "Chrono Cross" / ".hidden.mp3"), b"h")
    put(str(root / "Fan" / "Chrono Trigger Reimagined" / "CD1" / "01 Intro.mp3"), b"i1")
    put(str(root / "Fan" / "Chrono Trigger Reimagined" / "CD2" / "01 Intro.mp3"), b"i2")
    put(str(root / "Fan" / "Chrono Trigger Reimagined" / "CD2" / "02 End.ogg"), b"e2")
    put(str(root / "Other" / "Zelda" / "01 Hyrule.mp3"), b"z")
    put(str(root / "loose.mp3"), b"loose")
    return root


def tree(root):
    out = {}
    for dp, _d, fns in os.walk(root):
        for fn in fns:
            p = os.path.join(dp, fn)
            out[p] = (os.path.getsize(p), os.stat(p).st_mtime_ns, open(p, "rb").read())
    return out


# --- finding albums ---------------------------------------------------------

def test_albums_are_folders_with_audio_and_disc_folders_roll_up(lib):
    ids = {a["id"]: a for a in st.scan_albums(str(lib))}
    assert set(ids) == {"Yasunori Mitsuda/Chrono Cross", "Fan/Chrono Trigger Reimagined", "Other/Zelda"}   # root's loose.mp3 is not an album
    assert ids["Yasunori Mitsuda/Chrono Cross"]["tracks"] == 2            # cover, log, cue and the dotfile do not count
    assert ids["Fan/Chrono Trigger Reimagined"]["tracks"] == 3            # CD1 + CD2


def test_search_needs_every_word_and_prefers_the_album_folder_name(lib):
    albums = st.scan_albums(str(lib))
    assert [a["id"] for a in st.search_albums(albums, "chrono cross")] == ["Yasunori Mitsuda/Chrono Cross"]
    hits = [a["id"] for a in st.search_albums(albums, "mitsuda chrono")]
    assert hits == ["Yasunori Mitsuda/Chrono Cross"]
    assert st.search_albums(albums, "   ") == [] and st.search_albums(albums, "nothing here") == []
    assert st.search_albums(albums, "zelda")[0]["name"] == "Zelda"


def test_a_symlinked_folder_is_not_followed_out_of_the_library(lib, tmp_path):
    outside = tmp_path / "secret"
    put(str(outside / "Private" / "01 Song.mp3"))
    os.symlink(outside, lib / "link")
    assert not any("Private" in a["id"] for a in st.scan_albums(str(lib)))


# --- path safety ------------------------------------------------------------

@pytest.mark.parametrize("bad", ["../secret/Private", "Fan/../../secret", "/etc", "..", ".", "", "Fan/Missing", "Other/Zelda/../../..",
                                 "loose.mp3", "bad\x00name"])
def test_an_album_id_cannot_leave_the_library(lib, tmp_path, bad):
    put(str(tmp_path / "secret" / "Private" / "01 Song.mp3"))
    with pytest.raises(ValueError):
        st.resolve_album(str(lib), bad)


def test_a_symlinked_album_pointing_outside_is_refused(lib, tmp_path):
    put(str(tmp_path / "secret" / "Private" / "01 Song.mp3"))
    os.symlink(tmp_path / "secret" / "Private", lib / "Sneaky")
    with pytest.raises(ValueError):
        st.resolve_album(str(lib), "Sneaky")


def test_a_non_string_album_id_is_refused(lib):
    for bad in (None, 5, ["Other/Zelda"], {"a": 1}):
        with pytest.raises(ValueError):
            st.resolve_album(str(lib), bad)


def test_a_symlinked_file_leading_outside_the_album_is_not_collected(lib, tmp_path):
    put(str(tmp_path / "secret.mp3"), b"private")
    os.symlink(tmp_path / "secret.mp3", lib / "Other" / "Zelda" / "02 Stolen.mp3")
    names = [n for n, _p, _s in st.collect_album(st.resolve_album(str(lib), "Other/Zelda"))]
    assert names == ["01 Hyrule.mp3"]


# --- what is collected ------------------------------------------------------

def test_only_jukebox_audio_is_collected_and_disc_folders_are_folded_into_names(lib):
    got = st.collect_album(st.resolve_album(str(lib), "Fan/Chrono Trigger Reimagined"))
    assert [n for n, _p, _s in got] == ["CD1 - 01 Intro.mp3", "CD2 - 01 Intro.mp3", "CD2 - 02 End.ogg"]
    flat = st.collect_album(st.resolve_album(str(lib), "Yasunori Mitsuda/Chrono Cross"))
    assert [n for n, _p, _s in flat] == ["01 Scars.flac", "02 Time.mp3"]


@pytest.mark.parametrize("ext,ok", [(".mp3", 1), (".ogg", 1), (".opus", 1), (".m4a", 1), (".aac", 1), (".wav", 1), (".flac", 1),
                                    (".spc", 1), (".vgm", 1), (".vgz", 1), (".gym", 1), (".FLAC", 1),
                                    (".nsf", 0), (".jpg", 0), (".cue", 0), (".log", 0), (".txt", 0), (".wma", 0), (".mid", 0)])
def test_extension_filter_matches_what_the_jukebox_accepts(tmp_path, ext, ok):
    put(str(tmp_path / "lib" / "Album" / f"01 Track{ext}"))
    assert len(st.collect_album(st.resolve_album(str(tmp_path / "lib"), "Album"))) == ok


def test_names_are_made_safe_for_romm(tmp_path):
    put(str(tmp_path / "lib" / "A" / "Sub:Dir" / "Café?.mp3"))
    name = st.collect_album(st.resolve_album(str(tmp_path / "lib"), "A"))[0][0]
    assert "/" not in name and ":" not in name and "?" not in name


# --- the job ----------------------------------------------------------------

def test_the_album_is_uploaded_through_romm_with_the_expected_wire_shape(store, api, romm, lib):
    before = tree(lib)
    job = store.start_album(GAME, api, str(lib), "Fan/Chrono Trigger Reimagined", run_async=False)
    done = store.jobs()[0]
    assert job["kind"] == "album" and done["state"] == "done" and done["made"] == 3
    assert sorted(romm.tracks[42].values()) == ["CD1 - 01 Intro.mp3", "CD2 - 01 Intro.mp3", "CD2 - 02 End.ogg"]
    ups = romm.uploads()
    assert len(ups) == 3
    method, path, headers, body = ups[0]
    assert path == "/api/roms/42/soundtracks" and headers["x-upload-filename"] == "CD1 - 01 Intro.mp3"
    assert headers["content-type"].startswith("multipart/form-data; boundary=")
    assert b'name="CD1 - 01 Intro.mp3"' in body and b"i1" in body
    assert headers["authorization"] == "Bearer " + TOKEN
    assert sorted(romm.blobs.values()) == [b"e2", b"i1", b"i2"]
    assert tree(lib) == before                                          # nothing in the library changed


def test_existing_tracks_are_skipped_and_romm_refusals_do_not_stop_the_rest(store, api, romm, lib):
    romm.seed(42, {"01 Scars.flac": b"mine"})
    romm.refuse = {"02 Time.mp3"}
    store.start_album(GAME, api, str(lib), "Yasunori Mitsuda/Chrono Cross", run_async=False)
    job = store.jobs()[0]
    assert job["state"] == "done" and job["made"] == 0 and job["skipped"] == 2
    assert list(romm.tracks[42].values()) == ["01 Scars.flac"]


def test_undo_removes_only_the_album_files_that_were_added(store, api, romm, lib):
    romm.seed(42, {"Mine.mp3": b"mine"})
    store.start_album(GAME, api, str(lib), "Other/Zelda", run_async=False)
    assert sorted(romm.tracks[42].values()) == ["01 Hyrule.mp3", "Mine.mp3"]
    assert store.status(GAME, api)["undoable"] == 1
    assert store.undo(GAME, api) == {"removed": 1, "remaining": 0}
    assert list(romm.tracks[42].values()) == ["Mine.mp3"]
    assert os.path.exists(lib / "Other" / "Zelda" / "01 Hyrule.mp3")


def test_a_failure_part_way_still_journals_what_went_up(store, api, romm, lib, monkeypatch):
    real = api.upload
    calls = []

    def flaky(rom, name, path, size):
        calls.append(name)
        if len(calls) == 2:
            raise st.RommError(500, "boom Authorization: Bearer " + TOKEN)
        return real(rom, name, path, size)
    monkeypatch.setattr(api, "upload", flaky)
    store.start_album(GAME, api, str(lib), "Yasunori Mitsuda/Chrono Cross", run_async=False)
    job = store.jobs()[0]
    assert job["state"] == "error" and TOKEN not in job["message"]
    assert store.status(GAME, api)["undoable"] == 1


def test_a_rom_whose_name_would_change_is_left_alone(store, api, romm, lib):
    romm.rom = {"fs_name": "Game v1.2.sfc", "has_multiple_files": False}
    store.start_album(GAME, api, str(lib), "Other/Zelda", run_async=False)
    assert store.jobs()[0]["state"] == "error" and not romm.uploads()


def test_a_missing_or_empty_library_is_a_plain_error(store, api, tmp_path, lib):
    with pytest.raises(ValueError):
        store.start_album(GAME, api, str(tmp_path / "nope"), "x", run_async=False)
    with pytest.raises(ValueError):
        store.start_album(GAME, api, "", "x", run_async=False)
    put(str(tmp_path / "lib2" / "Art" / "cover.jpg"))
    with pytest.raises(ValueError, match="no audio"):
        store.start_album(GAME, api, str(tmp_path / "lib2"), "Art", run_async=False)
    with pytest.raises(ValueError):
        store.library_search(str(tmp_path / "nope"), "x")


def test_one_job_per_game_applies_to_album_uploads(store, api, romm, lib):
    store._begin_job(["42"], "x", "download")
    with pytest.raises(st.JobBusy):
        store.start_album(GAME, api, str(lib), "Other/Zelda", run_async=False)


def test_library_search_is_cached_and_reports_albums(store, lib):
    out = store.library_search(str(lib), "zelda")
    assert [a["id"] for a in out["albums"]] == ["Other/Zelda"]
    put(str(lib / "New" / "Zelda Two" / "01.mp3"))
    assert [a["id"] for a in store.library_search(str(lib), "zelda")["albums"]] == ["Other/Zelda"]      # within the cache window
    store._albums = None
    assert len(store.library_search(str(lib), "zelda")["albums"]) == 2


def test_browse_lists_one_level_and_refuses_to_leave_the_library(lib, tmp_path):
    top = st.browse_library(str(lib))
    assert top["path"] == "" and {d["name"] for d in top["dirs"]} >= {"Other", "Fan"}
    sub = st.browse_library(str(lib), "Other")
    assert sub["path"] == "Other" and any(d["name"] == "Zelda" and d["tracks"] > 0 for d in sub["dirs"])
    for bad in ("..", "../x", "/etc", "bad\x00", "Other/Missing"):
        with pytest.raises(ValueError):
            st.browse_library(str(lib), bad)
    os.symlink(tmp_path, lib / "escape")
    assert "escape" not in {d["name"] for d in st.browse_library(str(lib))["dirs"]}
