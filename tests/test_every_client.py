"""The rest of the download clients: every debrid, torrent and usenet one.

One file rather than eighteen because what is being checked is the same four
things every time -- it builds, it knows whether it is configured, it can hand
a release over, and it can report a finished one -- and the interesting part is
the trap each service hides behind that, which is what the docstrings name.
"""

from __future__ import annotations

import json
import os

import pytest
import requests

from romarr.downloaders import (
    CLIENT_TYPES, DEBRID_CLIENTS, AllDebrid, Aria2, Aria2Config, BiglyBT,
    Blackhole, BlackholeConfig, DebridConfig, DebridLink, Flood, FloodConfig,
    FreeboxConfig, FreeboxDownload, Hadouken, HadoukenConfig, Linksnappy,
    NZBVortex, NzbVortexConfig, Offcloud, Porla, PorlaConfig, Premiumize,
    PutIo, TorBox, UTorrent, UTorrentConfig, Vuze, build_client, hand_off,
    merge_secrets, pick_client, redact,
)


# --- the fakes -------------------------------------------------------------

class Reply:
    """A canned response, in the shapes `requests` hands back."""

    def __init__(self, body=None, *, status=200, text=None, content=b"",
                 headers=None):
        self._body = body
        self.status_code = status
        self.headers = headers or {}
        self.content = content
        self.text = ("" if body is None else json.dumps(body)) if text is None else text

    def json(self):
        if self._body is None:
            raise ValueError("not json")
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(response=self)

    def iter_content(self, chunk_size=None):
        yield self.content

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class Router:
    """A session that answers from one function and remembers every call."""

    def __init__(self, handler):
        self._handler = handler
        self.calls = []

    def _do(self, method, url, **kw):
        self.calls.append({"method": method, "url": url, **kw})
        return self._handler(method, url, kw) or Reply({})

    def get(self, url, **kw):
        return self._do("get", url, **kw)

    def post(self, url, **kw):
        return self._do("post", url, **kw)

    def paths(self):
        return [c["url"] for c in self.calls]


ROM = b"ROMBYTES"


# --- aria2 -----------------------------------------------------------------

def aria2_handler(method, url, kw):
    call = kw["json"]
    if call["method"] == "aria2.getVersion":
        return Reply({"result": {"version": "1.36.0"}})
    if call["method"] == "aria2.addUri":
        return Reply({"result": "gid1"})
    if call["method"] == "aria2.tellStopped":
        return Reply({"result": [
            {"gid": "meta", "status": "complete", "followedBy": ["real"],
             "dir": "/dl", "files": [{"path": "/dl/x.torrent"}]},
            {"gid": "real", "status": "complete", "dir": "/dl",
             "bittorrent": {"info": {"name": "Super Metroid (USA)"}},
             "files": [{"path": "/dl/Super Metroid (USA)/rom.smc"}]},
            {"gid": "bad", "status": "error", "dir": "/dl"},
        ]})
    return None


def test_aria2_sends_the_secret_as_the_first_argument():
    """THE aria2 trap. The RPC secret is a positional argument spelled
    `token:<secret>`, not a header -- sent any other way it is not seen and
    a working daemon answers Unauthorized."""
    session = Router(aria2_handler)
    client = Aria2(Aria2Config(base_url="http://a:6800", secret="s3cret"),
                   session=session)
    assert client.add("magnet:?xt=urn:btih:abc")
    body = session.calls[0]["json"]
    assert body["params"][0] == "token:s3cret"
    assert body["params"][1] == ["magnet:?xt=urn:btih:abc"]


def test_aria2_omits_the_token_when_there_is_no_secret():
    session = Router(aria2_handler)
    Aria2(Aria2Config(base_url="http://a:6800"), session=session).add("magnet:?x")
    assert session.calls[0]["json"]["params"][0] == ["magnet:?x"]


def test_aria2_skips_the_metadata_download():
    """A magnet finishes twice: aria2 fetches the metadata as its own
    download, and reporting that one imports a .torrent file."""
    client = Aria2(Aria2Config(base_url="http://a:6800"),
                   session=Router(aria2_handler))
    done = client.completed()
    assert [d["name"] for d in done] == ["Super Metroid (USA)"]
    assert done[0]["content_path"] == "/dl/Super Metroid (USA)"


def test_aria2_reachable_and_inert_when_unconfigured():
    assert Aria2(Aria2Config(base_url="http://a:6800"),
                 session=Router(aria2_handler)).reachable()
    blank = Aria2(Aria2Config(base_url=""))
    assert not blank.configured
    assert not blank.add("magnet:?x")
    assert blank.completed() == []


# --- Flood -----------------------------------------------------------------

def flood_handler(method, url, kw):
    if url.endswith("/auth/authenticate"):
        return Reply({"success": True})
    if url.endswith("/auth/verify"):
        return Reply({"configs": {}})
    if url.endswith("/torrents/add-urls"):
        return Reply({})
    if url.endswith("/api/torrents"):
        return Reply({"torrents": {
            "AAA": {"name": "Super Metroid (USA)", "directory": "/dl",
                    "status": ["complete", "seeding"], "tags": ["romarr"]},
            "BBB": {"name": "Still Going", "directory": "/dl",
                    "status": ["downloading"], "tags": ["romarr"]},
            "CCC": {"name": "Not Ours", "directory": "/dl",
                    "status": ["complete"], "tags": ["manual"]},
        }})
    return None


