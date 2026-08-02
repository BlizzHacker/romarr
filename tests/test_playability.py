"""How a platform will play, said before the grab rather than after.

A ROM filed under a platform with no route is imported but dead: it appears
in the library, it has a cover, and clicking it does nothing. This module
does not refuse anything -- cataloguing is a legitimate use of a library --
it makes sure the operator knows which of the four routes applies first.
"""

from __future__ import annotations

import pytest

from romarr import playability
from romarr.playability import (
    ARCHIVE, DOWNLOAD, LOCAL, STREAM, Playability, routes_for)
from romarr.platforms import PLATFORMS, by_slug


class FakeStream:
    """Stands in for a romm-stream server's /api/play/route."""

    def __init__(self, tiers, unreachable=""):
        self.tiers = tiers
        self.unreachable = unreachable
        self.asked = []

    def tier(self, slug):
        self.asked.append(slug)
        if self.unreachable:
            return None
        return self.tiers.get(slug)

    @property
    def reachable(self):
        return not self.unreachable

    @property
    def label(self):
        return "http://stream.test"


# --- nothing is excluded ---------------------------------------------------

def test_every_platform_has_at_least_one_route():
    """The whole claim, as a test. No platform in the table is a dead end."""
    for platform in PLATFORMS:
        got = routes_for(platform)
        assert got.routes, platform.slug
        assert DOWNLOAD in got.kinds, platform.slug


def test_download_is_always_available():
    """A ROM in a library is a ROM you can download. That is an answer, not a
    failure, and it is the floor every platform stands on."""
    for slug in ("snes", "psx", "ps2", "neo-geo-cd"):
        assert DOWNLOAD in routes_for(by_slug(slug)).kinds


# --- EmulatorJS, the local tier -------------------------------------------

@pytest.mark.parametrize("slug", [
    "psx", "psp", "saturn", "segacd", "3do", "philips-cd-i", "pc-fx",
    "turbografx-16-slash-pc-engine-cd", "amiga-cd32",
])
def test_the_optical_systems_emulatorjs_runs_are_reported_as_local(slug):
    """Nine optical systems in RomM 4.9.2's base core map.

    This is the fact the README's exclusion denied outright, so it is pinned
    per platform rather than as a count.
    """
    got = routes_for(by_slug(slug))
    assert LOCAL in got.kinds, got.summary()


def test_a_local_route_names_the_core_that_runs_it():
    got = routes_for(by_slug("psx"))
    local = next(r for r in got.routes if r.kind == LOCAL)
    assert "pcsx_rearmed" in local.detail


@pytest.mark.parametrize("slug", ["ps2", "ngc", "wii", "dc", "3ds"])
def test_the_systems_emulatorjs_cannot_run_are_not_claimed_as_local(slug):
    """Over-claiming here is the failure mode that matters.

    A platform wrongly reported playable sends the operator to a game that
    imports, displays and does nothing. Under-claiming costs them a warning
    they did not need.
    """
    assert LOCAL not in routes_for(by_slug(slug)).kinds


def test_cartridge_platforms_kept_their_local_route():
    for slug in ("nes", "snes", "gba", "n64", "genesis-slash-megadrive"):
        assert LOCAL in routes_for(by_slug(slug)).kinds, slug


# --- the headless RetroArch stream tier ------------------------------------

def test_a_stream_server_supplies_routes_emulatorjs_cannot():
    stream = FakeStream({"ps2": "stream", "wii": "stream", "dc": "stream"})
    for slug in ("ps2", "wii", "dc"):
        got = routes_for(by_slug(slug), stream=stream)
        assert STREAM in got.kinds, got.summary()


def test_the_stream_server_is_asked_by_slug():
    stream = FakeStream({"ps2": "stream"})
    routes_for(by_slug("ps2"), stream=stream)
    assert stream.asked == ["ps2"]


def test_a_stream_server_that_answers_local_does_not_invent_a_stream_route():
    """The server distinguishes the two tiers and so must this. Reporting
    'streamed server-side' for something the browser runs is a lie about
    where the CPU time goes."""
    stream = FakeStream({"psx": "local"})
    got = routes_for(by_slug("psx"), stream=stream)
    assert STREAM not in got.kinds
    assert LOCAL in got.kinds


def test_no_stream_server_is_not_an_error():
    """Most installs have no stream server. That must degrade to the other
    routes, never to a failure or an empty answer."""
    got = routes_for(by_slug("psx"), stream=None)
    assert LOCAL in got.kinds and DOWNLOAD in got.kinds
    assert STREAM not in got.kinds


def test_an_unreachable_stream_server_degrades_and_says_so():
    stream = FakeStream({"ps2": "stream"}, unreachable="connection refused")
    got = routes_for(by_slug("ps2"), stream=stream)
    assert STREAM not in got.kinds
    assert DOWNLOAD in got.kinds
    assert "unreachable" in got.summary().lower()


def test_ps2_has_no_route_but_download_without_a_stream_server():
    """Honest, and the reason the stream server is worth configuring."""
    got = routes_for(by_slug("ps2"), stream=None)
    assert got.kinds == (DOWNLOAD,)
    assert not got.plays_without_downloading


