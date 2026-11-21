"""Per-game preferred release group (behaviour from RetroArr's ReleaseScorer, MIT)."""
import pytest

from romarr.platforms import resolve
from romarr.selection import (PREFERRED_GROUP_BONUS, Release, best_release,
                              clean_group_name, judge, names_group)
from romarr.store import Store


def _rel(title, **kw):
    kw.setdefault("seeders", 10)
    kw.setdefault("size", 2 * 1024 ** 3)
    kw.setdefault("protocol", "torrent")
    kw.setdefault("categories", (1000,))
    return Release(title=title, download_url="magnet:?xt=urn:btih:" + "a" * 40, **kw)


PS2 = resolve("ps2")


def test_names_group_is_a_whole_word_and_ignores_case():
    assert names_group("Okami.PS2.NTSC-Kamui", "kamui")
    assert names_group("Okami [PS2] (Kamui)", "Kamui")
    assert not names_group("Okami.PS2-KamuiX", "Kamui")
    assert not names_group("Batman.PS2-GRP", "bat")
    assert not names_group("Okami.PS2-Kamui", "")
    assert names_group("Okami.PS2-Team.Ego", "team.ego")


def test_the_preferred_group_wins_an_otherwise_equal_pair():
    a = _rel("Okami PS2 NTSC-Alpha")
    b = _rel("Okami PS2 NTSC-Kamui")
    plain = (judge(a, "Okami", PS2).points, judge(b, "Okami", PS2).points)
    assert plain[0] == plain[1]
    favoured = judge(b, "Okami", PS2, preferred_group="Kamui")
    assert favoured.points == plain[1] + PREFERRED_GROUP_BONUS
    assert any("preferred group (Kamui)" in why for why in favoured.why())
    assert best_release([a, b], "Okami", PS2, preferred_group="kamui") is b


def test_the_preference_never_rescues_a_refused_release():
    wrong_title = _rel("Shadow of the Colossus PS2-Kamui")
    assert judge(wrong_title, "Okami", PS2, preferred_group="Kamui").points < 0
    no_seeders = _rel("Okami PS2 NTSC-Kamui", seeders=0)
    assert judge(no_seeders, "Okami", PS2, preferred_group="Kamui").points < 0


def test_a_cartridge_release_by_the_preferred_group_is_not_docked_as_a_hack():
    snes = resolve("snes")
    credited = _rel("Super Metroid (USA) [Hacked by Kamui].sfc", size=4 * 1024 ** 2)
    plain = judge(credited, "Super Metroid", snes)
    favoured = judge(credited, "Super Metroid", snes, preferred_group="Kamui")
    assert favoured.points > plain.points


@pytest.mark.parametrize("raw, expected", [(None, ""), ("  Kamui ", "Kamui"), ("", "")])
def test_clean_group_name_trims(raw, expected):
    assert clean_group_name(raw) == expected


@pytest.mark.parametrize("raw", [12, "a" * 101, "Kamui\nX", "tab\there"])
def test_clean_group_name_refuses_what_it_cannot_store(raw):
    with pytest.raises(ValueError):
        clean_group_name(raw)


def test_store_keeps_the_group_per_wanted_entry(tmp_path):
    store = Store(tmp_path / "romarr.json")
    store.want("Okami", "ps2")
    assert store.preferred_group("Okami", "ps2") == ""
    assert store.set_preferred_group("okami", "ps2", "Kamui")
    assert store.preferred_group("OKAMI", "ps2") == "Kamui"
    assert not store.set_preferred_group("Okami", "ngc", "Kamui")
    again = Store(tmp_path / "romarr.json")
    assert again.preferred_group("Okami", "ps2") == "Kamui"
    assert again.set_preferred_group("Okami", "ps2", "")
    assert again.preferred_group("Okami", "ps2") == ""
