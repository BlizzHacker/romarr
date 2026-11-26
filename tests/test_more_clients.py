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
        FakeResponse(200, {"result": "success", "arguments": {"rpc-version": 17}}),
        FakeResponse(200, {"result": "success",
                           "arguments": {"torrent-added": {"id": 1, "hashString": "a" * 40}}}),
    ])
    client = Transmission(TransmissionConfig(base_url="http://t:9091"),
                          session=session)

    assert client.add("magnet:?xt=urn:btih:abc")
    assert len(session.calls) == 3, "it must retry discovery, then submit"
    assert session.calls[1]["headers"]["X-Transmission-Session-Id"] == "abc123"


def test_transmission_reuses_the_session_id_it_was_given():
    """Re-challenging on every call doubles every request for no reason."""
    from romarr.downloaders import Transmission, TransmissionConfig

    session = FakeSession([
        FakeResponse(409, {}, {"X-Transmission-Session-Id": "abc"}),
        FakeResponse(200, {"result": "success", "arguments": {"rpc-version": 17}}),
        FakeResponse(200, {"result": "success"}),
    ])
    client = Transmission(TransmissionConfig(base_url="http://t:9091"),
                          session=session)
    client.add("magnet:?xt=urn:btih:a")
    client.add("magnet:?xt=urn:btih:b")
    assert len(session.calls) == 4, "only the first call should be challenged"


def test_transmission_posts_to_the_rpc_path():
    from romarr.downloaders import Transmission, TransmissionConfig

    session = FakeSession([FakeResponse(body={"result": "success", "arguments": {"rpc-version": 17}}),
                           FakeResponse(body={"result": "success"})])
    Transmission(TransmissionConfig(base_url="http://t:9091"),
                 session=session).add("magnet:?xt=urn:btih:a")
    assert session.calls[0]["url"].endswith("/transmission/rpc")
    body = json.loads(session.calls[1]["data"])
    assert body["method"] == "torrent-add"
    assert body["arguments"]["filename"] == "magnet:?xt=urn:btih:a"


def test_transmission_reports_a_non_success_result_as_failure():
    """Transmission returns HTTP 200 with {"result": "<error text>"}. Reading
    only the status code calls every rejection a success."""
    from romarr.downloaders import Transmission, TransmissionConfig

    session = FakeSession([FakeResponse(body={"result": "success", "arguments": {"rpc-version": 17}}),
                           FakeResponse(200, {"result": "invalid or corrupt"})])
    assert not Transmission(TransmissionConfig(base_url="http://t:9091"),
                            session=session).add("magnet:?xt=urn:btih:a")


def test_transmission_refuses_an_already_owned_duplicate():
    """A second request cannot adopt a torrent already labelled for ROMarr."""
    from romarr.downloaders import Transmission, TransmissionConfig

    session = FakeSession([
        FakeResponse(body={"result": "success", "arguments": {"rpc-version": 17}}),
        FakeResponse(200, {"result": "success", "arguments": {
            "torrent-duplicate": {"id": 3, "hashString": "a" * 40}}}),
        FakeResponse(200, {"result": "success", "arguments": {"torrents": [{
            "hashString": "a" * 40, "labels": ["romarr"],
            "downloadDir": "/downloads/romarr"}]}}),
    ])
    assert not Transmission(TransmissionConfig(base_url="http://t:9091",
                            save_path="/downloads/romarr"),
                            session=session).add("magnet:?xt=urn:btih:" + "a" * 40)
    assert [json.loads(call["data"])["method"] for call in session.calls] == [
        "session-get", "torrent-add", "torrent-get"]


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


def test_transmission_imports_a_completed_job_by_hash_and_mapped_file(tmp_path):
    from romarr.app import QueueItem, ROMarr
    from romarr.downloaders import Transmission, TransmissionConfig, hand_off_with_id

    roms = tmp_path / "roms"
    downloads = tmp_path / "downloads"
    roms.mkdir()
    downloads.mkdir()
    rom = downloads / "Actual File.sfc"
    rom.write_bytes(b"test ROM bytes")
    info_hash = "a" * 40
    torrent = {"hashString": info_hash, "name": "Different client title",
               "downloadDir": r"D:\completed", "percentDone": 1,
               "metadataPercentComplete": 1, "files": [{"name": rom.name, "length": 14, "bytesCompleted": 14}],
               "labels": ["romarr"], "isFinished": False, "leftUntilDone": 0, "totalSize": 14}
    session = FakeSession([
        FakeResponse(409, {}, {"X-Transmission-Session-Id": "fixture"}),
        FakeResponse(body={"result": "success", "arguments": {"rpc-version": 17}}),
        FakeResponse(body={"result": "success", "arguments": {
            "torrent-added": {"hashString": info_hash.upper()},
        }}),
        FakeResponse(body={"result": "success", "arguments": {"torrents": [torrent]}}),
    ])
    client = Transmission(TransmissionConfig("http://transmission:9091"), session)
    accepted, job_id = hand_off_with_id(client, "http://indexer/game.torrent")
    assert accepted and job_id == info_hash
    service = ROMarr({"ROMARR_DATA": str(tmp_path / "state.json"),
                      "LIBRARY_KIND": "folder", "LIBRARY_PATH": str(roms)})
    service.store.settings["remote_path_mappings"] = [
        {"remote": r"D:\completed", "local": str(downloads)},
    ]
    service.clients = [client]
    item = QueueItem("Game", "snes", "Indexer Title", 1, "grabbed",
                     download_id=job_id)
    service.queue.append(item)

    result = service.import_finished()
    assert item.state == "imported", result
    assert (roms / "snes" / rom.name).read_bytes() == rom.read_bytes()
    assert json.loads(session.calls[-1]["data"])["method"] == "torrent-get"
    assert session.calls[-1]["headers"]["X-Transmission-Session-Id"] == "fixture"