# --- Archive.org's in-page emulator ---------------------------------------

def test_archive_org_is_offered_where_it_really_emulates():
    for slug in ("nes", "snes", "gba", "atari2600", "genesis-slash-megadrive"):
        assert ARCHIVE in routes_for(by_slug(slug)).kinds, slug


@pytest.mark.parametrize("slug", ["psx", "ps2", "saturn", "3do", "dc", "psp"])
def test_archive_org_is_not_claimed_for_disc_systems(slug):
    """Measured against Archive.org's own `emulator` metadata field, which
    272,424 of their items carry. The disc drivers return nothing:
    psj/psu/pse 0, saturn 0, 3do 0, dc 2, segacd 1.

    Archive.org is a *source* for disc images, not a player of them, and
    claiming otherwise would send an operator to a details page that offers
    a download and no emulator.
    """
    assert ARCHIVE not in routes_for(by_slug(slug)).kinds


def test_the_archive_table_records_when_it_was_measured():
    """A vendored copy of somebody else's data goes stale. One that says
    what it is stale relative to can be checked; one that does not, cannot."""
    assert playability.ARCHIVE_MEASURED_ON


# --- shape -----------------------------------------------------------------

def test_routes_are_ordered_best_first():
    got = routes_for(by_slug("psx"))
    assert got.kinds[0] == LOCAL
    assert got.kinds[-1] == DOWNLOAD


def test_stream_outranks_download_but_not_local():
    stream = FakeStream({"psx": "stream"})
    got = routes_for(by_slug("psx"), stream=stream)
    assert list(got.kinds).index(LOCAL) < list(got.kinds).index(DOWNLOAD)


def test_summary_is_one_readable_line():
    summary = routes_for(by_slug("psx")).summary()
    assert "\n" not in summary
    assert "PlayStation" in summary


def test_plays_without_downloading_is_the_honest_question():
    assert routes_for(by_slug("psx")).plays_without_downloading
    assert not routes_for(by_slug("neo-geo-cd")).plays_without_downloading


def test_a_platform_with_no_player_says_which_would_fix_it():
    """"download only" on its own leaves the operator with nowhere to go."""
    got = routes_for(by_slug("ps2"), stream=None)
    assert "stream server" in got.summary().lower()


def test_the_stream_servers_own_reason_wins_when_it_gave_one():
    """Live example from the stream server on CT104: PCSX2 is installed and
    PS2 is still unplayable, because it has no BIOS.

    "configure a stream server to play it here" is exactly wrong for that
    operator -- they have one. The actionable answer is the firmware, and only
    the server knows which of its three reasons applies.
    """
    class Firmwareless(FakeStream):
        def why(self, slug):
            return "the stream server needs firmware for this platform: bios"

    got = routes_for(by_slug("ps2"), stream=Firmwareless({}))
    assert "firmware" in got.summary()
    assert "configure a stream server" not in got.summary()


def test_the_generic_reason_is_used_when_the_server_says_nothing():
    got = routes_for(by_slug("ps2"), stream=FakeStream({}))
    assert "configure a stream server" in got.summary()


def test_a_stream_server_whose_why_raises_does_not_break_the_answer():
    class Broken(FakeStream):
        def why(self, slug):
            raise RuntimeError("nope")

    got = routes_for(by_slug("ps2"), stream=Broken({}))
    assert DOWNLOAD in got.kinds


def test_result_is_frozen():
    got = routes_for(by_slug("snes"))
    assert isinstance(got, Playability)
    with pytest.raises(Exception):
        got.routes = ()


def test_a_slug_string_works_as_well_as_a_platform():
    assert routes_for("psx").kinds == routes_for(by_slug("psx")).kinds


def test_an_unknown_platform_is_not_guessed_at():
    got = routes_for("playstation 5")
    assert got.kinds == (DOWNLOAD,)


# --- the cache must not outlive the fact it caches -------------------------

def test_a_successful_answer_expires_so_a_new_core_is_noticed():
    """Found by installing one.

    The cache was for the process lifetime, on the reasoning that a routing
    table built from what is on disk does not change while the server is up.
    Installing `neocd_libretro.so` and restarting the stream server falsified
    that: Neo Geo CD went from unplayable to streaming, and ROMarr kept
    reporting "no emulator exists" until it too was restarted -- a confusing
    way to be told that your work succeeded.
    """
    from romarr.playability import StreamServer

    server = StreamServer("http://stream.test")
    server._cache["neo-geo-cd"] = ("stream", 0.0)      # already expired
    # An expired entry must not be returned; with no reachable server the call
    # falls through to the network and fails closed rather than serving stale.
    server.timeout = 0.01
    assert server.tier("neo-geo-cd") is None


def test_a_live_answer_inside_the_ttl_is_reused():
    import time as _time

    from romarr.playability import StreamServer

    server = StreamServer("http://stream.test")
    server._cache["psx"] = ("local", _time.monotonic() + 300)
    assert server.tier("psx") == "local"
