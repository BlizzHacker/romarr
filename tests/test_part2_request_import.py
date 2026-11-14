"""Opt-in request gate through real ZIP import; not an automatic-queue test."""
from __future__ import annotations

import io
import zlib
import zipfile

import pytest

from romarr.dat import DatIndex, parse_dat
from romarr.library import import_rom
from romarr.platforms import resolve

ROM = b"\x5A" * 65536


def _catalog(game="Super Metroid (USA)", *, data=ROM, system="Nintendo - SNES"):
    crc = f"{zlib.crc32(data) & 0xFFFFFFFF:08x}"
    dat = parse_dat(
        f'<datafile><header><name>{system}</name></header>'
        f'<game name="{game}"><rom name="{game}.sfc" '
        f'size="{len(data)}" crc="{crc}"/></game></datafile>'
    )
    index = DatIndex()
    index.add(dat)
    return index


def _zip(path, *, name="Super Metroid (USA).sfc", payload=ROM, nested=False):
    if nested:
        inner = io.BytesIO()
        with zipfile.ZipFile(inner, "w") as f:
            f.writestr(name, payload)
        with zipfile.ZipFile(path, "w") as f:
            f.writestr("Disc.zip", inner.getvalue())
    else:
        with zipfile.ZipFile(path, "w") as f:
            f.writestr(name, payload)
    return path


def _import(tmp_path, source, *, game="Super Metroid", map_catalog=True, dats=None):
    return import_rom(
        source, resolve("snes"), tmp_path / "library", dats=_catalog() if dats is None else dats,
        requested_game=game,
        catalog_platforms={"Nintendo - SNES": "snes"} if map_catalog else {},
    )


@pytest.mark.parametrize("nested", [False, True])
def test_matching_requested_game_is_imported_once(tmp_path, nested):
    source = _zip(tmp_path / "dl.zip", nested=nested)
    results = _import(tmp_path, source)
    assert len(results) == 1 and results[0].ok, results
    assert results[0].destination.read_bytes() == ROM
    assert len(list((tmp_path / "library").rglob("*.sfc"))) == 1
    assert source.exists()


@pytest.mark.parametrize("nested", [False, True])
def test_wrong_game_is_held_before_publication(tmp_path, nested):
    source = _zip(tmp_path / "dl.zip", nested=nested)
    results = _import(tmp_path, source, game="Chrono Trigger")
    assert len(results) == 1 and not results[0].ok
    assert "REQUEST_TITLE_MISMATCH" in results[0].reason
    assert not (tmp_path / "library").exists()
    assert source.exists()


def test_unmapped_dat_platform_is_held_before_publication(tmp_path):
    source = _zip(tmp_path / "dl.zip")
    results = _import(tmp_path, source, map_catalog=False)
    assert not results[0].ok
    assert "DAT_PLATFORM_UNCONFIRMED" in results[0].reason
    assert not (tmp_path / "library").exists()


def test_bad_dump_is_rejected_before_publication(tmp_path):
    source = _zip(tmp_path / "dl.zip", payload=b"\x00" * len(ROM))
    results = _import(tmp_path, source)
    assert not results[0].ok
    assert "DAT_FAILED" in results[0].reason
    assert not (tmp_path / "library").exists()


def test_multiple_games_are_held_without_import(tmp_path):
    source = tmp_path / "dl.zip"
    with zipfile.ZipFile(source, "w") as f:
        f.writestr("Super Metroid (USA).sfc", ROM)
        f.writestr("Other Game (USA).sfc", b"\x22" * 65536)
    results = _import(tmp_path, source)
    assert len(results) == 1 and not results[0].ok
    assert "AMBIGUOUS_MATCH" in results[0].reason
    assert not (tmp_path / "library").exists()


def test_legacy_manual_import_without_request_is_unchanged(tmp_path):
    source = _zip(tmp_path / "dl.zip")
    results = import_rom(source, resolve("snes"), tmp_path / "library", dats=_catalog())
    assert len(results) == 1 and results[0].ok
    assert results[0].destination.exists()



