"""Every backend must answer the same four questions the same way."""

import pytest

from romarr.libraries import (
    Game,
    GaseousConfig,
    GaseousLibrary,
    Library,
    RetromConfig,
    RetromLibrary,
    build_library,
)


class FakeResponse:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status
        self.ok = status < 400

    def json(self):
        return self._payload

    def raise_for_status(self):
        if not self.ok:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeSession:
    """Records calls so the shape of a request can be asserted, not guessed."""

    def __init__(self, payload):
        self.payload = payload
        self.gets = []
        self.posts = []

    def get(self, url, **kw):
        self.gets.append((url, kw))
        return FakeResponse(self.payload)

    def post(self, url, **kw):
        self.posts.append((url, kw))
        return FakeResponse(self.payload)


# --- Gaseous ---------------------------------------------------------------

def test_gaseous_lists_games():
    session = FakeSession({"count": 2, "games": [
        {"id": 7, "name": "Sonic", "platformName": "Mega Drive"},
        {"id": 8, "name": "Ecco"},
    ]})
    lib = GaseousLibrary(GaseousConfig(base_url="http://g"), session=session)
    games = lib.games(limit=10)
    assert [g.name for g in games] == ["Sonic", "Ecco"]
    assert games[0].platform == "Mega Drive"
    assert games[0].cover.startswith("http://g/api/v1.1/Games/7/")


def test_gaseous_searches_with_a_post_not_a_get():
    """Its listing endpoint is a POST; a GET returns 405 and reads as a bad path."""
    session = FakeSession({"games": []})
    GaseousLibrary(GaseousConfig(base_url="http://g"), session=session).games()
    assert session.posts, "expected a POST to the games endpoint"
    assert session.posts[0][0].endswith("/api/v1.1/Games")
    assert "json" in session.posts[0][1]


def test_gaseous_count_prefers_the_declared_total():
    session = FakeSession({"count": 4212, "games": [{"id": 1, "name": "x"}]})
    assert GaseousLibrary(GaseousConfig(base_url="http://g"), session=session).count() == 4212


# --- Retrom ----------------------------------------------------------------

def test_retrom_reads_protobuf_shaped_json():
    session = FakeSession({"totalCount": 1, "games": [
        {"id": "11", "platformId": "3",
         "metadata": {"name": "Chrono Trigger", "coverUrl": "/covers/11.png"}},
    ]})
    lib = RetromLibrary(RetromConfig(base_url="http://r"), session=session)
    g = lib.games()[0]
    assert g.name == "Chrono Trigger"
    assert g.cover == "http://r/covers/11.png"


def test_retrom_falls_back_to_the_filename_not_the_whole_path():
    """Without metadata a bare path would otherwise be shown to a user."""
    session = FakeSession({"games": [
        {"id": "12", "path": "/mnt/roms/snes/Secret of Mana.sfc"},
    ]})
    lib = RetromLibrary(RetromConfig(base_url="http://r"), session=session)
    assert lib.games()[0].name == "Secret of Mana.sfc"


# --- registry --------------------------------------------------------------

def test_build_library_selects_each_backend():
    env = {"LIBRARY_URL": "http://x"}
    assert build_library("gaseous", env).name == "Gaseous"
    assert build_library("retrom", env).name == "Retrom"
    assert build_library("romm", env).name == "RomM"


def test_unknown_backend_is_refused_rather_than_defaulted():
    """Silently importing into the wrong library is worse than a clear error."""
    with pytest.raises(ValueError, match="unknown library kind"):
        build_library("mystery", {"LIBRARY_URL": "http://x"})


def test_existing_romm_settings_keep_working():
    """An install that predates the rename must not need reconfiguring."""
    lib = build_library("romm", {"ROMM_URL": "http://old", "ROMM_USERNAME": "u"})
    assert lib.name == "RomM"
    assert lib.configured


def test_every_backend_satisfies_the_protocol():
    env = {"LIBRARY_URL": "http://x"}
    for kind in ("romm", "gaseous", "retrom"):
        assert isinstance(build_library(kind, env), Library), kind


def test_library_view_serialises_games_and_names_the_backend(tmp_path):
    """The HTTP layer must emit plain JSON, not dataclasses, and say which
    library it is reading -- a Romarr pointed at the wrong one should be
    obvious from the page rather than from a config file."""
    from dataclasses import asdict

    from romarr.app import Romarr

    svc = Romarr({"ROMMARR_DATA": str(tmp_path / "s.json"),
                  "ROMM_LIBRARY": str(tmp_path / "lib"),
                  "LIBRARY_KIND": "gaseous",
                  "LIBRARY_URL": "http://gaseous.example"})
    assert svc.game_library.name == "Gaseous"

    svc._library_cache = ([Game(id="1", name="Sonic", platform="MD", cover="")], 1.0, "")
    view = svc.library_view()
    assert view["library"] == "Gaseous"
    assert view["items"] == [asdict(Game(id="1", name="Sonic", platform="MD", cover=""))]
    # Must survive json.dumps, which a dataclass would not.
    import json
    json.dumps(view)
