"""Startup with DATs (#230): listen first, load DATs in the background, and
reuse parsed DATs across restarts."""
import threading
import time
import zipfile

import pytest

from romarr import dat as dat_module
from romarr.app import ROMarr
from romarr.dat import (Game, Rom, dat_from_record, parse_dat,
                        parse_dat_record)

XML = ('<?xml version="1.0"?><datafile><header><name>Nintendo - Game Boy</name>'
       '<version>20261119</version></header>'
       # cloneofid points forward, to a game the parser has not seen yet
       '<game name="Game (Europe)" id="2" cloneofid="1" language="en,fr">'
       '<rom name="Game (Europe).gb" size="32768" crc="AABBCCDD" md5="0f" sha1="11"/></game>'
       '<game name="Game (USA)" id="1"><rom name="Game (USA).gb" size="32768" crc="01020304"/></game>'
       # a duplicate name keeps both entries' hashes, as parsing always did
       '<game name="Game (USA)" id="3"><rom name="Game (USA) [b].gb" size="1" crc="0a0b0c0d"/></game>'
       '</datafile>')

CMP = ('clrmamepro (\n\tname "Sega - Mega Drive"\n\tversion 1\n)\n\n'
       'game (\n\tname "Sonic (USA)"\n\tlanguage "English"\n'
       '\trom ( name "Sonic (USA).md" size 524288 crc 0011aabb md5 aa sha1 bb )\n)\n\n'
       'game (\n\tname "Sonic (Japan)"\n\tcloneof "Sonic (USA)"\n'
       '\trom ( name "Sonic (Japan).md" size 524288 crc 0011aacc )\n)\n')


def _shape(d):
    rom = lambda r: (r.name, r.size, r.crc, r.md5, r.sha1)
    hit = lambda v: (v[0], rom(v[1]))
    return (d.name, d.version,
            [(g.name, [rom(r) for r in g.roms], g.cloneof, g.languages) for g in d.games.values()],
            sorted(d._ambiguous_game_names),
            {k: hit(v) for k, v in d._by_crc.items()}, {k: hit(v) for k, v in d._by_sha1.items()},
            {k: hit(v) for k, v in d._by_md5.items()}, dict(d._sizes))


@pytest.mark.parametrize("body", [XML, CMP])
def test_a_dat_rebuilt_from_its_record_is_identical_to_parsing_it(body):
    parsed = parse_dat(body)
    assert parsed.games
    assert _shape(dat_from_record(parse_dat_record(body))) == _shape(parsed)


def test_logiqx_streaming_resolves_forward_clone_ids_and_keeps_duplicates():
    d = parse_dat(XML)
    assert d.games["Game (Europe)"].cloneof == "Game (USA)"
    assert d.games["Game (Europe)"].languages == ("en", "fr")
    assert "Game (USA)" in d._ambiguous_game_names
    # the first entry's ROM is still indexed even though the name repeats
    assert d._by_crc[("01020304", 32768)][0] == "Game (USA)"
    assert d._by_crc[("0a0b0c0d", 1)][0] == "Game (USA)"


def test_rom_and_game_carry_no_per_instance_dict():
    # A full DAT collection holds millions of these.
    assert not hasattr(Rom(name="a"), "__dict__")
    assert not hasattr(Game(name="a"), "__dict__")


def _service(tmp_path, dats, **kw):
    env = {"ROMARR_DATA": str(tmp_path / "state" / "romarr.json"),
           "ROMARR_API_KEY": "testkey", "DAT_PATH": str(dats),
           "LIBRARY_PATH": str(tmp_path / "library")}
    svc = ROMarr(env, **kw)
    svc.scheduler.stop()
    return svc


def _dat_dir(tmp_path):
    dats = tmp_path / "dats"
    dats.mkdir()
    (dats / "gb.xml").write_text(XML, encoding="utf-8")
    (dats / "md.dat").write_text(CMP, encoding="utf-8")
    with zipfile.ZipFile(dats / "pack.zip", "w") as zf:
        zf.writestr("Nintendo - NES.dat", XML.replace("Game Boy", "NES"))
    return dats


def _count_parses(monkeypatch):
    calls = []
    real = dat_module.parse_dat_record

    def counting(body):
        calls.append(1)
        return real(body)

    monkeypatch.setattr(dat_module, "parse_dat_record", counting)
    return calls


def test_a_restart_reuses_parsed_dats_instead_of_parsing_them_again(tmp_path, monkeypatch):
    dats = _dat_dir(tmp_path)
    calls = _count_parses(monkeypatch)
    first = _service(tmp_path, dats)
    assert len(calls) == 3  # two files and one archive member
    before = sorted(_shape(d) for d in first.dats.dats)
    calls.clear()
    second = _service(tmp_path, dats)
    assert calls == []
    assert sorted(_shape(d) for d in second.dats.dats) == before


