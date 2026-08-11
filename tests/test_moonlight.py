"""Moonlight hosts: what a Wolf or Sunshine box genuinely proves, and what it
does not.

Every test here is against a fixture, not a live host. That is stated in
`docs/design/streaming-hosts.md` and in `docs/PROOF.md` rather than implied,
because a green suite proving ROMarr's half of a conversation is not the same
thing as proving the conversation. What these do pin down is the half that is
ROMarr's fault when it goes wrong: the shapes it sends, the shapes it accepts,
and -- most of the file -- every place it must refuse to claim something.
"""

from __future__ import annotations

import json

import pytest

from romarr import playability
from romarr.playability import (
    DOWNLOAD, STREAM, SUNSHINE, WOLF, MoonlightHost, StreamSources,
    platforms_from_apps, routes_for)


SERVERINFO = """<?xml version="1.0"?>
<root status_code="200">
  <hostname>whitebox</hostname>
  <appversion>7.1.431.0</appversion>
  <GfeVersion>3.23.0.74</GfeVersion>
  <uniqueid>0123456789ABCDEF</uniqueid>
  <HttpsPort>47984</HttpsPort>
  <ExternalPort>47989</ExternalPort>
  <mac>00:00:00:00:00:00</mac>
  <LocalIP>192.168.0.50</LocalIP>
  <PairStatus>0</PairStatus>
  <currentgame>0</currentgame>
  <state>SUNSHINE_SERVER_FREE</state>
</root>
"""


def live(host: MoonlightHost, apps=None, *, apps_problem="") -> MoonlightHost:
    """A host that has already answered, without touching a network.

    Written as a helper rather than a mock of `urlopen` because every test
    below is about what ROMarr *concludes*, not about how it fetched. The
    fetching is exercised separately in the transport tests.
    """
    host._reachable, host._problem = True, ""
    host._info = playability._serverinfo_fields(SERVERINFO)
    host._apps = list(apps or [])
    host._apps_read = apps is not None
    host._apps_problem = apps_problem
    host._coverage = platforms_from_apps(apps or [])
    host._fresh_until = float("inf")
    return host


# --- the parsing of the one call that needs no credential ------------------

def test_serverinfo_yields_the_fields_the_ui_shows():
    fields = playability._serverinfo_fields(SERVERINFO)
    assert fields["hostname"] == "whitebox"
    assert fields["appversion"] == "7.1.431.0"
    assert fields["HttpsPort"] == "47984"
    assert fields["state"] == "SUNSHINE_SERVER_FREE"


def test_serverinfo_does_not_choke_on_rubbish():
    """It is a LAN service, and anything at all could be on that port.

    The parser is not asked to validate -- it scrapes whatever flat tags it
    finds -- so what matters is that nothing raises and that none of the
    fields the UI shows come out of a page that is not a `/serverinfo`.
    """
    assert playability._serverinfo_fields("") == {}
    from_a_web_page = playability._serverinfo_fields(
        "<html><title>Router</title><body>login</body></html>")
    for field in ("hostname", "appversion", "HttpsPort", "state"):
        assert field not in from_a_web_page


def test_pair_status_over_plain_http_is_always_zero_and_is_not_read_as_truth():
    """Both hosts hardcode 0 here, so it can never mean "you are not paired".

    Sunshine sets `pair_status = 1` only on the HTTPS server and only when a
    `uniqueid` was supplied; Wolf passes `is_https`. ROMarr therefore reads
    pairing state from the paired-client list and never from `/serverinfo`,
    and this test exists so that nobody "fixes" that later.
    """
    host = live(MoonlightHost("h", kind=WOLF), apps=[])
    assert "PairStatus" not in host.status()
    assert host.status()["paired"] == []


# --- what an app list proves, and mostly does not --------------------------

def test_a_single_machine_emulator_is_evidence():
    assert platforms_from_apps(["PCSX2"]) == {"ps2": "PCSX2"}
    assert platforms_from_apps(["pcsx2-qt"]) == {"ps2": "pcsx2-qt"}


def test_dolphin_carries_both_of_its_machines():
    got = platforms_from_apps(["Dolphin Emulator"])
    assert set(got) == {"ngc", "wii"}


