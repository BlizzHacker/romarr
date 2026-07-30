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
    def __init__(self, payload, status=200, raw=b""):
        self._payload = payload
        self.status_code = status
        self.ok = status < 400
        self.content = raw

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

def _varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def _tag(field: int, wire: int) -> bytes:
    return _varint((field << 3) | wire)


def _msg(field: int, payload: bytes) -> bytes:
    return _tag(field, 2) + _varint(len(payload)) + payload


def _str(field: int, value: str) -> bytes:
    raw = value.encode()
    return _tag(field, 2) + _varint(len(raw)) + raw


def _int(field: int, value: int) -> bytes:
    return _tag(field, 0) + _varint(value)


def _grpc_web(payload: bytes) -> bytes:
    """A message frame followed by the trailer frame a real server sends."""
    body = bytes([0]) + len(payload).to_bytes(4, "big") + payload
    trailer = b"grpc-status:0"
    # High bit set marks the trailer, which carries headers not a message.
    return body + bytes([0x80]) + len(trailer).to_bytes(4, "big") + trailer


class GrpcSession:
    def __init__(self, payload: bytes):
        self.payload = payload
        self.posts = []

    def post(self, url, **kw):
        self.posts.append((url, kw))
        return FakeResponse(None, raw=_grpc_web(self.payload))

    def get(self, url, **kw):
        return FakeResponse(None, status=303)


def test_retrom_decodes_grpc_web_games_and_metadata():
    """Retrom has no REST listing at all; games come back over grpc-web."""
    game = _msg(1, _int(1, 11) + _str(3, "/library/snes/Chrono Trigger.sfc") + _int(4, 3))
    meta = _msg(2, _int(1, 11) + _str(2, "Chrono Trigger") + _str(4, "/covers/11.png"))
    session = GrpcSession(game + meta)

    lib = RetromLibrary(RetromConfig(base_url="http://r"), session=session)
    games = lib.games()
    assert len(games) == 1
    g = games[0]
    assert g.id == "11"
    assert g.name == "Chrono Trigger"
    assert g.platform == "3"
    assert g.cover == "http://r/covers/11.png"
    # It must ask for metadata, or every tile shows a filename.
    assert session.posts[0][1]["data"].endswith(bytes([(3 << 3) | 0, 1]))


def test_retrom_ignores_the_trailer_frame():
    """The trailer carries headers, not a message; decoding it yields nonsense."""
    game = _msg(1, _int(1, 5) + _str(3, "/library/x.rom"))
    lib = RetromLibrary(RetromConfig(base_url="http://r"), session=GrpcSession(game))
    assert [g.id for g in lib.games()] == ["5"]


def test_retrom_falls_back_to_the_filename_not_the_whole_path():
    """Without metadata a bare path would otherwise be shown to a user."""
    game = _msg(1, _int(1, 12) + _str(3, "/mnt/roms/snes/Secret of Mana.sfc"))
    lib = RetromLibrary(RetromConfig(base_url="http://r"), session=GrpcSession(game))
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
    library it is reading -- a ROMarr pointed at the wrong one should be
    obvious from the page rather than from a config file."""
    from dataclasses import asdict

    from romarr.app import ROMarr

    svc = ROMarr({"ROMARR_DATA": str(tmp_path / "s.json"),
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


# --- contracts learned from running the real servers -----------------------

def test_gaseous_logs_in_before_listing():
    """Gaseous authenticates with a session cookie, not a bearer token.

    An unauthenticated call does not return 401 -- it redirects to
    /Identity/Account/Login, which 404s under /api, so the failure arrives
    looking like a missing endpoint.
    """
    session = FakeSession({"count": 0, "games": []})
    lib = GaseousLibrary(GaseousConfig(base_url="http://g", username="u", password="p"),
                         session=session)
    lib.games()
    assert any(url.endswith("/Account/Login") for url, _ in session.posts), \
        "expected a login before listing games"


def test_gaseous_sends_every_required_filter_field():
    """Name, Genre, Theme, GameMode, Platform and PlayerPerspective are all
    required even when empty; omitting them is a 400 naming each one."""
    session = FakeSession({"count": 0, "games": []})
    GaseousLibrary(GaseousConfig(base_url="http://g"), session=session).games()
    body = next(kw["json"] for url, kw in session.posts if url.endswith("/Games"))
    for field in ("Name", "Genre", "Theme", "GameMode", "Platform", "PlayerPerspective"):
        assert field in body, f"{field} missing from the filter body"


def test_gaseous_login_is_attempted_once():
    """The cookie lives on the session, so re-authenticating every call would
    be pure overhead against a server that already trusts us."""
    session = FakeSession({"count": 0, "games": []})
    lib = GaseousLibrary(GaseousConfig(base_url="http://g", username="u", password="p"),
                         session=session)
    lib.games()
    lib.games()
    logins = [url for url, _ in session.posts if url.endswith("/Account/Login")]
    assert len(logins) == 1, f"logged in {len(logins)} times"


def test_every_library_backend_exposes_the_background_timeout():
    """app.py fetches the shelf on a background thread with
    `library.BACKGROUND_TIMEOUT`, but only Romm ever defined it -- so Gaseous and
    Retrom raised AttributeError the moment the refresh ran.

    _refresh_counts catches Exception and logs the class name alone, so the
    symptom was a Library page that stayed empty forever with nothing but the
    word "AttributeError" to go on -- no attribute named, and on a host without
    persistent journald, no log at all.

    The timeout is part of what the service needs from a library, so it belongs
    on the protocol rather than on whichever backend happened to be written
    first.
    """
    from romarr.libraries import LIBRARY_KINDS, build_library
    for kind in LIBRARY_KINDS:
        lib = build_library(kind, {"LIBRARY_URL": "http://library.example:1"})
        got = getattr(lib, "BACKGROUND_TIMEOUT", None)
        assert isinstance(got, int) and got > 0, f"{kind} has no BACKGROUND_TIMEOUT"


def test_gaseous_rescan_makes_no_call_because_there_is_nothing_to_call():
    """Gaseous has no scan trigger. It used to POST ContentManager/Rescan, which
    does not exist, so every import logged a 404 for a call that could never
    work -- enumerated from the server's own OpenAPI documents: 89 paths under
    /api/v1, 93 under /api/v1.1, none of them starting a scan.

    True, because nothing failed. Reporting False would mark every successful
    import as partly broken over a call that does not exist.
    """
    session = FakeSession({})
    lib = GaseousLibrary(GaseousConfig(base_url="http://g", username="u", password="p"),
                         session=session)
    assert lib.rescan("snes") is True
    assert session.posts == []
    assert session.gets == []


def test_retrom_rescan_still_asks_the_server():
    """Retrom does have a trigger, so the no-op above must not be mistaken for
    the protocol allowing every backend to skip it."""
    session = GrpcSession(b"")
    lib = RetromLibrary(RetromConfig(base_url="http://r"), session=session)
    lib.rescan("snes")
    assert session.posts, "Retrom rescan should reach the server"
