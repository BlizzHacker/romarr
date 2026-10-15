"""Real RetroArch: what its Network Control Interface proves, and what it does not.

Every test runs against a fake UDP transport -- no datagram in this file
touches a socket, and none may: the suite runs on machines with no LAN and
no RetroArch. What is pinned down is ROMarr's half of the conversation, the
same split `test_moonlight.py` states: the commands it sends, the shapes it
accepts, and -- most of the file -- every place it must refuse to claim
something.

The read-only guarantee is tested twice, from two directions: once as "the
class raises when asked to send a state-changing command" and once as "the
datagrams a full probe actually puts on the wire are only reads". The second
is the one that matters, because it fails no matter how the first is
refactored away.
"""

from __future__ import annotations

import time

import pytest

from romarr import playability
from romarr.playability import (
    DOWNLOAD, NCI_DEFAULT_PORT, NCI_READ_ONLY, STREAM, NCIStatus,
    RetroArchNCI, StreamServer, StreamSources, parse_nci_status, routes_for)


class FakeUDP:
    """A RetroArch's answers, or a hostile/silent one, without a network.

    `replies` maps a command to the text RetroArch would send back; anything
    not in it times out, which is what a real UDP host does with a command
    it never received. Every command handed over is recorded, which is how
    the read-only tests scan the wire rather than the code.
    """

    def __init__(self, replies=None, *, raises=None):
        self.replies = dict(replies or {})
        self.raises = raises or TimeoutError("timed out")
        self.sent = []

    def __call__(self, command: str) -> str:
        self.sent.append(command)
        if command in self.replies:
            return self.replies[command]
        raise self.raises


def nci(replies=None, *, raises=None, **kwargs) -> RetroArchNCI:
    return RetroArchNCI("192.0.2.10",
                        transport=FakeUDP(replies, raises=raises), **kwargs)


PLAYING = {
    "VERSION": "1.19.1\n",
    "GET_STATUS": "GET_STATUS PLAYING playstation,Final Fantasy VII (USA).chd,crc32=a1b2c3d4\n",
    "GET_CONFIG_PARAM libretro_directory": (
        "GET_CONFIG_PARAM libretro_directory /home/retroarch/.config/retroarch/cores"),
}

CONTENTLESS = {
    "VERSION": "1.19.1",
    "GET_STATUS": "GET_STATUS CONTENTLESS",
}


# --- GET_STATUS parsing, in both published shapes --------------------------

def test_a_playing_reply_yields_state_system_game_and_crc():
    """The exact line v1.19.1's `command.c` formats with
    `"GET_STATUS %s %s,%s,crc32=%x\\n"`, including the trailing newline a
    real datagram carries."""
    got = parse_nci_status(PLAYING["GET_STATUS"])
    assert got.state == "playing"
    assert got.system_id == "playstation"
    assert got.game == "Final Fantasy VII (USA).chd"
    assert got.crc32 == "a1b2c3d4"


def test_a_paused_reply_is_parsed_and_keeps_the_distinction():
    got = parse_nci_status("GET_STATUS PAUSED mega_drive,Sonic 2.md,crc32=0")
    assert got.state == "paused"
    assert got.system_id == "mega_drive"


def test_contentless_is_its_own_answer_not_an_empty_one():
    """"Sitting at the menu with nothing loaded" is a fact about the host.
    Collapsing it into "we did not hear" would tell an operator fixing a
    network problem that they have a RetroArch problem."""
    got = parse_nci_status("GET_STATUS CONTENTLESS")
    assert got == NCIStatus(state="contentless")
    assert got.system_id == ""


def test_masters_crc32less_shape_is_accepted_too():
    """Upstream master dropped the crc32 field from GET_STATUS. A parser
    pinned to one release's line would answer "empty" on the next one."""
    got = parse_nci_status("GET_STATUS PLAYING game_boy,AP_Lonk.sfc")
    assert got.state == "playing"
    assert got.system_id == "game_boy"
    assert got.crc32 == ""


def test_masters_error_shape_is_accepted_too():
    assert parse_nci_status("GET_STATUS ERROR").state == "error"


def test_a_comma_inside_the_basename_does_not_split_the_line():
    """"Game, The (USA).chd" is a real filename shape. The crc32 is peeled
    off the end and the system_id off the front precisely so the middle can
    contain anything a filesystem allows."""
    got = parse_nci_status("GET_STATUS PLAYING nes,Game, The (USA).nes,crc32=deadbeef")
    assert got.system_id == "nes"
    assert got.game == "Game, The (USA).nes"
    assert got.crc32 == "deadbeef"


