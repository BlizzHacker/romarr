"""Which browser player opens which file, and what it says when none does.

Every assertion here is either a capability read off the player's own
documentation or source, or a refusal. The refusals outnumber the capabilities
on purpose: this model exists because "plays in the browser on EmulatorJS" was
being said about Flash files, about DOS zips with three other options, and
about 94,428 rows with no file behind them at all.
"""

from __future__ import annotations

import pytest

from romarr import playability
from romarr.app import ROMarr
from romarr.playability import (
    ARCHIVE, DOWNLOAD, EMULARITY, EMULATORJS, JSDOS, LOCAL, PLAYERS, RUFFLE,
    PlayerPolicy, routes_for, routes_for_file)


def svc(tmp_path, **env):
    return ROMarr({"ROMARR_DATA": str(tmp_path / "s.json"), **env})


def detail(got, player):
    """The detail string a player contributed, from routes or alternatives."""
    for route in got.routes + got.alternatives:
        if route.player == player:
            return route.detail
    return ""


# --- the extension is the question ----------------------------------------

@pytest.mark.parametrize("given", ["swf", ".swf", "Game.swf", "GAME (US).SWF",
                                   "Grow Cube__1a2b3c4d.swf"])
def test_an_extension_is_read_from_whatever_shape_it_arrives_in(given):
    """RomM reports `fs_extension` with no dot; a person pastes a filename.

    Getting this wrong answers "no player opens this" for the whole library
    and does not announce itself, which is why it is pinned per shape.
    """
    assert playability._extension(given) == ".swf"


def test_no_extension_falls_back_to_the_platform_question():
    """A backend that does not report extensions must not read as unplayable."""
    got = routes_for_file("", "snes")
    assert LOCAL in got.kinds


# --- Ruffle ---------------------------------------------------------------

def test_a_swf_on_the_browser_platform_plays_on_ruffle():
    got = routes_for_file("game.swf", "browser")
    assert got.players == (RUFFLE,)
    assert "Ruffle" in detail(got, RUFFLE)


def test_ruffle_states_its_own_coverage_rather_than_promising():
    """Ruffle publishes AVM 1 at 99%/82% and AVM 2 at 90%/82%, and a SWF
    cannot be told apart from a filename. So the route is offered for any SWF
    and guaranteed for none, in their numbers rather than ours."""
    said = detail(routes_for_file("game.swf", "browser"), RUFFLE)
    assert "AVM 1" in said and "AVM 2" in said


def test_a_flash_projector_exe_is_not_a_swf_and_ruffle_says_so():
    """The refusal that saves the most time.

    A projector is a Windows executable with the movie appended to a player
    stub: Flash icon, opens in a file manager, and not a SWF. Ruffle loads SWF
    bytes and has no projector reader -- asked for upstream since 2021
    (ruffle-rs/ruffle#2279) and still open (#11539).
    """
    got = routes_for_file("game.exe", "browser")
    assert RUFFLE not in got.players
    said = detail(got, RUFFLE)
    assert "projector" in said and "11539" in said


@pytest.mark.parametrize("name,word", [
    ("game.dcr", "Shockwave"),
    ("game.dir", "Director"),
    ("applet.jar", "Java"),
    ("game.unity3d", "Unity"),
    ("app.xap", "Silverlight"),
])
def test_the_other_things_in_a_flash_archive_are_not_flash(name, word):
    """Flashpoint is not a Flash archive; it is a *web game* archive, and
    Shockwave, Unity, Silverlight and Java applets are all in it. Ruffle
    implements none of those runtimes and must not be offered for them."""
    got = routes_for_file(name, "browser")
    assert RUFFLE not in got.players
    assert word in detail(got, RUFFLE)


def test_a_swf_filed_off_the_flash_platforms_names_the_library_servers_gate():
    """RomM gates Ruffle on the platform slug and never looks at the file
    (`isRuffleEmulationSupported` returns `["flash","browser"].includes(slug)`).

    So Ruffle would run this and no Play button will appear. Saying only the
    first half would send somebody hunting for a control that is not there.
    """
    got = routes_for_file("game.swf", "snes")
    assert RUFFLE not in got.players
    said = detail(got, RUFFLE)
    assert "browser" in said and "snes" in said