def flood(session):
    return Flood(FloodConfig(base_url="http://f:3000", username="u",
                             password="p", category="romarr"), session=session)


def test_flood_authenticates_before_anything_else():
    """Flood has no key to send: `/auth/authenticate` sets a JWT cookie and
    every later call depends on the session carrying it."""
    session = Router(flood_handler)
    assert flood(session).add("magnet:?xt=urn:btih:abc")
    assert session.paths()[0].endswith("/auth/authenticate")
    assert session.calls[1]["json"]["urls"] == ["magnet:?xt=urn:btih:abc"]
    assert session.calls[1]["json"]["tags"] == ["romarr"]


def test_flood_reports_complete_only_and_ours_only():
    """`status` is a list -- a finished torrent still seeding carries both
    words -- and a tag that is not ours is somebody else's download."""
    done = flood(Router(flood_handler)).completed()
    assert [d["name"] for d in done] == ["Super Metroid (USA)"]
    assert done[0]["content_path"] == "/dl/Super Metroid (USA)"


def test_flood_re_authenticates_once_when_the_token_expired():
    replies = {"n": 0}

    def handler(method, url, kw):
        if url.endswith("/auth/authenticate"):
            return Reply({"success": True})
        if url.endswith("/auth/verify"):
            replies["n"] += 1
            if replies["n"] == 1:
                return Reply({}, status=403)
            return Reply({"configs": {}})
        return None

    session = Router(handler)
    assert flood(session).reachable()
    assert session.paths().count("http://f:3000/api/auth/authenticate") == 2


def test_flood_needs_a_username():
    assert not Flood(FloodConfig(base_url="http://f:3000")).configured


# --- Freebox ---------------------------------------------------------------

import base64 as _b64  # noqa: E402  -- only the tests need it


def freebox_handler(method, url, kw):
    if url.endswith("/login") and method == "get":
        return Reply({"success": True, "result": {"challenge": "abcd"}})
    if url.endswith("/login/session"):
        return Reply({"success": True, "result": {"session_token": "sess1"}})
    if url.endswith("/downloads/add"):
        return Reply({"success": True, "result": {"id": 7}})
    if url.endswith("/downloads/"):
        return Reply({"success": True, "result": [
            {"id": 7, "name": "Super Metroid (USA)", "status": "done",
             "download_dir": _b64.b64encode(b"/dl/roms").decode()},
            {"id": 8, "name": "Still Going", "status": "downloading",
             "download_dir": _b64.b64encode(b"/dl/roms").decode()},
        ]})
    return None


def freebox(session, **kw):
    return FreeboxDownload(FreeboxConfig(
        base_url="http://fbx/api/v1", app_id="romarr", app_token="tok",
        **kw), session=session)


def test_freebox_answers_the_challenge_rather_than_sending_the_token():
    """The app_token never crosses the wire: the box issues a challenge and
    the app replies with HMAC-SHA1 of it."""
    session = Router(freebox_handler)
    assert freebox(session).add("magnet:?xt=urn:btih:abc")
    login = [c for c in session.calls if c["url"].endswith("/login/session")][0]
    assert login["json"]["app_id"] == "romarr"
    assert login["json"]["password"] != "tok"
    assert len(login["json"]["password"]) == 40      # hex sha1
    add = [c for c in session.calls if c["url"].endswith("/downloads/add")][0]
    assert add["headers"]["X-Fbx-App-Auth"] == "sess1"


def test_freebox_base64s_the_destination_on_the_way_out():
    session = Router(freebox_handler)
    freebox(session, save_path="/dl/roms", category="romarr").add("magnet:?x")
    add = [c for c in session.calls if c["url"].endswith("/downloads/add")][0]
    assert (_b64.b64decode(add["data"]["download_dir"]).decode()
            == "/dl/roms/romarr")


def test_freebox_decodes_the_destination_on_the_way_back():
    """And base64 in the other direction: read it raw and the importer looks
    in a directory whose name is a base64 string."""
    done = freebox(Router(freebox_handler)).completed()
    assert [d["name"] for d in done] == ["Super Metroid (USA)"]
    assert done[0]["save_path"] == "/dl/roms"
    assert done[0]["content_path"] == "/dl/roms/Super Metroid (USA)"


def test_freebox_without_an_app_token_is_not_configured():
    assert not FreeboxDownload(FreeboxConfig(base_url="http://fbx/api/v1",
                                             app_id="romarr")).configured


# --- Hadouken and uTorrent (one list format, two clients) ------------------

def utorrent_rows():
    """The positional array both of them answer with. 2 name, 4 progress in
    tenths of a percent, 11 label, 26 save path."""
    def row(name, progress, label, path):
        cells = [""] * 27
        cells[0] = "HASH" + name[:4]
        cells[2] = name
        cells[4] = progress
        cells[11] = label
        cells[26] = path
        return cells

    return [
        row("Super Metroid (USA)", 1000, "romarr", "/dl"),
        row("Still Going", 640, "romarr", "/dl"),
        row("Not Ours", 1000, "manual", "/dl"),
    ]


