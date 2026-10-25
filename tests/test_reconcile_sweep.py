"""Reconcile sweep: Wanted entries whose verified dump is already in the
library are fulfilled even when no new import fires and the title is aliased.

Closes the gaps that #86 deliberately leaves open: a wanted title written with
a colon, a short IGDB/GG name, or a different word order (`Cotton - Fantastic
Night Dreams` vs `Fantastic Night Dreams - Cotton (Japan)`) stays on the
Missing tab because `fulfil` only runs on a completed import. The sweep walks
the platform's library dir, hash-verifies each file against the DATs, and
fulfils any same-platform wanted title that maps to a verified dump.
"""

from __future__ import annotations

import hashlib
from xml.sax.saxutils import quoteattr

from romarr.app import ROMarr
from romarr.dat import DatIndex, parse_dat

BODY = b"verified-rom-bytes-" + b"y" * 512


def dat_for(game_name: str) -> DatIndex:
    xml = (
        "<datafile><header><name>Test</name></header>"
        "<game name=%s><rom name=%s size=%s sha1=%s/></game></datafile>"
        % (quoteattr(game_name), quoteattr(game_name + ".sfc"),
           quoteattr(str(len(BODY))), quoteattr(hashlib.sha1(BODY).hexdigest()))
    )
    index = DatIndex()
    index.add(parse_dat(xml))
    return index


def svc(tmp_path, dat: DatIndex) -> ROMarr:
    s = ROMarr({"ROMARR_DATA": str(tmp_path / "s.json"),
                "LIBRARY_KIND": "folder",
                "LIBRARY_PATH": str(tmp_path / "roms")})
    s.dats = dat
    return s


def place_verified(tmp_path, filename: str) -> None:
    """Put a byte-verified dump on disk under the SNES folder."""
    folder = tmp_path / "roms" / "snes"
    folder.mkdir(parents=True)
    (folder / filename).write_bytes(BODY)


def test_stale_short_title_reconciled_when_verified_dump_present(tmp_path):
    """A short IGDB/GG title for a dump that is already verified on disk (but
    was never fulfilled by an import sweep) is dropped by the reconcile."""
    dat = dat_for("King of Fighters R-2 - Pocket Fighting Series (World) (En,Ja)")
    s = svc(tmp_path, dat)
    place_verified(tmp_path, "KOF R-2 scrub.smc")
    s.store.want("King of Fighters R-2", "snes")

    result = s.reconcile_platform("snes")

    assert result["ok"] is True
    assert result["count"] == 1
    assert s.store.wanted == []


def test_word_order_alias_reconciled(tmp_path):
    """Wanted `Cotton - Fantastic Night Dreams` is the same game as the verified
    `Fantastic Night Dreams - Cotton (Japan)` on disk. Substring cannot relate
    them (the #86 boundary); the token-overlap fallback in the sweep can."""
    dat = dat_for("Fantastic Night Dreams - Cotton (Japan)")
    s = svc(tmp_path, dat)
    place_verified(tmp_path, "Cotton variant.smc")
    s.store.want("Cotton - Fantastic Night Dreams", "snes")

    result = s.reconcile_platform("snes")

    assert result["count"] == 1
    assert s.store.wanted == []


def test_colon_vs_dash_alias_reconciled(tmp_path):
    dat = dat_for("Dragon Ball Z - Super Butouden (Japan)")
    s = svc(tmp_path, dat)
    place_verified(tmp_path, "DBZ Super Butouden JP.smc")
    s.store.want("Dragon Ball Z: Super Butouden", "snes")

    assert s.reconcile_platform("snes")["count"] == 1
    assert s.store.wanted == []


def test_reconcile_does_not_touch_another_platform(tmp_path):
    dat = dat_for("Fantastic Night Dreams - Cotton (Japan)")
    s = svc(tmp_path, dat)
    place_verified(tmp_path, "Cotton variant.smc")  # lands on SNES
    s.store.want("Cotton - Fantastic Night Dreams", "gba")

    assert s.reconcile_platform("snes")["count"] == 0
    assert len(s.store.wanted) == 1
    assert s.store.wanted[0].platform == "gba"


def test_reconcile_does_not_drop_a_title_with_no_verified_dump(tmp_path):
    """A wanted title for a game that is NOT the verified file on disk must not
    be dropped -- guards against token-overlap false positives, and against a
    whole-word superstring match (Sonic 3 vs Sonic 3D Blast)."""
    dat = dat_for("Bust-A-Move Pocket (USA)")  # the file on disk is this game
    s = svc(tmp_path, dat)
    place_verified(tmp_path, "Bust-A-Move Pocket (USA).smc")
    s.store.want("Bust-A-Move 2", "snes")

    result = s.reconcile_platform("snes")

    assert result["count"] == 0
    assert [w.game for w in s.store.wanted] == ["Bust-A-Move 2"]


def test_reconcile_is_auto_invoked_after_a_verified_import(tmp_path):
    """With reconcile_after_import on (the default), a verified import on a
    platform also clears wanted aliases that were already present on disk --
    without needing a second download of that title."""
    from romarr.app import QueueItem

    class FakeClient:
        name = "fake"
        configured = True
        def __init__(self, rows): self._rows = rows
        def completed(self): return list(self._rows)

    cotton = b"cotton-rom-bytes-" + b"c" * 400
    other = b"other-game-bytes-" + b"o" * 400

    def rom(game, body):
        return ("<game name=%s><rom name=%s size=%s sha1=%s/></game>"
                % (quoteattr(game), quoteattr(game + ".sfc"),
                   quoteattr(str(len(body))),
                   quoteattr(hashlib.sha1(body).hexdigest())))

    idx = DatIndex()
    idx.add(parse_dat("<datafile><header><name>Test</name></header>"
                      + rom("Fantastic Night Dreams - Cotton (Japan)", cotton)
                      + rom("Other Game (USA)", other) + "</datafile>"))

    s = svc(tmp_path, idx)
    (tmp_path / "roms" / "snes").mkdir(parents=True)
    # An alias-wanted game whose verified dump is already on disk...
    (tmp_path / "roms" / "snes" / "Cotton.smc").write_bytes(cotton)
    s.store.want("Cotton - Fantastic Night Dreams", "snes")
    before = len(s.store.wanted)

    # ...gets cleared by the per-platform reconcile that a *different* verified
    # SNES import triggers.
    down = tmp_path / "downloads"
    down.mkdir()
    f = down / "Other Game.smc"
    f.write_bytes(other)
    s.queue.append(QueueItem("Other Game", "snes", "Other Game.smc", 1, "grabbed"))
    s.clients = [FakeClient([{"name": "Other Game.smc", "content_path": str(f)}])]

    ok = [r for r in s.import_finished() if r.get("ok")]
    assert ok, "the SNES import did not succeed"
    assert before == 1 and s.store.wanted == []