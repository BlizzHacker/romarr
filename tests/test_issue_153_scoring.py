"""Regression tests for Issue #153: release scoring, title matching, size ceilings, and Hub scoring."""

from __future__ import annotations

import pytest

from romarr import hub
from romarr.platforms import PLATFORMS, by_slug, resolve
from romarr.profiles import ReleaseProfile
from romarr.selection import (
    Release,
    best_release,
    infer_platform,
    judge,
    score,
    title_matches,
)
from romarr.store import DEFAULT_SETTINGS
from romarr.ui import page

KB = 1024
MB = 1024 * KB
GB = 1024 * MB

NES = by_slug("nes")
SNES = by_slug("snes")
PSX = by_slug("psx")
PS2 = by_slug("ps2")
SWITCH = by_slug("switch")
WIN = by_slug("win")


def _usenet(title: str, size: int, *, categories: tuple[int, ...] = (1000,)) -> Release:
    """Build a synthetic Usenet release matching Prowlarr metadata."""
    return Release(
        title=title,
        size=size,
        seeders=0,
        categories=categories,
        download_url="http://prowlarr.local/1/download?id=1",
        protocol="usenet",
        indexer="UsenetIndexer",
    )


def _hub(title: str, size: int, *, plugin: str = "archive-org") -> Release:
    """Build a synthetic ROM Hub release without region or extension tags."""
    row = {
        "source_id": f"{plugin}:{title}",
        "title": title,
        "platform": "nes",
        "size_bytes": size,
        "url": "https://archive.org/details/example",
        "extra": {},
        "plugin": plugin,
    }
    return Release(
        title=title,
        size=size,
        seeders=0,
        categories=(1000,),
        download_url=hub.encode_result(row),
        protocol="hub",
        indexer=f"hub:{plugin}",
    )


def _torrent(
    title: str,
    size: int,
    *,
    seeders: int = 10,
    categories: tuple[int, ...] = (1000,),
    private: bool = False,
) -> Release:
    """Build a synthetic torrent release."""
    return Release(
        title=title,
        size=size,
        seeders=seeders,
        categories=categories,
        download_url="magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567",
        protocol="torrent",
        indexer="TestTracker",
        private=private,
    )


# ---------------------------------------------------------------------------
# Issue #153 exact reproducers
# ---------------------------------------------------------------------------


def test_issue_153_smb3_wrong_usenet_results_rejected_and_hub_picked():
    """All five wrong SMB3 Usenet results are refused; the Hub ROM wins."""
    profile = ReleaseProfile.from_settings(DEFAULT_SETTINGS, NES)
    wrong_releases = [
        _usenet("New Super Mario Bros 2 . The Next Levels (Ntsc) Scrubbed", 423 * MB),
        _usenet("New Super Mario Bros ntsc.nzb", 423 * MB),
        _usenet("New Super Mario Bros Remake (NTSC)", 428 * MB),
        _usenet("Puzzle & Dragons Z + Puzzle & Dragons: Super Mario Bros. Edition", int(1.12 * GB)),
        _usenet("Puzzle & Dragons Z + Puzzle & Dragons: Super Mario Bros. Edition (Nintendo)", int(1.12 * GB)),
        _usenet("New Super Mario Bros (Pal) 2009", int(1.99 * GB)),
    ]
    hub_rom = _hub("Super Mario Bros. 3", 610 * KB)

    for rel in wrong_releases:
        verdict = judge(rel, "Super Mario Bros. 3", NES, profile=profile)
        assert not verdict.accepted, f"Expected rejection for {rel.title!r}, got {verdict}"
        assert verdict.points < 0, f"Expected negative score for {rel.title!r}"

    hub_verdict = judge(hub_rom, "Super Mario Bros. 3", NES, profile=profile)
    assert hub_verdict.accepted, f"Expected Hub SMB3 acceptance, got {hub_verdict}"
    assert hub_verdict.points > 0
    assert hub_verdict.why(), "Accepted release must include human-readable reasons"

    picked = best_release([*wrong_releases, hub_rom], "Super Mario Bros. 3", NES, profile=profile)
    assert picked is hub_rom


def test_issue_153_collections_dat_name_accepts_hub_and_rejects_wrong_usenet():
    """No-Intro DAT titles with region suffixes still match clean Hub titles."""
    profile = ReleaseProfile.from_settings(DEFAULT_SETTINGS, NES)
    wrong = _usenet("New Super Mario Bros 2 . The Next Levels (Ntsc) Scrubbed", 423 * MB)
    hub_rom = _hub("Super Mario Bros. 3", 610 * KB)

    assert not judge(wrong, "Super Mario Bros. 3 (USA)", NES, profile=profile).accepted
    assert judge(hub_rom, "Super Mario Bros. 3 (USA)", NES, profile=profile).accepted
    assert best_release([wrong, hub_rom], "Super Mario Bros. 3 (USA)", NES, profile=profile) is hub_rom


