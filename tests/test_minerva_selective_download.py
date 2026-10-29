"""Exact-file handling for collection-backed Minerva torrents."""

from romarr.clients import QBittorrent, QbitConfig


class Response:
    def __init__(self, body=None, *, status=200, text="Ok."):
        self._body = body
        self.status_code = status
        self.ok = 200 <= status < 300
        self.text = text

    def json(self):
        return self._body

    def raise_for_status(self):
        if not self.ok:
            import requests
            raise requests.HTTPError(f"HTTP {self.status_code}")


class Session:
    def __init__(self, files, *, state="pausedDL", add_text="Ok."):
        self.files = [dict(row) for row in files]
        self.state = state
        self.add_text = add_text
        self.tag = ""
        self.hash = "abc123"
        self.posts = []
        self.gets = []
        self.resumed = False
        self.deleted = False

    def post(self, url, *, data=None, **kwargs):
        endpoint = url.rsplit("/api/v2/", 1)[-1]
        self.posts.append((endpoint, dict(data or {})))
        if endpoint == "torrents/add":
            self.tag = data["tags"]
            return Response(text=self.add_text)
        if endpoint == "torrents/filePrio":
            ids = [int(value) for value in data["id"].split("|")]
            priority = int(data["priority"])
            for row in self.files:
                if int(row["index"]) in ids:
                    row["priority"] = priority
            return Response(text="Ok.")
        if endpoint in ("torrents/pause", "torrents/stop"):
            self.state = "pausedDL"
            return Response(text="Ok.")
        if endpoint in ("torrents/resume", "torrents/start"):
            self.resumed = True
            self.state = "downloading"
            return Response(text="Ok.")
        if endpoint == "torrents/delete":
            self.deleted = True
            return Response(text="Ok.")
        return Response(status=404, text="Not Found")

    def get(self, url, *, params=None, **kwargs):
        endpoint = url.rsplit("/api/v2/", 1)[-1]
        self.gets.append((endpoint, dict(params or {})))
        if endpoint == "torrents/info":
            return Response([{"hash": self.hash, "tags": self.tag,
                              "state": self.state}])
        if endpoint == "torrents/files":
            return Response([dict(row) for row in self.files])
        return Response(status=404, text="Not Found")


def qbit(files, **kwargs):
    session = Session(files, **kwargs)
    client = QBittorrent(QbitConfig(base_url="http://qbit:8080",
                                   timeout=2), session=session)
    return client, session


def test_selects_only_the_exact_normalized_minerva_path_before_resume():
    client, session = qbit([
        {"index": 0, "name": "TOSEC/Nintendo/other.zip", "priority": 1},
        {"index": 1, "name": "TOSEC\\Nintendo\\Chrono Trigger.zip", "priority": 1},
        {"index": 2, "name": "TOSEC/Nintendo/readme.txt", "priority": 1},
    ])
    assert client.add_magnet_selective(
        "magnet:?xt=urn:btih:abc",
        "./TOSEC/Nintendo/Chrono Trigger.zip")
    assert session.posts[0][0] == "torrents/add"
    assert session.posts[0][1]["paused"] == "true"
    priorities = [data for endpoint, data in session.posts
                  if endpoint == "torrents/filePrio"]
    assert priorities == [
        {"hash": "abc123", "id": "0|2", "priority": "0"},
        {"hash": "abc123", "id": "1", "priority": "1"},
    ]
    assert session.resumed is True
    assert session.posts.index(next(
        item for item in session.posts if item[0] == "torrents/resume")) > 0
    assert client.last_job_id.startswith("qbit-tag:")


def test_nonmatching_path_is_deleted_while_paused_and_never_resumed():
    client, session = qbit([
        {"index": 0, "name": "TOSEC/Nintendo/other.zip", "priority": 1},
    ])
    assert not client.add_magnet_selective(
        "magnet:?xt=urn:btih:abc",
        "./TOSEC/Nintendo/Chrono Trigger.zip")
    assert "did not match exactly one" in client.last_error
    assert session.deleted is True
    assert session.resumed is False
    assert not any(endpoint == "torrents/filePrio"
                   for endpoint, _ in session.posts)


