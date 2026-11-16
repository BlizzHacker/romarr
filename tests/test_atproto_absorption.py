"""Tests for AT Protocol, Games Lexicon, HappyView, and Cartridge Studio absorption."""
from pathlib import Path
import json
import pytest

from romarr.atproto import (
    LEXICONS,
    ATProtoClient,
    ATProtoKirbyHub,
    HappyViewAppView,
    build_at_uri,
    generate_tid,
    parse_at_uri,
)


class MockResponse:
    def __init__(self, data: dict, status_code: int = 200):
        self._data = data
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise ValueError(f"HTTP {self.status_code}")

    def json(self):
        return self._data


class MockSession:
    def __init__(self):
        self.posts = []
        self.gets = []

    def post(self, url, json=None, headers=None, **kwargs):
        self.posts.append((url, json, headers))
        if "createSession" in url:
            return MockResponse({"did": "did:plc:testuser123", "handle": "testuser.bsky.social", "accessJwt": "jwt_token_abc"})
        if "putRecord" in url:
            return MockResponse({"uri": f"at://{json['repo']}/{json['collection']}/{json['rkey']}", "cid": "cid123"})
        return MockResponse({})

    def get(self, url, params=None, **kwargs):
        self.gets.append((url, params))
        if "resolveHandle" in url:
            return MockResponse({"did": "did:plc:resolved123"})
        if "listRecords" in url:
            return MockResponse({"records": [{"uri": "at://did:plc:test/games.gamesgamesgamesgames.game/123", "value": {"name": "Zelda"}}]})
        return MockResponse({})


def test_lexicons_loaded():
    assert "games.gamesgamesgamesgames.game" in LEXICONS
    assert "games.gamesgamesgamesgames.release" in LEXICONS
    assert "games.gamesgamesgamesgames.playState" in LEXICONS
    assert "games.gamesgamesgamesgames.achievement" in LEXICONS
    assert "dev.cartridge.verificationRequest" in LEXICONS


def test_tid_and_at_uri():
    tid = generate_tid()
    assert len(tid) == 13
    assert tid.isalnum()

    uri = build_at_uri("did:plc:alice", "games.gamesgamesgamesgames.game", "3ktest123")
    assert uri == "at://did:plc:alice/games.gamesgamesgamesgames.game/3ktest123"

    parsed = parse_at_uri(uri)
    assert parsed["did"] == "did:plc:alice"
    assert parsed["collection"] == "games.gamesgamesgamesgames.game"
    assert parsed["rkey"] == "3ktest123"

    with pytest.raises(ValueError):
        parse_at_uri("invalid://format")


def test_happyview_appview_storage_and_xrpc(tmp_path):
    appview = HappyViewAppView(tmp_path)
    game_uri = appview.store_record(
        "games.gamesgamesgamesgames.game",
        "super-mario-64",
        {"name": "Super Mario 64", "createdAt": "2026-01-01T00:00:00Z"},
        did="did:plc:gamer1",
    )
    assert game_uri.startswith("at://did:plc:gamer1/games.gamesgamesgamesgames.game/")

    # Query getGame by URI
    res = appview.handle_xrpc_get_game({"uri": game_uri})
    assert res["value"]["name"] == "Super Mario 64"

    # Query getGame by name
    res_name = appview.handle_xrpc_get_game({"name": "super mario 64"})
    assert res_name["uri"] == game_uri

    # Query listGames
    listing = appview.handle_xrpc_list_games({"limit": 10})
    assert listing["count"] == 1
    assert listing["games"][0]["uri"] == game_uri

    # Jetstream event processing
    event = {
        "kind": "commit",
        "did": "did:plc:gamer2",
        "commit": {
            "operation": "create",
            "collection": "games.gamesgamesgamesgames.game",
            "rkey": "chrono-trigger",
            "record": {"name": "Chrono Trigger", "createdAt": "2026-01-01T00:00:00Z"},
        },
    }
    event_res = appview.process_jetstream_event(event)
    assert event_res["indexed"] is True
    assert "chrono-trigger" in event_res["uri"]

    # Delete event
    del_event = {
        "kind": "commit",
        "did": "did:plc:gamer2",
        "commit": {
            "operation": "delete",
            "collection": "games.gamesgamesgamesgames.game",
            "rkey": "chrono-trigger",
        },
    }
    del_res = appview.process_jetstream_event(del_event)
    assert del_res["deleted"] is True