def test_same_catalog_hash_collision_holds_before_publication(tmp_path):
    source = _zip(tmp_path / "dl.zip")
    crc = f"{zlib.crc32(ROM) & 0xFFFFFFFF:08x}"
    catalog = parse_dat(f"""
    <datafile><header><name>Nintendo - SNES</name></header>
      <game name="Super Metroid (USA)">
        <rom name="Super Metroid (USA).sfc" size="{len(ROM)}" crc="{crc}"/>
      </game>
      <game name="Different Game (USA)">
        <rom name="Different Game (USA).sfc" size="{len(ROM)}" crc="{crc}"/>
      </game>
    </datafile>""")
    index = DatIndex()
    index.add(catalog)
    results = _import(tmp_path, source, dats=index)
    assert len(results) == 1 and not results[0].ok
    assert "AMBIGUOUS_MATCH" in results[0].reason
    assert not (tmp_path / "library").exists()


def test_source_swaps_to_another_verified_game_during_copy(tmp_path, monkeypatch):
    from romarr import library

    original = tmp_path / "Super Metroid (USA).sfc"
    original.write_bytes(ROM)
    different = b"\x23" * len(ROM)
    first_crc = f"{zlib.crc32(ROM) & 0xFFFFFFFF:08x}"
    second_crc = f"{zlib.crc32(different) & 0xFFFFFFFF:08x}"
    catalog = parse_dat(f"""
    <datafile><header><name>Nintendo - SNES</name></header>
      <game name="Super Metroid (USA)">
        <rom name="Super Metroid (USA).sfc" size="{len(ROM)}" crc="{first_crc}"/>
      </game>
      <game name="Different Game (USA)">
        <rom name="Different Game (USA).sfc" size="{len(different)}" crc="{second_crc}"/>
      </game>
    </datafile>""")
    index = DatIndex()
    index.add(catalog)
    copy_original = library._PathSource.copy
    swapped = False

    def swap_after_approval(self, name, destination):
        nonlocal swapped
        if self.path == original and not swapped:
            swapped = True
            original.write_bytes(different)
        return copy_original(self, name, destination)

    monkeypatch.setattr(library._PathSource, "copy", swap_after_approval)
    results = _import(tmp_path, original, dats=index)
    assert swapped
    assert len(results) == 1 and not results[0].ok
    assert "request identity changed during staging" in results[0].reason
    assert list((tmp_path / "library").rglob("*.sfc")) == []
    assert original.read_bytes() == different


def test_opt_in_request_disables_hardlink_publication(tmp_path):
    original = tmp_path / "Super Metroid (USA).sfc"
    original.write_bytes(ROM)
    outcomes = import_rom(
        original, resolve("snes"), tmp_path / "library",
        dats=_catalog(), requested_game="Super Metroid",
        catalog_platforms={"Nintendo - SNES": "snes"}, prefer_hardlinks=True,
    )
    assert len(outcomes) == 1 and outcomes[0].ok, outcomes
    assert outcomes[0].import_method != "hardlink"
    assert outcomes[0].destination.read_bytes() == ROM


@pytest.mark.parametrize("nested", [False, True])
def test_incomplete_multitrack_dat_game_never_published(tmp_path, nested):
    """One verified member must not stand in for a two-ROM DAT game."""
    source = _zip(tmp_path / "dl.zip", nested=nested)
    crc_first = f"{zlib.crc32(ROM) & 0xFFFFFFFF:08x}"
    crc_second = f"{zlib.crc32(b'B' * len(ROM)) & 0xFFFFFFFF:08x}"
    catalog = parse_dat(f"""
    <datafile><header><name>Nintendo - SNES</name></header>
      <game name="Super Metroid (USA)">
        <rom name="Super Metroid (USA).sfc" size="{len(ROM)}" crc="{crc_first}"/>
        <rom name="Super Metroid (Disc 2).sfc" size="{len(ROM)}" crc="{crc_second}"/>
      </game>
    </datafile>""")
    index = DatIndex()
    index.add(catalog)
    results = _import(tmp_path, source, dats=index)

    assert len(results) == 1 and not results[0].ok
    assert "SOURCE_INCOMPLETE" in results[0].reason
    assert not (tmp_path / "library").exists()
    assert source.exists()
