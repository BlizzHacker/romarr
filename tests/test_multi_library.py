"""More than one library server, and which one a request goes to.

One library was a fair simplification until it was not: people run a RomM and a
Retrom, or two RomMs, and "the *arr for games" has to mean the same thing for
them as for somebody with exactly one.
"""

from romarr.app import QueueItem, Romarr
from romarr.libraries import route_library


def svc(tmp_path, **env):
    base = {"ROMARR_DATA": str(tmp_path / "s.json")}
    base.update(env)
    return Romarr(base)


# --- routing ----------------------------------------------------------------

def test_a_platform_rule_beats_the_default():
    """Naming a platform is the more specific statement, so it wins -- the same
    precedence a longest-prefix remote path mapping uses."""
    configs = [
        {"id": "a", "name": "RomM", "is_default": True},
        {"id": "b", "name": "Retrom", "platforms": ["n64"]},
    ]
    assert route_library(configs, "n64")["id"] == "b"
    assert route_library(configs, "snes")["id"] == "a"


def test_a_single_library_needs_no_default_ticked():
    """An install with one library must not have to know that "default" was a
    box it needed to tick."""
    assert route_library([{"id": "only", "name": "RomM"}], "snes")["id"] == "only"


def test_a_disabled_library_is_never_routed_to():
    configs = [
        {"id": "off", "platforms": ["snes"], "enable": False},
        {"id": "on", "is_default": True},
    ]
    assert route_library(configs, "snes")["id"] == "on"


def test_platform_rules_accept_a_comma_separated_string():
    """The settings file is hand-edited as often as it is form-filled, and a
    string is the shape a plain text input produces."""
    configs = [{"id": "b", "platforms": "n64, snes"}, {"id": "a", "is_default": True}]
    assert route_library(configs, "snes")["id"] == "b"
    assert route_library(configs, "gba")["id"] == "a"


def test_no_libraries_routes_nowhere():
    assert route_library([], "snes") is None
    assert route_library([{"id": "x", "enable": False}], "snes") is None


# --- the service ------------------------------------------------------------

def test_the_environment_seeds_one_default_library(tmp_path):
    s = svc(tmp_path, LIBRARY_KIND="romm", LIBRARY_URL="http://romm.example",
            LIBRARY_USERNAME="u", LIBRARY_PASSWORD="p",
            LIBRARY_PATH=str(tmp_path / "roms"))
    stored = s.store.list_items("libraries")
    assert len(stored) == 1
    assert stored[0]["type"] == "romm"
    assert stored[0]["is_default"] is True
    assert stored[0]["path"] == str(tmp_path / "roms")
    assert stored[0]["username"] == "u"


def test_an_install_that_predates_libraries_still_gets_one_seeded(tmp_path):
    """_seeded_clients is already set on every existing install, so reusing it
    as the gate would leave an upgrade with an empty Libraries page and nowhere
    to import to -- an upgrade that silently unconfigures the thing."""
    data = str(tmp_path / "s.json")
    env = {"ROMARR_DATA": data, "LIBRARY_URL": "http://romm.example",
           "LIBRARY_PATH": str(tmp_path / "roms")}
    first = Romarr(env)
    # Rewind to what an older settings file looks like: clients seeded, and no
    # knowledge of libraries at all.
    first.store.settings.pop("_seeded_libraries", None)
    first.store.settings["libraries"] = []
    first.store.settings["_seeded_clients"] = True
    first.store.save()

    assert len(Romarr(env).store.list_items("libraries")) == 1


def test_seeding_happens_once_not_on_every_restart(tmp_path):
    data = str(tmp_path / "s.json")
    env = {"ROMARR_DATA": data, "LIBRARY_URL": "http://romm.example",
           "LIBRARY_PATH": str(tmp_path / "roms")}
    Romarr(env)
    Romarr(env)
    assert len(Romarr(env).store.list_items("libraries")) == 1


