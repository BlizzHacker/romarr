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


def test_unrelated_files_are_excluded_from_library_audit(tmp_path):
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

    # An unrelated file with exact same size as the good ROM.
    # Without extension filtering, this would be hashed and flagged as BAD_DUMP (#120)!
    (shelf / "game_notes.txt").write_bytes(b"NOTE1234")
    (shelf / "cover.jpg").write_bytes(b"JPEGDATA")
    (shelf / "info.nfo").write_bytes(b"NFODATA0")

    try:
        (shelf / "symlink_to_good.smc").symlink_to(shelf / "Good Game (USA).smc")
    except (OSError, NotImplementedError):
        pass

    out = svc.audit_library("snes")
    assert "error" not in out
    state = _wait_done(svc)
    assert state["scanned"] == 1
    assert state["verified"] == 1
    assert state["bad"] == 0
    assert state["unknown"] == 0
    assert len(state["duplicates"]) == 0
    assert len(state["bad_files"]) == 0


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
    (shelf / "keeper.smc").write_bytes(body)
    for n in range(205):
        (shelf / f"copy-{n}.smc").write_bytes(body)

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


def test_audit_and_quarantine_work_with_custom_platform_folders(tmp_path):
    """Issue #154: audit, duplicate quarantine, and restore honor platform_folders."""
    import hashlib
    import zlib
    from romarr.dat import DatIndex

    body = b"CUSTOMFOLDER"
    xml = ('<datafile><header><name>Custom Folder Audit</name></header>'
           '<game name="Game (USA)">'
           '<rom name="Game.smc" size="' + str(len(body)) + '" crc="'
           + f"{zlib.crc32(body) & 0xFFFFFFFF:08x}" + '" md5="'
           + hashlib.md5(body).hexdigest() + '" sha1="'
           + hashlib.sha1(body).hexdigest() + '"/></game></datafile>')
    svc = _svc(tmp_path)
    svc.store.update_settings({"platform_folders": {"snes": "Nintendo - Super Nintendo"}})
    index = DatIndex()
    index.add(parse_dat(xml))
    svc.dats = index

    shelf = tmp_path / "lib" / "Nintendo - Super Nintendo"
    shelf.mkdir(parents=True)
    (shelf / "Game (USA).smc").write_bytes(body)
    (shelf / "Copy.smc").write_bytes(body)

    svc.audit_library("snes")
    state = _wait_done(svc)
    assert state["scanned"] == 2
    assert len(state["duplicates"]) == 1
    moved = svc.quarantine_duplicates("snes", [{"file": state["duplicates"][0]["file"]}])
    assert moved["ok"], moved
    assert not (shelf / moved["entries"][0]["original"]).exists()
    restored = svc.restore_duplicate(moved["manifest_id"], moved["entries"][0]["id"])
    assert restored["ok"]
    assert (shelf / moved["entries"][0]["original"]).read_bytes() == body


def test_audit_keeps_library_root_snapshot_if_library_changes_mid_audit(tmp_path, monkeypatch):
    """Changing svc.library mid-audit must not raise ValueError on path.relative_to."""
    import hashlib
    import zlib
    import romarr.dat as dat_mod
    from romarr.dat import DatIndex

    body = b"SNAPSHOTTEST"
    xml = ('<datafile><header><name>Snapshot Audit</name></header>'
           '<game name="Good (USA)">'
           '<rom name="Good.smc" size="' + str(len(body)) + '" crc="'
           + f"{zlib.crc32(body) & 0xFFFFFFFF:08x}" + '" md5="'
           + hashlib.md5(body).hexdigest() + '" sha1="'
           + hashlib.sha1(body).hexdigest() + '"/></game></datafile>')
    svc = _svc(tmp_path)
    index = DatIndex()
    index.add(parse_dat(xml))
    svc.dats = index

    shelf = tmp_path / "lib" / "snes"
    shelf.mkdir(parents=True)
    (shelf / "Good.smc").write_bytes(body)
    (shelf / "Dup.smc").write_bytes(body)
    (shelf / "Bad.smc").write_bytes(b"WRONGBYTES12")

    real_hash_file = dat_mod.hash_file
    relocated = tmp_path / "other-lib"
    relocated.mkdir(parents=True)

    def mutate_library_on_hash(path, **kwargs):
        """Swap svc.library to a sibling directory while audit is running."""
        svc.library = relocated
        return real_hash_file(path, **kwargs)

    monkeypatch.setattr(dat_mod, "hash_file", mutate_library_on_hash)
    out = svc.audit_library("snes")
    assert "error" not in out
    state = _wait_done(svc)
    assert state["status"] == "done"
    assert state["scanned"] == 3
    assert state["verified"] == 2
    assert state["bad"] == 1
    assert len(state["duplicates"]) == 1