def test_emulatorjs_is_not_offered_for_flash():
    """EmulatorJS has never run a Flash file. It was still the only name the
    `local` tier had, which is how a library that is 43% SWF got told it had
    no browser player."""
    got = routes_for_file("game.swf", "browser")
    assert EMULATORJS not in got.players


# --- EmulatorJS -----------------------------------------------------------

def test_a_core_is_named_by_the_extension_it_declares():
    got = routes_for_file("mario.sfc", "snes")
    assert got.players[0] == EMULATORJS
    assert "snes9x" in detail(got, EMULATORJS)


def test_an_extension_no_core_declares_is_refused_with_the_core_list():
    """"no browser core for this platform" is false for a platform that has
    five of them. The useful sentence names the cores and the extension."""
    got = routes_for_file("game.chd", "arcade")
    assert EMULATORJS not in got.players
    said = detail(got, EMULATORJS)
    assert ".chd" in said and "mame2003" in said


@pytest.mark.parametrize("ext", [".zip", ".7z", ".rar"])
def test_emulatorjs_unpacks_archives_itself(ext):
    """Read off `data/src/compression.js` in the 4.2.3 build RomM ships: it
    sniffs `PK`, `7z\\xBC\\xAF` and `Rar!` and loads the matching extractor.

    "EmulatorJS cannot open 7z" is a real bug report
    (linuxserver/docker-emulatorjs#45) and it is stale. Believing it would
    make ROMarr refuse a file that plays.
    """
    got = routes_for_file(f"game{ext}", "nes")
    assert EMULATORJS in got.players


def test_the_threaded_cores_name_the_header_they_need():
    """dosbox_pure, ppsspp and azahar need SharedArrayBuffer, so the page has
    to be cross-origin isolated. It fails as a black canvas and no error,
    which is not a thing anybody diagnoses from a blank screen."""
    for name, slug in (("game.zip", "dos"), ("game.iso", "psp")):
        said = detail(routes_for_file(name, slug), EMULATORJS)
        assert "COOP" in said and "COEP" in said, slug


def test_a_platform_with_no_core_at_all_still_says_the_old_thing():
    """The reason `NO_EJS_CORE` exists has not changed."""
    got = routes_for_file("game.iso", "ps2")
    assert got.kinds == (DOWNLOAD,)
    assert "PCSX2" in got.routes[0].detail


# --- DOS: three players, which is the whole point -------------------------

def test_a_dos_zip_has_three_players_and_only_one_is_installed():
    """The case the alternatives field exists for. EmulatorJS's dosbox_pure
    is already on the library server; js-dos and Emularity are real, better
    for some jobs, and nobody has stood one up -- so they are named with the
    setting that turns each into a link."""
    got = routes_for_file("dune2.zip", "dos")
    assert got.players == (EMULATORJS,)
    offered = {r.player for r in got.alternatives}
    assert {JSDOS, EMULARITY} <= offered
    assert "ROMARR_JSDOS_URL" in detail(got, JSDOS)
    assert "ROMARR_EMULARITY_URL" in detail(got, EMULARITY)


def test_configuring_js_dos_turns_the_alternative_into_a_route():
    policy = PlayerPolicy(urls={JSDOS: "https://dos.test/"})
    got = routes_for_file("dune2.zip", "dos", players=policy)
    assert JSDOS in got.players
    assert "https://dos.test" in detail(got, JSDOS)


def test_the_operators_order_wins_over_the_tables():
    """"Best" depends on what somebody runs, and this file cannot know that.
    An operator with a tuned js-dos in front of their DOS library wants it
    first, and nothing here is in a position to argue."""
    policy = PlayerPolicy(order=[JSDOS, EMULATORJS, RUFFLE, EMULARITY],
                          urls={JSDOS: "https://dos.test"})
    got = routes_for_file("dune2.zip", "dos", players=policy)
    assert got.players[0] == JSDOS


def test_js_dos_is_not_offered_for_things_that_are_not_dos():
    for name, slug in (("mario.sfc", "snes"), ("game.swf", "browser"),
                       ("romset.zip", "arcade")):
        assert JSDOS not in routes_for_file(name, slug).players, slug


