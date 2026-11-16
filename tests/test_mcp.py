"""Exercise MCP JSON-RPC over real protected HTTP sockets and the stdio bridge."""
from __future__ import annotations

import io
import json
import threading
from http.server import ThreadingHTTPServer

import pytest
import requests

from romarr import hub, mcp
from romarr.app import ROMarr, make_handler
from romarr.libraries import Game
from romarr.store import Event, QueueItem


@pytest.fixture
def server(tmp_path):
    """Start a disposable ROMarr with a private key and no background networking."""
    service = ROMarr({"ROMARR_DATA": str(tmp_path / "state.json"), "ROMARR_API_KEY": "mcp-fixture-key"})
    service._library_cache = ([Game("1", "Test Game", "snes")], 0, "")
    service.queue = [QueueItem("Test Game", "snes", "release title", 1, "grabbed", detail="private-key-url")]
    service.store.events = [Event("grabbed", "Test Game", "snes", detail="private-key-url")]
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(service))
    worker = threading.Thread(target=httpd.serve_forever, daemon=True)
    worker.start()
    yield "http://127.0.0.1:" + str(httpd.server_port), service
    httpd.shutdown()
    httpd.server_close()


def rpc(method, params=None, ident=1):
    """Build one JSON-RPC request for real transport tests."""
    return {"jsonrpc": "2.0", "id": ident, "method": method, "params": params or {}}


def send(server, message, **kwargs):
    """Use the production HTTP handler rather than a mocked JSON dispatcher."""
    base, _ = server
    headers = {"X-Api-Key": "mcp-fixture-key", **kwargs.pop("headers", {})}
    return requests.post(base + mcp.PATH, json=message, headers=headers, timeout=5, **kwargs)


