"""The service's HTTP surface: inbound webhooks from a request front-end."""

from romarr.app import ROMarr


# --- inbound request webhooks ----------------------------------------------
#
# GG Requestz posts a notification-shaped event rather than a request body of
# our choosing, so the mapping from its payload to ours is the thing that has
# to hold.

def test_webhook_reads_gg_requestz_payload():
    game, platform = ROMarr.parse_request_webhook({
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
    assert ROMarr.parse_request_webhook({
        "type": "game_request",
        "title": "New Game Request: Contra",
        "data": {"platforms": ["NES"]},
    }) == ("Contra", "NES")


def test_webhook_ignores_events_that_are_not_game_requests():
    # A webhook URL receives whatever anybody points at it. Guessing a game
    # out of an unrelated event would start a download nobody asked for.
    assert ROMarr.parse_request_webhook({"type": "user_registered",
                                          "data": {"user": "wade"}}) is None
    assert ROMarr.parse_request_webhook({"data": {}}) is None
    assert ROMarr.parse_request_webhook("not a dict") is None


def test_webhook_records_an_unknown_platform_rather_than_dropping_it(tmp_path):
    # An unmapped platform name is a mapping problem somebody can fix. Silence
    # would leave a request that simply never happened, with nothing to look at.
    svc = ROMarr({"ROMARR_DATA": str(tmp_path / "s.json"),
                   "ROMM_LIBRARY": str(tmp_path / "lib")})
    result = svc.handle_request_webhook({
        "type": "game_request",
        "data": {"game_title": "Halo", "platforms": ["Sega Nomad"]},
    })
    assert result["ok"] is False
    assert "Sega Nomad" in result["error"]
    assert any(e.get("kind") == "failed" and "Sega Nomad" in (e.get("detail") or "")
               for e in svc.store.history())


# --- where ROMs are filed ---------------------------------------------------
#
# The library path is configurable three ways -- LIBRARY_PATH, the older
# ROMM_LIBRARY, and the Settings page -- and for a long time only the third of
# those did anything.

def test_the_environment_sets_the_library_path_on_a_fresh_install(tmp_path):
    """DEFAULT_SETTINGS carried a library_path, so the "saved" value was always
    truthy and always won -- including on a first boot where nobody had saved
    anything. Both documented environment variables were dead: the default
    simply happened to match the path the docs used as their example."""
    svc = ROMarr({"ROMARR_DATA": str(tmp_path / "s.json"),
                  "LIBRARY_PATH": str(tmp_path / "roms")})
    assert svc.library == tmp_path / "roms"


def test_the_older_romm_library_name_still_sets_it(tmp_path):
    svc = ROMarr({"ROMARR_DATA": str(tmp_path / "s.json"),
                  "ROMM_LIBRARY": str(tmp_path / "old")})
    assert svc.library == tmp_path / "old"


def test_library_path_wins_over_romm_library_when_both_are_given(tmp_path):
    svc = ROMarr({"ROMARR_DATA": str(tmp_path / "s.json"),
                  "LIBRARY_PATH": str(tmp_path / "new"),
                  "ROMM_LIBRARY": str(tmp_path / "old")})
    assert svc.library == tmp_path / "new"


def test_a_path_chosen_in_the_ui_outlives_a_restart_and_beats_the_environment(tmp_path):
    """The reason the environment cannot simply win: a path set on the Settings
    page is a decision, and an environment default must not silently undo it on
    the next restart."""
    data = str(tmp_path / "s.json")
    env = {"ROMARR_DATA": data, "LIBRARY_PATH": str(tmp_path / "from-env")}
    first = ROMarr(env)
    first.store.update_settings({"library_path": str(tmp_path / "chosen")})

    assert ROMarr(env).library == tmp_path / "chosen"


def test_the_effective_path_is_stored_so_the_settings_page_shows_it(tmp_path):
    """An empty field on the Settings page would read as "unset" while the
    service was busy filing ROMs into the environment's path."""
    svc = ROMarr({"ROMARR_DATA": str(tmp_path / "s.json"),
                  "LIBRARY_PATH": str(tmp_path / "roms")})
    assert svc.store.settings["library_path"] == str(tmp_path / "roms")


# --- download categories ----------------------------------------------------
#
# ROMarr labels its own jobs so they are distinguishable in a shared client. The
# label was hardcoded, which left an env-configured install -- every Docker one
# -- unable to change it without opening the UI.

def test_each_client_takes_its_category_from_the_environment(tmp_path):
    svc = ROMarr({"ROMARR_DATA": str(tmp_path / "s.json"),
                  "QBITTORRENT_URL": "http://qbit:8080",
                  "SABNZBD_URL": "http://sab:8080",
                  "NZBGET_URL": "http://nzbget:6789",
                  "QBITTORRENT_CATEGORY": "roms-torrent",
                  "SABNZBD_CATEGORY": "software",
                  "NZBGET_CATEGORY": "roms-usenet"})
    assert svc.qbit._config.category == "roms-torrent"
    assert svc.sab._config.category == "software"
    assert svc.nzbget._config.category == "roms-usenet"


def test_the_category_defaults_to_romarr_for_every_client(tmp_path):
    svc = ROMarr({"ROMARR_DATA": str(tmp_path / "s.json"),
                  "QBITTORRENT_URL": "http://qbit:8080",
                  "SABNZBD_URL": "http://sab:8080",
                  "NZBGET_URL": "http://nzbget:6789"})
    assert svc.qbit._config.category == "romarr"
    assert svc.sab._config.category == "romarr"
    assert svc.nzbget._config.category == "romarr"


def test_the_seeded_client_config_records_the_same_category(tmp_path):
    """The live client and the stored client configuration have to agree, or the
    Download Clients page shows a category the service is not actually using."""
    svc = ROMarr({"ROMARR_DATA": str(tmp_path / "s.json"),
                  "SABNZBD_URL": "http://sab:8080",
                  "SABNZBD_CATEGORY": "software"})
    stored = [c for c in svc.store.list_items("download_clients") if c["type"] == "sabnzbd"]
    assert stored and stored[0]["category"] == "software"


def test_an_empty_category_variable_falls_back_rather_than_sending_nothing(tmp_path):
    """SABNZBD_CATEGORY= in an env file is an unset variable, not a request for
    an empty category -- sending `cat=` would drop the label entirely."""
    svc = ROMarr({"ROMARR_DATA": str(tmp_path / "s.json"),
                  "SABNZBD_URL": "http://sab:8080",
                  "SABNZBD_CATEGORY": ""})
    assert svc.sab._config.category == "romarr"


# --- failures must arrive as replies ----------------------------------------

def test_every_verb_is_guarded_so_a_crash_is_a_500_not_a_dead_socket():
    """An exception reaching BaseHTTPRequestHandler logs a traceback and closes
    the connection having sent nothing: the caller sees no status code at all --
    curl reports 000 -- so there is nothing to search for and no way to tell a
    crash from a network problem. A search that exceeded Prowlarr's 60s timeout
    did exactly this.

    Every verb now goes through `_gate`, which authenticates and then hands
    off to `_guard` -- so the rule this pins is unchanged, but it is one layer
    further out. Both links are asserted, because a `_gate` that stopped
    calling `_guard` would restore the dead socket without failing anything
    else.
    """
    import io as _io
    import pathlib
    src = _io.open(pathlib.Path(__file__).resolve().parents[1] / "romarr" / "app.py",
                   encoding="utf-8").read()
    for verb, inner in (("do_GET", "_get"), ("do_POST", "_post"),
                        ("do_PUT", "_put"), ("do_DELETE", "_delete")):
        assert f"def {verb}(self):\n            return self._gate(self.{inner})" in src, verb
    assert "def _guard(self, handler):" in src
    assert "return self._guard(handler)" in src, "_gate must still guard"


def test_an_unresolvable_platform_is_reported_rather_than_silently_ignored(tmp_path):
    """A name that resolves to nothing is searched with no platform evidence,
    which quietly changes what the scores mean, so it has to be said out loud.

    This used to use `psx` as its example of an unmodelled platform. It is
    modelled now -- that is the point of the disc work -- so the example moved
    to a console generation nothing in this project can acquire or play.
    """
    svc = ROMarr({"ROMARR_DATA": str(tmp_path / "s.json")})
    out = svc.search("Astro Bot", "playstation 5")
    assert out["unknown_platform"] == "playstation 5"
    assert out["platform"] is None

    known = svc.search("Super Metroid", "snes")
    assert known["unknown_platform"] is None