def hadouken_handler(method, url, kw):
    call = kw["json"]
    if call["method"] == "core.getSystemInfo":
        return Reply({"result": {"versions": {}}})
    if call["method"] == "webui.addTorrent":
        return Reply({"result": ""})
    if call["method"] == "webui.list":
        return Reply({"result": {"torrents": utorrent_rows()}})
    return None


def test_hadouken_adds_by_url_with_the_label():
    session = Router(hadouken_handler)
    client = Hadouken(HadoukenConfig(base_url="http://h:7070", username="u",
                                     password="p"), session=session)
    assert client.add("magnet:?xt=urn:btih:abc")
    params = session.calls[0]["json"]["params"]
    assert params[0] == "url"
    assert params[2] == {"label": "romarr"}
    assert session.calls[0]["auth"] == ("u", "p")


def test_hadouken_treats_an_empty_string_result_as_success():
    """It answers "" on success. `if not result` would call every accepted
    torrent a refusal."""
    client = Hadouken(HadoukenConfig(base_url="http://h:7070"),
                      session=Router(hadouken_handler))
    assert client.add("magnet:?x")


def test_hadouken_reads_the_positional_list():
    client = Hadouken(HadoukenConfig(base_url="http://h:7070"),
                      session=Router(hadouken_handler))
    done = client.completed()
    assert [d["name"] for d in done] == ["Super Metroid (USA)"]
    assert done[0]["content_path"] == "/dl/Super Metroid (USA)"


TOKEN_PAGE = "<html><div id='token' style='display:none;'>TKN123</div></html>"


def utorrent_handler(method, url, kw):
    if url.endswith("token.html"):
        return Reply(text=TOKEN_PAGE)
    params = kw.get("params") or {}
    if params.get("list"):
        return Reply({"torrents": utorrent_rows()})
    return Reply({"build": 45000})


def test_utorrent_takes_the_token_from_the_html_page():
    """The token is in a div in an HTML fragment, not in JSON, and every
    call is refused without it."""
    session = Router(utorrent_handler)
    client = UTorrent(UTorrentConfig(base_url="http://u:8080"), session=session)
    assert client.add("magnet:?xt=urn:btih:" + "a" * 40)
    add = [c for c in session.calls if (c.get("params") or {}).get("action") == "add-url"][0]
    assert add["params"]["token"] == "TKN123"


def test_utorrent_labels_a_magnet_with_its_own_infohash():
    """add-url cannot carry a label and does not return a hash, so the only
    label that can be set is the one a magnet brought with it."""
    session = Router(utorrent_handler)
    client = UTorrent(UTorrentConfig(base_url="http://u:8080"), session=session)
    client.add("magnet:?xt=urn:btih:" + "ab" * 20)
    props = [c for c in session.calls
             if (c.get("params") or {}).get("action") == "setprops"]
    assert props and props[0]["params"]["hash"] == ("AB" * 20)
    assert props[0]["params"]["v"] == "romarr"


def test_utorrent_leaves_a_torrent_url_unlabelled_rather_than_guessing():
    session = Router(utorrent_handler)
    client = UTorrent(UTorrentConfig(base_url="http://u:8080"), session=session)
    assert client.add("https://indexer/file.torrent")
    assert not [c for c in session.calls
                if (c.get("params") or {}).get("action") == "setprops"]


def test_utorrent_completed_filters_to_our_finished_label():
    client = UTorrent(UTorrentConfig(base_url="http://u:8080"),
                      session=Router(utorrent_handler))
    done = client.completed()
    assert [d["name"] for d in done] == ["Super Metroid (USA)"]


# --- porla -----------------------------------------------------------------

def porla_handler(method, url, kw):
    call = kw["json"]
    if call["method"] == "sys.versions":
        return Reply({"result": {"porla": {"version": "0.30.0"}}})
    if call["method"] == "torrents.add":
        return Reply({"result": {"info_hash": ["abc", None]}})
    if call["method"] == "torrents.list":
        return Reply({"result": {"torrents": [
            {"name": "Super Metroid (USA)", "save_path": "/dl", "progress": 1},
            {"name": "Still Going", "save_path": "/dl", "progress": 0.4},
        ]}})
    return None


def test_porla_sends_params_as_an_object():
    """Every other JSON-RPC client here sends a positional array; porla takes
    a named object and a list is a parse error."""
    session = Router(porla_handler)
    client = Porla(PorlaConfig(base_url="http://p:1337", api_token="jwt",
                               save_path="/dl"), session=session)
    assert client.add("magnet:?xt=urn:btih:abc")
    body = session.calls[0]["json"]
    assert body["params"] == {"magnet_uri": "magnet:?xt=urn:btih:abc",
                              "save_path": "/dl"}
    assert session.calls[0]["headers"]["Authorization"] == "Bearer jwt"


def test_porla_completion_is_a_fraction_not_a_word():
    client = Porla(PorlaConfig(base_url="http://p:1337", api_token="jwt"),
                   session=Router(porla_handler))
    done = client.completed()
    assert [d["name"] for d in done] == ["Super Metroid (USA)"]
    assert done[0]["content_path"] == "/dl/Super Metroid (USA)"


def test_porla_needs_a_token():
    assert not Porla(PorlaConfig(base_url="http://p:1337")).configured


# --- Vuze and BiglyBT ------------------------------------------------------

