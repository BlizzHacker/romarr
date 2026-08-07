"""One download, several games.

From PR #7 by @m3, found deploying against qBittorrent 5.x and RomM: a
cartridge archive holding more than one ROM had all but the best-ranked game
silently dropped. `pick_rom_set` answers "which game is this", which is the
right question for a disc and the wrong one for a 3-in-1 collection.

The risk in the fix is everything that must *not* become several games: a cue
with its bins, a gdi with its tracks, an m3u playlist. Those are one game made
of several files, and the difference is the whole of this file.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from romarr.library import import_rom
from romarr.platforms import resolve
from romarr.selection import pick_all_rom_sets, pick_rom_set

SNES = resolve("SNES")
NES = resolve("NES")
PSX = resolve("PlayStation")
DC = resolve("Dreamcast")


def make_zip(path: Path, members: dict[str, bytes]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        for name, blob in members.items():
            zf.writestr(name, blob)
    return path


@pytest.fixture
def dirs(tmp_path):
    downloads, lib = tmp_path / "dl", tmp_path / "lib"
    downloads.mkdir()
    return downloads, lib


# --- the bug the PR fixes --------------------------------------------------

def test_every_game_in_a_multi_rom_archive_is_imported(dirs):
    downloads, lib = dirs
    make_zip(downloads / "3in1.zip", {
        "Super Mario Bros (USA).nes": b"\x4e\x45\x53\x1a" + b"a" * 2048,
        "Duck Hunt (USA).nes": b"\x4e\x45\x53\x1a" + b"b" * 2048,
        "Excitebike (USA).nes": b"\x4e\x45\x53\x1a" + b"c" * 2048,
    })

    results = import_rom(downloads / "3in1.zip", NES, lib)

    assert len(results) == 3
    assert all(r.ok for r in results), [r.reason for r in results]
    landed = sorted(p.name for p in (lib / NES.slug).iterdir())
    assert landed == ["Duck Hunt (USA).nes", "Excitebike (USA).nes",
                      "Super Mario Bros (USA).nes"]


def test_a_single_rom_archive_still_produces_exactly_one(dirs):
    downloads, lib = dirs
    make_zip(downloads / "one.zip", {"Chrono Trigger (USA).sfc": b"x" * 4096})
    results = import_rom(downloads / "one.zip", SNES, lib)
    assert len(results) == 1 and results[0].ok


# --- what must stay one game ----------------------------------------------

def test_a_cue_and_its_bin_are_one_game_not_two(dirs):
    downloads, lib = dirs
    make_zip(downloads / "d.zip", {
        "Game (USA).cue": b'FILE "Game (USA).bin" BINARY\n  TRACK 01 MODE2/2352\n',
        "Game (USA).bin": b"\x00" * 4096,
    })
    results = import_rom(downloads / "d.zip", PSX, lib)
    assert len(results) == 1, "the bin was imported as a second game"
    assert results[0].ok


def test_a_gdi_and_its_tracks_are_one_game(dirs):
    """The regression the shim caught while integrating PR #7.

    Extension priority was lost, so `track01.bin` -- a shorter name -- became
    the primary of its own set and the .gdi that owns it became a second one.
    A Dreamcast import produced two "games", one of which was a raw track.
    """
    downloads, lib = dirs
    make_zip(downloads / "dc.zip", {
        "102 Dalmatians.gdi": b"3\n1 0 4 2352 track01.bin 0\n"
                              b"2 600 0 2352 track02.raw 0\n"
                              b"3 45000 4 2352 track03.bin 0\n",
        "track01.bin": b"a" * 512,
        "track02.raw": b"b" * 512,
        "track03.bin": b"c" * 512,
    })
    sets = pick_all_rom_sets(
        ["102 Dalmatians.gdi", "track01.bin", "track02.raw", "track03.bin"],
        DC)
    assert len(sets) == 1, [s.primary for s in sets]
    assert sets[0].primary == "102 Dalmatians.gdi"

    results = import_rom(downloads / "dc.zip", DC, lib)
    assert len(results) == 1 and results[0].ok


def test_a_playlist_keeps_a_multi_disc_game_whole(dirs):
    downloads, lib = dirs
    make_zip(downloads / "ff7.zip", {
        "Final Fantasy VII (USA).m3u": b"FFVII (Disc 1).cue\nFFVII (Disc 2).cue\n",
        "FFVII (Disc 1).cue": b'FILE "FFVII (Disc 1).bin" BINARY\n',
        "FFVII (Disc 1).bin": b"a" * 1024,
        "FFVII (Disc 2).cue": b'FILE "FFVII (Disc 2).bin" BINARY\n',
        "FFVII (Disc 2).bin": b"b" * 1024,
    })
    results = import_rom(downloads / "ff7.zip", PSX, lib)
    assert len(results) == 1, "a playlist is one game"
    assert results[0].ok


# --- agreement between the two selectors -----------------------------------

@pytest.mark.parametrize("platform,names", [
    (SNES, ["Game (USA).sfc"]),
    (NES, ["A (USA).nes", "B (USA).nes"]),
    (PSX, ["Game (USA).cue", "Game (USA).bin"]),
    (DC, ["G.gdi", "track01.bin"]),
])
def test_the_first_set_is_what_the_single_picker_would_have_chosen(platform, names):
    """Otherwise the two disagree about which file is the game, and an import
    lands somewhere different depending on which path reached it."""
    one = pick_rom_set(names, platform)
    every = pick_all_rom_sets(names, platform)
    assert every, "the plural picker found nothing the singular one did"
    assert every[0].primary == one.primary
    assert set(every[0].members) == set(one.members)


def test_no_file_is_claimed_by_two_sets():
    names = ["A (USA).nes", "B (USA).nes", "C (USA).nes"]
    sets = pick_all_rom_sets(names, NES)
    seen: set[str] = set()
    for one in sets:
        assert not (seen & set(one.members)), "a file landed in two games"
        seen.update(one.members)


def test_an_archive_with_nothing_playable_yields_nothing():
    assert pick_all_rom_sets(["readme.txt", "cover.png"], SNES) == []
    assert pick_all_rom_sets([], SNES) == []


# --- partial failure -------------------------------------------------------

def test_one_game_already_present_does_not_stop_the_others(dirs):
    downloads, lib = dirs
    make_zip(downloads / "two.zip", {
        "A (USA).nes": b"\x4e\x45\x53\x1a" + b"a" * 1024,
        "B (USA).nes": b"\x4e\x45\x53\x1a" + b"b" * 1024,
    })
    (lib / NES.slug).mkdir(parents=True)
    (lib / NES.slug / "A (USA).nes").write_bytes(b"already here")

    results = import_rom(downloads / "two.zip", NES, lib)

    assert len(results) == 2
    ok = [r for r in results if r.ok]
    refused = [r for r in results if not r.ok]
    assert len(ok) == 1 and len(refused) == 1
    assert "already in the library" in refused[0].reason
    # The one that was already there is untouched, and the other arrived.
    assert (lib / NES.slug / "A (USA).nes").read_bytes() == b"already here"
    assert (lib / NES.slug / "B (USA).nes").exists()


def test_a_missing_download_reports_once_not_per_game(tmp_path):
    results = import_rom(tmp_path / "nope.zip", NES, tmp_path / "lib")
    assert len(results) == 1
    assert not results[0].ok
    assert "does not exist" in results[0].reason
