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
