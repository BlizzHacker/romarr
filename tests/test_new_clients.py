"""rTorrent, Synology Download Station and Real-Debrid."""

import json

import pytest

from romarr.downloaders import (
    CLIENT_TYPES, RealDebrid, RealDebridConfig, Rtorrent, RtorrentConfig,
    SynologyConfig, SynologyDownloadStation, build_client, pick_client,
)


# -- rTorrent ----------------------------------------------------------------

class FakeRtorrentProxy:
    """Speaks the modern dialect and records what it was told."""

    def __init__(self, fail_modern=False):
        self.calls = []
        outer = self

        class _Load:
            def start(self, target, url, *commands):
                if fail_modern:
                    raise Exception("unknown method")
                outer.calls.append(("load.start", target, url, commands))

        class _System:
            def client_version(self):
                return "0.9.8"

        class _D:
            def multicall2(self, target, view, *fields):
                return [
                    ["Super Metroid (USA)", 1, "/dl/smetroid", "romarr"],
                    ["Not Ours", 1, "/dl/other", "manual"],
                    ["Still Going", 0, "/dl/going", "romarr"],
                ]

        self.load = _Load()
        self.system = _System()
        self.d = _D()

    def load_start(self, url, *commands):
        self.calls.append(("load_start", url, commands))


def test_rtorrent_adds_with_the_label():
    proxy = FakeRtorrentProxy()
    client = Rtorrent(RtorrentConfig(base_url="http://rt/RPC2"), proxy=proxy)
    assert client.add("magnet:?xt=urn:btih:abc")
    assert proxy.calls == [
        ("load.start", "", "magnet:?xt=urn:btih:abc", ("d.custom1.set=romarr",))]


def test_rtorrent_falls_back_to_the_old_dialect():
    """`load_start` became `load.start` around 0.9; speaking only one dialect
    looks broken against exactly half the installs."""
    proxy = FakeRtorrentProxy(fail_modern=True)
    client = Rtorrent(RtorrentConfig(base_url="http://rt/RPC2"), proxy=proxy)
    assert client.add("magnet:?xt=urn:btih:abc")
    assert proxy.calls[0][0] == "load_start"


def test_rtorrent_completed_filters_to_our_finished_label():
    client = Rtorrent(RtorrentConfig(base_url="http://rt/RPC2"),
                      proxy=FakeRtorrentProxy())
    done = client.completed()
    assert [d["name"] for d in done] == ["Super Metroid (USA)"]
    assert done[0]["content_path"] == "/dl/smetroid"


def test_rtorrent_unconfigured_is_inert():
    client = Rtorrent(RtorrentConfig(base_url=""))
    assert not client.configured
    assert not client.add("magnet:?xt=urn:btih:abc")
    assert client.completed() == []


def test_rtorrent_build_embeds_credentials_in_the_url():
    client = build_client({"type": "rtorrent", "host": "rt", "port": 8080,
                           "username": "u", "password": "p@ss"})
    assert client._config.base_url == "http://u:p%40ss@rt:8080/RPC2"


# -- Synology ----------------------------------------------------------------

class FakeSynoResponse:
    def __init__(self, body):
        self._body = body

    def json(self):
        return self._body


class FakeSynoSession:
    def __init__(self):
        self.stale_sids = 0
        self.creates = []
        self.sid_serial = 0

    def get(self, url, params=None, timeout=None):
        params = params or {}
        if url.endswith("auth.cgi"):
            self.sid_serial += 1
            return FakeSynoResponse(
                {"success": True, "data": {"sid": f"sid{self.sid_serial}"}})
        if params.get("method") == "create":
            if self.stale_sids > 0:
                self.stale_sids -= 1
                return FakeSynoResponse(
                    {"success": False, "error": {"code": 106}})
            self.creates.append(params)
            return FakeSynoResponse({"success": True})
        if params.get("method") == "list":
            return FakeSynoResponse({"success": True, "data": {"tasks": [
                {"title": "Super Metroid (USA)", "status": "finished",
                 "additional": {"detail": {"destination": "/downloads"}}},
                {"title": "Slowpoke", "status": "downloading"},
            ]}})
        return FakeSynoResponse({"success": False, "error": {"code": 101}})


def syno(session):
    return SynologyDownloadStation(
        SynologyConfig(base_url="http://nas:5000", username="u", password="p"),
        session=session)


def test_synology_logs_in_and_creates_the_task():
    session = FakeSynoSession()
    client = syno(session)
    assert client.add("magnet:?xt=urn:btih:abc")
    assert session.creates[0]["uri"] == "magnet:?xt=urn:btih:abc"
    assert session.creates[0]["_sid"] == "sid1"


def test_synology_relogs_in_when_the_sid_expires():
    """The sid expires without saying when; codes 105/106/119 mean 'stale
    sid', not 'you failed'."""
    session = FakeSynoSession()
    client = syno(session)
    assert client.add("magnet:?xt=urn:btih:a")   # login -> sid1
    session.stale_sids = 1
    assert client.add("magnet:?xt=urn:btih:b")   # 106 -> relogin -> sid2
    assert session.creates[-1]["_sid"] == "sid2"