@pytest.mark.parametrize("app", [
    "RetroArch", "Steam", "EmulationStation", "Pegasus", "Lutris",
    "Kodi", "Firefox", "Prismlauncher", "Desktop (xfce)", "Test ball",
])
def test_a_general_purpose_app_proves_nothing(app):
    """The whole discipline of this module, as a test.

    Every one of these ships in Wolf's stock config or is the obvious thing an
    operator adds, and every one of them will happily play half this library.
    None of them can be *asked* what it will play, so none of them may grant a
    route -- a RetroArch with no cores and a RetroArch with forty look
    identical from outside the container.
    """
    assert platforms_from_apps([app]) == {}


def test_a_generic_app_does_not_talk_a_specific_one_out_of_its_claim():
    """Order in the app list must not decide the answer."""
    both = ["RetroArch", "PCSX2", "Steam"]
    assert platforms_from_apps(both) == {"ps2": "PCSX2"}
    assert platforms_from_apps(list(reversed(both))) == {"ps2": "PCSX2"}


def test_an_emulator_that_does_not_narrow_to_one_machine_is_dropped():
    """The three entries in the table carrying an empty tuple.

    Mesen is the interesting one: it was briefly mapped to `nes`, which is
    wrong because Mesen 2 also runs SNES, Game Boy and PC Engine -- so the
    name does not narrow to one machine and the basis of the whole table
    collapses. ScummVM is not a platform ROMarr models at all.
    """
    assert platforms_from_apps(["ScummVM", "Mednafen", "Mesen"]) == {}


def test_steam_headless_does_not_match_on_the_word_steam_alone():
    """"Steam Headless Desktop" is a desktop, and `steam` is in it. Both
    entries are generic, so the answer is nothing either way -- this pins that
    the match does not instead fall through to some emulator."""
    assert platforms_from_apps(["Steam Headless Desktop"]) == {}


# --- reachability is not capability ----------------------------------------

def test_a_reachable_host_with_no_readable_app_list_grants_nothing():
    """The default case, and the one worth being loudest about.

    Reading the app list needs an admin credential ROMarr may not have. A host
    that answers `/serverinfo` is up, and being up says nothing at all about
    what it can play.
    """
    host = MoonlightHost("192.168.0.50", kind=SUNSHINE)
    host._reachable, host._fresh_until = True, float("inf")
    host._apps_problem = "no admin credential configured"

    assert host.tier("ps2") is None
    assert "cannot read its app list" in host.why("ps2")
    assert routes_for("ps2", stream=host).kinds == (DOWNLOAD,)


def test_the_three_refusals_read_differently_because_the_fixes_differ():
    unreachable = MoonlightHost("192.168.0.50", kind=WOLF)
    unreachable._fresh_until = float("inf")
    assert unreachable.why("ps2") == ""          # nothing useful to say

    blind = MoonlightHost("192.168.0.50", kind=SUNSHINE)
    blind._reachable, blind._fresh_until = True, float("inf")
    assert "cannot read its app list" in blind.why("ps2")

    seen = live(MoonlightHost("192.168.0.50", kind=WOLF), apps=["Steam"])
    assert "no emulator for this platform" in seen.why("ps2")


def test_a_host_that_does_play_it_has_nothing_to_explain():
    host = live(MoonlightHost("h", kind=WOLF), apps=["PCSX2"])
    assert host.why("ps2") == ""


# --- the route it grants ---------------------------------------------------

def test_a_wolf_with_pcsx2_moves_ps2_off_the_download_floor():
    host = live(MoonlightHost("192.168.0.50", kind=WOLF), apps=["PCSX2"])
    got = routes_for("ps2", stream=host)
    assert STREAM in got.kinds
    assert got.plays_without_downloading
    assert "Wolf" in got.summary()


def test_the_route_names_the_host_that_granted_it_not_retroarch():
    """The detail string used to hardcode "headless RetroArch"."""
    host = live(MoonlightHost("192.168.0.50", kind=SUNSHINE), apps=["PCSX2"])
    detail = routes_for("ps2", stream=host).summary()
    assert "Sunshine" in detail
    assert "RetroArch" not in detail


def test_a_moonlight_host_never_claims_the_browser_tier():
    """It renders elsewhere and sends video. That is the stream tier, always,
    however the pixels get drawn on the far end."""
    host = live(MoonlightHost("h", kind=WOLF), apps=["PCSX2", "Duckstation"])
    for slug in ("ps2", "psx"):
        assert host.tier(slug) == STREAM


def test_an_unreachable_host_grants_nothing_and_says_so():
    host = MoonlightHost("192.0.2.1", kind=WOLF, timeout=0.05)
    host.refresh(force=True)
    assert host.reachable is False
    assert host.tier("ps2") is None
    assert routes_for("ps2", stream=host).kinds == (DOWNLOAD,)


