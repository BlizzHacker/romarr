import json

from rommarr.store import DEFAULT_SETTINGS, Event, Store


def test_history_and_settings_survive_a_restart(tmp_path):
    """The whole point of the store: a restart used to lose everything."""
    path = tmp_path / "rommarr.json"
    s = Store(path)
    s.record(Event(kind="grabbed", game="Super Mario World", platform="snes",
                   release="Super Mario World (USA)", seeders=120))
    s.update_settings({"min_seeders": 5, "library_path": "/srv/roms"})

    reopened = Store(path)
    assert reopened.settings["min_seeders"] == 5
    assert reopened.settings["library_path"] == "/srv/roms"
    assert len(reopened.events) == 1
    assert reopened.events[0].game == "Super Mario World"


def test_a_setting_added_later_gets_its_default(tmp_path):
    """An older file must not leave a new setting missing entirely."""
    path = tmp_path / "rommarr.json"
    path.write_text(json.dumps({"settings": {"min_seeders": 9}}), encoding="utf-8")
    s = Store(path)
    assert s.settings["min_seeders"] == 9
    for key, value in DEFAULT_SETTINGS.items():
        if key != "min_seeders":
            assert s.settings[key] == value


def test_unknown_settings_are_dropped_rather_than_stored(tmp_path):
    """A typo in a PUT should not become permanent state nothing reads."""
    s = Store(tmp_path / "r.json")
    s.update_settings({"min_seeders": 3, "totally_made_up": "yes"})
    assert s.settings["min_seeders"] == 3
    assert "totally_made_up" not in s.settings


def test_a_corrupt_file_does_not_stop_the_service_starting(tmp_path):
    path = tmp_path / "r.json"
    path.write_text("{ this is not json", encoding="utf-8")
    s = Store(path)
    assert s.settings == DEFAULT_SETTINGS
    assert s.events == []


def test_requesting_the_same_game_twice_is_a_retry_not_a_duplicate(tmp_path):
    s = Store(tmp_path / "r.json")
    s.want("Contra", "nes")
    s.want("contra", "nes")          # different case, same game
    s.want("Contra", "snes")         # different platform, genuinely different
    assert len(s.wanted) == 2


def test_an_arrival_clears_the_wanted_entry(tmp_path):
    s = Store(tmp_path / "r.json")
    s.want("Zelda", "snes")
    assert s.fulfil("zelda", "snes") is True
    assert s.missing() == []
    # Fulfilling something that was never wanted is a no-op, not an error.
    assert s.fulfil("Zelda", "snes") is False


def test_failures_are_counted_against_the_wanted_entry(tmp_path):
    s = Store(tmp_path / "r.json")
    s.want("Earthbound", "snes")
    s.note_failure("Earthbound", "snes", "no usable release")
    s.note_failure("Earthbound", "snes", "no usable release")
    item = s.missing()[0]
    assert item["attempts"] == 2
    assert item["last_error"] == "no usable release"


def test_history_is_newest_first_and_filterable(tmp_path):
    s = Store(tmp_path / "r.json")
    s.record(Event(kind="grabbed", game="A", platform="nes"))
    s.record(Event(kind="failed", game="B", platform="nes"))
    s.record(Event(kind="imported", game="C", platform="nes"))

    assert [e["game"] for e in s.history()] == ["C", "B", "A"]
    assert [e["game"] for e in s.history(kind="failed")] == ["B"]
    assert len(s.history(limit=2)) == 2


def test_history_is_capped_so_the_file_cannot_grow_without_bound(tmp_path):
    path = tmp_path / "r.json"
    s = Store(path)
    s.MAX_EVENTS = 10
    for i in range(25):
        s.record(Event(kind="grabbed", game=f"Game {i}", platform="nes"))
    assert len(s.events) == 10
    # And the cap has to survive the round trip, not just live in memory.
    assert len(json.loads(path.read_text(encoding="utf-8"))["events"]) == 10


def test_a_write_leaves_no_temp_files_behind(tmp_path):
    s = Store(tmp_path / "r.json")
    s.record(Event(kind="grabbed", game="A", platform="nes"))
    leftovers = [p.name for p in tmp_path.iterdir() if p.name.startswith(".rommarr-")]
    assert leftovers == []


# --- remote path mapping ---------------------------------------------------

from rommarr.library import map_remote_path  # noqa: E402


def test_a_client_path_is_translated_to_one_we_can_open():
    """qBittorrent said /mnt/usb1/Downloads/nestest.nes and this process had
    that volume mounted elsewhere, so the import failed with "download path
    does not exist" while the file was sitting right there."""
    m = [{"remote": "/mnt/usb1/Downloads", "local": "/mnt/downloads"}]
    assert str(map_remote_path("/mnt/usb1/Downloads/nestest.nes", m)) \
        .replace("\\", "/") == "/mnt/downloads/nestest.nes"
    assert str(map_remote_path("/mnt/usb1/Downloads/sub/x.nes", m)) \
        .replace("\\", "/") == "/mnt/downloads/sub/x.nes"