def test_vuze_and_biglybt_speak_transmissions_rpc():
    """Neither has a protocol of its own worth writing: what they expose is
    the Transmission RPC, 409 handshake included."""
    for kind, label in (("vuze", "Vuze"), ("biglybt", "BiglyBT")):
        client = build_client({"type": kind, "host": "v", "port": 9091})
        assert client.name == label
        assert client.RPC_PATH == "/transmission/rpc"
        assert client.protocol == "torrent"


def test_vuze_answers_the_409_challenge_like_transmission_does():
    replies = [Reply({}, status=409, headers={"X-Transmission-Session-Id": "s1"}),
               Reply({"result": "success"})]
    session = Router(lambda *a: replies.pop(0))
    from romarr.downloaders import TransmissionConfig
    client = Vuze(TransmissionConfig(base_url="http://v:9091"), session=session)
    assert client.add("magnet:?xt=urn:btih:abc")
    assert session.calls[1]["headers"]["X-Transmission-Session-Id"] == "s1"
    assert isinstance(BiglyBT(TransmissionConfig(base_url="http://b:9091")),
                      Vuze)


# --- NZBVortex -------------------------------------------------------------

def vortex_handler(method, url, kw):
    if url.endswith("/api/auth/nonce"):
        return Reply({"authNonce": "N1"})
    if url.endswith("/api/auth/login"):
        return Reply({"loginResult": "successful", "sessionID": "SID1"})
    if url.endswith("/api/app/appversion"):
        return Reply({"app": "2.5"})
    if url.endswith("/api/nzb/add"):
        return Reply({"result": 0, "add_uuid": "U1"})
    if url.endswith("/api/nzb"):
        return Reply({"result": 0, "nzbs": [
            {"id": 1, "uiTitle": "Super Metroid (USA)", "state": 20,
             "destinationPath": "/done/Super Metroid (USA)"},
            {"id": 2, "uiTitle": "Repairing", "state": 9,
             "destinationPath": "/done/Repairing"},
        ]})
    return Reply(content=b"<nzb/>", text="<nzb/>")


def vortex(session):
    return NZBVortex(NzbVortexConfig(base_url="https://v:4321", api_key="KEY"),
                     session=session)


def test_nzbvortex_hashes_the_key_instead_of_sending_it():
    """THE NZBVortex trap. There is no `?apikey=` mode: the key is hashed
    with a nonce from each side and never crosses the wire."""
    import base64
    import hashlib

    session = Router(vortex_handler)
    assert vortex(session).add("https://indexer/get?id=1", name="Super Metroid")
    login = [c for c in session.calls if c["url"].endswith("/auth/login")][0]
    params = login["params"]
    expected = base64.b64encode(hashlib.sha256(
        f"N1:{params['cnonce']}:KEY".encode()).digest()).decode()
    assert params["hash"] == expected
    assert "KEY" not in json.dumps(params)


def test_nzbvortex_uploads_the_nzb_under_the_release_name():
    """It takes no URLs at all, so ROMarr does the GET -- and names the
    upload after the release, because that is what the importer matches on."""
    session = Router(vortex_handler)
    assert vortex(session).add("https://indexer/get?id=1", name="Super Metroid")
    add = [c for c in session.calls if c["url"].endswith("/nzb/add")][0]
    assert add["files"]["name"][0] == "Super Metroid.nzb"
    assert add["params"]["sessionid"] == "SID1"


def test_nzbvortex_reports_only_state_twenty():
    """20 is Done. Everything under it -- repairing, joining, moving -- is
    still in flight, and 21 and up are the failures."""
    done = vortex(Router(vortex_handler)).completed()
    assert [d["name"] for d in done] == ["Super Metroid (USA)"]
    assert done[0]["content_path"] == "/done/Super Metroid (USA)"


def test_nzbvortex_logs_back_in_when_the_session_goes_stale():
    state = {"stale": 1}

    def handler(method, url, kw):
        if url.endswith("/api/nzb") and state["stale"]:
            state["stale"] = 0
            return Reply({"result": 1})       # not logged in
        return vortex_handler(method, url, kw)

    session = Router(handler)
    assert vortex(session).completed()
    assert len([c for c in session.calls
                if c["url"].endswith("/auth/login")]) == 2


def test_nzbvortex_takes_the_release_name():
    assert NZBVortex.TAKES_NAME


# --- the debrid services ---------------------------------------------------

def debrid(cls, tmp_path, session, **kw):
    return cls(DebridConfig(api_token="tok", save_path=str(tmp_path),
                            base_url=cls.DEFAULT_URL, **kw), session=session)


def alldebrid_handler(method, url, kw):
    if url.endswith("/user"):
        return Reply({"status": "success", "data": {"user": {"username": "u"}}})
    if url.endswith("/magnet/upload"):
        return Reply({"status": "success",
                      "data": {"magnets": [{"id": 42, "ready": False}]}})
    if url.endswith("/magnet/status"):
        return Reply({"status": "success", "data": {"magnets": {
            "id": 42, "filename": "Super Metroid (USA)", "status": "Ready"}}})
    if url.endswith("/magnet/files"):
        return Reply({"status": "success", "data": {"magnets": [{
            "id": 42, "files": [
                {"n": "disc", "e": [{"n": "rom.smc", "s": 8,
                                     "l": "https://ad/f/deep"}]},
                {"n": "readme.txt", "s": 4, "l": "https://ad/f/flat"},
            ]}]}})
    if url.endswith("/link/unlock"):
        name = "deep.smc" if "deep" in (kw.get("data") or {}).get("link", "") \
            else "flat.txt"
        return Reply({"status": "success",
                      "data": {"link": "https://cdn/" + name, "filename": name}})
    return Reply(content=ROM)


