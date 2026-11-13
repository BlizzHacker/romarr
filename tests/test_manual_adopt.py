"""Manual import, with the verdict rules that decide what an operator may do.

The Manual Import page could scan and report and nothing else, so a file it
identified perfectly still had to be moved by hand. These cover the action half.

The rule the rest of this file exists to protect:

    UNKNOWN is not BAD.

UNKNOWN means "not in the DAT you loaded". That is the normal state for
homebrew, for translations, for a hack, and for anything released after your
DAT was published. Treating it as a problem would make ROMarr refuse most of
what people actually own. BAD_DUMP is different and specific: a file the size
of a known ROM whose hash does not match. That one is worth stopping for -- and
still worth letting the operator override, because they may well know exactly
why their Pokemon ROM does not match No-Intro.
"""

from __future__ import annotations

import zlib
from pathlib import Path

import pytest

from romarr.app import ROMarr
from romarr.dat import (BAD_DUMP, UNKNOWN, VERIFIED, DatIndex, header_size,
                        parse_dat)

NES_ROM = b"\x4e\x45\x53\x1a" + b"\xa5" * 4096


def dat_for(name: str, blob: bytes) -> DatIndex:
    """A DAT recording `blob` the way No-Intro records it.

    These fixtures carry a 16-byte iNES header, and ROMarr strips a copier
    header before hashing -- as it must, because No-Intro's checksums are of
    the headerless ROM. So the DAT has to record the stripped hash too; using
    the whole file's CRC makes every fixture report UNKNOWN and quietly turns
    the verification tests into no-ops.
    """
    payload = blob[header_size(".nes", len(blob)):]
    crc = format(zlib.crc32(payload) & 0xFFFFFFFF, "08x")
    index = DatIndex()
    index.add(parse_dat(f"""<?xml version="1.0"?>
<datafile><header><name>Test</name></header>
<game name="{name}"><rom name="{name}.nes" size="{len(payload)}" crc="{crc}"/></game>
</datafile>"""))
    return index


@pytest.fixture
def service(tmp_path):
    lib = tmp_path / "library"
    lib.mkdir()
    svc = ROMarr({"ROMARR_DATA": str(tmp_path / "s.json"),
                  "ROMARR_API_KEY": "k",
                  "LIBRARY_KIND": "folder",
                  "LIBRARY_PATH": str(lib)})
    svc.store.put_item("libraries", {
        "type": "folder", "name": "Shelf", "enable": True,
        "path": str(lib), "is_default": True})
    svc.reload_libraries()
    return svc, lib


def drop(tmp_path: Path, name: str, blob: bytes = NES_ROM) -> Path:
    src = tmp_path / "incoming"
    src.mkdir(exist_ok=True)
    path = src / name
    path.write_bytes(blob)
    return path


# --- the rule ---------------------------------------------------------------

def test_an_unknown_rom_imports_without_being_forced(service, tmp_path):
    """The Pokemon case from the feedback: a ROM that is not in the DAT is
    not evidence of anything wrong, and must not need an override."""
    svc, lib = service
    svc.dats = dat_for("Something Else", b"different")
    path = drop(tmp_path, "Pokemon Brown (Hack).nes")

    got = svc.adopt(str(path), "nes")

    assert got["ok"] is True, got
    assert got["imported"][0]["verdict"] == UNKNOWN
    assert got["imported"][0]["forced"] is False
    assert (lib / "nes" / "Pokemon Brown (Hack).nes").exists()


def test_a_bad_dump_is_refused_until_the_operator_says_otherwise(service, tmp_path):
    svc, lib = service
    # Same size as the known ROM, different contents: a hash mismatch, which
    # is the only thing that earns a refusal.
    svc.dats = dat_for("Real Game", NES_ROM)
    path = drop(tmp_path, "Real Game.nes", b"\x4e\x45\x53\x1a" + b"\x5a" * 4096)

    got = svc.adopt(str(path), "nes")

    assert got["ok"] is False
    assert got["refused"][0]["needs_force"] is True
    assert got["refused"][0]["verdict"] == BAD_DUMP
    assert not (lib / "nes" / "Real Game.nes").exists(), (
        "a refused dump was left in the library")


def test_a_bad_dump_imports_when_forced_and_is_recorded_as_forced(service, tmp_path):
    svc, lib = service
    svc.dats = dat_for("Real Game", NES_ROM)
    path = drop(tmp_path, "Real Game.nes", b"\x4e\x45\x53\x1a" + b"\x5a" * 4096)

    got = svc.adopt(str(path), "nes", force=True)

    assert got["ok"] is True
    assert got["imported"][0]["forced"] is True
    assert (lib / "nes" / "Real Game.nes").exists()

    # Never silently reinterpreted as verified: the history says it was forced
    # and over what.
    events = [e for e in svc.store.events if e.kind == "imported"]
    assert events, "no import was recorded"
    detail = events[-1].detail
    assert "FORCED" in detail and BAD_DUMP in detail


def test_forcing_a_clean_rom_does_not_mark_it_forced(service, tmp_path):
    """Force is permission, not a label. A verified file is still verified."""
    svc, lib = service
    svc.dats = dat_for("Real Game", NES_ROM)
    path = drop(tmp_path, "Real Game.nes")

    got = svc.adopt(str(path), "nes", force=True)

    assert got["ok"] is True
    assert got["imported"][0]["verdict"] == VERIFIED
    assert got["imported"][0]["forced"] is False