@pytest.mark.parametrize("rubbish", [
    "", "GET_STATUS", "GET_STATUS DANCING nes,game",
    "<html><title>Router</title></html>", "PLAYING nes,game,crc32=0",
])
def test_anything_that_is_not_a_shape_retroarch_publishes_is_empty(rubbish):
    """It is a LAN port and anything could answer on it. The parser is not
    asked to be clever -- it is asked never to invent a status."""
    assert parse_nci_status(rubbish).state == ""


def test_a_version_reply_is_the_reachability_gate():
    host = nci(PLAYING)
    host.refresh()
    assert host.reachable
    assert host.engine == "RetroArch 1.19.1"


def test_a_non_version_answer_from_the_port_is_not_read_as_retroarch():
    """Something else is on 55355. Reporting it as a RetroArch that plays
    nothing would send the operator to debug cores instead of ports."""
    host = nci({"VERSION": "<html>router login</html>"})
    host.refresh()
    assert not host.reachable
    assert "not RetroArch" in host._problem


# --- what the loaded game proves, and what it does not ---------------------

def test_loaded_content_grants_a_stream_route_to_its_platform():
    """The strongest single-platform evidence in the module: not a guess
    from a name, a core that is running right now."""
    host = nci(PLAYING)
    assert host.tier("psx") == STREAM
    got = routes_for("psx", stream=host)
    assert STREAM in got.kinds


def test_a_paused_game_still_proves_its_core():
    host = nci({"VERSION": "1.19.1",
                "GET_STATUS": "GET_STATUS PAUSED gamecube,Mario Kart.gcm,crc32=1"})
    assert host.tier("ngc") == STREAM
    assert host.tier("wii") == STREAM          # Dolphin carries both machines


def test_contentless_grants_nothing():
    host = nci(CONTENTLESS)
    host.refresh()
    assert host.reachable
    for slug in ("psx", "ps2", "nes", "snes"):
        assert host.tier(slug) is None


def test_an_unmapped_system_id_grants_nothing():
    """A core whose system_id is not in the table is unknown, not playable.
    The table under-claims on purpose -- see its comment."""
    host = nci({"VERSION": "1.19.1",
                "GET_STATUS": "GET_STATUS PLAYING chailove,something.chai,crc32=0"})
    assert host.tier("chailove") is None
    assert host._loaded_slugs() == ()


def test_the_nci_is_never_asked_for_a_local_tier():
    host = nci(PLAYING)
    # tier() answers stream or None -- nothing it can say is browser work.
    assert host.tier("psx") in (STREAM, None)


def test_unknown_is_said_as_unknown_not_as_no():
    """The sentence an operator reads for a platform this host may well
    play. It must not contain a refusal; it must contain the reason.
    (`why` reads the last probe, like `MoonlightHost.why` -- `tier` is what
    probes, and in the real flow it is always asked first.)"""
    host = nci(PLAYING)
    assert host.tier("ps2") is None
    said = host.why("ps2").lower()
    assert "reports what is playing" in said
    assert "cannot say" in said
    assert "not installed" not in said and "no core" not in said


def test_why_names_what_is_loaded_instead():
    host = nci(PLAYING)
    host.refresh()
    assert "playstation" in host.why("ps2")


def test_why_says_nothing_for_a_platform_it_granted():
    assert nci(PLAYING).why("psx") == ""


def test_why_says_nothing_when_the_host_is_silent():
    """A host that did not answer has no words to put in an operator's
    mouth; `stream_unreachable` carries that half of the story."""
    assert nci().why("psx") == ""


# --- silence, refusal and the breaker --------------------------------------

def test_a_silent_host_is_unreachable_with_an_honest_problem():
    host = nci()                       # nothing answers VERSION
    host.refresh()
    assert not host.reachable
    assert "cannot say which" in host._problem
    assert host.tier("psx") is None


def test_a_refused_port_names_the_fix():
    host = nci(raises=ConnectionRefusedError("refused"))
    host.refresh()
    assert not host.reachable
    assert "network_cmd_enable" in host._problem