def test_alldebrid_walks_the_file_tree(tmp_path):
    """v4.1 moved the links out of magnet/status into magnet/files, which
    answers with a folder TREE -- reading only its top level finds none of a
    multi-file torrent's files."""
    client = debrid(AllDebrid, tmp_path, Router(alldebrid_handler))
    assert client.add("magnet:?xt=urn:btih:abc")
    assert client._ours() == ["42"]
    done = client.completed()
    assert [d["name"] for d in done] == ["Super Metroid (USA)"]
    folder = tmp_path / "Super Metroid (USA)"
    assert (folder / "deep.smc").read_bytes() == ROM
    assert (folder / "flat.txt").read_bytes() == ROM


def test_alldebrid_refuses_a_torrent_url(tmp_path):
    client = debrid(AllDebrid, tmp_path, Router(alldebrid_handler))
    assert not client.add("https://indexer/file.torrent")


def test_alldebrid_reads_the_envelope_not_the_status_code(tmp_path):
    """It answers HTTP 200 with `"status": "error"`; believing the status
    code calls every refusal a success."""
    session = Router(lambda *a: Reply({"status": "error",
                                       "error": {"code": "AUTH_BAD_APIKEY"}}))
    client = debrid(AllDebrid, tmp_path, session)
    assert not client.reachable()
    assert not client.add("magnet:?xt=urn:btih:abc")


def premiumize_handler(method, url, kw):
    if url.endswith("/account/info"):
        return Reply({"status": "success", "customer_id": "1"})
    if url.endswith("/transfer/create"):
        return Reply({"status": "success", "id": "T1",
                      "name": "Super Metroid (USA)"})
    if url.endswith("/transfer/list"):
        return Reply({"status": "success", "transfers": [
            {"id": "T1", "name": "Super Metroid (USA)", "status": "seeding",
             "folder_id": "F1"},
            {"id": "T9", "name": "Somebody Else", "status": "finished",
             "folder_id": "F9"},
        ]})
    if url.endswith("/folder/list"):
        return Reply({"status": "success", "content": [
            {"id": "f1", "name": "rom.smc", "type": "file",
             "link": "https://pm/rom.smc"},
            {"id": "d1", "name": "subdir", "type": "folder"},
        ]})
    return Reply(content=ROM)


def test_premiumize_treats_seeding_as_finished(tmp_path):
    """A transfer being seeded back has every byte. Waiting for `finished`
    alone leaves it in the queue for as long as the ratio target lasts."""
    client = debrid(Premiumize, tmp_path, Router(premiumize_handler))
    assert client.add("magnet:?xt=urn:btih:abc")
    done = client.completed()
    assert [d["name"] for d in done] == ["Super Metroid (USA)"]
    assert (tmp_path / "Super Metroid (USA)" / "rom.smc").read_bytes() == ROM


def test_premiumize_ignores_transfers_it_did_not_create(tmp_path):
    """There are no categories, so the id ledger is the only thing keeping
    somebody else's transfers out of the library."""
    client = debrid(Premiumize, tmp_path, Router(premiumize_handler))
    assert client.completed() == []


def torbox_handler(method, url, kw):
    if url.endswith("/user/me"):
        return Reply({"success": True, "data": {"id": 1}})
    if url.endswith("/torrents/createtorrent"):
        return Reply({"success": True, "data": {"torrent_id": 5}})
    if url.endswith("/torrents/mylist"):
        return Reply({"success": True, "data": [
            {"id": 5, "name": "Super Metroid (USA)", "download_finished": True,
             "download_present": True,
             "files": [{"id": 0, "short_name": "rom.smc",
                        "name": "Super Metroid (USA)/rom.smc"}]},
            {"id": 6, "name": "Expired", "download_finished": True,
             "download_present": False, "files": [{"id": 0, "name": "x"}]},
        ]})
    if "/torrents/requestdl" in url:
        return Reply({"success": True, "data": "https://cdn.torbox/rom.smc"})
    return Reply(content=ROM)


def test_torbox_mints_a_url_per_file(tmp_path):
    """mylist names the files but does not link them; a CDN URL comes from
    requestdl and lasts three hours."""
    session = Router(torbox_handler)
    client = debrid(TorBox, tmp_path, session)
    assert client.add("magnet:?xt=urn:btih:abc")
    assert session.calls[0]["files"]["magnet"][1] == "magnet:?xt=urn:btih:abc"
    done = client.completed()
    assert [d["name"] for d in done] == ["Super Metroid (USA)"]
    assert (tmp_path / "Super Metroid (USA)" / "rom.smc").read_bytes() == ROM


def test_torbox_skips_a_torrent_no_longer_present(tmp_path):
    """`download_finished` stays true after TorBox expires the data off its
    storage; only `download_present` says whether there is anything left."""
    client = debrid(TorBox, tmp_path, Router(torbox_handler))
    client._remember(6)
    assert [d["name"] for d in client.completed()] == []