def test_a_down_host_stops_being_asked():
    """Same breaker as `StreamServer`, and for the same measured reason: a
    status page walks 59 platforms and must not pay 59 timeouts."""
    host = MoonlightHost("192.0.2.1", kind=WOLF, timeout=0.05)
    host.refresh(force=True)
    assert host._down_until > 0

    calls = []
    host._probe = lambda: calls.append(1)
    for slug in ("ps2", "ngc", "wii", "dc", "3ds"):
        host.tier(slug)
    assert calls == [], "the breaker was open and the network was hit anyway"


# --- several sources behind one slot ---------------------------------------

class Retro:
    """Stands in for the headless RetroArch stream server."""

    reachable = True
    label = "http://stream.test"
    engine = "headless RetroArch"

    def __init__(self, tiers):
        self.tiers = tiers

    def tier(self, slug):
        return self.tiers.get(slug)

    def why(self, slug):
        return ""


def test_both_tiers_answer_and_each_route_names_the_right_one():
    """The attribution bug this was written against: with two sources
    configured, every route was labelled with the *first* one, so a platform
    Wolf granted was reported as streaming from RetroArch."""
    wolf = live(MoonlightHost("192.168.0.50", kind=WOLF), apps=["PCSX2"])
    both = StreamSources(Retro({"dc": STREAM}), wolf)

    assert "Wolf" in routes_for("ps2", stream=both).summary()
    assert "headless RetroArch" in routes_for("dc", stream=both).summary()


def test_a_known_answer_is_not_displaced_by_an_inferred_one():
    """RetroArch reads its own core directory; Wolf is guessing from names.
    When both claim a platform, the one that knows is credited."""
    wolf = live(MoonlightHost("192.168.0.50", kind=WOLF), apps=["PCSX2"])
    both = StreamSources(Retro({"ps2": STREAM}), wolf)
    assert "headless RetroArch" in routes_for("ps2", stream=both).summary()


def test_one_dead_source_does_not_hide_a_live_one():
    class Dead:
        reachable = False
        label = "http://stream.test"

        def tier(self, slug):
            raise OSError("connection refused")

    wolf = live(MoonlightHost("192.168.0.50", kind=WOLF), apps=["PCSX2"])
    both = StreamSources(Dead(), wolf)
    assert STREAM in routes_for("ps2", stream=both).kinds


def test_the_combiner_prefers_an_explanation_over_silence():
    """A RetroArch server with nothing to say must not suppress Wolf's much
    more actionable "I am up but you have not given me a credential"."""
    blind = MoonlightHost("192.168.0.50", kind=SUNSHINE)
    blind._reachable, blind._fresh_until = True, float("inf")
    both = StreamSources(Retro({}), blind)
    assert "cannot read its app list" in both.why("ps2")


def test_no_sources_is_the_same_as_no_stream_server():
    assert StreamSources(None, None).tier("ps2") is None
    assert routes_for("ps2", stream=StreamSources()).kinds == (DOWNLOAD,)


# --- pairing: the part that is a human's -----------------------------------

def test_the_manual_sentence_exists_and_says_the_hard_thing():
    """It is quoted into the API, the UI and the docs from this one constant,
    so it must actually contain the claim."""
    said = playability.PAIRING_IS_MANUAL.lower()
    assert "cannot be automated" in said
    assert "your moonlight client" in said


def test_every_status_payload_carries_the_manual_warning():
    host = live(MoonlightHost("h", kind=WOLF), apps=["PCSX2"])
    host._wolf_get = lambda path: {"requests": []}
    assert host.status()["pairing"]["manual"] == playability.PAIRING_IS_MANUAL


def test_wolf_pending_requests_become_a_pin_page_url():
    """Wolf logs that URL at startup with the secret in the fragment. Rebuilding
    it beats telling an operator to go and read container logs."""
    host = live(MoonlightHost("192.168.0.50", kind=WOLF), apps=[])
    host._wolf_get = lambda path: {"requests": [
        {"pair_secret": "337327E8A6FC0C66", "client_ip": "192.168.0.9"}]}

    pending = host.pairing().pending
    assert pending[0]["client_ip"] == "192.168.0.9"
    assert pending[0]["pin_url"] == \
        "http://192.168.0.50:47989/pin/#337327E8A6FC0C66"