def _zip_dat_index(entries):
    import hashlib
    import zlib
    from romarr.dat import DatIndex, parse_dat
    rows = []
    for name, body in entries:
        rows.append(
            f'<game name="{name}"><rom name="{name}" size="{len(body)}" '
            f'crc="{zlib.crc32(body) & 0xFFFFFFFF:08x}" '
            f'md5="{hashlib.md5(body).hexdigest()}" '
            f'sha1="{hashlib.sha1(body).hexdigest()}"/></game>')
    index = DatIndex()
    index.add(parse_dat('<datafile><header><name>zip test</name></header>'
                        + ''.join(rows) + '</datafile>'))
    return index


def _run_zip_audit(tmp_path, members, dat_entries, filename="game.zip", platform_slug="snes"):
    import zipfile
    svc = _svc(tmp_path)
    svc.dats = _zip_dat_index(dat_entries)
    shelf = tmp_path / "lib" / platform_slug
    shelf.mkdir(parents=True)
    with zipfile.ZipFile(shelf / filename, "w") as archive:
        for name, data in members:
            archive.writestr(name, data)
    out = svc.audit_library(platform_slug)
    assert "error" not in out
    return _wait_done(svc)


def test_zip_audit_hashes_rom_member_with_member_suffix(tmp_path):
    body = b"A" * 16 + b"ROM-CONTENT"
    state = _run_zip_audit(tmp_path, [("Game.nes", body)],
                           [("Game.nes", body[16:])], platform_slug="nes")
    assert (state["scanned"], state["verified"], state["bad"], state["unknown"]) == (1, 1, 0, 0)
    # The old behavior hashed the ZIP container and did not match the ROM DAT.
    import zipfile
    archive_path = tmp_path / "lib" / "nes" / "game.zip"
    from romarr.dat import hash_file
    assert _zip_dat_index([("Game.nes", body[16:])]).lookup(**hash_file(archive_path)).status == "unknown"


def test_zip_audit_does_not_call_mixed_rom_bundle_verified(tmp_path):
    good, unknown = b"GOOD-ONE", b"UNLISTED-LONGER"
    state = _run_zip_audit(tmp_path, [("One.smc", good), ("Two.smc", unknown)],
                           [("One.smc", good)])
    assert (state["verified"], state["bad"], state["unknown"]) == (0, 0, 1)


def test_zip_audit_requires_disc_tracks_named_by_cue(tmp_path):
    cue = b'FILE "missing.bin" BINARY\nTRACK 01 MODE2/2352\nINDEX 01 00:00:00\n'
    state = _run_zip_audit(tmp_path, [("Game.cue", cue)], [("Game.cue", cue)])
    assert state["verified"] == state["bad"] == 0
    assert state["unknown"] == 1


def test_zip_audit_unsafe_member_is_unknown_even_when_its_size_matches_dat(tmp_path):
    body = b"ROM-DATA"
    state = _run_zip_audit(tmp_path, [("../Game.smc", body)],
                           [("Game.smc", body)])
    assert state["verified"] == state["bad"] == 0
    assert state["unknown"] == 1


def test_zip_audit_crc_failure_is_unknown(tmp_path):
    import struct
    import zipfile
    from romarr.audit_archive import audit_zip
    from romarr.dat import DatIndex
    from romarr.platforms import resolve

    path = tmp_path / "broken.zip"
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr("Game.smc", b"ROM-DATA-CONTENT")
    with zipfile.ZipFile(path) as archive:
        info = archive.getinfo("Game.smc")
        offset = info.header_offset
        with path.open("r+b") as handle:
            handle.seek(offset + 26)
            name_len, extra_len = struct.unpack("<HH", handle.read(4))
            handle.seek(offset + 30 + name_len + extra_len)
            byte = handle.read(1)
            handle.seek(-1, 1)
            handle.write(bytes([byte[0] ^ 1]))
    verdict = audit_zip(path, resolve("snes"), DatIndex())
    assert verdict.status == "unknown"