def debridlink_handler(method, url, kw):
    if url.endswith("/account/infos"):
        return Reply({"success": True, "value": {"email": "u@example.invalid"}})
    if url.endswith("/seedbox/add"):
        return Reply({"success": True, "value": {"id": "D1"}})
    if url.endswith("/seedbox/list"):
        return Reply({"success": True, "value": [
            {"id": "D1", "name": "Super Metroid (USA)", "downloadPercent": 100,
             "files": [{"name": "rom.smc", "downloadUrl": "https://dl/rom.smc"}]},
            {"id": "D1x", "name": "Half", "downloadPercent": 40, "files": []},
        ]})
    return Reply(content=ROM)


def test_debridlink_links_every_file_in_the_listing(tmp_path):
    """One call does the whole job: seedbox/list carries a downloadUrl per
    file, so nothing has to be resolved afterwards."""
    client = debrid(DebridLink, tmp_path, Router(debridlink_handler))
    assert client.reachable()
    assert client.add("magnet:?xt=urn:btih:abc")
    done = client.completed()
    assert [d["name"] for d in done] == ["Super Metroid (USA)"]
    assert (tmp_path / "Super Metroid (USA)" / "rom.smc").read_bytes() == ROM


def test_debridlink_reads_value_not_data(tmp_path):
    """Its envelope key is `value`; every other service in the file calls it
    `data`, and reading the wrong one is a client that never finds anything."""
    session = Router(lambda *a: Reply({"success": True, "data": {"id": "X"}}))
    assert not debrid(DebridLink, tmp_path, session).add("magnet:?x")


def offcloud_handler(method, url, kw):
    if url.endswith("/proxy"):
        return Reply([])
    if url.endswith("/api/cloud"):
        return Reply({"requestId": "R1", "fileName": "Super Metroid (USA)",
                      "status": "created", "url": "https://oc/single.smc"})
    if url.endswith("/cloud/status"):
        return Reply({"status": {"requestId": "R1", "status": "downloaded",
                                 "fileName": "Super Metroid (USA)"}})
    if "/cloud/explore/" in url:
        return Reply(["https://oc/rom.smc", "https://oc/manual.pdf"])
    return Reply(content=ROM)


def test_offcloud_authenticates_with_a_key_parameter(tmp_path):
    session = Router(offcloud_handler)
    client = debrid(Offcloud, tmp_path, session)
    assert client.add("magnet:?xt=urn:btih:abc")
    add = [c for c in session.calls if c["url"].endswith("/api/cloud")][0]
    assert add["data"]["key"] == "tok"
    assert add["headers"] == {}


def test_offcloud_explores_a_multi_file_download(tmp_path):
    client = debrid(Offcloud, tmp_path, Router(offcloud_handler))
    client.add("magnet:?xt=urn:btih:abc")
    done = client.completed()
    folder = tmp_path / "Super Metroid (USA)"
    assert [d["name"] for d in done] == ["Super Metroid (USA)"]
    assert (folder / "rom.smc").read_bytes() == ROM
    assert (folder / "manual.pdf").read_bytes() == ROM


def test_offcloud_falls_back_to_the_url_it_was_given_at_add_time(tmp_path):
    """A single file has nothing to explore. Its URL came back from the add,
    which is why it is written into the ledger there rather than guessed at
    from a URL pattern later."""
    def handler(method, url, kw):
        if "/cloud/explore/" in url:
            return Reply({"error": "not an archive"})
        return offcloud_handler(method, url, kw)

    client = debrid(Offcloud, tmp_path, Router(handler))
    client.add("magnet:?xt=urn:btih:abc")
    done = client.completed()
    assert [d["name"] for d in done] == ["Super Metroid (USA)"]
    assert (tmp_path / "Super Metroid (USA)" / "single.smc").read_bytes() == ROM


def putio_handler(method, url, kw):
    if url.endswith("/account/info"):
        return Reply({"info": {"username": "u"}, "status": "OK"})
    if url.endswith("/transfers/add"):
        return Reply({"transfer": {"id": 11, "name": "Super Metroid (USA)"}})
    if url.endswith("/transfers/11"):
        return Reply({"transfer": {"id": 11, "name": "Super Metroid (USA)",
                                   "status": "COMPLETED", "file_id": 900}})
    if url.endswith("/files/list"):
        if (kw.get("params") or {}).get("parent_id") == 900:
            return Reply({"files": [
                {"id": 901, "name": "rom.smc", "file_type": "VIDEO"},
                {"id": 902, "name": "extras", "file_type": "FOLDER"},
            ]})
        return Reply({"files": [
            {"id": 903, "name": "manual.pdf", "file_type": "FILE"}]})
    if url.endswith("/url"):
        return Reply({"url": "https://put.io/dl"})
    return Reply(content=ROM)


def test_putio_walks_the_folder_a_transfer_left_behind(tmp_path):
    """A transfer's `file_id` is a folder for a multi-file torrent, and its
    subfolders hold files too."""
    client = debrid(PutIo, tmp_path, Router(putio_handler))
    assert client.reachable()
    assert client.add("magnet:?xt=urn:btih:abc")
    done = client.completed()
    folder = tmp_path / "Super Metroid (USA)"
    assert [d["name"] for d in done] == ["Super Metroid (USA)"]
    assert (folder / "rom.smc").read_bytes() == ROM
    assert (folder / "manual.pdf").read_bytes() == ROM


