"""The Compressatorium client against the service's v4.5.2 HTTP contract.

The service is faked at the `requests.Session` boundary, so these pin what
ROMarr sends and how it reads each answer: the tool list that gates
submissions, the DAT-match `error` field and 503 that are not verdicts, and
the single-cartridge rule `romz_extract` enforces.
"""

from __future__ import annotations

import requests

from romarr.compressatorium import (
    CompressatoriumClient, CompressatoriumConfig, TOOL_BY_MODE, is_clutter,
    romz_accepts,
)


class _Response:
    def __init__(self, status=200, payload=None, text=""):
        self.status_code = status
        self._payload = payload
        self.text = text
        self.content = b"x" if payload is not None else b""

    @property
    def ok(self):
        return 200 <= self.status_code < 400

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class _Session:
    """Answers each (method, path) with a canned response or raises."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def _answer(self, method, url, **kwargs):
        path = "/" + url.split("://", 1)[1].split("/", 1)[1]
        self.calls.append((method, path, kwargs))
        answer = self.routes.get((method, path))
        if isinstance(answer, Exception):
            raise answer
        return answer or _Response(404, {"detail": "Not Found"})

    def get(self, url, **kwargs):
        return self._answer("GET", url, **kwargs)

    def post(self, url, **kwargs):
        return self._answer("POST", url, **kwargs)

    def delete(self, url, **kwargs):
        return self._answer("DELETE", url, **kwargs)


def _client(routes, api_key=""):
    session = _Session(routes)
    return CompressatoriumClient(
        CompressatoriumConfig(base_url="http://comp:8080", api_key=api_key),
        session=session), session


# ------------------------------------------------------------- /api/tools --

def test_available_tools_reads_the_available_list():
    client, session = _client({("GET", "/api/tools"): _Response(
        200, {"available": ["chdman", "dolphin"], "unavailable": ["nsz"]})},
        api_key="tok")

    assert client.available_tools() == frozenset({"chdman", "dolphin"})
    assert session.calls[0][2]["headers"] == {"Authorization": "Bearer tok"}


def test_a_release_without_the_tool_route_is_not_gated():
    client, _ = _client({})

    assert client.available_tools() is None


def test_an_unreachable_or_garbled_tool_list_is_not_gated():
    down, _ = _client({("GET", "/api/tools"): requests.ConnectionError("down")})
    garbled, _ = _client({("GET", "/api/tools"): _Response(200, ["chdman"])})
    textual, _ = _client({("GET", "/api/tools"): _Response(200, None)})

    assert down.available_tools() is None
    assert garbled.available_tools() is None
    assert textual.available_tools() is None


def test_every_decompression_mode_names_a_tool():
    """A mode missing from the map would never be gated, silently."""
    from romarr.compressatorium import _MODE_BY_SUFFIX

    for _suffix, mode in _MODE_BY_SUFFIX:
        assert mode in TOOL_BY_MODE, mode
    for mode in ("extractdvd", "extracthd", "extractld", "extractraw"):
        assert TOOL_BY_MODE[mode] == "chdman"


# --------------------------------------------------------- /api/dat/match --

def test_a_match_reads_game_and_dat_names():
    client, _ = _client({("POST", "/api/dat/match"): _Response(
        200, {"path": "/x", "matched": True, "game_name": "Game (USA)",
              "dat_name": "Redump PS2"})})

    result = client.match_file("/x")

    assert result.matched and result.game == "Game (USA)"
    assert result.dat == "Redump PS2" and result.error == ""


def test_a_match_that_could_not_be_worked_out_carries_its_error():
    client, _ = _client({("POST", "/api/dat/match"): _Response(
        200, {"path": "/x", "matched": False,
              "error": "embedded hash unavailable"})})

    result = client.match_file("/x")

    assert not result.matched
    assert result.error == "embedded hash unavailable"


def test_a_503_is_an_error_with_the_service_reason():
    client, _ = _client({("POST", "/api/dat/match"): _Response(
        503, {"detail": "Matching left a process stuck on unresponsive storage"})})

    result = client.match_file("/x")

    assert not result.matched
    assert result.error.startswith("status 503: Matching left a process stuck")


def test_a_403_says_the_path_is_outside_the_service_volumes():
    client, _ = _client({("POST", "/api/dat/match"): _Response(403, None)})

    assert client.match_file("/x").error == (
        "status 403: path is outside the service's volumes")


def test_an_unreachable_service_is_an_error_not_a_miss():
    client, _ = _client({("POST", "/api/dat/match"):
                         requests.ConnectionError("refused")})

    result = client.match_file("/x")

    assert not result.matched and "unreachable" in result.error


# ------------------------------------------------------ romz single-ROM rule --

def test_romz_takes_one_cartridge_rom():
    assert romz_accepts(["Game (USA).gba"])
    assert romz_accepts(["Game/Game (USA).sfc"])


def test_romz_ignores_os_clutter_beside_the_rom():
    assert romz_accepts(["Game.gba", "__MACOSX/._Game.gba", ".DS_Store",
                         "Thumbs.db", "@eaDir/Game.gba@SynoEAStream"])


def test_romz_refuses_discs_sets_and_empty_archives():
    assert not romz_accepts(["Game (USA).iso"])
    assert not romz_accepts(["Game (USA).cue", "Game (USA).bin"])
    assert not romz_accepts(["Game (USA).gba", "Game (Europe).gba"])
    assert not romz_accepts(["Game (USA).gba", "readme.txt"])
    assert not romz_accepts([])
    assert not romz_accepts(["__MACOSX/._Game.gba"])


def test_clutter_is_recognised_in_any_path_component():
    assert is_clutter("__MACOSX/Game/._Game.gba")
    assert is_clutter("Game/.DS_Store")
    assert is_clutter("._Game.gba")
    assert not is_clutter("Game (USA).gba")
    assert not is_clutter("Games/Disc 1.bin")
