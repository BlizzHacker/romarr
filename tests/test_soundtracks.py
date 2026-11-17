"""Soundtracks: titles, source parsing, archive safety, upload shape, journal and undo, jobs.

No network: source pages are hand-written fixtures served through a patched
fetch, and RomM is a loopback fake (see soundtracks_support).
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time

import pytest
import requests

from romarr import soundtracks as st
from soundtracks_support import (ARCHIVE_META, ARCHIVE_SEARCH, TOKEN, ZOPHAR_PAGE,
                                 ZOPHAR_SEARCH, FakeRomm, make_zip)


@pytest.fixture
def romm():
    r = FakeRomm()
    yield r
    r.close()


@pytest.fixture
def api(romm):
    return st.RommApi(romm.url, lambda refresh=False: TOKEN)


@pytest.fixture
def store(tmp_path):
    return st.Soundtracks(tmp_path / "romarr-soundtracks.db")


GAME = {"id": "42", "name": "Chrono Trigger", "platform_slug": "snes", "platform_name": "Super Nintendo"}


# --- titles -----------------------------------------------------------------

def test_clean_title_drops_tags_extension_and_moves_the_article():
    assert st.clean_title("Legend of Zelda, The (USA) [!].sfc") == "The Legend of Zelda"
    assert st.clean_title("Super_Mario_World.smc") == "Super Mario World"
    assert st.clean_title("Half-Life 2.0", is_folder=True) == "Half-Life 2.0"


def test_norm_folds_accents_ampersands_and_version_words():
    assert st.norm("Pokémon LeafGreen Version") == "pokemon leafgreen"
    assert st.norm("The Legend & Co") == "legend and co"


def test_similarity_ranks_the_same_title_above_a_different_one():
    assert st.similarity("Chrono Trigger", "chrono trigger") == 1.0
    assert st.similarity("Chrono Trigger", "Chrono Cross") < 0.9
    assert st.similarity("", "x") == 0.0


def test_best_for_auto_needs_same_platform_and_a_near_exact_title():
    cands = [{"platform_match": False, "score": 1.0}, {"platform_match": True, "score": 0.8},
             {"platform_match": True, "score": 0.95}]
    assert st.best_for_auto(cands, 0.92) is cands[2]
    assert st.best_for_auto(cands[:2], 0.92) is None


def test_stem_stable_matches_romms_extension_rule():
    assert st.stem_stable("Chrono Trigger.sfc")
    assert not st.stem_stable("Game v1.2.sfc")


def test_manual_links_cover_every_site_and_quote_the_title():
    links = st.manual_links("Sonic & Knuckles", "Genesis")
    assert [x["name"] for x in links] == ["KHInsider", "VGMRips", "Zophar's Domain", "Internet Archive",
                                          "MusicBrainz", "Bandcamp", "YouTube"]
    assert all("Sonic+%26+Knuckles" in x["url"] for x in links[:3])


# --- source parsing ---------------------------------------------------------

@pytest.fixture
def pages(monkeypatch):
    seen: list[str] = []

    def fake(url, binary=False, gap=1.0):
        seen.append(url)
        if "zophar.net/music/search" in url:
            return ZOPHAR_SEARCH
        if "zophar.net/music/snes-spc/chrono-trigger" in url:
            return ZOPHAR_PAGE
        if "advancedsearch" in url:
            return ARCHIVE_SEARCH
        if "archive.org/metadata/" in url:
            return ARCHIVE_META
        raise AssertionError("unexpected fetch " + url)
    monkeypatch.setattr(st, "polite_get", fake)
    return seen


def test_zophar_search_parses_rows_dedupes_and_flags_the_platform(pages):
    out = st.zophar_search("Chrono Trigger", "snes")
    ids = [x["id"] for x in out]
    assert ids == ["/music/snes-spc/chrono-trigger", "/music/gameboy-gbs/chrono-trigger-hack", "/music/snes-spc/zelda"]
    first = out[0]
    assert first["platform_match"] and first["jukebox_original"] and first["year"] == "1995"
    assert first["score"] == 1.0 and first["url"] == "https://www.zophar.net/music/snes-spc/chrono-trigger"
    assert out[1]["title"] == "Chrono Trigger & Friends" and not out[1]["platform_match"]
    assert len(pages) == 1                      # the first query hit, so no fallback queries


def test_zophar_plan_resolves_relative_links_and_classifies_kinds(pages):
    plan = st.zophar_plan("/music/snes-spc/chrono-trigger")
    assert plan["tracks"] == 3
    assert [d["kind"] for d in plan["downloads"]] == ["mp3", "original"]
    assert plan["downloads"][1]["url"] == "https://www.zophar.net/soundfiles/snes-spc/chrono-trigger/Chrono%20Trigger%20(spc).zip"


def test_zophar_plan_refuses_anything_that_is_not_a_music_page(pages):
    for bad in ("http://evil.example/x", "/music/../../etc", "//evil.example/music/a"):
        with pytest.raises(ValueError):
            st.zophar_plan(bad)


def test_archive_search_and_plan(pages):
    out = st.archive_search("Chrono Trigger", "snes")
    assert out[0]["id"] == "chrono-trigger-ost" and out[0]["score"] > 0.45
    assert out[0]["size"] == 123456 and not out[0]["platform_match"]
    plan = st.archive_plan("chrono-trigger-ost")
    files = plan["downloads"][0]["files"]
    # originals with a Jukebox extension only: not the derivative, the cover or the notes
    assert [f["name"] for f in files] == ["01 Prologue.mp3", "02 Theme.flac"]
    assert files[0]["url"] == "https://archive.org/download/chrono-trigger-ost/disc1/01%20Prologue.mp3"
    with pytest.raises(ValueError):
        st.archive_plan("../etc/passwd")


def test_search_ranks_platform_match_first_drops_weak_scores_and_survives_a_dead_source(pages, store, monkeypatch):
    def dead(title, platform):
        raise requests.ConnectionError("no route")
    monkeypatch.setitem(store.providers, "archive", (dead, st.archive_plan))
    out = store.search(GAME)
    titles = [c["title"] for c in out["candidates"]]
    assert titles[0] == "Chrono Trigger" and "Zelda" not in titles          # Zelda scores under 0.45
    assert out["candidates"][0]["formats"] == ["mp3", "original"]
    assert out["errors"] == ["archive: ConnectionError"]
    assert len(out["links"]) == 7
    # the candidates are remembered, status reads "found"
    assert store.status(GAME, None)["state"] == "found"


# --- polite http ------------------------------------------------------------

def test_one_request_per_second_per_host(monkeypatch):
    clock = [1000.0]
    slept: list[float] = []
    monkeypatch.setattr(st.time, "time", lambda: clock[0])
    monkeypatch.setattr(st.time, "sleep", lambda s: (slept.append(s), clock.__setitem__(0, clock[0] + s)))
    st._last.clear()
    st._wait_turn("a.example")
    st._wait_turn("a.example")
    st._wait_turn("b.example")                      # another host does not wait
    st._wait_turn("a.example")
    assert slept == [pytest.approx(1.0), pytest.approx(1.0)]


def test_user_agent_names_romarr_and_stream_refuses_foreign_hosts(monkeypatch):
    assert st.UA.startswith("ROMarr")
    with pytest.raises(ValueError):
        list(st.stream("http://127.0.0.1:1/x.zip"))
    with pytest.raises(ValueError):
        list(st.stream("https://notzophar.net.evil.example/x.zip"))
    assert st.allowed_url("https://fi.zophar.net/a.zip") and st.allowed_url("https://ia800.us.archive.org/b")
    assert not st.allowed_url("file:///etc/passwd")


# --- archive extraction safety ----------------------------------------------

def zip_path(tmp_path, members):
    p = tmp_path / "a.zip"
    p.write_bytes(make_zip(members))
    return str(p)


def test_zip_members_are_flattened_filtered_and_never_written_to_disk(tmp_path):
    before = set(os.listdir(tmp_path))
    p = zip_path(tmp_path, {
        "../../etc/evil.mp3": b"1", "disc 1/01 a.MP3": b"22", "..\\win\\02 b.flac": b"333",
        "cover.jpg": b"x", "readme.txt": b"x", "run.exe": b"x", ".hidden.mp3": b"x", "tune.nsf": b"4444",
        "dir/": b""})
    got = {}
    for name, chunks in st.iter_zip_audio(p):          # lazily: each member is read before the next is asked for
        got[name] = b"".join(chunks)
    assert got == {"evil.mp3": b"1", "01 a.MP3": b"22", "02 b.flac": b"333", "tune.nsf": b"4444"}
    assert set(os.listdir(tmp_path)) == before | {"a.zip"}        # nothing extracted anywhere


def test_an_archive_over_the_limit_is_refused_before_anything_is_read(tmp_path):
    p = zip_path(tmp_path, {"a.mp3": b"x" * 100, "b.mp3": b"y" * 100})
    with pytest.raises(ValueError, match="too large"):
        list(st.iter_zip_audio(p, limit=150))
    assert st.MAX_ARCHIVE_BYTES == 6 << 30


def test_do_download_unpacks_through_the_sink_and_cleans_its_temp_file(tmp_path, monkeypatch):
    blob = make_zip({"x/01.mp3": b"aa", "x/02.spc": b"bb", "x/info.txt": b"t", "x/03.nsf": b"cc"})
    monkeypatch.setattr(st, "stream", lambda url, on_total=None: (on_total and on_total(len(blob)), iter([blob[:50], blob[50:]]))[1])
    taken = {}
    steps = []

    def sink(name, chunks):
        taken[name] = b"".join(chunks)
        return len(taken[name]) if name != "03.nsf" else None         # the sink may decline a file

    made, skipped = st.do_download({"kind": "mp3", "url": "https://www.zophar.net/a.zip"}, sink,
                                   str(tmp_path / "tmp"), lambda step, pct=None: steps.append(step))
    assert taken == {"01.mp3": b"aa", "02.spc": b"bb", "03.nsf": b"cc"}
    assert (made, skipped) == (2, 1)
    assert os.listdir(tmp_path / "tmp") == []
    assert steps[0] == "Downloading the soundtrack"


def test_a_download_larger_than_the_limit_is_aborted_and_removed(tmp_path, monkeypatch):
    monkeypatch.setattr(st, "stream", lambda url, on_total=None: iter([b"x" * 60, b"x" * 60]))
    with pytest.raises(ValueError, match="too large"):
        st.do_download({"kind": "mp3", "url": "https://www.zophar.net/a.zip"}, lambda n, c: 1,
                       str(tmp_path / "tmp"), lambda *a, **k: None, limit=100)
    assert os.listdir(tmp_path / "tmp") == []


def test_a_download_link_off_the_supported_hosts_is_never_fetched(tmp_path):
    with pytest.raises(ValueError):
        st.do_download({"kind": "mp3", "url": "http://169.254.169.254/latest"}, lambda n, c: 1,
                       str(tmp_path), lambda *a, **k: None)


# --- upload shape -----------------------------------------------------------

def test_upload_is_multipart_named_after_the_file_with_the_filename_header(api, romm, tmp_path):
    f = tmp_path / "t.mp3"
    f.write_bytes(b"ID3-bytes")
    api.upload("42", "01 Prologue.mp3", str(f), 9)
    method, path, headers, body = romm.uploads()[0]
    assert path == "/api/roms/42/soundtracks"
    assert headers["x-upload-filename"] == "01 Prologue.mp3"
    assert headers["authorization"] == "Bearer " + TOKEN
    assert headers["user-agent"].startswith("ROMarr")
    boundary = headers["content-type"].split("boundary=")[1]
    assert headers["content-type"].startswith("multipart/form-data; boundary=----romarr")
    assert int(headers["content-length"]) == len(body) and "transfer-encoding" not in headers
    assert body.startswith(f'--{boundary}\r\nContent-Disposition: form-data; name="01 Prologue.mp3"; filename="01 Prologue.mp3"\r\n'.encode())
    assert body.endswith(b"ID3-bytes\r\n--" + boundary.encode() + b"--\r\n")
    assert romm.tracks[42][1001] == "01 Prologue.mp3"


def test_a_401_is_retried_once_with_a_fresh_token(romm, tmp_path):
    calls = []

    def token(refresh=False):
        calls.append(refresh)
        return "stale" if not refresh else TOKEN
    f = tmp_path / "t.mp3"
    f.write_bytes(b"abc")
    st.RommApi(romm.url, token).upload("42", "a.mp3", str(f), 3)
    assert [r[2]["authorization"] for r in romm.uploads()] == ["Bearer stale", "Bearer " + TOKEN]
    assert romm.uploads()[1][3].count(b"abc") == 1          # the retry sent a whole body again


def test_non_ascii_names_are_made_header_safe():
    assert st.header_safe("Café.mp3") == "Café.mp3"                      # latin-1 survives
    assert st.header_safe("曲 Theme.mp3") == "Theme.mp3"
    assert st.header_safe("曲.mp3").startswith("track-")


def test_sink_skips_existing_wrong_formats_and_files_romm_refuses(api, romm, tmp_path):
    romm.refuse = {"bad.mp3"}
    sink = st.RommSink(api, "42", str(tmp_path / "tmp"))
    assert sink("Mine Already.mp3", iter([b"x"])) is None                # never overwrite
    assert sink("tune.nsf", iter([b"x"])) is None                         # RomM would refuse it
    assert sink("../../escape.mp3", iter([b"1", b"2"])) == 2
    assert sink("bad.mp3", iter([b"x"])) is None                          # 409: carry on
    assert sink("fine.ogg", iter([b"z"])) == 1
    assert sink.sent == ["escape.mp3", "fine.ogg"] and sink.refused == ["bad.mp3"]
    assert [r[2]["x-upload-filename"] for r in romm.uploads()] == ["escape.mp3", "bad.mp3", "fine.ogg"]
    assert os.listdir(tmp_path / "tmp") == []


# --- journal, undo, jobs ----------------------------------------------------

def fake_provider(plan_gate=None, files=("01 A.mp3", "02 B.mp3", "03 C.nsf")):
    def search(title, platform):
        return [{"provider": "fake", "id": "x", "title": title, "console": "", "year": "", "url": "",
                 "platform_match": True, "score": 1.0}]

    def plan(cid):
        if plan_gate:
            plan_gate.wait(5)
        return {"downloads": [{"kind": "files", "label": "x", "files": [
            {"url": "https://archive.org/download/x/" + n, "name": n} for n in files]}], "tracks": len(files)}
    return {"fake": (search, plan)}


@pytest.fixture
def fake_stream(monkeypatch):
    monkeypatch.setattr(st, "stream", lambda url, on_total=None: iter([url.encode()]))


def run(store, api, fmt="files", game=GAME):
    return store.start_download(game, api, {"provider": "fake", "id": "x"}, fmt, run_async=False)


def test_download_uploads_new_audio_journals_it_and_leaves_preexisting_tracks(tmp_path, api, romm, fake_stream):
    s = st.Soundtracks(tmp_path / "s.db", providers=fake_provider())
    job = run(s, api)
    assert s.jobs()[0]["state"] == "done" and (s.jobs()[0]["made"], s.jobs()[0]["skipped"]) == (2, 1)
    assert sorted(romm.tracks[42].values()) == ["01 A.mp3", "02 B.mp3", "Mine Already.mp3"]
    status = s.status(GAME, api)
    assert status["track_count"] == 3 and status["undoable"] == 2 and status["state"] == "downloaded"
    assert job["game_id"] == "42"


def test_undo_removes_exactly_what_was_added_and_nothing_else(tmp_path, api, romm, fake_stream):
    s = st.Soundtracks(tmp_path / "s.db", providers=fake_provider())
    run(s, api)
    out = s.undo(GAME, api)
    assert out == {"removed": 2, "remaining": 0}
    assert list(romm.tracks[42].values()) == ["Mine Already.mp3"]         # the user's own track survives
    deletes = [r for r in romm.requests if r[0] == "DELETE"]
    assert sorted(r[1] for r in deletes) == ["/api/roms/42/soundtracks/1001", "/api/roms/42/soundtracks/1002"]
    assert s.undo(GAME, api) == {"removed": 0, "remaining": 0}            # a second undo does nothing
    assert s.status(GAME, api)["state"] == "unsearched"


def test_undo_with_no_journal_deletes_nothing(tmp_path, api, romm):
    s = st.Soundtracks(tmp_path / "s.db")
    assert s.undo(GAME, api)["removed"] == 0
    assert not [r for r in romm.requests if r[0] == "DELETE"]


def test_undo_ignores_journal_rows_for_another_game_and_tolerates_a_file_already_gone(tmp_path, api, romm, fake_stream):
    s = st.Soundtracks(tmp_path / "s.db", providers=fake_provider())
    run(s, api)
    del romm.tracks[42][1001]                                              # removed in RomM by hand
    c = s._db()
    c.execute("INSERT INTO journal(key,path,size,ts) VALUES('42','romm://7/900/Mine Already.mp3',0,0)")
    c.commit()
    c.close()
    out = s.undo(GAME, api)
    assert out["removed"] == 1                                             # only 1002 was still there
    assert 900 in romm.tracks[42]                                          # a row naming another rom is never acted on


def test_a_failed_delete_keeps_the_rest_journaled_for_a_retry(tmp_path, api, romm, fake_stream):
    s = st.Soundtracks(tmp_path / "s.db", providers=fake_provider())
    run(s, api)
    romm.fail_delete = {1002}
    with pytest.raises(st.RommError):
        s.undo(GAME, api)
    assert s.status(GAME, api)["undoable"] == 1
    romm.fail_delete = set()
    assert s.undo(GAME, api)["removed"] == 1


def test_a_download_that_fails_part_way_still_journals_what_it_uploaded(tmp_path, api, romm, monkeypatch):
    def flaky(url, on_total=None):
        if url.endswith("02 B.mp3"):
            raise requests.ConnectionError("dropped")
        return iter([b"x"])
    monkeypatch.setattr(st, "stream", flaky)
    s = st.Soundtracks(tmp_path / "s.db", providers=fake_provider())
    run(s, api)
    job = s.jobs()[0]
    assert job["state"] == "error" and job["message"] == "ConnectionError"
    assert s.status(GAME, api)["undoable"] == 1                           # 01 A.mp3 can still be undone
    assert s.undo(GAME, api)["removed"] == 1


def test_a_rom_whose_name_would_change_when_moved_into_a_folder_is_not_touched(tmp_path, api, romm, fake_stream):
    romm.rom = {"fs_name": "Game v1.2.sfc", "has_multiple_files": False}
    s = st.Soundtracks(tmp_path / "s.db", providers=fake_provider())
    run(s, api)
    assert s.jobs()[0]["state"] == "error" and "dotted number" in s.jobs()[0]["message"]
    assert not romm.uploads()


def test_one_job_per_game_at_a_time(tmp_path, api, romm, fake_stream):
    gate = threading.Event()
    s = st.Soundtracks(tmp_path / "s.db", providers=fake_provider(plan_gate=gate))
    first = s.start_download(GAME, api, {"provider": "fake", "id": "x"}, "files")
    assert first["state"] == "running"
    with pytest.raises(st.JobBusy):
        s.start_download(GAME, api, {"provider": "fake", "id": "x"}, "files")
    with pytest.raises(st.JobBusy):
        s.undo(GAME, api)                                                  # undo waits for the job too
    other = dict(GAME, id="43")
    romm.tracks[43] = {}
    s.start_download(other, api, {"provider": "fake", "id": "x"}, "files")  # another game is independent
    assert s.job_for("42")["state"] == "running"
    gate.set()
    for _ in range(100):
        if all(j["state"] != "running" and j.get("finished") for j in s.jobs()):
            break
        time.sleep(0.05)
    assert {j["game_id"]: j["state"] for j in s.jobs()} == {"42": "done", "43": "done"}
    s.start_download(GAME, api, {"provider": "fake", "id": "x"}, "files", run_async=False, force=True)   # free again


@pytest.mark.parametrize("cand,fmt", [
    ({"provider": "nope", "id": "x"}, "mp3"),
    ({"provider": "zophar", "id": "http://evil.example/"}, "mp3"),
    ({"provider": "archive", "id": "../x"}, "files"),
    ({"provider": "archive", "id": "ok"}, "exe"),
])
def test_download_requests_are_validated_before_a_job_exists(store, api, cand, fmt):
    with pytest.raises(ValueError):
        store.start_download(GAME, api, cand, fmt)
    assert store.jobs() == []


def test_pick_downloads_chooses_by_format():
    d = [{"kind": "mp3"}, {"kind": "original"}, {"kind": "flac"}, {"kind": "files"}]
    kinds = lambda fmt: [x["kind"] for x in st.pick_downloads(d, fmt)]       # noqa: E731
    assert kinds("mp3") == ["mp3", "files"] and kinds("original") == ["original", "files"]
    assert kinds("both") == ["mp3", "original", "files"] and kinds("flac") == ["flac", "files"]
    assert [x["kind"] for x in st.pick_downloads([{"kind": "flac"}], "mp3")] == ["flac"]
    assert st.pick_downloads([{"kind": "original"}], "mp3") == []


def test_the_database_lives_beside_the_data_file_with_the_original_table_names(tmp_path):
    p = st.db_path_for(str(tmp_path / "cfg" / "romarr.json"))
    assert p == tmp_path / "cfg" / "romarr-soundtracks.db"
    s = st.Soundtracks(p)
    c = s._db()
    names = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    c.close()
    assert {"sound", "journal"} <= names


# --- credentials never leak --------------------------------------------------

def test_the_token_never_reaches_logs_jobs_status_or_errors(tmp_path, api, romm, fake_stream, caplog):
    caplog.set_level(logging.DEBUG)
    s = st.Soundtracks(tmp_path / "s.db", providers=fake_provider())
    run(s, api)
    romm.fail_delete = {1001}                       # RomM echoes the credential in this 500
    with pytest.raises(st.RommError) as err:
        s.undo(GAME, api)
    seen = [caplog.text, json.dumps(s.jobs()), json.dumps(s.status(GAME, api)), str(err.value)]
    c = s._db()
    seen.append(json.dumps([tuple(r) for r in c.execute("SELECT * FROM sound")] + [tuple(r) for r in c.execute("SELECT * FROM journal")]))
    c.close()
    assert not any(TOKEN in x for x in seen)
    assert "***" in str(err.value)


def test_a_failed_upload_is_reported_without_the_token(tmp_path, api, romm, fake_stream, monkeypatch):
    s = st.Soundtracks(tmp_path / "s.db", providers=fake_provider())
    orig = st.RommApi.upload

    def boom(self, *a, **k):
        raise st.RommError(500, self._redact("Authorization: Bearer " + TOKEN))
    monkeypatch.setattr(st.RommApi, "upload", boom)
    run(s, api)
    job = s.jobs()[0]
    assert job["state"] == "error" and TOKEN not in json.dumps(job)
    monkeypatch.setattr(st.RommApi, "upload", orig)