def test_sunshine_cannot_list_waiting_clients_and_says_so():
    """It has no such endpoint. An empty list would read as "nobody is
    waiting", which is a different and wrong statement."""
    host = live(MoonlightHost("h", kind=SUNSHINE), apps=[])
    pairing = host.pairing()
    assert pairing.can_list_pending is False
    assert pairing.pending == []
    assert "no endpoint that lists waiting clients" in pairing.detail


def test_wolf_needs_a_pair_secret_and_refuses_without_one():
    host = live(MoonlightHost("h", kind=WOLF), apps=[])
    got = host.submit_pin("1234")
    assert got["ok"] is False
    assert "pair secret" in got["detail"]


def test_a_wolf_pin_goes_to_the_documented_endpoint_in_the_documented_shape():
    sent = {}

    host = live(MoonlightHost("h", kind=WOLF), apps=[])
    host._wolf_call = lambda method, path, payload: sent.update(
        method=method, path=path, payload=payload) or {"success": True}

    got = host.submit_pin("1234", pair_secret="abc")
    assert got["ok"] is True
    assert sent["method"] == "POST"
    assert sent["path"] == "/pair/client"
    assert sent["payload"] == {"pair_secret": "abc", "pin": "1234"}


def test_a_sunshine_pin_goes_to_the_documented_endpoint_in_the_documented_shape():
    sent = {}

    host = live(MoonlightHost("h", kind=SUNSHINE), apps=[])
    host._sunshine_call = lambda method, path, payload: sent.update(
        method=method, path=path, payload=payload) or {"status": True}

    got = host.submit_pin("1234", name="ROMarr")
    assert got["ok"] is True
    assert (sent["method"], sent["path"]) == ("POST", "/pin")
    assert sent["payload"] == {"pin": "1234", "name": "ROMarr"}


def test_sunshine_false_is_reported_because_false_is_the_meaningful_one():
    """`nvhttp::pin` returns false only when no client is waiting or the PIN
    is not four digits. Its `true` is not trustworthy -- see the next test."""
    host = live(MoonlightHost("h", kind=SUNSHINE), apps=[])
    host._sunshine_call = lambda *a: {"status": False}
    got = host.submit_pin("1234")
    assert got["ok"] is False
    assert "no Moonlight client is waiting" in got["detail"]


def test_success_never_claims_the_client_actually_paired():
    """Neither host will tell you. Sunshine returns true for a *wrong* PIN
    whenever any request is outstanding (LizardByte/Sunshine#3944), and Wolf
    answers 200 on secret match and decides correctness later, inside the
    crypto. So the strongest honest word is "submitted"."""
    host = live(MoonlightHost("h", kind=SUNSHINE), apps=[])
    host._sunshine_call = lambda *a: {"status": True}
    detail = host.submit_pin("1234")["detail"].lower()
    assert "paired" not in detail.replace("appears in the paired list", "")
    assert "does not report whether the pin was correct" in detail


def test_an_empty_pin_is_refused_before_anything_is_sent():
    host = live(MoonlightHost("h", kind=WOLF), apps=[])

    def explode(*a, **k):
        raise AssertionError("a blank PIN reached the host")

    host._wolf_call = explode
    assert host.submit_pin("   ")["ok"] is False


# --- transports ------------------------------------------------------------

def test_wolf_without_a_socket_or_a_proxy_says_which_to_configure():
    host = MoonlightHost("h", kind=WOLF)
    with pytest.raises(playability._NoWayIn) as raised:
        host._wolf_get("/apps")
    assert "WOLF_SOCKET_PATH" in str(raised.value)
    assert "WOLF_API_URL" in str(raised.value)


def test_sunshine_without_a_credential_says_which_to_configure():
    host = MoonlightHost("h", kind=SUNSHINE)
    with pytest.raises(playability._NoWayIn) as raised:
        host._sunshine_get("/apps")
    assert "MOONLIGHT_USER" in str(raised.value)


def test_a_missing_credential_is_reported_as_configuration_not_as_a_fault():
    """`_NoWayIn` exists so "you have not told me the password" and "the host
    is broken" do not print the same way; they lead to different fixes."""
    host = MoonlightHost("192.168.0.50", kind=SUNSHINE)
    host._probe = lambda: setattr(host, "_reachable", True)
    host.refresh(force=True)

    assert host.reachable is True
    assert host._apps_read is False
    assert "MOONLIGHT_USER" in host._apps_problem


