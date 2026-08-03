"""Transmission and Deluge, completing the five gamarr has.

Both are small JSON APIs with one famous trap each, and both traps produce the
same symptom: the client looks configured, Test appears to work or fails
mysteriously, and no torrent is ever added.
"""

from __future__ import annotations

import json

import pytest

from romarr.downloaders import CLIENT_TYPES, build_client, redact


class FakeResponse:
    def __init__(self, status=200, body=None, headers=None):
        self.status_code = status
        self._body = body if body is not None else {}
        self.headers = headers or {}
        self.text = json.dumps(self._body) if isinstance(self._body, dict) else str(self._body)

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            raise requests.HTTPError(response=self)


class FakeSession:
    """Records requests and replays a scripted list of responses."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.cookies = {}

    def post(self, url, **kw):
        self.calls.append({"url": url, **kw})
        return self.responses.pop(0) if self.responses else FakeResponse()

    def get(self, url, **kw):
        self.calls.append({"url": url, **kw})
        return self.responses.pop(0) if self.responses else FakeResponse()


# --- the registry ----------------------------------------------------------

@pytest.mark.parametrize("kind", ["qbittorrent", "transmission", "deluge",
                                  "sabnzbd", "nzbget"])
def test_all_five_clients_are_offered(kind):
    assert kind in CLIENT_TYPES


@pytest.mark.parametrize("kind,protocol,port", [
    ("transmission", "torrent", 9091),
    ("deluge", "torrent", 8112),
])
def test_the_new_clients_declare_the_right_defaults(kind, protocol, port):
    spec = CLIENT_TYPES[kind]
    assert spec["protocol"] == protocol
    assert spec["default_port"] == port


@pytest.mark.parametrize("kind", sorted(CLIENT_TYPES))
def test_no_client_type_leaks_its_secret(kind):
    out = redact({"type": kind, "password": "SECRET", "api_key": "SECRET"})
    assert "SECRET" not in str(out), kind


@pytest.mark.parametrize("kind", sorted(CLIENT_TYPES))
def test_every_client_type_can_be_built(kind):
    client = build_client({"type": kind, "host": "localhost", "port": 1,
                           "password": "p", "api_key": "k"})
    assert client is not None, kind
    assert hasattr(client, "add") and hasattr(client, "reachable"), kind


# --- Transmission: the 409 session handshake -------------------------------

def test_transmission_answers_the_409_session_challenge():
    """THE Transmission trap.

    Every first request is answered 409 with an X-Transmission-Session-Id
    header, and the call must be repeated carrying it. A client that treats
    409 as a failure never adds a single torrent and reports the daemon as
    broken -- which is exactly what it looks like from outside.
    """
    from romarr.downloaders import Transmission, TransmissionConfig

    session = FakeSession([
        FakeResponse(409, {}, {"X-Transmission-Session-Id": "abc123"}),
        FakeResponse(200, {"result": "success",
                           "arguments": {"torrent-added": {"id": 1}}}),
    ])
    client = Transmission(TransmissionConfig(base_url="http://t:9091"),
                          session=session)

    assert client.add("magnet:?xt=urn:btih:abc")
    assert len(session.calls) == 2, "it must retry, not give up"
    assert session.calls[1]["headers"]["X-Transmission-Session-Id"] == "abc123"


def test_transmission_reuses_the_session_id_it_was_given():
    """Re-challenging on every call doubles every request for no reason."""
    from romarr.downloaders import Transmission, TransmissionConfig

    session = FakeSession([
        FakeResponse(409, {}, {"X-Transmission-Session-Id": "abc"}),
        FakeResponse(200, {"result": "success"}),
        FakeResponse(200, {"result": "success"}),
    ])
    client = Transmission(TransmissionConfig(base_url="http://t:9091"),
                          session=session)
    client.add("magnet:?xt=urn:btih:a")
    client.add("magnet:?xt=urn:btih:b")
    assert len(session.calls) == 3, "only the first call should be challenged"


def test_transmission_posts_to_the_rpc_path():
    from romarr.downloaders import Transmission, TransmissionConfig

    session = FakeSession([FakeResponse(200, {"result": "success"})])
    Transmission(TransmissionConfig(base_url="http://t:9091"),
                 session=session).add("magnet:?xt=urn:btih:a")
    assert session.calls[0]["url"].endswith("/transmission/rpc")
    body = json.loads(session.calls[0]["data"])
    assert body["method"] == "torrent-add"
    assert body["arguments"]["filename"] == "magnet:?xt=urn:btih:a"


def test_transmission_reports_a_non_success_result_as_failure():
    """Transmission returns HTTP 200 with {"result": "<error text>"}. Reading
    only the status code calls every rejection a success."""
    from romarr.downloaders import Transmission, TransmissionConfig

    session = FakeSession([FakeResponse(200, {"result": "invalid or corrupt"})])
    assert not Transmission(TransmissionConfig(base_url="http://t:9091"),
                            session=session).add("magnet:?xt=urn:btih:a")


def test_transmission_treats_a_duplicate_as_added():
    """`torrent-duplicate` means it is already there, which is the outcome
    the caller wanted. Calling it a failure makes a re-run look broken."""
    from romarr.downloaders import Transmission, TransmissionConfig

    session = FakeSession([FakeResponse(
        200, {"result": "success", "arguments": {"torrent-duplicate": {"id": 3}}})])
    assert Transmission(TransmissionConfig(base_url="http://t:9091"),
                        session=session).add("magnet:?xt=urn:btih:a")


# --- Deluge: log in first, and check the daemon is attached ----------------

def test_deluge_logs_in_before_adding():
    """Deluge's WebUI refuses everything until auth.login succeeds, and it
    answers an unauthenticated call with a perfectly ordinary-looking JSON
    error rather than a 401."""
    from romarr.downloaders import Deluge, DelugeConfig

    session = FakeSession([
        FakeResponse(200, {"result": True, "error": None}),      # auth.login
        FakeResponse(200, {"result": True, "error": None}),      # connected
        FakeResponse(200, {"result": "hash", "error": None}),    # add
    ])
    client = Deluge(DelugeConfig(base_url="http://d:8112", password="deluge"),
                    session=session)

    assert client.add("magnet:?xt=urn:btih:abc")
    methods = [json.loads(c["data"])["method"] for c in session.calls]
    assert methods[0] == "auth.login"
    assert "core.add_torrent_magnet" in methods


def test_deluge_refuses_when_the_password_is_wrong():
    from romarr.downloaders import Deluge, DelugeConfig

    session = FakeSession([FakeResponse(200, {"result": False, "error": None})])
    assert not Deluge(DelugeConfig(base_url="http://d:8112", password="wrong"),
                      session=session).add("magnet:?xt=urn:btih:a")


def test_deluge_connects_the_webui_to_a_daemon_when_it_is_detached():
    """The second Deluge trap. The WebUI is a separate process from the
    daemon, and a freshly started WebUI is attached to nothing -- every call
    succeeds and no torrent appears anywhere.
    """
    from romarr.downloaders import Deluge, DelugeConfig

    session = FakeSession([
        FakeResponse(200, {"result": True, "error": None}),        # login
        FakeResponse(200, {"result": False, "error": None}),       # connected? no
        FakeResponse(200, {"result": [["host1", "127.0.0.1", 58846, "user"]],
                           "error": None}),                        # get_hosts
        FakeResponse(200, {"result": True, "error": None}),        # web.connect
        FakeResponse(200, {"result": "hash", "error": None}),      # add
    ])
    client = Deluge(DelugeConfig(base_url="http://d:8112", password="deluge"),
                    session=session)

    assert client.add("magnet:?xt=urn:btih:a")
    methods = [json.loads(c["data"])["method"] for c in session.calls]
    assert "web.connect" in methods


def test_deluge_reports_a_json_rpc_error_as_failure():
    from romarr.downloaders import Deluge, DelugeConfig

    session = FakeSession([
        FakeResponse(200, {"result": True, "error": None}),
        FakeResponse(200, {"result": True, "error": None}),
        FakeResponse(200, {"result": None,
                           "error": {"message": "Torrent already in session"}}),
    ])
    assert not Deluge(DelugeConfig(base_url="http://d:8112", password="p"),
                      session=session).add("magnet:?xt=urn:btih:a")


def test_deluge_sends_the_category_as_a_label():
    from romarr.downloaders import Deluge, DelugeConfig

    session = FakeSession([
        FakeResponse(200, {"result": True, "error": None}),
        FakeResponse(200, {"result": True, "error": None}),
        FakeResponse(200, {"result": "abc", "error": None}),
        FakeResponse(200, {"result": None, "error": None}),
    ])
    Deluge(DelugeConfig(base_url="http://d:8112", password="p",
                        category="romarr"), session=session).add("magnet:?x")
    methods = [json.loads(c["data"])["method"] for c in session.calls]
    assert any("label" in m for m in methods)


# --- both are torrent clients as far as routing is concerned --------------

def test_the_new_clients_are_picked_for_torrents():
    from romarr.downloaders import pick_client

    transmission = build_client({"type": "transmission", "host": "t"})
    assert pick_client("torrent", [transmission]) is transmission
    assert pick_client("usenet", [transmission]) is None