def test_an_unreachable_host_degrades_the_route_and_says_so():
    got = routes_for("psx", stream=nci())
    assert STREAM not in got.kinds
    assert DOWNLOAD in got.kinds
    assert "unreachable" in got.summary().lower()


def test_a_down_host_is_asked_once_not_once_per_platform():
    """`StreamServer`'s breaker, carried over because the failure mode is
    the same and was measured there at 69 seconds a page. UDP makes it
    worse, not better: a silent host costs a full timeout every time."""
    fake = FakeUDP()
    host = RetroArchNCI("192.0.2.10", transport=fake)
    for _ in range(30):
        assert host.tier("psx") is None
    # One probe's worth of datagrams, then the breaker swallowed the rest.
    assert len(fake.sent) <= len(NCI_READ_ONLY)


def test_a_host_that_answers_is_probed_once_per_ttl():
    fake = FakeUDP(PLAYING)
    host = RetroArchNCI("192.0.2.10", transport=fake)
    for slug in ("psx", "snes", "nes", "gba"):
        host.tier(slug)
    assert fake.sent.count("VERSION") == 1


def test_only_successes_are_cached_and_a_revived_host_is_noticed():
    """A failure must never be remembered as "cannot play this" -- the rule
    `StreamServer`'s TTL test was written for."""
    fake = FakeUDP()
    host = RetroArchNCI("192.0.2.10", transport=fake)
    assert host.tier("psx") is None
    host._down_until = 0.0                      # breaker manually expired
    fake.replies.update(PLAYING)
    assert host.tier("psx") == STREAM


def test_the_breaker_expires_so_a_started_host_is_found():
    fake = FakeUDP()
    host = RetroArchNCI("192.0.2.10", transport=fake)
    host.refresh()
    assert host._down_until > time.monotonic()
    assert host.DOWN_COOLDOWN == StreamServer.DOWN_COOLDOWN


# --- the read-only guarantee ------------------------------------------------

@pytest.mark.parametrize("command", [
    "LOAD_CORE /cores/pcsx_rearmed_libretro.so",
    "LOAD_CONTENT /roms/game.chd",
    "QUIT", "MENU_TOGGLE", "PAUSE_TOGGLE", "RESET",
    "SET_CONFIG_PARAM network_cmd_port 1",
])
def test_a_state_changing_command_is_refused_before_the_socket(command):
    """The NCI serves these on the same unauthenticated socket. A probe
    that could disturb a running game is not a probe."""
    fake = FakeUDP(PLAYING)
    host = RetroArchNCI("192.0.2.10", transport=fake)
    with pytest.raises(PermissionError):
        host._command(command)
    assert fake.sent == []          # refused, not sent-then-regretted


def test_a_full_probe_puts_only_reads_on_the_wire():
    """Scans what was *sent*, not what the code looks like: if a future
    refactor reaches the transport by any path, this fails."""
    fake = FakeUDP(PLAYING)
    host = RetroArchNCI("192.0.2.10", transport=fake)
    host.status()
    assert fake.sent, "the probe sent nothing at all"
    for datagram in fake.sent:
        assert datagram.split(" ", 1)[0].upper() in NCI_READ_ONLY, datagram
    assert NCI_READ_ONLY == ("VERSION", "GET_STATUS", "GET_CONFIG_PARAM")


def test_a_lost_get_status_does_not_downgrade_a_host_that_answered_version():
    """UDP drops datagrams. A host that proved itself reachable must not be
    reported down because one optional read went missing."""
    fake = FakeUDP({"VERSION": "1.19.1"})
    host = RetroArchNCI("192.0.2.10", transport=fake)
    host.refresh()
    assert host.reachable
    assert host.tier("psx") is None     # unknown, not unreachable
    assert host._status == NCIStatus()


# --- GET_CONFIG_PARAM -------------------------------------------------------

def test_libretro_directory_is_read_and_shown():
    host = nci(PLAYING)
    host.refresh()
    assert host._libretro_directory == (
        "/home/retroarch/.config/retroarch/cores")
    assert host.status()["libretro_directory"].endswith("/cores")


def test_an_unsupported_param_is_no_value_not_a_directory_called_unsupported():
    host = nci({**CONTENTLESS,
                "GET_CONFIG_PARAM libretro_directory":
                    "GET_CONFIG_PARAM libretro_directory unsupported"})
    host.refresh()
    assert host._libretro_directory == ""