def linksnappy_handler(method, url, kw):
    if url.endswith("/AUTHENTICATE"):
        return Reply({"status": "OK", "error": False})
    if url.endswith("/torrents/ADDMAGNET"):
        return Reply({"status": "OK", "error": False,
                      "return": [{"torrentid": 77, "error": False}]})
    if url.endswith("/torrents/FOLDERLIST"):
        return Reply({"status": "OK", "error": False,
                      "return": [{"id": "5", "type": "root",
                                  "text": "Downloads"}]})
    if url.endswith("/torrents/START"):
        return Reply({"status": "OK", "error": False, "return": {}})
    if url.endswith("/torrents/STATUS"):
        return Reply({"status": "OK", "error": False,
                      "return": {"name": "Super Metroid (USA)",
                                 "status": "FINISHED"}})
    if url.endswith("/torrents/FILES"):
        return Reply({"status": "OK", "error": False, "return": {
            "disc": {"rom": {"filename": "rom.smc",
                             "downloadLink": "https://ls/rom.smc"}}}})
    return Reply(content=ROM)


def linksnappy(tmp_path, session):
    return Linksnappy(DebridConfig(
        api_token="", save_path=str(tmp_path),
        base_url=Linksnappy.DEFAULT_URL, username="u", password="p"),
        session=session)


def test_linksnappy_starts_what_it_added(tmp_path):
    """THE Linksnappy trap. ADDMAGNET registers the torrent stopped; without
    START into a folder it never downloads and nothing says why."""
    session = Router(linksnappy_handler)
    client = linksnappy(tmp_path, session)
    assert client.configured
    assert client.add("magnet:?xt=urn:btih:abc")
    started = [c for c in session.calls if c["url"].endswith("/torrents/START")]
    assert started and started[0]["params"] == {"tid": 77, "fid": "5"}


def test_linksnappy_authenticates_with_the_account(tmp_path):
    """It has no API key at all: the session cookie from AUTHENTICATE is the
    credential every later call rides on."""
    session = Router(linksnappy_handler)
    linksnappy(tmp_path, session).reachable()
    first = session.calls[0]
    assert first["url"].endswith("/AUTHENTICATE")
    assert first["params"] == {"username": "u", "password": "p"}
    assert first["headers"] == {}


def test_linksnappy_walks_the_nested_files_answer(tmp_path):
    client = linksnappy(tmp_path, Router(linksnappy_handler))
    client.add("magnet:?xt=urn:btih:abc")
    done = client.completed()
    assert [d["name"] for d in done] == ["Super Metroid (USA)"]
    assert (tmp_path / "Super Metroid (USA)" / "rom.smc").read_bytes() == ROM


def test_a_debrid_client_without_a_save_path_is_inert(tmp_path):
    for kind, cls in DEBRID_CLIENTS.items():
        client = cls(DebridConfig(api_token="t", save_path="",
                                  base_url=cls.DEFAULT_URL,
                                  username="u", password="p"))
        assert not client.configured, kind
        assert client.completed() == [], kind


def test_every_debrid_service_keeps_its_own_ledger():
    """Two rows sharing a save path must not read each other's ids, or each
    would "import" the other's downloads."""
    ledgers = [cls.LEDGER for cls in DEBRID_CLIENTS.values()]
    assert len(set(ledgers)) == len(ledgers)


# --- the blackholes --------------------------------------------------------

def blackhole(tmp_path, mode="torrent", **kw):
    drop = tmp_path / "drop"
    watch = tmp_path / "watch"
    session = Router(lambda *a: Reply(content=b"d8:announce", text="x"))
    return Blackhole(BlackholeConfig(
        drop_path=str(drop), watch_path=str(watch), mode=mode, **kw),
        session=session), drop, watch


def test_torrent_blackhole_writes_the_release_name(tmp_path):
    """The importer matches a finished download to its queue row by the
    release title, so a file named after the indexer's URL matches nothing."""
    client, drop, _ = blackhole(tmp_path)
    assert hand_off(client, "https://indexer/get?id=99",
                    name="Super Metroid (USA)")
    assert (drop / "Super Metroid (USA).torrent").read_bytes() == b"d8:announce"


def test_usenet_blackhole_writes_an_nzb(tmp_path):
    client, drop, _ = blackhole(tmp_path, mode="usenet")
    assert client.protocol == "usenet"
    assert client.name == "Usenet Blackhole"
    assert client.add("https://indexer/get?id=99", name="Super Metroid")
    assert (drop / "Super Metroid.nzb").exists()


def test_blackhole_writes_a_magnet_as_its_own_file(tmp_path):
    """A magnet has no file to write. Writing one line of text is a client's
    choice to make; refusing the release is not."""
    client, drop, _ = blackhole(tmp_path)
    assert client.add("magnet:?xt=urn:btih:abc", name="Super Metroid")
    written = drop / "Super Metroid.magnet"
    assert written.read_text() == "magnet:?xt=urn:btih:abc"


def test_blackhole_never_lets_a_title_choose_a_path(tmp_path):
    """A release title is somebody else's text; it does not get to contain a
    path separator."""
    client, drop, _ = blackhole(tmp_path)
    client.add("magnet:?x", name="../../etc/Super Metroid")
    assert [p.name for p in drop.iterdir()] == ["Super Metroid.magnet"]