def test_issue_153_megaman2_psx2psp_usenet_results_rejected():
    """Mega Man 8 / Battle and Chase PSX2PSP must never pass a Mega Man 2 NES search."""
    profile = ReleaseProfile.from_settings(DEFAULT_SETTINGS, NES)
    bad = [
        _usenet("Mega Man 8 PSX2PSP", 280 * MB),
        _usenet("Mega Man Battle and Chase PSX2PSP", 330 * MB),
    ]
    for rel in bad:
        verdict = judge(rel, "Mega Man 2", NES, profile=profile)
        assert not verdict.accepted, f"Expected rejection for {rel.title!r}, got {verdict}"
        assert verdict.points < 0


# ---------------------------------------------------------------------------
# Frozen real-world ranking suite
# ---------------------------------------------------------------------------


def test_frozen_smb3_nes_exact_ranking_order():
    """Assert exact descending rank order over a frozen real-world SMB3 candidate set."""
    profile = ReleaseProfile.from_settings(DEFAULT_SETTINGS, NES)
    verified_usa = _torrent("Super Mario Bros. 3 (USA) [!].nes", 384 * KB, seeders=8)
    usa_usenet = _usenet("Super Mario Bros. 3 (USA) (Rev 1).nes", 384 * KB)
    world_usenet = _usenet("Super Mario Bros. 3 (World)", 410 * KB)
    europe_usenet = _usenet("Super Mario Bros. 3 (Europe)", 410 * KB)
    hub_bare = _hub("Super Mario Bros. 3", 610 * KB)
    japan_usenet = _usenet("Super Mario Bros. 3 (Japan)", 384 * KB)
    wrong_nsmb2 = _usenet("New Super Mario Bros 2 . The Next Levels (Ntsc) Scrubbed", 423 * MB)
    wrong_crossover = _usenet("Puzzle & Dragons Z + Puzzle & Dragons: Super Mario Bros. Edition", int(1.12 * GB))
    wrong_compact_combo = _usenet("Super Mario Bros.+Duck Hunt (USA)", 1 * MB)
    wrong_smb1 = _usenet("Super Mario Bros (USA).nes", 40 * KB)

    pool = [
        wrong_smb1,
        japan_usenet,
        wrong_nsmb2,
        hub_bare,
        europe_usenet,
        world_usenet,
        wrong_compact_combo,
        usa_usenet,
        wrong_crossover,
        verified_usa,
    ]
    ranked = sorted(
        [(score(r, "Super Mario Bros. 3", NES, profile=profile), r) for r in pool],
        key=lambda pair: (-pair[0], pair[1].size),
    )
    assert [r for _, r in ranked[:6]] == [
        verified_usa,
        usa_usenet,
        world_usenet,
        europe_usenet,
        hub_bare,
        japan_usenet,
    ]
    assert all(s > 0 for s, _ in ranked[:6])
    assert all(s < 0 for s, _ in ranked[6:])
    assert best_release(pool, "Super Mario Bros. 3", NES, profile=profile) is verified_usa

    # Unmarked bare Usenet release without platform/region evidence cannot qualify on title alone,
    # whereas a platform-scoped Hub release is accepted; explicit higher-tier Europe+ext still beats Hub Japan.
    bare_usenet = _usenet("Super Mario Bros. 3", 610 * KB)
    europe_nes = _usenet("Super Mario Bros. 3 (Europe).nes", 410 * KB)
    hub_japan = _hub("Super Mario Bros. 3 (Japan)", 384 * KB)
    assert not judge(bare_usenet, "Super Mario Bros. 3", NES).accepted
    assert judge(bare_usenet, "Super Mario Bros. 3", NES).points == 0
    assert score(hub_bare, "Super Mario Bros. 3", NES) > score(bare_usenet, "Super Mario Bros. 3", NES)
    assert score(hub_japan, "Super Mario Bros. 3", NES) < score(europe_nes, "Super Mario Bros. 3", NES)

    # Compact "+" compilation, subtitle spin-off, and mismatched release years are refused by judge too.
    assert not judge(wrong_compact_combo, "Super Mario Bros", NES, profile=profile).accepted
    assert not judge(_usenet("The Legend of Zelda: A Link to the Past (USA)", 1 * MB), "The Legend of Zelda", NES, profile=profile).accepted
    assert not judge(_usenet("Doom (1993) (USA)", 10 * MB), "Doom (2016)", WIN).accepted


