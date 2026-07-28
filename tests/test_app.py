"""The service's HTTP surface: inbound webhooks from a request front-end."""

from rommarr.app import Rommarr


# --- inbound request webhooks ----------------------------------------------
#
# GG Requestz posts a notification-shaped event rather than a request body of
# our choosing, so the mapping from its payload to ours is the thing that has
# to hold.

def test_webhook_reads_gg_requestz_payload():
    game, platform = Rommarr.parse_request_webhook({
        "type": "game_request",
        "title": "New Game Request: Chrono Trigger",
        "message": "requested by wade",
        "priority": 5,
        "data": {"request_id": 42, "game_title": "Chrono Trigger",
                 "igdb_id": 1234, "platforms": ["Super Nintendo"]},
    })
    assert game == "Chrono Trigger"
    assert platform == "Super Nintendo"


def test_webhook_falls_back_to_the_human_title():
    # data.game_title is what we want, but the title carries the same name in
    # a fixed "New Game Request: <name>" shape when it is missing.
    assert Rommarr.parse_request_webhook({
        "type": "game_request",
        "title": "New Game Request: Contra",
        "data": {"platforms": ["NES"]},
    }) == ("Contra", "NES")


def test_webhook_ignores_events_that_are_not_game_requests():
    # A webhook URL receives whatever anybody points at it. Guessing a game
    # out of an unrelated event would start a download nobody asked for.
    assert Rommarr.parse_request_webhook({"type": "user_registered",
                                          "data": {"user": "wade"}}) is None
    assert Rommarr.parse_request_webhook({"data": {}}) is None
    assert Rommarr.parse_request_webhook("not a dict") is None


def test_webhook_records_an_unknown_platform_rather_than_dropping_it(tmp_path):
    # An unmapped platform name is a mapping problem somebody can fix. Silence
    # would leave a request that simply never happened, with nothing to look at.
    svc = Rommarr({"ROMMARR_DATA": str(tmp_path / "s.json"),
                   "ROMM_LIBRARY": str(tmp_path / "lib")})
    result = svc.handle_request_webhook({
        "type": "game_request",
        "data": {"game_title": "Halo", "platforms": ["Sega Nomad"]},
    })
    assert result["ok"] is False
    assert "Sega Nomad" in result["error"]
    assert any(e.get("kind") == "failed" and "Sega Nomad" in (e.get("detail") or "")
               for e in svc.store.history())