@pytest.mark.parametrize("version", mcp.VERSIONS)
def test_initialize_and_notification_http(server, version):
    message = rpc("initialize", {"protocolVersion": version, "clientInfo": {"name": "fixture", "version": "1"}, "capabilities": {}})
    answer = send(server, message)
    assert answer.status_code == 200
    assert answer.json()["result"]["protocolVersion"] == version
    notification = send(server, {"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert notification.status_code == 202 and not notification.content


def test_http_auth_cannot_be_disabled_and_does_not_accept_cookie_or_query(server):
    base, service = server
    service.auth.enabled = False
    for suffix, headers in [("", {}), ("?apikey=mcp-fixture-key", {}),
                            ("", {"Cookie": "romarr_session=" + service.auth.issue_session()})]:
        answer = requests.post(base + mcp.PATH + suffix, json=rpc("ping"), headers=headers, timeout=5)
        assert answer.status_code == 401
    answer = requests.post(base + mcp.PATH, json=rpc("ping"),
                           headers={"Authorization": "Bearer mcp-fixture-key"}, timeout=5)
    assert answer.json()["result"] == {}


def test_origin_check_and_no_sse_stream(server):
    base, service = server
    assert send(server, rpc("ping"), headers={"Origin": "https://untrusted.invalid"}).status_code == 403
    service._env["ROMARR_MCP_ORIGINS"] = "https://owner.example"
    assert send(server, rpc("ping"), headers={"Origin": "https://owner.example"}).status_code == 200
    stream = requests.get(base + mcp.PATH, headers={"X-Api-Key": "mcp-fixture-key", "Accept": "text/event-stream"}, timeout=5)
    assert stream.status_code == 405 and stream.headers["Allow"] == "POST"
    assert requests.get(base + mcp.PATH, headers={"X-Api-Key": "mcp-fixture-key"}, timeout=5).json()["transport"] == "streamable-http"


def test_requests_are_bounded_and_json_errors_have_no_private_details(server):
    base, _ = server
    too_big = requests.post(base + mcp.PATH, data=b"x" * (mcp.MAX_BODY + 1),
                            headers={"X-Api-Key": "mcp-fixture-key", "Content-Type": "application/json"}, timeout=5)
    assert too_big.status_code == 413
    invalid = requests.post(base + mcp.PATH, data=b'{bad private input',
                            headers={"X-Api-Key": "mcp-fixture-key", "Content-Type": "application/json"}, timeout=5)
    assert invalid.status_code == 400 and invalid.json()["error"]["code"] == -32700
    assert "private input" not in invalid.text
    assert send(server, rpc("ping"), headers={"MCP-Protocol-Version": "nope"}).status_code == 400


def test_tools_are_cached_readonly_bounded_and_redact_diagnostics(server, monkeypatch):
    _, service = server
    def forbidden(*args, **kwargs):
        """Any backend probe would violate the read-only cached tool contract."""
        raise AssertionError("MCP must not probe backends")
    monkeypatch.setattr(service, "queue_rows", forbidden)
    monkeypatch.setattr(service, "library_view", forbidden)
    listing = send(server, rpc("tools/list")).json()["result"]["tools"]
    assert len(listing) == 5 and all(t["annotations"]["readOnlyHint"] for t in listing)
    for definition in listing:
        answer = send(server, rpc("tools/call", {"name": definition["name"], "arguments": {}})).json()["result"]
        assert not answer["isError"]
        assert "private-key-url" not in json.dumps(answer)
    bad = send(server, rpc("tools/call", {"name": "romarr_library", "arguments": {"limit": True}})).json()
    assert bad["result"]["isError"]
    assert send(server, rpc("tools/call", {"name": "romarr_library", "arguments": {"limit": 101}})).json()["result"]["isError"]
    assert send(server, rpc("tools/call", {"name": "arbitrary_rest", "arguments": {"path": "/api/v1/config"}})).json()["result"]["isError"]
    assert send(server, rpc("tools/call", {"name": "romarr_add_wanted", "arguments": {"game": "x", "platform": "snes"}})).json()["result"]["isError"]
    assert not service.store.wanted


def test_writes_require_server_opt_in_and_android_stays_readonly(server, monkeypatch):
    _, service = server
    service._env["ROMARR_MCP_WRITE"] = "1"
    result = send(server, rpc("tools/call", {"name": "romarr_add_wanted", "arguments": {"game": "Example", "platform": "snes"}})).json()["result"]
    assert not result["isError"] and service.store.wanted[0].game == "Example"
    wakeups = []
    real_set = service._refresh_now.set
    def record_wakeup():
        """The worker may consume the event immediately; record the actual request."""
        wakeups.append(True)
        real_set()
    monkeypatch.setattr(service._refresh_now, "set", record_wakeup)
    result = send(server, rpc("tools/call", {"name": "romarr_refresh_library", "arguments": {}})).json()["result"]
    assert not result["isError"] and wakeups == [True]
    assert result["structuredContent"]["completed"] is False
    service._env["ROMARR_ANDROID"] = "1"
    assert len(send(server, rpc("tools/list")).json()["result"]["tools"]) == 5


def test_stdio_bridge_uses_real_auth_headers_and_clean_stdout(server):
    base, _ = server
    incoming = io.BytesIO((json.dumps(rpc("ping")) + "\n" + json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n").encode())
    outgoing = io.StringIO()
    mcp.bridge(base, "mcp-fixture-key", incoming, outgoing)
    assert json.loads(outgoing.getvalue()) == {"jsonrpc": "2.0", "id": 1, "result": {}}
    for bad in ("http://remote.example", "https://owner:secret@example.com", base + "/api/v1/config"):
        with pytest.raises(ValueError):
            mcp.bridge(bad, "key", io.BytesIO(), io.StringIO())


def test_invalid_rpc_and_manifest_compatibility(server):
    _, service = server
    assert mcp.dispatch(service, []) == mcp.error(None, -32600, "Invalid Request")
    assert mcp.dispatch(service, rpc("shell.exec"))["error"]["code"] == -32601
    assert mcp.dispatch(service, rpc("ping", ident=True))["error"]["code"] == -32600
    manifest = hub.mcp_manifest("https://owner.example")
    assert manifest["mcp_client_config"]["mcpServers"]["romarr"]["url"] == "https://owner.example/api/v1/mcp"
    assert "open-eyes" not in manifest["mcp_client_config"]["mcpServers"]
    assert "mcp-remote" not in json.dumps(manifest)


def test_library_filters_backend_display_names_by_supported_slug(server):
    _,service=server
    service._library_cache=([Game("1","Display-name game","Super Nintendo Entertainment System",platform_slug="snes"),
                             Game("2","Resolved-name game","Super Nintendo Entertainment System")],0,"")
    result=send(server,rpc("tools/call",{"name":"romarr_library","arguments":{"platform":"snes"}})).json()["result"]
    assert not result["isError"]
    assert len(result["structuredContent"]["items"])==2
    assert all(row["platform_slug"]=="snes" for row in result["structuredContent"]["items"])


@pytest.mark.parametrize("title",["batch: /home/alice/roms","batch: C:\\private\\roms","batch: relative-root", "/home/alice/private", "C:\\private\\roms"])
def test_history_omits_library_paths_carried_as_event_titles(server,title):
    _,service=server
    service.store.events=[Event("decompressed",title,"snes")]
    result=send(server,rpc("tools/call",{"name":"romarr_history"})).json()["result"]["structuredContent"]
    assert result["items"][0]["game"]=="[path omitted]"
    assert title not in json.dumps(result)


def test_stdio_non_ascii_results_are_valid_utf8_with_legacy_stdout_encoding(server):
    base,service=server
    service._library_cache=([Game("1","日本語のゲーム","snes")],0,"")
    incoming=io.BytesIO((json.dumps(rpc("tools/call",{"name":"romarr_library"}))+"\n").encode())
    buffer=io.BytesIO();writer=io.TextIOWrapper(buffer,encoding="cp1252")
    mcp.bridge(base,"mcp-fixture-key",incoming,writer);writer.flush()
    result=json.loads(buffer.getvalue().decode("utf-8"))
    assert result["result"]["structuredContent"]["items"][0]["name"]=="日本語のゲーム"


def test_library_does_not_export_path_like_backend_platform_identifiers(server):
    _,service=server
    service._library_cache=([Game("1","Game","snes",platform_slug="/home/alice/private")],0,"")
    result=send(server,rpc("tools/call",{"name":"romarr_library"})).json()["result"]["structuredContent"]
    assert result["items"][0]["platform_slug"]==""
    assert "/home/alice" not in json.dumps(result)
    refused=send(server,rpc("tools/call",{"name":"romarr_library","arguments":{"platform":"../private"}})).json()["result"]
    assert refused["isError"]


@pytest.mark.parametrize("title",['A / B','Ys I / II'])
def test_mcp_retains_normal_slash_separators_in_titles(server,title):
    _,service=server
    service._library_cache=([Game("1",title,"snes")],0,"")
    service.queue=[QueueItem(title,"snes","release",1,"grabbed")]
    service.store.events=[Event("grabbed",title,"snes")]
    for name,field in [("romarr_library","name"),("romarr_queue","game"),("romarr_history","game")]:
        result=send(server,rpc("tools/call",{"name":name})).json()["result"]["structuredContent"]
        assert result["items"][0][field]==title


@pytest.mark.parametrize("title",["file '/home/alice/private'",'file "/home/alice/private"', "file 'C:\\private\\roms'"])
def test_mcp_omits_quoted_absolute_history_paths(server,title):
    _,service=server
    service.store.events=[Event("decompressed",title,"snes")]
    result=send(server,rpc("tools/call",{"name":"romarr_history"})).json()["result"]["structuredContent"]
    assert result["items"][0]["game"]=="[path omitted]"