# ---------------------------------------------------------------------------
# Title matching edge cases
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("release_title", "wanted"),
    [
        ("Super Mario Bros. 3 (USA).nes", "Super Mario Bros. 3"),
        ("Super.Mario.Bros.3.USA.nzb", "Super Mario Bros. 3"),
        ("Super Mario Bros. 3", "Super Mario Bros. 3 (USA)"),
        ("The Legend of Zelda (USA).nes", "Legend of Zelda, The (USA)"),
        ("The Legend of Zelda (USA).nes", "legend of zelda"),
        ("Final Fantasy 3 (USA).sfc", "Final Fantasy III"),
        ("Final Fantasy V (Japan).sfc", "Final Fantasy 5"),
        ("Final Fantasy X (USA) PlayStation 2", "final fantasy x"),
        ("SNES - Super Metroid (USA).sfc", "Super Metroid"),
        ("[Nintendo Switch] Super Mario World (smw) [NSP][ENG]", "super mario world"),
        ("Ys (USA).sms", "Ys"),
        ("Mr.Do (USA).nes", "Mr. Do!"),
        ("Mr. Do! (USA).nes", "Mr.Do"),
        ("Super Metroid.smc (USA)", "Super Metroid"),
        ("Mario + Rabbids: Kingdom Battle (Switch)", "Mario + Rabbids Kingdom Battle"),
        ("Mario + Rabbids: Kingdom Battle (Switch)", "Mario Rabbids Kingdom Battle"),
        ("Doom (2016) (Repack)", "Doom (2016)"),
        ("Doom.2016.FitGirl.Repack", "Doom (2016)"),
        ("Legend of Zelda, The - A Link to the Past (USA).sfc", "The Legend of Zelda: A Link to the Past"),
        ("Super Mario Bros. 3 - NES (USA).nes", "Super Mario Bros. 3"),
        ("Disgaea 5 Complete+ (Switch)", "Disgaea 5 Complete"),
        ("Battle Heat - NEC PC-FX", "Battle Heat"),
    ],
)
def test_title_matches_accepts_valid_variants(release_title: str, wanted: str):
    """Valid regional/article/numeral/platform-prefixed titles must match."""
    assert title_matches(release_title, wanted), f"{release_title!r} should match {wanted!r}"


@pytest.mark.parametrize(
    ("release_title", "wanted"),
    [
        ("New Super Mario Bros 2 . The Next Levels (Ntsc) Scrubbed", "Super Mario Bros. 3"),
        ("New Super Mario Bros ntsc.nzb", "Super Mario Bros. 3"),
        ("New Super Mario Bros Remake (NTSC)", "Super Mario Bros. 3"),
        ("Puzzle & Dragons Z + Puzzle & Dragons: Super Mario Bros. Edition", "Super Mario Bros. 3"),
        ("New Super Mario Bros (Pal) 2009", "Super Mario Bros. 3"),
        ("New Super Mario Bros 2 . The Next Levels (Ntsc) Scrubbed", "Super Mario Bros"),
        ("New Super Mario Bros ntsc.nzb", "Super Mario Bros"),
        ("New Super Mario Bros Remake (NTSC)", "Super Mario Bros"),
        ("Puzzle & Dragons Z + Puzzle & Dragons: Super Mario Bros. Edition", "Super Mario Bros"),
        ("Super Mario Bros. + Duck Hunt (USA)", "Super Mario Bros"),
        ("Super Mario Bros.+Duck Hunt (USA)", "Super Mario Bros"),
        ("Super Mario Bros. 3 (USA)", "Super Mario Bros"),
        ("Super Mario Bros. 3: Mix (USA)", "Super Mario Bros. 3"),
        ("The Legend of Zelda: A Link to the Past (USA)", "The Legend of Zelda"),
        ("Legend of Zelda, The - Link's Awakening (USA)", "The Legend of Zelda"),
        ("Doom (1993) (USA)", "Doom (2016)"),
        ("Doom (2016) (Repack)", "Doom (1993)"),
        ("Doom (USA)", "Doom (2016)"),
        ("Mega Man 8 PSX2PSP", "Mega Man 2"),
        ("Mega Man Battle and Chase PSX2PSP", "Mega Man 2"),
        ("Final Fantasy X-2 (USA)", "Final Fantasy X"),
        ("Mega Man X (USA)", "Mega Man 10"),
        ("Mega Man X (USA)", "Mega Man"),
        ("Sonic 3D Blast (USA)", "Sonic 3"),
        ("Super Mario World 2: Yoshi's Island (USA)", "Super Mario World"),
        ("Doom 64 (USA)", "Doom"),
        ("Dr. Mario (USA)", "Mario"),
        ("Paper Mario (USA)", "Mario"),
        ("Nintendo World Championships 1990 (USA)", "World Championships 1990"),
        ("V-Rally 99 (USA)", "5-Rally 99"),
    ],
)
def test_title_matches_rejects_sequels_prefixes_and_crossovers(release_title: str, wanted: str):
    """Different installments, prefixed spin-offs, and crossovers must never match."""
    assert not title_matches(release_title, wanted), f"{release_title!r} must NOT match {wanted!r}"