def test_transmission_only_returns_completed_torrent_content_paths():
    from romarr.downloaders import Transmission, TransmissionConfig

    row = {"name": "Display name", "downloadDir": "/downloads",
           "percentDone": 1, "metadataPercentComplete": 1,
           "files": [{"name": "Folder/A.cue", "length": 7, "bytesCompleted": 7}, {"name": "Folder/A.bin", "length": 7, "bytesCompleted": 7}],
           "hashString": "a" * 40, "labels": ["romarr"], "leftUntilDone": 0, "totalSize": 14}
    session = FakeSession([FakeResponse(body={"result": "success", "arguments": {
        "torrents": [
            row, {**row, "percentDone": 0.9}, {**row, "metadataPercentComplete": 0},
            {**row, "labels": ["other"]}, {**row, "files": []},
            {**row, "files": [{"name": "../elsewhere.sfc"}]},
            {**row, "files": [{"name": "A.cue"}, {"name": "A.bin"}]},
        ],
    }})])
    client = Transmission(TransmissionConfig("http://transmission:9091"), session)
    completed, = client.completed()
    assert completed["content_path"] == "/downloads/Folder"
    assert completed["name"] == "Display name"


def test_deluge_uploads_torrent_files_creates_missing_label_and_reports_completed(tmp_path):
    from romarr.app import QueueItem, ROMarr
    from romarr.downloaders import Deluge, DelugeConfig

    roms = tmp_path / "roms"
    downloads = tmp_path / "downloads"
    roms.mkdir()
    downloads.mkdir()
    rom = downloads / "Super Metroid (USA).sfc"
    rom.write_bytes(b"test ROM bytes")
    info_hash = "b" * 40
    session = FakeSession([
        FakeResponse(200, {"result": True, "error": None}),          # auth.login
        FakeResponse(200, {"result": True, "error": None}),          # web.connected
        FakeResponse(200, {"result": info_hash.upper(), "error": None}),  # core.add_torrent_file
        FakeResponse(200, {"result": None, "error": {"message": "Unknown Label"}}),  # label.set_torrent
        FakeResponse(200, {"result": None, "error": None}),          # label.add
        FakeResponse(200, {"result": None, "error": None}),          # label.set_torrent retry
        FakeResponse(200, {"result": True, "error": None}),          # completed: auth.login
        FakeResponse(200, {"result": True, "error": None}),          # completed: web.connected
        FakeResponse(200, {"result": {
            info_hash: {
                "name": rom.name,
                "save_path": str(downloads),
                "state": "Seeding",
                "progress": 100.0,
                "is_finished": True,
                "label": "romarr",
                "files": [{"path": rom.name}],
            },
            "c" * 40: {
                "name": "Other.sfc",
                "save_path": str(downloads),
                "state": "Downloading",
                "progress": 42.0,
                "is_finished": False,
                "label": "romarr",
                "files": [{"path": "Other.sfc"}],
            },
        }, "error": None}),
    ])
    client = Deluge(DelugeConfig("http://d:8112", password="p", category="romarr"),
                    session=session)
    assert client.supports_torrent_file is True
    assert client.add_torrent_file(b"d8:announce4:test4:infod4:name4:gameee")
    assert client.last_job_id == info_hash
    methods = [json.loads(c["data"])["method"] for c in session.calls]
    assert "core.add_torrent_file" in methods
    assert "label.add" in methods

    service = ROMarr({"ROMARR_DATA": str(tmp_path / "state.json"),
                      "LIBRARY_KIND": "folder", "LIBRARY_PATH": str(roms)})
    service.clients = [client]
    item = QueueItem("Super Metroid", "snes", rom.name, 1, "grabbed",
                     download_id=client.last_job_id)
    service.queue.append(item)
    result = service.import_finished()
    assert item.state == "imported", result
    assert (roms / "snes" / rom.name).read_bytes() == rom.read_bytes()