# --- Emularity ------------------------------------------------------------

def test_the_archive_route_belongs_to_emularity():
    """Archive.org's in-page emulator *is* Emularity. Naming the route
    without naming the player is how it ended up being the one route with no
    switch on it."""
    got = routes_for("snes")
    archive = next(r for r in got.routes if r.kind == ARCHIVE)
    assert archive.player == EMULARITY


def test_turning_emularity_off_removes_the_archive_route():
    """The owner's actual request, as a test."""
    without = PlayerPolicy(order=[EMULATORJS, RUFFLE, JSDOS])
    got = routes_for("snes", players=without)
    assert ARCHIVE not in got.kinds
    assert LOCAL in got.kinds, "turning one player off must not cost another"


def test_a_disabled_player_still_explains_itself():
    """Silence would read as "nothing can play this", which is a different
    and much worse answer than "you turned the thing off"."""
    without = PlayerPolicy(order=[EMULATORJS])
    got = routes_for_file("game.swf", "browser", players=without)
    assert not got.plays_without_downloading
    assert "turned off" in detail(got, RUFFLE)
    assert "ROMARR_PLAYERS" in got.summary()


def test_emularity_does_not_claim_the_disc_systems():
    """Measured against Archive.org's own `emulator` field: psj/psu/pse 0
    items, saturn 0, 3do 0. They are a *source* for disc images, not a
    player of them."""
    for slug in ("psx", "saturn", "3do"):
        assert ARCHIVE not in routes_for(slug).kinds, slug


def test_a_self_hosted_emularity_is_a_local_route_not_an_archive_one():
    policy = PlayerPolicy(urls={EMULARITY: "https://emularity.test"})
    got = routes_for_file("dune2.zip", "dos", players=policy)
    local = [r for r in got.routes if r.player == EMULARITY]
    assert local and local[0].kind == LOCAL
    assert "EM-DOSBOX" in local[0].detail


# --- the 94,428 rows with nothing behind them -----------------------------

def test_a_row_with_no_file_plays_on_nothing_and_says_why():
    """The single most misleading answer this module used to give.

    A catalogued row has no bytes: RomM says `missing_from_fs` and asking for
    the content returns 404. "download only" named a route that 404s and hid
    the real answer, which is that ROMarr has not fetched the game yet.
    """
    got = routes_for_file("game.swf", "browser", present=False)
    assert got.routes == ()
    assert DOWNLOAD not in got.kinds
    assert "no file on the library server" in got.absent


def test_a_missing_file_still_says_what_would_play_it():
    """The actionable half: fetch this and Ruffle runs it."""
    got = routes_for_file("game.swf", "browser", present=False)
    assert RUFFLE in {r.player for r in got.alternatives}


def test_a_missing_file_keeps_the_archive_route_because_it_is_someone_elses_copy():
    """Archive.org plays *their* copy, so it is the one route a row with no
    local bytes can still have -- and the reason a catalogued row was
    catalogued in the first place."""
    got = routes_for_file("mario.nes", "nes", present=False)
    assert ARCHIVE in got.kinds
    assert got.plays_without_downloading


def test_missing_and_unsupported_are_different_answers():
    absent = routes_for_file("game.swf", "browser", present=False)
    unsupported = routes_for_file("game.dcr", "browser", present=True)
    assert absent.absent and not unsupported.absent
    assert DOWNLOAD in unsupported.kinds, "the file is here; take it away"


# --- nothing about the platform answer changed ----------------------------

def test_the_platform_answer_is_word_for_word_what_it_was():
    """`routes_for` feeds the Platforms page and the status counts. Adding
    players was not licence to churn a string thousands of installs read."""
    got = routes_for("psx")
    assert got.routes[0].detail == (
        "plays in the browser on EmulatorJS (pcsx_rearmed, mednafen_psx_hw)")


def test_every_platform_still_has_a_route():
    from romarr.platforms import PLATFORMS
    for platform in PLATFORMS:
        assert routes_for(platform).routes, platform.slug


# --- the policy -----------------------------------------------------------