# ---------------------------------------------------------------------------
# Per-platform size ceilings & settings migration
# ---------------------------------------------------------------------------


def test_default_settings_uses_platform_ceiling_and_legacy_8192_is_ignored():
    """DEFAULT_SETTINGS['max_size_mb'] is 0, and stored legacy 8192 does not override NES."""
    assert DEFAULT_SETTINGS["max_size_mb"] == 0

    legacy = {"max_size_mb": 8192, "platform_profiles": {"nes": {"max_size_mb": 8192}}}
    nes_prof = ReleaseProfile.from_settings(legacy, NES)
    ps2_prof = ReleaseProfile.from_settings(legacy, PS2)
    win_prof = ReleaseProfile.from_settings(legacy, WIN)

    assert nes_prof.max_size_mb is None
    assert ps2_prof.max_size_mb is None
    assert win_prof.max_size_mb is None

    # NES rejects > 8 MB even with every positive signal present.
    oversized_nes = _torrent("Super Mario Bros. 3 (USA) [!].nes", 20 * MB, seeders=50)
    assert score(oversized_nes, "Super Mario Bros. 3", NES, profile=nes_prof) < 0

    # PS2 and Switch are not falsely capped at legacy 8192 MB either.
    ps2_10gb = _torrent("Shadow of the Colossus (USA) PS2.iso", 10 * GB, seeders=10)
    assert score(ps2_10gb, "Shadow of the Colossus", PS2, profile=ps2_prof) > 0


def test_explicit_global_cap_cannot_raise_smaller_platform_ceiling():
    """An explicit global cap can tighten large consoles without widening NES past 8 MB."""
    profile_nes = ReleaseProfile.from_settings({"max_size_mb": 4096}, NES)
    oversized_nes = _torrent("Super Mario Bros. 3 (USA) [!].nes", 50 * MB, seeders=30)
    assert score(oversized_nes, "Super Mario Bros. 3", NES, profile=profile_nes) < 0

    profile_ps2 = ReleaseProfile.from_settings({"max_size_mb": 4096}, PS2)
    too_big_global = _torrent("Shadow of the Colossus (USA) PS2.iso", 6 * GB, seeders=10)
    assert score(too_big_global, "Shadow of the Colossus", PS2, profile=profile_ps2) < 0


def test_oversized_release_never_survives_via_bonuses():
    """Exceeding Platform.max_size is an outright refusal even with +240 in bonuses."""
    super_tagged = _torrent("Super Mario Bros. 3 (USA) [!].nes", 20 * MB, seeders=100)
    verdict = judge(super_tagged, "Super Mario Bros. 3", NES)
    assert not verdict.accepted
    assert verdict.points == -400
    assert "too big" in verdict.why()[0].lower()

    # Redump + extension + [!] + USA + 20+ seeders + title earns +240; old -200 penalty survived at +40.
    heavy_disc = _torrent("Silent Hill (USA) [!][Redump].chd", 4 * GB, seeders=50)
    disc_verdict = judge(heavy_disc, "Silent Hill", PSX)
    assert not disc_verdict.accepted
    assert disc_verdict.points == -400
    assert "too big" in disc_verdict.why()[0].lower()


def test_profiles_ui_does_not_seed_legacy_8192():
    """Release Profiles UI keeps inherited max visible while saving 0 unless edited."""
    html = page()
    assert "!==8192" in html
    assert "Math.min(platInfo.max_size_mb,rawGlobalMax):platInfo.max_size_mb" in html
    assert "maxSizeDirty" in html
    assert "(!isGlobal&&!maxSizeDirty&&!rawPlatMax)?0:inputMaxSz" in html


# ---------------------------------------------------------------------------
# Foreign platform markers, "nintendo" alias, & 48-platform invariant
# ---------------------------------------------------------------------------