def test_ambiguous_normalized_paths_fail_closed():
    client, session = qbit([
        {"index": 0, "name": "./TOSEC/Nintendo/game.zip", "priority": 1},
        {"index": 1, "name": "TOSEC\\Nintendo\\game.zip", "priority": 1},
    ])
    assert not client.add_magnet_selective(
        "magnet:?xt=urn:btih:abc",
        "./TOSEC/Nintendo/game.zip")
    assert "did not match exactly one" in client.last_error
    assert session.deleted is True
    assert session.resumed is False


def test_unpaused_add_is_removed_before_any_file_priority_or_resume():
    client, session = qbit(
        [{"index": 0, "name": "x/game.zip", "priority": 1}],
        state="downloading")
    assert not client.add_magnet_selective(
        "magnet:?xt=urn:btih:abc", "x/game.zip")
    assert "did not confirm" in client.last_error
    assert session.deleted is True
    assert session.resumed is False
    assert not any(endpoint == "torrents/filePrio"
                   for endpoint, _ in session.posts)


def test_invalid_or_failed_add_never_falls_back_to_a_collection_download():
    client, session = qbit([], add_text="Fails.")
    assert not client.add_magnet_selective(
        "magnet:?xt=urn:btih:abc", "../outside/game.zip")
    assert "safe relative file path" in client.last_error
    assert session.posts == []

    assert not client.add_magnet_selective(
        "magnet:?xt=urn:btih:abc", "TOSEC/game.zip")
    assert "did not accept" in client.last_error
    assert session.resumed is False
    assert not any(endpoint == "torrents/filePrio"
                   for endpoint, _ in session.posts)


def test_minerva_grab_passes_the_exact_path_to_qbittorrent(tmp_path, monkeypatch):
    from romarr.app import ROMarr
    from romarr.clients import QBittorrent, QbitConfig
    from romarr.selection import Release

    service = ROMarr(env={"ROMARR_DATA": str(tmp_path / "state.json")})
    client, _session = qbit([])
    calls = []

    def select(magnet, path):
        calls.append((magnet, path))
        client._added.job_id = "qbit-tag:minerva-test"
        return True

    monkeypatch.setattr(client, "add_magnet_selective", select)
    service.clients = [client]
    release = Release(
        title="Chrono Trigger (1995)(Square)(US).zip", size=10, seeders=1,
        categories=(1000,), download_url="magnet:?xt=urn:btih:abc",
        protocol="torrent", indexer="hub:minerva-archive",
        torrent_file_path="./TOSEC/Nintendo/Chrono Trigger.zip",
        file_selection_required=True)
    result = service.grab(release, "Chrono Trigger", "snes", manual=True)
    assert result["ok"] is True
    assert calls == [("magnet:?xt=urn:btih:abc",
                      "./TOSEC/Nintendo/Chrono Trigger.zip")]


def test_minerva_grab_refuses_other_torrent_clients_without_fallback(tmp_path):
    from romarr.app import ROMarr
    from romarr.selection import Release

    class OtherTorrentClient:
        protocol = "torrent"
        name = "Other torrent client"
        configured = True
        adds = 0

        def add(self, _url, **_kwargs):
            self.adds += 1
            return True

    service = ROMarr(env={"ROMARR_DATA": str(tmp_path / "state.json")})
    client = OtherTorrentClient()
    service.clients = [client]
    release = Release(
        title="Chrono Trigger (US).zip", size=10, seeders=1,
        categories=(1000,), download_url="magnet:?xt=urn:btih:abc",
        protocol="torrent", indexer="hub:minerva-archive",
        torrent_file_path="./TOSEC/Nintendo/Chrono Trigger.zip",
        file_selection_required=True)
    result = service.grab(release, "Chrono Trigger", "snes", manual=True)
    assert result["ok"] is False
    assert "requires qBittorrent" in result["error"]
    assert client.adds == 0