def test_an_unmapped_path_is_left_alone():
    m = [{"remote": "/mnt/usb1/Downloads", "local": "/mnt/downloads"}]
    assert str(map_remote_path("/elsewhere/x.nes", m)).replace("\\", "/") == "/elsewhere/x.nes"
    assert str(map_remote_path("/x.nes", [])).replace("\\", "/") == "/x.nes"
    assert str(map_remote_path("/x.nes", None)).replace("\\", "/") == "/x.nes"


def test_the_most_specific_mapping_wins():
    """Otherwise which mapping applies depends on the order they were added."""
    m = [
        {"remote": "/mnt", "local": "/broad"},
        {"remote": "/mnt/usb1/Downloads", "local": "/exact"},
    ]
    assert str(map_remote_path("/mnt/usb1/Downloads/a.nes", m)) \
        .replace("\\", "/") == "/exact/a.nes"
    assert str(map_remote_path("/mnt/other/b.nes", m)).replace("\\", "/") == "/broad/other/b.nes"


def test_a_half_written_mapping_is_ignored_rather_than_applied():
    m = [{"remote": "/mnt/usb1", "local": ""}, {"remote": "", "local": "/x"}]
    assert str(map_remote_path("/mnt/usb1/a.nes", m)).replace("\\", "/") == "/mnt/usb1/a.nes"


def test_a_prefix_that_only_looks_similar_is_not_matched():
    """/mnt/usb1/Downloads2 is not inside /mnt/usb1/Downloads."""
    m = [{"remote": "/mnt/usb1/Downloads", "local": "/mnt/downloads"}]
    assert str(map_remote_path("/mnt/usb1/Downloads2/a.nes", m)) \
        .replace("\\", "/") == "/mnt/usb1/Downloads2/a.nes"


def test_two_stores_do_not_share_their_default_lists(tmp_path):
    """DEFAULT_SETTINGS holds mutable lists. A shallow copy handed every Store
    the same list object, so a client added in one leaked into all of them --
    and into the module default, poisoning every Store created afterwards."""
    a = Store(tmp_path / "a.json")
    b = Store(tmp_path / "b.json")
    a.put_item("download_clients", {"type": "qbittorrent", "name": "only in a"})

    assert len(a.list_items("download_clients")) == 1
    assert b.list_items("download_clients") == []
    assert DEFAULT_SETTINGS["download_clients"] == [], "the module default was mutated"
    # A third store created after the mutation must also be clean.
    assert Store(tmp_path / "c.json").list_items("download_clients") == []


# --- configuration CRUD ----------------------------------------------------

from rommarr.downloaders import (  # noqa: E402
    CLIENT_TYPES, SECRET_PLACEHOLDER, base_url_for, build_client, merge_secrets, redact,
)


def test_a_secret_is_never_sent_back_to_a_browser():
    cfg = {"type": "sabnzbd", "name": "sab", "api_key": "REAL-KEY", "host": "h"}
    out = redact(cfg)
    assert out["api_key"] == SECRET_PLACEHOLDER
    assert "REAL-KEY" not in str(out)
    # A blank secret stays blank rather than becoming asterisks, so the form
    # does not claim a credential exists when none does.
    assert redact({"type": "sabnzbd", "api_key": ""})["api_key"] == ""


def test_saving_an_unedited_form_keeps_the_stored_secret():
    """The edit form shows asterisks. Saving that verbatim would replace the
    real credential with eight asterisks, and the client would then fail to
    authenticate with nothing saying why."""
    merged = merge_secrets(
        {"type": "qbittorrent", "password": SECRET_PLACEHOLDER, "name": "renamed"},
        {"type": "qbittorrent", "password": "REAL", "name": "old"})
    assert merged["password"] == "REAL"
    assert merged["name"] == "renamed"


def test_a_deliberately_changed_secret_is_taken():
    merged = merge_secrets({"type": "qbittorrent", "password": "NEW"},
                           {"type": "qbittorrent", "password": "OLD"})
    assert merged["password"] == "NEW"


def test_url_is_assembled_from_the_parts_the_form_edits():
    assert base_url_for({"host": "h", "port": 8080}) == "http://h:8080"
    assert base_url_for({"host": "h", "port": 443, "use_ssl": True}) == "https://h:443"
    assert base_url_for({"host": "h", "port": 80, "url_base": "/qb/"}) == "http://h:80/qb"
    # Somebody will paste a whole URL into the host box.
    assert base_url_for({"host": "https://h", "port": 8080}) == "https://h:8080"
    assert base_url_for({}) == "http://localhost"


