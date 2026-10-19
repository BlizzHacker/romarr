from pathlib import Path

from romarr.collections import Policy, build_plan, explain, flags_in
from romarr.dat import parse_dat
from romarr.library import platform_dir
from romarr.platforms import by_slug
from test_romm_rescan import client, Resp


def data(names):
    return parse_dat('<datafile><header><name>test</name></header>' +
                     ''.join(f'<game name="{n}"><rom name="x" size="1" crc="00000001"/></game>' for n in names) +
                     '</datafile>')


def test_retail_wins_no_clone_region_tie_and_explanation_agrees():
    for marker in ("Debug", "Promo"):
        clean = "Game (USA) (Rev 1)"
        variant = f"Game (USA) ({marker})"
        dat = data([variant, clean])
        assert list(dat.one_game_one_rom(["USA"])) == [clean]
        plan = build_plan(dat, {}, Policy(regions=("usa",)))
        assert [t.name for t in plan.titles] == [clean]
        assert marker.lower() in flags_in(variant)
        _, lost = explain(dat, dat.games[clean], list(dat.games.values()), ("usa",))
        assert "retail" in lost[0][1]


def test_promo_only_game_is_preserved():
    dat = data(["Prize Cart (Japan) (Promo)"])
    assert list(dat.one_game_one_rom(["USA"])) == ["Prize Cart (Japan) (Promo)"]
    assert len(build_plan(dat, {}, Policy()).titles) == 1
    assert flags_in("Debug Adventures (USA)") == set()


def test_genesis_folders_use_romm_fs_slug():
    genesis = by_slug("genesis-slash-megadrive")
    assert platform_dir(Path("/library"), genesis) == Path("/library/genesis")
    assert platform_dir(Path("/library"), genesis.slug, layout="nested") == Path("/library/genesis/roms")


def test_genesis_rescan_translates_to_fs_slug():
    romm, session = client({"/api/tasks/scan": Resp(200)})
    assert romm.rescan("genesis-slash-megadrive")
    assert session.posts[0][1]["platform_fs_slugs"] == ["genesis"]
