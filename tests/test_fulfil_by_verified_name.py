"""Fulfilling the Wanted list by what an import *was* (hash-verified), not just its filename.

Issue #85. Finished downloads were only ever matched to the Wanted list by a
substring of the client-reported filename:

    w.game.lower() in name.lower()

A wanted title written with a colon (`Dragon Ball Z: Super Butouden`) never
matched a no-intro filename that writes a hyphen and regional suffix
(`Dragon Ball Z - Super Butouden (Japan)`) -- so the game landed in the
library, byte-verified against the DAT, yet stayed on the Missing tab forever.
The collection plan knew it was present; the Missing tab did not.

romarr hash-verifies every import (`verify_set`) and carries the canonical DAT
game name on the result (`outcome.verification.game`). These tests pin down
that a wanted title is now matched against that verified identity (and the
destination it was filed under) as well as the raw download name.
"""

from __future__ import annotations

import hashlib
from xml.sax.saxutils import quoteattr

from romarr.app import QueueItem, ROMarr
from romarr.dat import DatIndex, parse_dat

BODY = b"verified-rom-bytes-" + b"x" * 512


def dat_for(game_name: str, rom_name: str | None = None) -> DatIndex:
    rom_name = rom_name or game_name + ".sfc"
    xml = (
        "<datafile><header><name>Test</name></header>"
        "<game name=%s><rom name=%s size=%s sha1=%s/></game></datafile>"
        % (quoteattr(game_name), quoteattr(rom_name),
           quoteattr(str(len(BODY))), quoteattr(hashlib.sha1(BODY).hexdigest()))
    )
    index = DatIndex()
    index.add(parse_dat(xml))
    return index


class FakeClient:
    name = "fake"
    configured = True

    def __init__(self, rows):
        self._rows = rows

    def completed(self):
        return list(self._rows)


def svc(tmp_path, dat: DatIndex) -> ROMarr:
    s = ROMarr({"ROMARR_DATA": str(tmp_path / "s.json"),
                "LIBRARY_KIND": "folder",
                "LIBRARY_PATH": str(tmp_path / "roms")})
    s.dats = dat
    return s


def run_import(s: ROMarr, tmp_path, dat: DatIndex, *, wanted_title: str,
               wanted_platform: str, client_name: str,
               bytes_=BODY, other_wanted: tuple | None = None) -> None:
    """Add a wanted item, fake a finished download landing, run the import sweep.

    The queue row's `release` equals the client's download name so the sweep
    pairs them by the legacy title fallback (mirrors the existing test harness
    -- no hash/id on the fake download). `client_name` is the name the client
    reports; it doubles as the on-disk file, so give it a ROM extension for the
    platform (``.smc``/``.sfc`` for SNES) and make it deliberately *not* contain
    the wanted title where the point is that filename matching would fail.
    """
    download_dir = tmp_path / "downloads"
    download_dir.mkdir(exist_ok=True)
    path = download_dir / client_name
    path.write_bytes(bytes_)

    if other_wanted:
        s.store.want(*other_wanted)
    s.store.want(wanted_title, wanted_platform)
    s.queue.append(QueueItem(wanted_title, wanted_platform, client_name, 1,
                             "grabbed"))
    s.clients = [FakeClient([{"name": client_name, "content_path": str(path)}])]
    s.import_finished()


# --- the bug -----------------------------------------------------------------

def test_punctuation_colon_vs_dash_still_fulfilled(tmp_path):
    """Wanted `Dragon Ball Z: Super Butouden` (colon) fulfilled for a verified
    no-intro dump named `Dragon Ball Z - Super Butouden (Japan)` (dash)."""
    dat = dat_for("Dragon Ball Z - Super Butouden (Japan)")
    s = svc(tmp_path, dat)
    run_import(s, tmp_path, dat, wanted_title="Dragon Ball Z: Super Butouden",
               wanted_platform="snes",
               client_name="DBZ - Super Butouden JP.smc")
    assert s.store.wanted == []


def test_short_wanted_title_matches_full_verified_dat_name(tmp_path):
    """Wanted `King of Fighters R-2` (short IGDB/GG name) fulfilled for the
    verified full no-intro name `King of Fighters R-2 - Pocket Fighting Series
    (World) (En,Ja)`."""
    dat = dat_for("King of Fighters R-2 - Pocket Fighting Series (World) (En,Ja)")
    s = svc(tmp_path, dat)
    run_import(s, tmp_path, dat, wanted_title="King of Fighters R-2",
               wanted_platform="snes",
               client_name="KOF R2.smc")
    assert s.store.wanted == []


# --- regression: behaviour that must keep working ----------------------------

def test_exact_substring_match_still_fulfilled(tmp_path):
    """A wanted title that already substring-matches the download filename
    (Bust-A-Move case) keeps clearing -- the filename identity is retained."""
    dat = dat_for("Bust-A-Move Pocket (USA)")
    s = svc(tmp_path, dat)
    run_import(s, tmp_path, dat, wanted_title="Bust-A-Move Pocket",
               wanted_platform="snes",
               client_name="Bust-A-Move Pocket (USA).smc")
    assert s.store.wanted == []


def test_a_verified_import_does_not_fulfil_another_platform(tmp_path):
    """A wanted entry on a *different* platform must not be touched by a verified
    import landing on SNES -- the platform guard stays intact."""
    dat = dat_for("Dragon Ball Z - Super Butouden (Japan)")
    s = svc(tmp_path, dat)
    run_import(s, tmp_path, dat, wanted_title="Dragon Ball Z: Super Butouden",
               wanted_platform="gba",
               client_name="DBZ - Super Butouden JP.smc")
    assert len(s.store.wanted) == 1
    assert s.store.wanted[0].platform == "gba"


def test_word_order_change_needs_hash_identity_and_is_out_of_scope(tmp_path):
    """Wanted `Cotton - Fantastic Night Dreams` is the same game as the verified
    `Fantastic Night Dreams - Cotton (Japan)`, but word order is not fixable by
    substring matching. It stays wanted -- this documents the boundary; closing
    it needs a hash-to-wanted reconcile, deliberately not part of this fix."""
    dat = dat_for("Fantastic Night Dreams - Cotton (Japan)")
    s = svc(tmp_path, dat)
    run_import(s, tmp_path, dat, wanted_title="Cotton - Fantastic Night Dreams",
               wanted_platform="snes",
               client_name="Cotton variant.smc")
    assert len(s.store.wanted) == 1
    assert s.store.wanted[0].game == "Cotton - Fantastic Night Dreams"