def test_synology_completed_reports_finished_only():
    done = syno(FakeSynoSession()).completed()
    assert [d["name"] for d in done] == ["Super Metroid (USA)"]
    assert done[0]["content_path"] == "/downloads/Super Metroid (USA)"


# -- Real-Debrid -------------------------------------------------------------

class FakeRdResponse:
    def __init__(self, body=None, text="x"):
        self._body = body
        self.text = text if body is None else json.dumps(body)

    def raise_for_status(self):
        pass

    def json(self):
        return self._body

    def iter_content(self, chunk_size):
        yield b"ROMBYTES"

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeRdSession:
    def __init__(self):
        self.posts = []
        self.info_status = "downloaded"

    def get(self, url, headers=None, timeout=None, stream=False):
        if stream:
            return FakeRdResponse()
        if url.endswith("/user"):
            return FakeRdResponse({"id": 42})
        if "/torrents/info/" in url:
            return FakeRdResponse({
                "status": self.info_status,
                "filename": "Super Metroid (USA)",
                "links": ["https://rd/link1"],
            })
        return FakeRdResponse({})

    def post(self, url, data=None, headers=None, timeout=None):
        self.posts.append((url, data))
        if url.endswith("/torrents/addMagnet"):
            return FakeRdResponse({"id": "T1"})
        if "/unrestrict/link" in url:
            return FakeRdResponse({"download": "https://rd/file",
                                   "filename": "Super Metroid (USA).smc"})
        return FakeRdResponse({})


def rd(tmp_path, session):
    return RealDebrid(RealDebridConfig(api_token="tok",
                                       save_path=str(tmp_path)),
                      session=session)


def test_realdebrid_add_selects_files_and_remembers_the_id(tmp_path):
    session = FakeRdSession()
    client = rd(tmp_path, session)
    assert client.add("magnet:?xt=urn:btih:abc")
    assert any("/torrents/selectFiles/T1" in url for url, _ in session.posts), \
        "without file selection the torrent waits forever"
    assert client._ours() == ["T1"]


def test_realdebrid_refuses_non_magnets(tmp_path):
    client = rd(tmp_path, FakeRdSession())
    assert not client.add("https://indexer/file.torrent")


def test_realdebrid_completed_downloads_into_save_path(tmp_path):
    session = FakeRdSession()
    client = rd(tmp_path, session)
    client.add("magnet:?xt=urn:btih:abc")
    done = client.completed()
    assert len(done) == 1
    assert done[0]["name"] == "Super Metroid (USA)"
    fetched = tmp_path / "Super Metroid (USA)" / "Super Metroid (USA).smc"
    assert fetched.read_bytes() == b"ROMBYTES"


def test_realdebrid_never_touches_torrents_it_did_not_add(tmp_path):
    """Real-Debrid has no categories; the id ledger is the only thing keeping
    ROMarr from 'importing' whatever else the account holds."""
    session = FakeRdSession()
    client = rd(tmp_path, session)
    assert client.completed() == []


def test_realdebrid_waits_for_the_cloud(tmp_path):
    session = FakeRdSession()
    session.info_status = "downloading"
    client = rd(tmp_path, session)
    client.add("magnet:?xt=urn:btih:abc")
    assert client.completed() == []


def test_realdebrid_skips_files_already_fetched(tmp_path):
    session = FakeRdSession()
    client = rd(tmp_path, session)
    client.add("magnet:?xt=urn:btih:abc")
    client.completed()
    posts_before = len(session.posts)
    client.completed()
    # Second sweep unrestricts again (cheap) but must not re-download.
    assert (tmp_path / "Super Metroid (USA)"
            / "Super Metroid (USA).smc").read_bytes() == b"ROMBYTES"
    assert len(session.posts) == posts_before + 1  # one more unrestrict, no more


# -- registry ----------------------------------------------------------------

def test_every_new_client_is_buildable_and_routable(tmp_path):
    rtorrent = build_client({"type": "rtorrent", "host": "rt", "port": 80})
    synology = build_client({"type": "synology", "host": "nas", "port": 5000,
                             "username": "u", "password": "p"})
    debrid = build_client({"type": "realdebrid", "api_token": "t",
                           "save_path": str(tmp_path)})
    for client in (rtorrent, synology, debrid):
        assert client is not None
        assert client.protocol == "torrent"
    assert pick_client("torrent", [debrid]) is debrid


def test_the_schema_names_every_secret():
    for kind in ("rtorrent", "synology", "realdebrid"):
        fields = CLIENT_TYPES[kind]["fields"]
        secrets = [f["name"] for f in fields if f["type"] == "secret"]
        assert secrets, f"{kind} must mark its credential as a secret"