def test_an_unset_variable_means_every_player_not_none():
    """Environment variables get blanked by accident. An install that
    silently lost every play route would look like a bug in ROMarr."""
    assert PlayerPolicy.from_env({}).order == playability.DEFAULT_PLAYER_ORDER
    assert PlayerPolicy.from_env({"ROMARR_PLAYERS": ""}).order == \
        playability.DEFAULT_PLAYER_ORDER


def test_turning_everything_off_has_to_be_typed_on_purpose():
    assert PlayerPolicy.from_env({"ROMARR_PLAYERS": "none"}).order == ()


def test_an_unknown_player_is_dropped_and_the_rest_survive():
    policy = PlayerPolicy(order=["ruffle", "dosbox-x", "emulatorjs"])
    assert policy.order == (RUFFLE, EMULATORJS)


def test_every_player_says_what_it_cannot_do():
    """The second sentence is the useful one, so it is not optional."""
    for player in PLAYERS.values():
        assert player.cannot, player.key
        assert player.repo and player.site, player.key


def test_enabled_is_not_reachable():
    """js-dos with no URL is a capability, not a link. ROMarr must not print
    a Play button it has nowhere to point."""
    policy = PlayerPolicy()
    assert policy.enabled(JSDOS)
    assert not policy.reachable(JSDOS)
    assert policy.reachable(EMULATORJS), "the library server serves this one"


# --- the service and the API ----------------------------------------------

def test_the_players_endpoint_lists_the_off_ones_too(tmp_path):
    service = svc(tmp_path, ROMARR_PLAYERS="emulatorjs,ruffle")
    body = service.player_directory()
    keys = {p["key"]: p for p in body["players"]}
    assert set(keys) == set(PLAYERS)
    assert keys[EMULATORJS]["enabled"] and not keys[EMULARITY]["enabled"]
    assert keys[EMULARITY]["cannot"]


def test_the_play_endpoint_answers_about_a_bare_extension(tmp_path):
    body = svc(tmp_path).play_for("swf", "browser")
    assert body["plays"] is True
    assert body["routes"][0]["player"] == RUFFLE


def test_the_play_endpoint_is_blunt_about_a_missing_file(tmp_path):
    body = svc(tmp_path).play_for("swf", "browser", present=False)
    assert body["plays"] is False
    assert "no file on the library server" in body["absent"]
    assert body["routes"] == []
    assert any(a["player"] == RUFFLE for a in body["alternatives"])


def test_the_platform_directory_carries_the_players(tmp_path):
    rows = {r["slug"]: r for r in svc(tmp_path).platform_directory()}
    assert EMULATORJS in rows["snes"]["players"]
    assert EMULARITY in rows["snes"]["players"], "Archive.org emulates SNES"
    assert rows["ps2"]["players"] == []


def test_status_counts_the_players(tmp_path):
    counts = svc(tmp_path).status()["play_routes"]
    assert counts["players"][EMULATORJS] > 20
    assert counts["players"][EMULARITY] >= 8


def test_disabling_a_player_is_visible_in_status(tmp_path):
    counts = svc(tmp_path, ROMARR_PLAYERS="emulatorjs").status()["play_routes"]
    assert EMULARITY not in counts["players"]


def test_the_library_tally_separates_absent_from_unplayable(tmp_path):
    """The number the whole model exists to produce, over a shelf shaped like
    the real one: files on disk, and catalogued rows with nothing behind
    them."""
    from romarr.libraries import Game

    service = svc(tmp_path)
    shelf = [
        Game(id="1", name="Mario", platform="snes", extension=".sfc"),
        Game(id="2", name="Dune II", platform="dos", extension=".zip"),
        Game(id="3", name="Grow Cube", platform="browser", extension=".swf"),
        Game(id="4", name="Catalogued", platform="browser", extension=".swf",
             origin="cloud", provenance="flashpoint"),
        Game(id="5", name="Projector", platform="browser", extension=".exe"),
    ]
    tally = service._player_tally(shelf)
    assert tally["rows"] == 5
    assert tally["no_file"] == 1
    assert tally["by_player"][RUFFLE] == 1, "the catalogued one has no bytes"
    assert tally["by_player"][EMULATORJS] == 2
    assert tally["plays_in_browser"] == 3