def test_nintendo_word_does_not_award_nes_bonus_or_infer_nes():
    """Generic publisher word 'Nintendo' must never earn NES bonus or infer NES."""
    rel = _usenet("Example Game (Nintendo)", 500 * KB)
    verdict = judge(rel, "Example Game", NES)
    assert not verdict.accepted
    assert verdict.points == 0
    assert not any("Nintendo Entertainment System" in line for line in verdict.why())
    assert infer_platform(rel) is None


def test_unmarked_usenet_release_without_platform_evidence_is_not_accepted():
    """Bare indexer results without platform, region, or seeder evidence cannot win on title alone."""
    unmarked = _usenet("Tetris", 500 * KB)
    generic_nintendo = _usenet("Tetris (Nintendo)", 500 * KB)
    marked_name = _usenet("Tetris (NES)", 500 * KB)
    marked_ext = _usenet("Tetris.nes", 500 * KB)
    hub_tetris = _hub("Tetris", 500 * KB)

    for rel in (unmarked, generic_nintendo):
        verdict = judge(rel, "Tetris", NES)
        assert not verdict.accepted
        assert verdict.points == 0
        assert verdict.why()

    assert best_release([unmarked, generic_nintendo], "Tetris", NES) is None
    assert judge(marked_name, "Tetris", NES).accepted
    assert judge(marked_ext, "Tetris", NES).accepted
    assert judge(hub_tetris, "Tetris", NES).accepted
    assert best_release([unmarked, hub_tetris], "Tetris", NES) is hub_tetris


def test_foreign_platform_markers_catch_cross_system_ports_and_keep_pc_fx():
    """Cross-console markers reject reliably without breaking pc-fx or turbografx."""
    assert score(_usenet("Super Mario Bros. 3 (New Nintendo 3DS)", 1 * MB), "Super Mario Bros. 3", NES) < 0
    assert score(_usenet("Super Mario Bros. 3 (GBA)", 1 * MB), "Super Mario Bros. 3", NES) < 0
    assert score(_usenet("Super Mario Bros. 3 (SNES)", 1 * MB), "Super Mario Bros. 3", NES) < 0
    assert score(_usenet("Mega Man 2 PSX2PSP", 2 * MB), "Mega Man 2", NES) < 0

    tg16 = by_slug("turbografx16--1")
    pc_fx = by_slug("pc-fx")
    xbox360 = by_slug("xbox360")
    assert resolve("NEC PC-FX") == pc_fx
    assert score(_torrent("Bonk's Adventure [PC Engine].pce", 512 * KB), "Bonk's Adventure", tg16) > 0
    assert score(_torrent("Bonk's Adventure [PC]", 512 * KB), "Bonk's Adventure", tg16) < 0
    assert score(_torrent("Battle Heat (NEC PC-FX)", 400 * MB), "Battle Heat", pc_fx) > 0
    assert score(_usenet("Battle Heat - NEC PC-FX", 400 * MB), "Battle Heat", pc_fx) > 0
    assert score(_torrent("Battle Heat [PC]", 400 * MB), "Battle Heat", pc_fx) < 0
    assert score(_torrent("Geometry Wars [XBLA]", 200 * MB), "Geometry Wars", xbox360) > 0


@pytest.mark.parametrize("platform", PLATFORMS, ids=lambda p: p.slug)
def test_every_supported_platform_accepts_its_own_names_and_markers(platform):
    """Every one of the 48 platforms accepts releases naming its slug, name, aliases, or native markers."""
    sample_size = max(16 * MB, min(platform.max_size // 2, 100 * MB)) if platform.is_disc else min(platform.max_size // 2, 1 * MB)
    labels = {platform.name, platform.slug, *platform.aliases, *platform.native_markers}
    for label in sorted(labels):
        rel = _torrent(f"Crystal Quest ({label}) (USA)", sample_size, seeders=10)
        verdict = judge(rel, "Crystal Quest", platform)
        assert verdict.accepted, (
            f"Platform {platform.slug!r} rejected its own label {label!r}: {verdict.why()}"
        )


def test_disc_hub_release_without_extension_still_accepted():
    """A ROM Hub disc release scoped by platform succeeds even without filename extension in title."""
    ps2_hub = Release(
        title="Shadow of the Colossus",
        size=3 * GB,
        seeders=0,
        categories=(1000,),
        download_url=hub.encode_result({
            "source_id": "ia:sotc",
            "title": "Shadow of the Colossus",
            "platform": "ps2",
            "size_bytes": 3 * GB,
            "url": "https://archive.org/details/sotc",
            "extra": {},
            "plugin": "archive-org",
        }),
        protocol="hub",
        indexer="hub:archive-org",
    )
    verdict = judge(ps2_hub, "Shadow of the Colossus", PS2)
    assert verdict.accepted
    assert verdict.points > 0
    assert verdict.why()
