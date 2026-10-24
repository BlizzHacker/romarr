"""The library audit: fxfire's question, answered with hashes."""

import time

from romarr.app import ROMarr
from romarr.dat import Dat, parse_dat


DAT_XML = """<?xml version="1.0"?>
<datafile><header><name>Test</name></header>
  <game name="Good Game (USA)">
    <rom name="Good Game (USA).smc" size="8"
         crc="9c programmatically-wrong" sha1="{sha_good}" md5="{md5_good}"/>
  </game>
</datafile>"""


def _svc(tmp_path):
    return ROMarr(env={"ROMARR_DATA": str(tmp_path / "s.json"),
                       "LIBRARY_PATH": str(tmp_path / "lib")})


def _wait_done(svc, seconds=10):
    for _ in range(seconds * 10):
        if svc._audit_state.get("status") != "running":
            return svc._audit_state
        time.sleep(0.1)
    raise AssertionError("audit never finished")


def test_the_audit_finds_verified_bad_and_duplicate_files(tmp_path):
    import hashlib
    import zlib

    good = b"GOODGAME"
    dat_xml = f"""<?xml version="1.0"?>
<datafile><header><name>t</name></header>
  <game name="Good Game (USA)">
    <rom name="Good Game (USA).smc" size="{len(good)}"
         crc="{zlib.crc32(good) & 0xFFFFFFFF:08x}"
         md5="{hashlib.md5(good).hexdigest()}"
         sha1="{hashlib.sha1(good).hexdigest()}"/>
  </game>
</datafile>"""
    svc = _svc(tmp_path)
    from romarr.dat import DatIndex
    index = DatIndex()
    index.add(parse_dat(dat_xml))
    svc.dats = index

    shelf = tmp_path / "lib" / "snes"
    shelf.mkdir(parents=True)
    (shelf / "Good Game (USA).smc").write_bytes(good)
    # Same size as the known ROM, different bytes: the case worth finding.
    (shelf / "Bad Copy (USA).smc").write_bytes(b"BADBYTES")
    # Byte-identical duplicate under another name.
    (shelf / "Good Game (copy).smc").write_bytes(good)
    # Something the DAT has never heard of, at a different size.
    (shelf / "Homebrew Thing.smc").write_bytes(b"H" * 20)

    out = svc.audit_library("snes")
    assert "error" not in out
    state = _wait_done(svc)
    assert state["scanned"] == 4
    assert state["verified"] == 2          # the good file and its twin
    assert state["bad"] == 1
    assert state["unknown"] == 1
    assert state["bad_files"][0]["file"].endswith("Bad Copy (USA).smc")
    assert len(state["duplicates"]) == 1
    assert "Good Game" in state["duplicates"][0]["file"]


def test_the_audit_refuses_to_run_blind(tmp_path):
    svc = _svc(tmp_path)
    svc.dats = None
    out = svc.audit_library("snes")
    assert "DAT" in out["error"]
    assert "unknown platform" in svc.audit_library("atari 9000").get(
        "error", "") or svc.audit_library("atari 9000")["error"]


def test_quarantine_and_restore_are_available_from_the_audit_result(tmp_path):
    import hashlib
    import zlib
    from romarr.dat import DatIndex

    body = b"RESTORABLE"
    xml = ('<datafile><header><name>Audit Test</name></header><game name="Game (USA)">'
           '<rom name="Game.zip" size="' + str(len(body)) + '" crc="'
           + f"{zlib.crc32(body) & 0xFFFFFFFF:08x}" + '" md5="'
           + hashlib.md5(body).hexdigest() + '" sha1="'
           + hashlib.sha1(body).hexdigest() + '"/></game></datafile>')
    svc = _svc(tmp_path)
    index = DatIndex()
    index.add(parse_dat(xml))
    svc.dats = index
    shelf = tmp_path / "lib" / "snes"
    shelf.mkdir(parents=True)
    (shelf / "Game (USA).zip").write_bytes(body)
    (shelf / "Copy.zip").write_bytes(body)

    svc.audit_library("snes")
    state = _wait_done(svc)
    assert len(state["duplicates"]) == 1
    moved = svc.quarantine_duplicates("snes", [
        {"file": state["duplicates"][0]["file"]}])
    assert moved["ok"]
    assert len(svc.duplicate_quarantine_state()["manifests"]) == 1
    moved_path = shelf / moved["entries"][0]["original"]
    assert not moved_path.exists()

    restored = svc.restore_duplicate(moved["manifest_id"],
                                    moved["entries"][0]["id"])
    assert restored["ok"]
    assert moved_path.read_bytes() == body


def test_audit_preview_keeps_all_duplicates_beyond_the_old_display_cap(
        tmp_path):
    svc = _svc(tmp_path)
    from romarr.dat import DatIndex
    svc.dats = DatIndex()
    shelf = tmp_path / "lib" / "snes"
    shelf.mkdir(parents=True)
    body = b"same bytes"
    (shelf / "keeper.bin").write_bytes(body)
    for n in range(205):
        (shelf / f"copy-{n}.bin").write_bytes(body)

    svc.audit_library("snes")
    state = _wait_done(svc)
    assert state["scanned"] == 206
    assert len(state["duplicates"]) == 205


def test_audit_exact_duplicates_include_copier_header_bytes(tmp_path):
    from romarr.dat import DatIndex, hash_file

    svc = _svc(tmp_path)
    svc.dats = DatIndex()
    shelf = tmp_path / "lib" / "snes"
    shelf.mkdir(parents=True)
    body = b"R" * 32768
    first = shelf / "first.smc"
    second = shelf / "second.smc"
    first.write_bytes(b"A" * 512 + body)
    second.write_bytes(b"B" * 512 + body)
    assert hash_file(first)["sha1"] == hash_file(second)["sha1"]
    svc.audit_library("snes")
    state = _wait_done(svc)
    assert state["scanned"] == 2
    assert state["duplicates"] == []