def test_each_library_keeps_its_own_path(tmp_path):
    """Two libraries are two applications with two library roots. Filing into
    the wrong one is worse than not filing at all, because it looks like it
    worked."""
    s = svc(tmp_path, LIBRARY_URL="http://romm.example",
            LIBRARY_PATH=str(tmp_path / "romm"))
    s.store.put_item("libraries", {
        "type": "retrom", "name": "Retrom", "enable": True,
        "url": "http://retrom.example:5101", "path": str(tmp_path / "retrom"),
        "platforms": ["n64"],
    })
    s.reload_libraries()

    cfg, _ = s.library_for("n64")
    assert s.library_root(cfg) == tmp_path / "retrom"
    cfg, _ = s.library_for("snes")
    assert s.library_root(cfg) == tmp_path / "romm"


def test_two_servers_of_the_same_kind_are_allowed(tmp_path):
    """The most common "multi server" case is not three different applications --
    it is two RomMs."""
    s = svc(tmp_path, LIBRARY_URL="http://romm-one.example",
            LIBRARY_PATH=str(tmp_path / "one"))
    s.store.put_item("libraries", {
        "type": "romm", "name": "RomM (kids)", "enable": True,
        "url": "http://romm-two.example", "path": str(tmp_path / "two"),
        "platforms": ["gb", "gbc"],
    })
    s.reload_libraries()

    assert len(s.game_libraries) == 2
    cfg, _ = s.library_for("gbc")
    assert cfg["name"] == "RomM (kids)"
    cfg, _ = s.library_for("snes")
    assert s.library_root(cfg) == tmp_path / "one"


def test_the_default_library_still_drives_the_legacy_attributes(tmp_path):
    """status(), health() and the container healthcheck all read self.romm and
    self.library. They have to keep pointing at the default library, or a
    single-library install regresses in order to prove a multi-library feature.
    """
    s = svc(tmp_path, LIBRARY_URL="http://romm.example",
            LIBRARY_PATH=str(tmp_path / "romm"))
    assert s.library == tmp_path / "romm"
    assert s.romm is s.game_library
    assert s.game_library is s.game_libraries[0][1]


def test_libraries_status_reports_every_server_and_its_path(tmp_path):
    (tmp_path / "romm").mkdir()
    s = svc(tmp_path, LIBRARY_URL="http://romm.example",
            LIBRARY_PATH=str(tmp_path / "romm"))
    s.store.put_item("libraries", {
        "type": "retrom", "name": "Retrom", "enable": True,
        "url": "http://retrom.example:5101", "path": str(tmp_path / "missing"),
    })
    s.reload_libraries()

    rows = {r["name"]: r for r in s.libraries_status()}
    assert rows["RomM"]["path_exists"] is True
    assert rows["Retrom"]["path_exists"] is False     # never mounted here
    assert rows["RomM"]["is_default"] is True


def test_a_credential_never_reaches_the_settings_endpoint(tmp_path):
    s = svc(tmp_path, LIBRARY_URL="http://romm.example",
            LIBRARY_PASSWORD="hunter2", LIBRARY_PATH=str(tmp_path / "roms"))
    out = s.safe_settings()
    assert out["libraries"][0]["password"] == "********"
    assert "hunter2" not in repr(out)


def test_an_unknown_library_kind_is_skipped_rather_than_fatal(tmp_path):
    """One bad row must not stop the service starting, or a typo on the
    Libraries page becomes an outage that cannot be fixed through the UI that
    caused it."""
    s = svc(tmp_path, LIBRARY_URL="http://romm.example",
            LIBRARY_PATH=str(tmp_path / "roms"))
    s.store.put_item("libraries", {"type": "nonsense", "name": "Bad", "enable": True,
                                   "url": "http://x", "path": str(tmp_path)})
    s.reload_libraries()
    assert [c.get("type") for c, _ in s.game_libraries] == ["romm"]