def test_a_path_with_spaces_survives_the_split():
    got = playability._parse_config_param(
        "GET_CONFIG_PARAM libretro_directory /home/a b/My Cores")
    assert got == "/home/a b/My Cores"


def test_the_core_list_boundary_is_stated_on_every_status():
    """The directory is shown and never listed: listing it needs filesystem
    access ROMarr does not have. That is a fact about this integration, so
    it rides on every response rather than living in a docstring."""
    host = nci(PLAYING)
    block = host.status()
    assert block["cores_listed"] is False
    assert "never which cores are installed" in block["note"]


# --- status(), the shape the UI shows ---------------------------------------

def test_status_reports_the_running_game():
    block = nci(PLAYING).status()
    assert block["configured"] and block["ok"]
    assert block["version"] == "1.19.1"
    assert block["state"] == "playing"
    assert block["system_id"] == "playstation"
    assert block["game"] == "Final Fantasy VII (USA).chd"
    assert block["crc32"] == "a1b2c3d4"
    assert block["platforms"] == ["psx"]


def test_status_of_a_silent_host_is_a_problem_not_an_exception():
    block = nci().status()
    assert block["configured"] and not block["ok"]
    assert block["problem"]


# --- host:port parsing ------------------------------------------------------

def test_the_default_port_is_retroarchs():
    assert NCI_DEFAULT_PORT == 55355
    assert RetroArchNCI("192.0.2.10", transport=FakeUDP()).port == 55355


def test_a_port_can_be_given_with_the_host_or_alone():
    assert RetroArchNCI("192.0.2.10:55356", transport=FakeUDP()).port == 55356
    assert RetroArchNCI("192.0.2.10", port=55356, transport=FakeUDP()).port == 55356


# --- routing preference vs the HTTP stream server ----------------------------

def _answered_http(tiers: dict) -> StreamServer:
    """A `StreamServer` holding successful answers and tripped-down for
    everything else, so these tests never touch DNS: its cache serves the
    platforms it knows, and the breaker short-circuits the rest exactly as
    a real down server would."""
    http = StreamServer("http://stream.test")
    for slug, tier in tiers.items():
        http._cache[slug] = (tier, float("inf"))
    http._down_until = float("inf")
    return http


def test_the_http_server_answers_first_when_both_know():
    """`StreamServer` knows its installed cores; the NCI knows one loaded
    game. For every platform but the loaded one the HTTP server is the
    better-informed source, and confidence order says so."""
    combined = StreamSources(_answered_http({"ps2": "stream"}), nci(PLAYING))
    assert combined.tier("ps2") == "stream"
    assert combined.tier("psx") == STREAM             # NCI's loaded platform
    assert combined.attribution("psx")[1] == "RetroArch 1.19.1"


def test_the_nci_alone_can_answer_for_its_loaded_platform():
    combined = StreamSources(None, nci(PLAYING), None)
    assert combined.tier("psx") == STREAM
    assert combined.reachable


def test_a_silent_nci_does_not_hide_a_live_http_server():
    """One dead source must not hide a live one -- `StreamSources.tier`'s
    rule, re-pinned here because the NCI is the source most likely to be
    dead: it is off by default on every RetroArch."""
    combined = StreamSources(nci(), _answered_http({"ps2": "stream"}))
    assert combined.tier("ps2") == "stream"
    assert combined.reachable


def test_the_ncis_unknown_does_not_suppress_a_better_why():
    """The order app.py wires: HTTP first. Its firmware sentence is the
    actionable one, and the NCI's honest "unknown" must not crowd it out --
    `StreamSources.why` takes the first source that has something to say."""
    http = StreamServer("http://stream.test")
    http._why["ps2"] = "the stream server needs firmware for this platform: bios"
    combined = StreamSources(http, nci(PLAYING))
    assert "firmware" in combined.why("ps2")


def test_routes_for_names_the_nci_and_its_version_for_its_route():
    got = routes_for("psx", stream=StreamSources(nci(PLAYING)))
    stream = next(r for r in got.routes if r.kind == STREAM)
    assert "RetroArch 1.19.1" in stream.detail
    assert "192.0.2.10" in stream.detail


def test_routes_for_never_makes_an_nci_route_local():
    got = routes_for("psx", stream=StreamSources(nci(PLAYING)))
    stream = next(r for r in got.routes if r.kind == STREAM)
    assert "server-side" in stream.detail