# --- the operator's choice wins ---------------------------------------------

def test_the_operator_can_override_the_detected_platform(service, tmp_path):
    """They can see the file; ROMarr can only guess from its name.

    nes and famicom both claim .nes, which is exactly the ambiguity somebody
    would want to correct by hand.
    """
    svc, lib = service
    path = drop(tmp_path, "Famicom Only Title.nes")

    got = svc.adopt(str(path), "famicom")

    assert got["ok"] is True
    assert got["platform"] == "famicom"
    assert (lib / "famicom" / "Famicom Only Title.nes").exists()
    assert not (lib / "nes").exists(), "the guess won over the operator"


def test_without_a_platform_it_falls_back_to_detection(service, tmp_path):
    svc, lib = service
    path = drop(tmp_path, "Super Mario Bros (USA).nes")
    got = svc.adopt(str(path))
    assert got["ok"] is True and got["platform"] == "nes"


def test_an_undetectable_file_asks_rather_than_guessing(service, tmp_path):
    svc, _ = service
    path = drop(tmp_path, "mystery.qqq", b"nothing recognisable")
    got = svc.adopt(str(path))
    assert got["ok"] is False
    assert "choose one" in got["reason"]


# --- ordinary failures ------------------------------------------------------

def test_a_missing_file_says_so(service):
    svc, _ = service
    got = svc.adopt("/no/such/file.nes", "nes")
    assert got["ok"] is False and "does not exist" in got["reason"]


def test_no_library_configured_is_reported_not_crashed(tmp_path):
    svc = ROMarr({"ROMARR_DATA": str(tmp_path / "s.json"), "ROMARR_API_KEY": "k"})
    path = drop(tmp_path, "Game.nes")
    got = svc.adopt(str(path), "nes")
    assert got["ok"] is False
    assert "no library" in got["reason"]


def test_importing_twice_does_not_silently_duplicate(service, tmp_path):
    svc, lib = service
    path = drop(tmp_path, "Game.nes")
    assert svc.adopt(str(path), "nes")["ok"] is True
    again = svc.adopt(str(path), "nes")
    assert again["ok"] is False
    assert "already in the library" in again["refused"][0]["reason"]
    assert again["refused"][0]["needs_force"] is False, (
        "already-present is not a verification problem and must not offer a "
        "force override")


# --- the header's opinion ---------------------------------------------------

def test_a_header_mismatch_imports_and_says_so(service, tmp_path):
    """The operator picked Switch for a Switch 2 NUP. Their choice stands, and
    the result carries the header's opinion for the page to show."""
    from tests.nup_builder import Pkg, build_nup
    svc, lib = service
    path = drop(tmp_path, "Bananza.nup", build_nup([Pkg(0x040074A01BF12000)]))

    got = svc.adopt(str(path), "switch")

    assert got["ok"] is True, got
    assert got["header_says"] == "switch-2"
    assert "040074A01BF12000" in got["header_detail"]
    assert (lib / "switch" / "Bananza.nup").exists()


def test_an_agreeing_header_adds_no_warning(service, tmp_path):
    from tests.nup_builder import Pkg, build_nup
    svc, _ = service
    path = drop(tmp_path, "Bananza.nup", build_nup([Pkg(0x040074A01BF12000)]))

    got = svc.adopt(str(path), "switch-2")

    assert got["ok"] is True, got
    assert "header_says" not in got


def _run_adopt_in_node(response: dict) -> str:
    """Run the Manual Import page's adopt() against one canned response and
    return what it wrote into the row's status cell."""
    import json
    import shutil
    import subprocess

    from romarr import ui

    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    start = ui.JS.index("const headerPill=")
    end = ui.JS.index("$('#miscan').onclick=", start)
    script = f"""
const esc = s => String(s ?? '');
const j = async () => ({json.dumps(response)});
const cells = {{
  '.mi-go': {{ disabled: false, remove() {{}} }},
  '.mi-st': {{ innerHTML: '', textContent: '' }},
  '.mi-pf': {{ value: 'switch', disabled: false }},
}};
const row = {{ dataset: {{ path: '/downloads/x.nup' }}, querySelector: s => cells[s] }};
{ui.JS[start:end]}
adopt(row, false).then(() => console.log(cells['.mi-st'].innerHTML))
  .catch(err => {{ console.error(err); process.exit(1); }});
"""
    res = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=15)
    assert res.returncode == 0, res.stderr
    return res.stdout


def test_the_page_shows_the_header_warning_after_an_import():
    """The scan row's badge alone is not enough: the platform can be changed
    after the scan, and the import result is the last thing the operator sees."""
    shown = _run_adopt_in_node({
        "ok": True, "imported": [{"forced": False}], "refused": [],
        "header_says": "switch-2",
        "header_detail": "NUP title ID 040074A01BF12000 (Switch 2)"})

    assert "imported" in shown
    assert "header says switch-2" in shown
    assert "NUP title ID 040074A01BF12000 (Switch 2)" in shown


def test_the_page_shows_no_badge_when_the_header_agrees():
    shown = _run_adopt_in_node({"ok": True, "imported": [{"forced": False}], "refused": []})

    assert "imported" in shown
    assert "header says" not in shown
