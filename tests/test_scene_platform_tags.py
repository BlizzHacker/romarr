"""Scene release tags NSW (Switch) and PSV (Vita) name a foreign platform.

Switch releases tagged ``NSW-<group>`` and Vita releases tagged ``PSV-<group>``
often never spell the console out. Without those tags as markers, a GameCube
search for Paper Mario accepted the Switch remake's 28 MB update patch, and a
PSP search for Lumines accepted a Vita release. Every title, size and seeder
count below is a real indexer row, sizes rounded to MB.
"""

from __future__ import annotations

import pytest

from romarr.platforms import by_slug
from romarr.selection import Release, best_release, judge

MB = 1024 * 1024

GAMECUBE = by_slug("ngc")
PSP = by_slug("psp")
SWITCH = by_slug("switch")
SWITCH_2 = by_slug("switch-2")
VITA = by_slug("psvita")

PAPER_MARIO = "Paper Mario The Thousand-Year Door"

#: The whole result of one GameCube search for Paper Mario: (title, MB, seeders).
PAPER_MARIO_ROWS = (
    ("Paper Mario The Thousand Year Door USA NGC-STARCUBE", 310, 6),
    ("Paper Mario The Thousand-Year Door Update v1 0 1 NSW-SUXXORS", 28, 105),
    ("Paper Mario The Thousand-Year Door NSW-HR", 5540, 220),
    ("Paper Mario The Origami King NSW-VENOM", 6943, 104),
    ("Paper Mario The Origami King Update v1 0 1 NSW-VENOM", 29, 84),
    ("Paper Mario MULTi4 N64 VC WiiU-TRSI", 33, 5),
    ("Super Paper Mario v 1 1 PAL MULTI5 Wii DNi", 4314, 4),
    ("Super Paper Mario PAL MULTi5 Wii-BAHAMUT", 4314, 5),
    ("Paper Mario - The Thousand-Year Door [FitGirl Repack]", 4239, 8),
    ("Paper Mario The Origami King v1 01 NSW for PC REPACK-KaOs", 6675, 6),
    ("Paper Mario The Thousand-Year Door PAL MULTi5 NGC WORKING iNTERNAL For Wii-LaKiTu", 1098, 4),
    ("Super Paper Mario USA Wii-QwiiF", 4354, 12),
)


def _torrent(title: str, size_mb: int, seeders: int) -> Release:
    """A torrent row as Prowlarr hands it over, in the console category."""
    return Release(title=title, size=size_mb * MB, seeders=seeders, categories=(1000,),
                   download_url="magnet:?xt=urn:btih:fixture", protocol="torrent",
                   indexer="Fixture")


@pytest.mark.parametrize("title, size_mb, seeders, wanted, platform", [
    ("Paper Mario The Thousand-Year Door Update v1 0 1 NSW-SUXXORS", 28, 105,
     PAPER_MARIO, GAMECUBE),
    ("Lumines Electronic Symphony USA READNFO PSV-BigBlueBox", 649, 1, "Lumines", PSP),
])
def test_scene_tag_for_another_console_disqualifies(title, size_mb, seeders, wanted, platform):
    """A Switch or Vita tag rejects the row for a GameCube or PSP search."""
    assert judge(_torrent(title, size_mb, seeders), wanted, platform).points < 0


@pytest.mark.parametrize("title, size_mb, seeders, wanted, platform", [
    ("Paper Mario The Thousand-Year Door NSW-HR", 5540, 220, PAPER_MARIO, SWITCH),
    ("Lumines Electronic Symphony USA READNFO PSV-BigBlueBox", 649, 1,
     "Lumines Electronic Symphony", VITA),
])
def test_scene_tag_for_the_requested_console_is_accepted(title, size_mb, seeders, wanted, platform):
    """The same tags count as evidence for their own console."""
    assert judge(_torrent(title, size_mb, seeders), wanted, platform).points > 0


def test_a_switch_2_search_rejects_nsw_like_a_bare_switch():
    """A Switch 2 search rejects NSW releases, as it already rejects a bare "switch"."""
    row = _torrent("Metroid Prime 4 Beyond NSW-HR", 26444, 291)
    assert judge(row, "Metroid Prime 4 Beyond", SWITCH_2).points < 0


def test_the_gamecube_search_keeps_only_the_gamecube_release():
    """Of the whole Paper Mario result set, only the GameCube dump is usable."""
    rows = [_torrent(*row) for row in PAPER_MARIO_ROWS]
    usable = [r.title for r in rows if judge(r, PAPER_MARIO, GAMECUBE).points > 0]
    assert usable == ["Paper Mario The Thousand Year Door USA NGC-STARCUBE"]
    assert best_release(rows, PAPER_MARIO, GAMECUBE).title == usable[0]