def test_a_changed_dat_is_parsed_again(tmp_path, monkeypatch):
    dats = _dat_dir(tmp_path)
    _service(tmp_path, dats)
    calls = _count_parses(monkeypatch)
    time.sleep(0.01)
    (dats / "md.dat").write_text(CMP.replace("Sonic (USA)", "Sonic 2 (USA)"), encoding="utf-8")
    svc = _service(tmp_path, dats)
    assert len(calls) == 1
    md = next(d for d in svc.dats.dats if d.name == "Sega - Mega Drive")
    assert "Sonic 2 (USA)" in md.games


def test_an_unreadable_cache_entry_is_parsed_again(tmp_path, monkeypatch):
    dats = _dat_dir(tmp_path)
    first = _service(tmp_path, dats)
    for entry in first._dat_cache_dir().glob("*.bin"):
        entry.write_bytes(b"not a record")
    calls = _count_parses(monkeypatch)
    second = _service(tmp_path, dats)
    assert len(calls) == 3
    assert len(second.dats.dats) == 3


def test_cache_entries_for_removed_dats_are_pruned(tmp_path):
    dats = _dat_dir(tmp_path)
    svc = _service(tmp_path, dats)
    assert len(list(svc._dat_cache_dir().glob("*.bin"))) == 3
    (dats / "pack.zip").unlink()
    svc = _service(tmp_path, dats)
    assert len(list(svc._dat_cache_dir().glob("*.bin"))) == 2


def test_a_server_answers_before_its_dats_load_and_readers_wait_for_them(tmp_path, monkeypatch):
    dats = _dat_dir(tmp_path)
    gate = threading.Event()
    real = dat_module.parse_dat_record

    def slow(body):
        gate.wait(5)
        return real(body)

    monkeypatch.setattr(dat_module, "parse_dat_record", slow)
    svc = _service(tmp_path, dats, defer_dats=True)
    # Constructed without parsing anything, and health says so.
    assert svc.dats_status() == {"loading": True, "loaded": 0}
    assert svc.health()["dats"]["loading"] is True
    loader = svc.load_dats_in_background()
    seen = {}
    reader = threading.Thread(target=lambda: seen.setdefault("n", len(svc.dats.dats)))
    reader.start()
    reader.join(0.3)
    assert reader.is_alive(), "a verification read must wait for the DATs"
    gate.set()
    loader.join(5)
    reader.join(5)
    assert seen["n"] == 3
    assert svc.dats_status() == {"loading": False, "loaded": 3}


def test_status_pages_answer_while_dats_load(tmp_path, monkeypatch):
    dats = _dat_dir(tmp_path)
    gate = threading.Event()
    real = dat_module.parse_dat_record
    monkeypatch.setattr(dat_module, "parse_dat_record",
                        lambda body: gate.wait(5) and real(body))
    svc = _service(tmp_path, dats, defer_dats=True)
    loader = svc.load_dats_in_background()
    answered = {}

    def ask():
        answered["setup"] = svc.setup_status()
        answered["status"] = svc.status()

    asker = threading.Thread(target=ask)
    asker.start()
    asker.join(3)
    try:
        assert not asker.is_alive(), "status pages must not wait for DATs"
        assert answered["setup"]["dat_loading"] is True
        assert answered["status"]["dats_loading"] is True
    finally:
        gate.set()
        loader.join(5)
    assert svc.setup_status()["dat_loaded"] == 3


def test_a_failed_background_load_does_not_leave_readers_waiting(tmp_path, monkeypatch):
    dats = _dat_dir(tmp_path)
    svc = _service(tmp_path, dats, defer_dats=True)

    def broken(directory=""):
        raise RuntimeError("disk vanished")

    monkeypatch.setattr(svc, "_reload_dats", broken)
    svc.load_dats_in_background().join(5)
    assert svc.dats_status()["loading"] is False
    assert svc.dats.dats == []


def test_a_selection_made_during_startup_is_not_undone_by_the_startup_load(tmp_path):
    startup = _dat_dir(tmp_path)
    chosen = tmp_path / "chosen"
    chosen.mkdir()
    (chosen / "md.dat").write_text(CMP, encoding="utf-8")
    svc = _service(tmp_path, startup, defer_dats=True)
    # Setup picks another folder before the deferred load gets the lock.
    assert svc.setup_dats(str(chosen))["ok"]
    svc.load_dats_in_background().join(5)
    assert [d.name for d in svc.dats.dats] == ["Sega - Mega Drive"]
    assert svc.store.settings["dat_path"] == str(chosen)


def test_a_rejected_setup_candidate_does_not_cancel_the_startup_load(tmp_path):
    svc = _service(tmp_path, _dat_dir(tmp_path), defer_dats=True)
    empty = tmp_path / "empty"
    empty.mkdir()
    assert not svc.setup_dats(str(empty))["ok"]
    svc.load_dats_in_background().join(5)
    assert len(svc.dats.dats) == 3


def test_direct_construction_still_loads_dats_synchronously(tmp_path):
    svc = _service(tmp_path, _dat_dir(tmp_path))
    assert svc.dats_status() == {"loading": False, "loaded": 3}
    assert svc.load_dats_in_background() is None