def test_blackhole_reports_what_has_settled(tmp_path):
    client, _, watch = blackhole(tmp_path, settle=0)
    watch.mkdir(parents=True)
    rom = watch / "Super Metroid (USA).smc"
    rom.write_bytes(ROM)
    old = os.stat(rom).st_mtime - 600
    os.utime(rom, (old, old))
    done = client.completed()
    assert [d["name"] for d in done] == ["Super Metroid (USA)"]
    assert done[0]["content_path"] == str(rom)


def test_blackhole_ignores_a_file_still_being_written(tmp_path):
    """Nothing announces the end of somebody else's write; importing a file
    that is still growing produces a truncated ROM that looks like a bad
    dump."""
    client, _, watch = blackhole(tmp_path, settle=600)
    watch.mkdir(parents=True)
    (watch / "Still Arriving.smc").write_bytes(ROM)
    assert client.completed() == []


def test_blackhole_ignores_in_progress_suffixes(tmp_path):
    client, _, watch = blackhole(tmp_path, settle=0)
    watch.mkdir(parents=True)
    for name in ("half.part", "other.!qb", ".hidden"):
        path = watch / name
        path.write_bytes(ROM)
        os.utime(path, (0, 0))
    assert client.completed() == []


def test_blackhole_reachable_creates_both_folders(tmp_path):
    client, drop, watch = blackhole(tmp_path)
    assert client.reachable()
    assert drop.is_dir() and watch.is_dir()
    assert str(drop) in client.detail


def test_blackhole_without_both_folders_is_not_configured():
    client = Blackhole(BlackholeConfig(drop_path="/drop"))
    assert not client.configured
    assert not client.reachable()
    assert "both folders" in client.detail


# --- the registry ----------------------------------------------------------

EXPECTED = {
    "torrent": {"qbittorrent", "transmission", "deluge", "rtorrent",
                "synology", "vuze", "biglybt", "utorrent", "aria2", "flood",
                "freebox", "hadouken", "porla", "torrentblackhole",
                "realdebrid", "alldebrid", "premiumize", "torbox",
                "debridlink", "offcloud", "putio", "linksnappy"},
    "usenet": {"sabnzbd", "nzbget", "nzbvortex", "usenetblackhole"},
    "direct": {"direct"},
    "browser": {"browser"},
}


@pytest.mark.parametrize("protocol", sorted(EXPECTED))
def test_every_client_is_registered_under_its_protocol(protocol):
    found = {k for k, v in CLIENT_TYPES.items() if v["protocol"] == protocol}
    assert found == EXPECTED[protocol]


def filled_in(kind: str) -> dict:
    """A configuration with every field of this type answered.

    Built from the schema rather than written out, so a type that grows a
    required field is exercised with it rather than quietly stopping being
    covered.
    """
    cfg = {"type": kind, "host": "h", "port": 1}
    for field in CLIENT_TYPES[kind]["fields"]:
        if field["type"] in ("bool", "int"):
            continue
        cfg.setdefault(field["name"], "x")
    return cfg


@pytest.mark.parametrize("kind", sorted(
    EXPECTED["torrent"] | EXPECTED["usenet"]))
def test_every_client_can_be_built_and_routed(kind):
    client = build_client(filled_in(kind))
    assert client is not None, kind
    assert client.protocol == CLIENT_TYPES[kind]["protocol"], kind
    # `name` is read by the grab path to say which client refused a release;
    # a client without one raises inside the error handler instead.
    assert client.name, kind
    # A fully answered form must produce a client `pick_client` will route to.
    # A required field the builder does not read is a row that saves, tests
    # and is then skipped at grab time with nothing on screen.
    assert getattr(client, "configured", True), kind
    assert pick_client(client.protocol, [client]) is client


@pytest.mark.parametrize("kind", sorted(CLIENT_TYPES))
def test_no_new_secret_field_reaches_a_browser(kind):
    """redact() derives its list from the schema, so this fails the moment a
    credential is declared as an ordinary text field."""
    secrets = {f["name"] for f in CLIENT_TYPES[kind]["fields"]
               if f["type"] == "secret"}
    cfg = {"type": kind, **{name: "SECRET" for name in secrets}}
    out = redact(cfg)
    assert "SECRET" not in str(out), kind
    for name in secrets:
        assert out[name] != "SECRET"


@pytest.mark.parametrize("kind", sorted(CLIENT_TYPES))
def test_an_untouched_secret_survives_a_save(kind):
    """The edit form shows a placeholder; saving it verbatim would replace a
    real credential with eight asterisks and say nothing."""
    secrets = {f["name"] for f in CLIENT_TYPES[kind]["fields"]
               if f["type"] == "secret"}
    stored = {"type": kind, **{name: "real" for name in secrets}}
    merged = merge_secrets(redact(dict(stored)), stored)
    for name in secrets:
        assert merged[name] == "real", kind


def test_every_credential_is_declared_a_secret():
    """A field whose name says credential and whose type says text is a
    password in a browser page."""
    for kind, spec in CLIENT_TYPES.items():
        for field in spec["fields"]:
            if field["name"] in ("password", "api_key", "api_token", "secret",
                                 "app_token"):
                assert field["type"] == "secret", f"{kind}.{field['name']}"
