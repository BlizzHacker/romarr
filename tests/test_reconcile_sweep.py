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


def test_reconcile_keeps_sequels_and_series_games_wanted(tmp_path):
    """A verified game must not clear a wanted sequel or shorter series name."""
    cases = [
        ("Super Mario World", "Super Mario World 2: Yoshi's Island (USA)"),
        ("Golf", "Super Golf (USA)"),
    ]
    for index, (wanted, verified) in enumerate(cases):
        case_dir = tmp_path / str(index)
        case_dir.mkdir()
        dat = dat_for(verified)
        service = svc(case_dir, dat)
        place_verified(case_dir, "verified.sfc")
        service.store.want(wanted, "snes")

        result = service.reconcile_platform("snes")

        assert result["count"] == 0
        assert [(row.game, row.platform) for row in service.store.wanted] == [
            (wanted, "snes")]


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


def test_reconcile_streams_plain_headered_and_zip_without_whole_reads(tmp_path, monkeypatch):
    """reconcile_platform must stream via source.hash() instead of source.read()."""
    import zipfile
    from romarr import library as lib

    header = b"\x00" * 512
    smc_body = (b"snes-headered-rom-" + b"a" * 32768)[:32768]
    zip_body = b"zipped-snes-rom-" + b"b" * 1024

    def rom(game, fname, payload):
        return (
            "<game name=%s><rom name=%s size=%s sha1=%s/></game>"
            % (
                quoteattr(game),
                quoteattr(fname),
                quoteattr(str(len(payload))),
                quoteattr(hashlib.sha1(payload).hexdigest()),
            )
        )

    idx = DatIndex()
    idx.add(parse_dat(
        "<datafile><header><name>Test</name></header>"
        + rom("Alpha Game (USA)", "Alpha Game (USA).smc", smc_body)
        + rom("Beta Quest (USA)", "Beta Quest (USA).sfc", zip_body)
        + "</datafile>"
    ))

    s = svc(tmp_path, idx)
    folder = tmp_path / "roms" / "snes"
    folder.mkdir(parents=True)
    (folder / "alpha_renamed.smc").write_bytes(header + smc_body)
    with zipfile.ZipFile(folder / "beta_renamed.zip", "w") as zf:
        zf.writestr("Beta Quest (USA).sfc", zip_body)

    s.store.want("Alpha: Game", "snes")
    s.store.want("Beta: Quest", "snes")

    def forbid_read(self, name):
        raise AssertionError(f"reconcile_platform called .read({name!r}) instead of .hash()")

    monkeypatch.setattr(lib._PathSource, "read", forbid_read)
    monkeypatch.setattr(lib._ZipSource, "read", forbid_read)
    monkeypatch.setattr(lib._BsdtarSource, "read", forbid_read)

    result = s.reconcile_platform("snes")

    assert result["ok"] is True
    assert result["verified"] == 2
    assert result["count"] == 2
    assert s.store.wanted == []


def test_reconcile_skips_walk_when_no_dats_loaded(tmp_path, monkeypatch):
    """Empty or missing DatIndex short-circuits before touching library files."""
    from romarr import library as lib

    s = svc(tmp_path, DatIndex())
    place_verified(tmp_path, "Large Disc.iso")
    s.store.want("Some Game", "snes")

    def boom(*_args, **_kwargs):
        raise AssertionError("library files should not be opened when no DATs are loaded")

    monkeypatch.setattr(lib, "_source_for", boom)

    empty_res = s.reconcile_platform("snes")
    assert empty_res == {"ok": True, "platform": "snes", "verified": 0, "dropped": [], "count": 0}

    s.dats = None
    none_res = s.reconcile_platform("snes")
    assert none_res == {"ok": True, "platform": "snes", "verified": 0, "dropped": [], "count": 0}
    assert len(s.store.wanted) == 1