# --- env wiring --------------------------------------------------------------

def wired(tmp_path, **env):
    from romarr.app import ROMarr
    return ROMarr({"ROMARR_DATA": str(tmp_path / "s.json"), **env})


def test_no_host_configured_is_a_first_class_answer(tmp_path):
    service = wired(tmp_path)
    assert service.retroarch_nci is None
    block = service.retroarch_nci_status()
    assert block["configured"] is False
    assert "RETROARCH_NCI_HOST" in block["hint"]


def test_configuring_the_host_wires_the_source(tmp_path):
    service = wired(tmp_path, RETROARCH_NCI_HOST="192.0.2.10")
    assert service.retroarch_nci is not None
    assert service.retroarch_nci.port == NCI_DEFAULT_PORT
    assert service.stream is not None
    # The combiner must hold it even with no HTTP server and no Moonlight.
    assert service.retroarch_nci in service.stream.sources


def test_the_port_can_be_moved(tmp_path):
    service = wired(tmp_path, RETROARCH_NCI_HOST="192.0.2.10",
                    RETROARCH_NCI_PORT="55356")
    assert service.retroarch_nci.port == 55356


def test_a_garbage_port_falls_back_rather_than_crashing_startup(tmp_path):
    service = wired(tmp_path, RETROARCH_NCI_HOST="192.0.2.10",
                    RETROARCH_NCI_PORT="notaport")
    assert service.retroarch_nci.port == NCI_DEFAULT_PORT


def test_the_source_is_visible_in_status_without_a_probe_hanging_it(tmp_path):
    service = wired(tmp_path, RETROARCH_NCI_HOST="192.0.2.10")
    service.retroarch_nci._transport = FakeUDP()
    service.retroarch_nci.timeout = 0.05
    block = service.status()["retroarch_nci"]
    assert block["configured"] is True
    assert block["ok"] is False
    assert service.status()["play_routes"]["total"] > 30


def test_a_live_nci_grants_its_loaded_platform_through_the_service(tmp_path):
    service = wired(tmp_path, RETROARCH_NCI_HOST="192.0.2.10")
    service.retroarch_nci._transport = FakeUDP(PLAYING)
    counts = service.status()["play_routes"]
    assert counts["stream"] >= 1


def test_the_general_page_shows_the_nci_row(tmp_path):
    from romarr.ui import page
    rendered = page()
    assert "retroarch_nci" in rendered
    assert "RETROARCH_NCI_HOST" in rendered
    # The row's honest boundary, stated where the operator reads it.
    assert "never which cores are installed" in rendered


def test_nothing_secret_is_written_to_the_store(tmp_path):
    """The NCI has no credential to leak, but the host:port must land in
    settings for the General page like `_stream_url` and `_moonlight_host`."""
    service = wired(tmp_path, RETROARCH_NCI_HOST="192.0.2.10",
                    RETROARCH_NCI_PORT="55356")
    assert service.store.settings["_retroarch_nci"] == "192.0.2.10:55356"


# --- the system_id table -----------------------------------------------------

def test_every_slug_the_table_names_is_a_platform_romarr_models():
    """The table is read from libretro's .info files and this module's slugs
    come from `platforms.py`; nothing keeps them in step but this test."""
    from romarr.platforms import by_slug
    for system_id, slugs in playability.NCI_SYSTEM_PLATFORMS.items():
        for slug in slugs:
            assert by_slug(slug), f"{system_id} -> unknown slug {slug!r}"


def test_the_table_under_claims_where_cores_disagree():
    """fb_alpha is FBNeo (Neo Geo yes) and fbalpha2012_cps1/2 (Neo Geo no);
    mega_drive is Genesis Plus GX (no 32X) and PicoDrive (32X). Both stay on
    the intersection -- see the table's comment for why."""
    table = playability.NCI_SYSTEM_PLATFORMS
    assert "neogeoaes" not in table["fb_alpha"]
    assert "sega32" not in table["mega_drive"]


def test_the_heavy_platforms_the_nci_exists_to_help_are_reachable_from_it():
    table = playability.NCI_SYSTEM_PLATFORMS
    assert table["playstation2"] == ("ps2",)
    assert table["3ds"] == ("3ds",)
    assert table["dreamcast"] == ("dc",)
    assert set(table["gamecube"]) == {"ngc", "wii"}