def test_atproto_client_session_and_records():
    mock_session = MockSession()
    client = ATProtoClient(session=mock_session)

    did = client.resolve_handle("testuser.bsky.social")
    assert did == "did:plc:resolved123"

    session = client.create_session("testuser.bsky.social", "secret-password")
    assert session["did"] == "did:plc:testuser123"
    assert client.access_jwt == "jwt_token_abc"

    put = client.put_record("games.gamesgamesgamesgames.game", {"name": "Metroid Prime"})
    assert "at://" in put["uri"]

    records = client.list_records("did:plc:testuser123", "games.gamesgamesgamesgames.game")
    assert len(records) == 1
    assert records[0]["value"]["name"] == "Zelda"


def test_kirby_hub_export_and_claims(tmp_path):
    mock_session = MockSession()
    hub = ATProtoKirbyHub(tmp_path, session=mock_session)

    # Initial status
    status = hub.status()
    assert status["ok"] is True
    assert status["absorption"] == "Kirby-Style Complete"
    assert "games.gamesgamesgamesgames.game" in status["lexicons_loaded"]

    # Export a single game
    game_dict = {
        "title": "Super Metroid",
        "platform": "snes",
        "overview": "Classic 16-bit masterpiece",
        "sha256": "abcdef123456",
        "crc32": "d1e2f3",
        "verified": True,
    }
    exp = hub.export_game(game_dict)
    assert exp["ok"] is True
    assert exp["gameUri"].startswith("at://")
    assert exp["releaseUri"].startswith("at://")

    # Export whole library
    lib_exp = hub.export_library([game_dict, {"title": "EarthBound", "platform": "snes"}])
    assert lib_exp["ok"] is True
    assert lib_exp["exported_count"] == 2

    # PlayState publishing
    ps = hub.publish_play_state(exp["gameUri"], "playing", play_time_minutes=120, rating=5.0)
    assert ps["ok"] is True
    assert ps["record"]["status"] == "playing"
    assert ps["record"]["playTimeMinutes"] == 120

    # Verification claim (Cartridge Studio)
    claim = hub.create_verification_claim("Super Metroid", "snes", "abcdef123456", "d1e2f3")
    assert claim["ok"] is True
    assert claim["claimUri"].startswith("at://")
    assert claim["record"]["verified"] is True


def test_atproto_http_endpoints(tmp_path):
    import urllib.request
    from http.server import ThreadingHTTPServer
    import threading
    from romarr.app import ROMarr, make_handler

    service = ROMarr({"ROMARR_DATA": str(tmp_path / "s.json"), "ROMARR_API_KEY": "testkey"})
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(service))
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    port = httpd.server_address[1]
    base = f"http://127.0.0.1:{port}"

    def req(path, method="GET", data=None):
        url = base + path
        headers = {"X-Api-Key": "testkey"}
        if data is not None:
            headers["Content-Type"] = "application/json"
            body = json.dumps(data).encode("utf-8")
        else:
            body = None
        r = urllib.request.Request(url, data=body, headers=headers, method=method)
        with urllib.request.urlopen(r) as resp:
            return json.loads(resp.read().decode("utf-8"))

    try:
        # 1. Status
        status = req("/api/v1/atproto/status")
        assert status["ok"] is True
        assert status["absorption"] == "Kirby-Style Complete"

        # 2. Lexicons
        lexicons = req("/api/v1/atproto/lexicons")
        assert "games.gamesgamesgamesgames.game" in lexicons["lexicons"]

        # 3. Export game
        exp = req("/api/v1/atproto/export", method="POST", data={
            "games": [{"title": "Castlevania", "platform": "nes", "sha256": "12345678"}]
        })
        assert exp["ok"] is True
        assert exp["exported_count"] == 1
        game_uri = exp["games"][0]["gameUri"]

        # 4. PlayState
        ps = req("/api/v1/atproto/playstate", method="POST", data={
            "gameUri": game_uri,
            "status": "completed",
            "playTimeMinutes": 180,
            "rating": 5.0
        })
        assert ps["ok"] is True
        assert ps["record"]["status"] == "completed"

        # 5. Cartridge verification claim
        claim = req("/api/v1/atproto/claim", method="POST", data={
            "romTitle": "Castlevania",
            "platform": "nes",
            "sha256": "12345678",
            "datCatalog": "No-Intro"
        })
        assert claim["ok"] is True
        assert claim["record"]["verified"] is True

        # 6. HappyView AppView XRPC listing
        xrpc_list = req("/xrpc/games.gamesgamesgamesgames.listGames")
        assert xrpc_list["count"] >= 1

        # 7. HappyView AppView XRPC getGame
        xrpc_get = req(f"/xrpc/games.gamesgamesgamesgames.getGame?uri={urllib.parse.quote(game_uri)}")
        assert xrpc_get["value"]["name"] == "Castlevania"
    finally:
        httpd.shutdown()
        httpd.server_close()