def test_wolfs_app_list_is_read_from_its_title_field():
    """Wolf's `AppListResponse` carries `title`; Sunshine's apps.json carries
    `name`. Reading the wrong one yields a list of empty strings, which looks
    exactly like a host with no apps."""
    host = MoonlightHost("h", kind=WOLF)
    host._wolf_get = lambda path: {
        "success": True, "apps": [{"title": "PCSX2"}, {"title": "Steam"}]}
    assert host._wolf_apps() == ["PCSX2", "Steam"]


def test_sunshines_app_list_is_read_from_its_name_field():
    host = MoonlightHost("h", kind=SUNSHINE)
    host._sunshine_get = lambda path: {
        "apps": [{"name": "PCSX2", "cmd": "pcsx2-qt"}]}
    assert host._sunshine_apps() == ["PCSX2"]


def test_a_url_pasted_where_a_host_was_wanted_still_works():
    """Operators paste URLs. The scheme is dropped rather than honoured,
    because the Moonlight probe port is not an HTTPS port."""
    assert MoonlightHost("https://192.168.0.50").host == "192.168.0.50"
    assert MoonlightHost("https://192.168.0.50").port == 47989
    assert MoonlightHost("192.168.0.50:9999").port == 9999
    assert MoonlightHost("192.168.0.50:notaport").port == 47989


def test_an_unknown_kind_falls_back_rather_than_half_working():
    assert MoonlightHost("h", kind="nvidia-shield").kind == WOLF


# --- the service, the API and the openapi contract -------------------------

def wired(tmp_path, **env):
    from romarr.app import ROMarr
    return ROMarr({"ROMARR_DATA": str(tmp_path / "s.json"), **env})


def test_no_host_configured_is_a_first_class_answer(tmp_path):
    service = wired(tmp_path)
    assert service.moonlight is None
    got = service.moonlight_status()
    assert got["configured"] is False
    assert "MOONLIGHT_HOST" in got["hint"]
    assert got["manual"] == playability.PAIRING_IS_MANUAL
    assert set(got["kinds"]) == {"wolf", "sunshine", "steam-headless"}


def test_configuring_a_host_is_visible_in_status(tmp_path):
    service = wired(tmp_path, MOONLIGHT_HOST="192.0.2.1",
                    MOONLIGHT_KIND="sunshine")
    service.moonlight.timeout = 0.05
    block = service.status()["moonlight"]
    assert block["configured"] is True
    assert block["kind"] == "sunshine"
    assert block["kind_label"] == "Sunshine"


def test_a_host_that_is_off_does_not_break_the_status_page(tmp_path):
    service = wired(tmp_path, MOONLIGHT_HOST="192.0.2.1:9")
    service.moonlight.timeout = 0.05
    status = service.status()
    assert status["moonlight"]["ok"] is False
    assert status["play_routes"]["total"] > 30


def test_the_pin_endpoint_refuses_when_no_host_is_configured(tmp_path):
    got = wired(tmp_path).moonlight_pin({"pin": "1234"})
    assert got["ok"] is False
    assert "no Moonlight host" in got["detail"]


def test_the_pin_endpoint_passes_the_secret_through(tmp_path):
    service = wired(tmp_path, MOONLIGHT_HOST="192.0.2.1")
    seen = {}
    service.moonlight.submit_pin = lambda pin, **kw: seen.update(
        pin=pin, **kw) or {"ok": True}

    service.moonlight_pin({"pin": "4321", "pair_secret": "deadbeef"})
    assert seen["pin"] == "4321"
    assert seen["pair_secret"] == "deadbeef"


def test_the_credential_is_never_written_to_the_store(tmp_path):
    """Same rule as ROMARR_API_KEY: a secret from the environment must not end
    up in a file that gets backed up."""
    service = wired(tmp_path, MOONLIGHT_HOST="192.0.2.1",
                    MOONLIGHT_USER="sunshine", MOONLIGHT_PASS="hunter2")
    dumped = json.dumps(service.store.settings)
    assert "hunter2" not in dumped
    assert "sunshine" not in dumped


def test_both_new_routes_are_documented(tmp_path):
    from romarr.openapi import DESCRIPTIONS, served_routes
    for path in ("/api/v1/moonlight", "/api/v1/moonlight/pin"):
        assert path in served_routes(), f"{path} is not served"
        assert path in DESCRIPTIONS, f"{path} is served but undescribed"


def test_the_status_page_renders_the_host_and_the_manual_step():
    from romarr.ui import page
    rendered = page()
    assert "moonlightCard" in rendered
    assert "/api/v1/moonlight/pin" in rendered
    # The UI must never call it "Paired" off the back of a submission.
    assert "cannot start the stream for you" in rendered