def test_reconcile_caches_hashes_and_updates_on_dat_or_file_change(tmp_path, monkeypatch):
    """Unchanged files reuse cached hashes across calls and DAT reloads; modified files rehash."""
    import os
    from romarr import library as lib

    game_a = "First Game (USA)"
    game_b = "Second Game (USA)"
    body_b = b"second-game-payload-" + b"z" * 1024

    s = svc(tmp_path, dat_for(game_a))
    place_verified(tmp_path, "disk_image.sfc")
    rom_path = tmp_path / "roms" / "snes" / "disk_image.sfc"

    hash_calls = []
    real_hash = lib._PathSource.hash

    def spy_hash(self, name, suffix=""):
        hash_calls.append(name)
        return real_hash(self, name, suffix=suffix)

    monkeypatch.setattr(lib._PathSource, "hash", spy_hash)

    # First walk computes hashes once.
    res1 = s.reconcile_platform("snes")
    assert res1["verified"] == 1
    assert len(hash_calls) == 1

    # Second walk with an aliased wanted title reuses cached hashes (0 new hashes).
    s.store.want("First: Game", "snes")
    res2 = s.reconcile_platform("snes")
    assert res2["count"] == 1
    assert s.store.wanted == []
    assert len(hash_calls) == 1

    # Updating file bytes & mtime invalidates the cache entry and re-hashes.
    rom_path.write_bytes(body_b)
    st = rom_path.stat()
    os.utime(rom_path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))
    res3 = s.reconcile_platform("snes")
    assert res3["verified"] == 0
    assert len(hash_calls) == 2

    # Loading a matching DAT afterwards still verifies against the cached hashes without rehashing.
    xml_b = (
        "<datafile><header><name>TestB</name></header>"
        "<game name=%s><rom name=%s size=%s sha1=%s/></game></datafile>"
        % (quoteattr(game_b), quoteattr(game_b + ".sfc"),
           quoteattr(str(len(body_b))), quoteattr(hashlib.sha1(body_b).hexdigest()))
    )
    dat_b = DatIndex()
    dat_b.add(parse_dat(xml_b))
    s.dats = dat_b
    s.store.want("Second: Game", "snes")

    res4 = s.reconcile_platform("snes")
    assert res4["verified"] == 1
    assert res4["count"] == 1
    assert s.store.wanted == []
    assert len(hash_calls) == 2


def test_reconcile_after_import_setting_persists_controls_sweep_and_ui(tmp_path):
    """reconcile_after_import is in DEFAULT_SETTINGS, persisted across reloads,
    gates post-import reconcile_platform, and is exposed in the Media UI."""
    from romarr.app import QueueItem
    from romarr.store import DEFAULT_SETTINGS, Store
    from romarr.ui import page

    assert DEFAULT_SETTINGS["reconcile_after_import"] is True
    assert "chk('reconcile_after_import'" in page()

    data_path = tmp_path / "s.json"
    s = ROMarr({
        "ROMARR_DATA": str(data_path),
        "LIBRARY_KIND": "folder",
        "LIBRARY_PATH": str(tmp_path / "roms"),
    })
    assert s.safe_settings()["reconcile_after_import"] is True

    updated = s.store.update_settings({"reconcile_after_import": False})
    assert updated["reconcile_after_import"] is False
    reloaded = Store(data_path)
    assert reloaded.settings["reconcile_after_import"] is False

    # When disabled, a completed import must not invoke reconcile_platform.
    cotton = b"cotton-rom-bytes-" + b"c" * 400
    other = b"other-game-bytes-" + b"o" * 400

    def rom(game, body):
        return (
            "<game name=%s><rom name=%s size=%s sha1=%s/></game>"
            % (quoteattr(game), quoteattr(game + ".sfc"),
               quoteattr(str(len(body))), quoteattr(hashlib.sha1(body).hexdigest()))
        )

    idx = DatIndex()
    idx.add(parse_dat(
        "<datafile><header><name>Test</name></header>"
        + rom("Fantastic Night Dreams - Cotton (Japan)", cotton)
        + rom("Other Game (USA)", other)
        + "</datafile>"
    ))
    s.dats = idx
    (tmp_path / "roms" / "snes").mkdir(parents=True)
    (tmp_path / "roms" / "snes" / "Cotton.smc").write_bytes(cotton)
    s.store.want("Cotton - Fantastic Night Dreams", "snes")

    class FakeClient:
        name = "fake"
        configured = True
        def __init__(self, rows): self._rows = rows
        def completed(self): return list(self._rows)

    down = tmp_path / "downloads"
    down.mkdir()
    f = down / "Other Game.smc"
    f.write_bytes(other)
    s.queue.append(QueueItem("Other Game", "snes", "Other Game.smc", 1, "grabbed"))
    s.clients = [FakeClient([{"name": "Other Game.smc", "content_path": str(f)}])]

    ok = [r for r in s.import_finished() if r.get("ok")]
    assert ok
    assert [(w.game, w.platform) for w in s.store.wanted] == [
        ("Cotton - Fantastic Night Dreams", "snes")
    ]


def test_reconcile_does_not_cache_when_member_hash_returns_none(tmp_path, monkeypatch):
    """If source.hash returns None for any member, keep it uncached so a later call retries."""
    from romarr import library as lib

    s = svc(tmp_path, dat_for("Retry Game (USA)"))
    place_verified(tmp_path, "retry.sfc")
    s.store.want("Retry Game", "snes")

    real_hash = lib._PathSource.hash
    attempts = {"n": 0}

    def flaky_hash(self, name, suffix=""):
        attempts["n"] += 1
        if attempts["n"] == 1:
            return None
        return real_hash(self, name, suffix=suffix)

    monkeypatch.setattr(lib._PathSource, "hash", flaky_hash)

    first = s.reconcile_platform("snes")
    assert first["verified"] == 0
    assert len(s.store.wanted) == 1

    second = s.reconcile_platform("snes")
    assert second["verified"] == 1
    assert second["count"] == 1
    assert s.store.wanted == []
    assert attempts["n"] == 2