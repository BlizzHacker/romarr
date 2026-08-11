"""PC (Windows): DODI, FitGirl and the rest of Questarr's home turf."""

import zipfile

from romarr.library import import_rom
from romarr.platforms import by_slug, resolve
from romarr.selection import (Release, judge, pick_all_rom_sets, score)

WIN = by_slug("win")
SNES = by_slug("snes")


def rel(title, platform_hint=None, **kw):
    defaults = dict(size=40 << 30, seeders=25, categories=(4050,),
                    download_url="magnet:?xt=urn:btih:abc", protocol="torrent")
    defaults.update(kw)
    return Release(title=title, **defaults)


# -- resolution ---------------------------------------------------------------

def test_pc_resolves_from_the_names_stores_use():
    for name in ("PC", "windows", "Microsoft Windows", "PC (Windows)"):
        assert resolve(name).slug == "win", name


# -- scoring ------------------------------------------------------------------

def test_a_fitgirl_repack_is_the_normal_form_on_pc():
    """On every other platform 'repack' is a wrong grab; on PC it is how
    games ship, and FitGirl outranks an anonymous upload."""
    fitgirl = rel("Baldurs Gate 3 [FitGirl Repack]")
    anon = rel("Baldurs Gate 3")
    assert score(fitgirl, "baldurs gate 3", WIN) > \
        score(anon, "baldurs gate 3", WIN)


def test_scene_names_reject_on_snes_and_pass_on_pc():
    release = rel("Dynasty Warriors Origins-TENOKE", size=30 << 30)
    assert judge(release, "dynasty warriors origins", SNES).points <= -300
    assert judge(release, "dynasty warriors origins", WIN).accepted


def test_a_repack_still_rejects_on_a_cartridge_platform():
    release = rel("Chrono Trigger [FitGirl Repack]", size=100 << 20)
    assert score(release, "chrono trigger", SNES) < 0


def test_multi_language_is_a_feature_on_pc_not_a_penalty():
    multi = rel("Cyberpunk 2077 [MULTi18] [DODI Repack]")
    verdict = judge(multi, "cyberpunk 2077", WIN)
    assert verdict.accepted
    assert not any("not English" in why for why in verdict.why())


def test_pc_sizes_do_not_hit_a_cartridge_ceiling():
    big = rel("Baldurs Gate 3 [FitGirl Repack]", size=120 << 30)
    assert judge(big, "baldurs gate 3", WIN).accepted


def test_betas_and_trainers_are_still_junk_on_pc():
    assert score(rel("Baldurs Gate 3 Beta"), "baldurs gate 3", WIN) < \
        score(rel("Baldurs Gate 3"), "baldurs gate 3", WIN)


# -- the installer set --------------------------------------------------------

def test_a_repack_is_one_set_named_by_its_download(tmp_path):
    """Cherry-picking setup.exe imports an installer that cannot install,
    and naming the set 'setup' makes a library of nothing."""
    files = ["setup.exe", "fg-01.bin", "fg-02.bin", "MD5/qcheck.md5",
             "Verify BIN files.bat"]
    sets = pick_all_rom_sets(files, WIN)
    assert len(sets) == 1
    assert sets[0].primary == "setup.exe"
    assert set(sets[0].members) == set(files)

    download = tmp_path / "Baldurs Gate 3 [FitGirl Repack]"
    download.mkdir()
    for name in ("setup.exe", "fg-01.bin", "fg-02.bin"):
        (download / name).write_bytes(b"\x00" * 4096)
    library = tmp_path / "library"
    [result] = import_rom(download, WIN, library)
    assert result.ok
    assert result.destination == \
        library / "win" / "Baldurs Gate 3 [FitGirl Repack]"
    assert (result.destination / "fg-02.bin").exists()