def test_every_client_type_builds_and_declares_a_protocol():
    for kind, spec in CLIENT_TYPES.items():
        client = build_client({"type": kind, "host": "h", "port": 1234, "name": "x"})
        assert client is not None, kind
        assert client.protocol == spec["protocol"], kind
        assert client.display_name == "x", kind
    assert build_client({"type": "nope"}) is None


def test_every_declared_field_has_what_the_form_needs():
    """The form is generated from this, so a field missing a name or type
    renders as a broken input rather than failing loudly."""
    for kind, spec in CLIENT_TYPES.items():
        assert spec["fields"], kind
        for f in spec["fields"]:
            assert f.get("name") and f.get("label") and f.get("type"), (kind, f)


def test_the_config_endpoint_never_returns_a_stored_credential(tmp_path):
    """This endpoint feeds a browser. Returning the raw settings put real
    credentials -- including the qBittorrent password -- in a page anyone who
    could reach the UI could read."""
    from rommarr.app import Rommarr
    svc = Rommarr(env={
        "ROMMARR_DATA": str(tmp_path / "r.json"),
        "QBITTORRENT_URL": "http://qbit:8090",
        "QBITTORRENT_USER": "admin", "QBITTORRENT_PASS": "REAL-PASSWORD",
        "PROWLARR_URL": "http://prowlarr:9696", "PROWLARR_API_KEY": "REAL-KEY",
    })
    body = json.dumps(svc.safe_settings())
    assert "REAL-PASSWORD" not in body
    assert "REAL-KEY" not in body
    assert SECRET_PLACEHOLDER in body

    # ...while the stored copy keeps the real value, or nothing could connect.
    stored = svc.store.list_items("download_clients")[0]
    assert stored["password"] == "REAL-PASSWORD"


def test_prowlarr_exposes_its_indexer_listing():
    """This method was appended below the class and Python parsed it as a
    nested function inside sanitise_for_display -- so it existed in the file,
    was never reachable, and the Indexers page silently showed nothing."""
    from rommarr.indexers import Prowlarr
    assert callable(getattr(Prowlarr, "indexers", None))


def test_the_library_view_is_not_shadowed_by_the_library_path(tmp_path):
    """`self.library` is the ROM library Path. A method of the same name is
    shadowed by the instance attribute, so the route called a PosixPath and the
    connection closed with no response at all."""
    from pathlib import Path
    from rommarr.app import Rommarr
    svc = Rommarr(env={"ROMMARR_DATA": str(tmp_path / "r.json"),
                       "ROMM_LIBRARY": str(tmp_path)})
    assert isinstance(svc.library, Path)
    view = svc.library_view()
    assert isinstance(view, dict) and "items" in view
    # Before the first fetch lands it must say so rather than claim emptiness.
    assert view["loading"] is True


# --- romm token expiry -------------------------------------------------------
#
# The token is short-lived. Caching it for the life of the process made the
# library refresh correctly at startup and then fail identically forever after,
# which reads as a leak rather than an expired credential.

class _FakeResponse:
    def __init__(self, status): self.status_code = status; self.ok = status < 400
    def json(self): return {"items": [], "total": 0, "access_token": "t2"}
    def raise_for_status(self):
        if not self.ok: raise RuntimeError(f"HTTP {self.status_code}")


class _ExpiringSession:
    """Answers 401 once, then succeeds — a token that expired mid-life."""
    def __init__(self): self.gets = []; self.posts = 0
    def post(self, url, **kw):
        self.posts += 1
        return _FakeResponse(200)
    def get(self, url, **kw):
        self.gets.append(kw.get("headers", {}).get("Authorization"))
        return _FakeResponse(401 if len(self.gets) == 1 else 200)


def test_an_expired_romm_token_is_replaced_rather_than_returned_forever():
    from rommarr.clients import Romm, RommConfig
    session = _ExpiringSession()
    romm = Romm(RommConfig(base_url="http://romm", username="u", password="p"),
                session=session)
    romm.games(limit=5)
    # Two attempts: the 401, then the retry with a freshly fetched token.
    assert len(session.gets) == 2
    assert session.posts == 2, "should have re-authenticated after the 401"


def test_a_403_is_not_retried():
    # A permissions problem; another token gives the same answer, so retrying
    # only doubles the load on an already-struggling service.
    from rommarr.clients import Romm, RommConfig

    class Forbidden(_ExpiringSession):
        def get(self, url, **kw):
            self.gets.append(1)
            return _FakeResponse(403)

    session = Forbidden()
    romm = Romm(RommConfig(base_url="http://romm", username="u", password="p"),
                session=session)
    try:
        romm.games(limit=5)
    except Exception:
        pass
    assert len(session.gets) == 1