def test_a_finished_download_with_no_library_is_recorded_not_dropped(tmp_path):
    """Silence would leave a download that completed and simply never
    appeared."""
    s = svc(tmp_path)
    assert s.game_libraries == []
    s.queue.append(QueueItem("Contra", "nes", "Contra (U).nes", 1, "grabbed", ""))

    class FakeClient:
        name = "fake"
        configured = True

        def completed(self):
            return [{"name": "Contra (U).nes", "content_path": str(tmp_path / "d")}]

    s.clients = [FakeClient()]
    results = s.import_finished()

    assert results and results[0]["ok"] is False
    assert "no library configured" in results[0]["reason"]
    assert any(e["kind"] == "failed" and "no library configured" in e["detail"]
               for e in s.store.history())


def test_adding_a_library_clears_the_stale_no_library_message(tmp_path):
    """The background refresh caches "no library configured" and then sleeps for
    COUNT_TTL. Adding a library used to leave that message on the shelf for five
    minutes -- true when written, wrong by the time anybody read it."""
    s = svc(tmp_path)
    s._library_cache = (None, 0.0, "no library configured")

    s.store.put_item("libraries", {
        "type": "romm", "name": "RomM", "enable": True,
        "url": "http://romm.example", "path": str(tmp_path / "roms"),
    })
    s.reload_libraries()

    assert s._library_cache[2] == ""
    assert s.library_view()["error"] == ""


def test_a_library_change_wakes_the_refresh_rather_than_waiting_it_out(tmp_path):
    s = svc(tmp_path)
    s._refresh_now.clear()
    s.reload_libraries()
    assert s._refresh_now.is_set(), "reload must wake the background refresh"


# --- diagnosing a missing library path --------------------------------------
#
# "ROM library: Not available /mnt/roms" was the first thing a new user
# reported. It is true and it helps nobody: two different mistakes produce it and
# they need opposite fixes.

def test_a_missing_path_says_to_mount_it(tmp_path):
    s = svc(tmp_path, LIBRARY_URL="http://romm.example",
            LIBRARY_PATH=str(tmp_path / "never-made"))
    hint = s.path_hint(tmp_path / "never-made")
    assert "does not exist in this container" in hint
    assert "Mount your library volume there" in hint


def test_a_stored_path_that_lost_to_the_environment_says_to_use_settings(tmp_path):
    """The trap worth naming: a path stored on first run outranks the
    environment forever, so correcting LIBRARY_PATH in compose appears to do
    nothing. If the environment names a path that exists and the stored one does
    not, that is what happened."""
    mounted = tmp_path / "roms"
    mounted.mkdir()
    data = str(tmp_path / "s.json")

    first = Romarr({"ROMARR_DATA": data, "LIBRARY_PATH": str(tmp_path / "mnt-roms")})
    assert first.store.settings["library_path"] == str(tmp_path / "mnt-roms")

    # Operator corrects compose to the path they actually mounted.
    second = Romarr({"ROMARR_DATA": data, "LIBRARY_PATH": str(mounted)})
    assert second.library == tmp_path / "mnt-roms"        # stored value still wins

    hint = second.path_hint(second.library)
    assert str(mounted) in hint
    assert "stored path" in hint and "Settings page" in hint


def test_a_path_that_exists_has_no_hint(tmp_path):
    (tmp_path / "roms").mkdir()
    s = svc(tmp_path, LIBRARY_URL="http://romm.example",
            LIBRARY_PATH=str(tmp_path / "roms"))
    assert s.path_hint(tmp_path / "roms") == ""
    assert s.health()["library_path_hint"] == ""


def test_the_hint_reaches_every_surface_that_reports_the_path(tmp_path):
    s = svc(tmp_path, LIBRARY_URL="http://romm.example",
            LIBRARY_PATH=str(tmp_path / "gone"))
    assert s.health()["library_path_hint"]
    assert s.status()["library_path_hint"]
    assert s.libraries_status()[0]["path_hint"]
