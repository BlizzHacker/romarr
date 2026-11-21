"""Demo and executable refusals, ported in behaviour from Gamarr."""

import pytest

from romarr.platforms import by_slug
from romarr.release_guards import demo_build, demo_refusal, executable_refusal
from romarr.selection import Release, judge


def rel(title, *, size=512 * 1024, cats=(1030,)):
    return Release(title=title, size=size, seeders=20, categories=cats,
                   download_url="magnet:?xt=urn:btih:abc", protocol="torrent")


@pytest.mark.parametrize("title", [
    "Demon's Souls (USA)",
    "Demolition Man (USA)",
    "Trial of Mana (USA)",
    "Demon Crest (USA)",
    "The Preview Show (USA)",
])
def test_games_whose_names_contain_demo_words_are_not_demos(title):
    assert demo_build(title) == ""
    assert demo_refusal(title, title.split(" (")[0]) == ""


@pytest.mark.parametrize("title", [
    "Super Mario 64 (USA) (Demo)",
    "Sonic Adventure (Japan) (Kiosk)",
    "Gran Turismo (USA) (Sample)",
    "Halo.Infinite.Kiosk.Build-GRP",
    "Some.Game.Demo.Disc.PS2-GRP",
    "Some Game Store Demo",
    "Some Game Trial Version",
    "Some Game Preview Build",
    "Doom Shareware",
    "Mega Man Legends 2 Demo",
    "Metroid.Prime.DEMO.MULTi5.NSW-GRP",
    "Zelda.Echoes.Trial.v1.0.eShop.NSW-GRP",
])
def test_demo_kiosk_and_trial_builds_are_named(title):
    assert demo_build(title)
    assert demo_refusal(title, "Some Game").startswith("a demo, kiosk or trial build")


def test_a_trailing_word_followed_by_title_words_is_not_a_demo():
    assert demo_build("Demo Derby Legends (USA)") == ""
    assert demo_build("Trial of the Dragon (Europe)") == ""


def test_an_all_caps_title_that_is_named_trial_is_kept_when_it_was_asked_for():
    assert demo_build("TRIAL.OF.MANA.NSW-GRP")
    assert demo_refusal("TRIAL.OF.MANA.NSW-GRP", "Trial of Mana") == ""


def test_asking_for_a_demo_disc_by_name_gets_one():
    title = "PlayStation Underground Demo Disc (USA)"
    assert demo_refusal(title, "PlayStation Underground Demo Disc") == ""


def test_judge_refuses_a_demo_and_keeps_demons_souls():
    ps3 = by_slug("ps3")
    real = judge(rel("Demon's Souls (USA)", size=5 * 1024 ** 3), "Demon's Souls", ps3)
    assert "demo" not in real.verdict
    snes = by_slug("snes")
    refused = judge(rel("Super Metroid (USA) (Demo)"), "Super Metroid", snes)
    assert refused.points <= -1000
    assert "demo" in refused.verdict


@pytest.mark.parametrize("title,slug", [
    ("Halo 3 PS3.iso.exe", "ps3"),
    ("Super Mario Bros (USA).nes.scr", "nes"),
    ("Cyberpunk 2077.mkv.exe", "win"),
    ("Final Fantasy VII (USA).bin.com", "psx"),
])
def test_disguised_executables_are_refused_on_every_platform(title, slug):
    assert "disguised" in executable_refusal(title, slug)


def test_executables_are_refused_on_consoles_and_allowed_on_pc():
    assert executable_refusal("Some Game Setup.exe", "snes")
    assert executable_refusal("Some Game Setup.exe", "win") == ""
    assert executable_refusal("Some.Game.v1.2.exe", "ps2")


@pytest.mark.parametrize("title", [
    "Super Mario Bros (USA).zip",
    "Some Game - SomeSite.com",
    "https://example.org/file.exe",
    "Halo 3 (USA)",
    "",
])
def test_ordinary_titles_are_not_executables(title):
    assert executable_refusal(title, "snes") == ""


def test_judge_refuses_disguised_executables_before_scoring():
    verdict = judge(rel("Halo 3 PS3.iso.exe", size=5 * 1024 ** 3), "Halo 3", by_slug("ps3"))
    assert verdict.points <= -1500
    assert "disguised" in verdict.verdict
    nes = judge(rel("Super Mario Bros (USA).nes.scr"), "Super Mario Bros", by_slug("nes"))
    assert nes.points <= -1500
