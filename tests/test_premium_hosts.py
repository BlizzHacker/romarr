"""File-host accounts, MultiUp mirrors, their place in HosterRoute, and native clouds."""
from __future__ import annotations

import time

import pytest

from romarr import cloud_native, hosters
from romarr.hosters import HosterRoute
from romarr.premium_hosts import HostAccountError, OneFichier, Rapidgator, multiup_mirrors


class Resp:
    def __init__(self, body=None, status=200, text="", headers=None):
        self._body, self.status_code, self.text = body, status, text
        self.headers = headers or {}

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body

    def iter_content(self, n):
        yield b"data"

    def close(self):
        pass


class Session:
    def __init__(self, *answers):
        self.answers = list(answers)
        self.sent = []

    def _next(self, method, url, kw):
        self.sent.append((method, url, kw))
        return self.answers.pop(0)

    def post(self, url, **kw):
        return self._next("POST", url, kw)

    def get(self, url, **kw):
        return self._next("GET", url, kw)

    def put(self, url, **kw):
        return self._next("PUT", url, kw)

    def request(self, method, url, **kw):
        return self._next(method, url, kw)


# -- Rapidgator ----------------------------------------------------------------------


def test_rapidgator_logs_in_and_resolves():
    s = Session(Resp({"response": {"token": "T1", "user": {"is_premium": True}}, "status": 200}),
                Resp({"response": {"download_url": "https://pr1.rapidgator.net/f/x"}, "status": 200}))
    kept = []
    rg = Rapidgator("me@x", "pw", on_token=kept.append, session=s)
    assert rg.resolve("https://rapidgator.net/file/abc123def/Game.zip.html") == "https://pr1.rapidgator.net/f/x"
    assert s.sent[0][1].endswith("/user/login") and s.sent[0][2]["data"]["password"] == "pw"
    assert s.sent[1][2]["data"] == {"file_id": "abc123def", "token": "T1"}
    assert kept == ["T1"]


def test_rapidgator_logs_in_again_when_the_session_expired():
    s = Session(Resp({"response": None, "status": 401, "details": "Error. Session doesn't exist"}),
                Resp({"response": {"token": "T2", "user": {}}, "status": 200}),
                Resp({"response": {"download_url": "https://dl/x"}, "status": 200}))
    rg = Rapidgator("me@x", "pw", token="OLD", session=s)
    assert rg.resolve("https://rg.to/file/abc123") == "https://dl/x"


def test_rapidgator_refusals_are_readable():
    s = Session(Resp({"response": None, "status": 401, "details": "Error: Wrong e-mail or password."}))
    with pytest.raises(HostAccountError, match="Wrong e-mail"):
        Rapidgator("me@x", "bad", session=s).sign_in()
    with pytest.raises(HostAccountError, match="not a Rapidgator"):
        Rapidgator("me@x", "pw", token="T", session=Session()).resolve("https://rapidgator.net/folder/1")


def test_rapidgator_supports_only_its_hosts():
    rg = Rapidgator("a", "b")
    assert rg.supports_host("rapidgator.net") and rg.supports_host("rg.to")
    assert not rg.supports_host("1fichier.com")


# -- 1fichier ------------------------------------------------------------------------


def test_onefichier_resolves_with_bearer_key():
    s = Session(Resp({"url": "https://a-1.1fichier.com/c1", "status": "OK"}))
    assert OneFichier("KEY", session=s).resolve("https://1fichier.com/?abcde") == "https://a-1.1fichier.com/c1"
    method, url, kw = s.sent[0]
    assert url.endswith("/download/get_token.cgi") and kw["headers"]["Authorization"] == "Bearer KEY"
    assert kw["json"] == {"url": "https://1fichier.com/?abcde"}


def test_onefichier_ko_is_an_error():
    s = Session(Resp({"message": "Not authenticated #246", "status": "KO"}, status=403))
    with pytest.raises(HostAccountError, match="Not authenticated"):
        OneFichier("bad", session=s).resolve("https://1fichier.com/?abcde")


# -- MultiUp -------------------------------------------------------------------------


def test_multiup_reads_mirror_links_from_the_page():
    page = ('<button link="https://1fichier.com/?zz9" namehost="1fichier.com">'
            '<a href="https://rapidgator.net/file/abc/x.html">rg</a>'
            '<a href="https://multiup.io/en/premium">ignore</a><a href="https://example.com/">ad</a>')
    s = Session(Resp({"error": "success", "hosts": {"1fichier.com": "valid"}}), Resp(text=page))
    out = multiup_mirrors("https://multiup.io/en/mirror/0123456789abcdef0123456789abcdef/game.zip",
                          session=s)
    assert out == ["https://1fichier.com/?zz9", "https://rapidgator.net/file/abc/x.html"]


def test_multiup_gone_file_has_no_mirrors():
    s = Session(Resp({"error": "file not found"}))
    assert multiup_mirrors("https://multiup.io/download/0123456789abcdef0123456789abcdef/x", session=s) == []
    assert multiup_mirrors("https://multiup.io/en/upload", session=Session()) == []


# -- HosterRoute ---------------------------------------------------------------------


class Direct:
    protocol = "direct"
    TAKES_NAME = True
    configured = True

    def __init__(self):
        self.added = []

    def add(self, url, name=""):
        self.added.append((url, name))
        return True


class Account:
    label = "Rapidgator"
    configured = True

    def __init__(self, fail=False):
        self.fail = fail

    def supports_host(self, host):
        return host in ("rapidgator.net", "rg.to")

    def resolve(self, url):
        if self.fail:
            raise HostAccountError("traffic exhausted")
        return "https://dl.rapidgator.net/file"


def test_premium_account_unlocks_its_host_through_the_direct_client():
    direct = Direct()
    route = HosterRoute(direct=direct, premium=[Account()])
    assert route.add("https://rapidgator.net/file/abc/x.html", name="Game")
    assert direct.added == [("https://dl.rapidgator.net/file", "Game")]


def test_a_refusing_account_falls_through_to_the_next_mirror():
    direct = Direct()
    route = HosterRoute(direct=direct, premium=[Account(fail=True)])
    assert route.add("https://rapidgator.net/file/abc/x.html",
                     mirrors=["https://pixeldrain.com/u/AbC123"])
    assert direct.added[0][0] == "https://pixeldrain.com/api/file/AbC123?download"


def test_multiup_link_is_replaced_by_its_mirrors():
    direct = Direct()
    route = HosterRoute(direct=direct, expand=lambda url: ["https://pixeldrain.com/u/Mir123"])
    assert route.add("https://multiup.io/en/mirror/0123456789abcdef0123456789abcdef/g.zip")
    assert direct.added[0][0] == "https://pixeldrain.com/api/file/Mir123?download"


def test_no_route_names_the_account_option():
    route = HosterRoute(direct=Direct())
    assert not route.add("https://rapidgator.net/file/abc/x.html")
    assert "Rapidgator or 1fichier account" in route.last_error


def test_for_clients_picks_up_registered_accounts():
    hosters.register_accounts(lambda: [Account()])
    try:
        route = HosterRoute.for_clients([Direct()])
        assert route._premium and route._expand is not None
    finally:
        hosters.register_accounts(None)


def test_broken_account_source_never_breaks_a_grab():
    def boom():
        raise RuntimeError("disk")
    hosters.register_accounts(boom)
    try:
        assert hosters.premium_accounts() == []
    finally:
        hosters.register_accounts(None)


# -- native clients ------------------------------------------------------------------


def test_auth_url_uses_pkce_and_offline_access():
    verifier, challenge = cloud_native.pkce_pair()
    assert 43 <= len(verifier) <= 128 and "=" not in challenge
    url = cloud_native.auth_url("gdrive", "cid", "http://127.0.0.1:53682/", "st", challenge)
    assert "access_type=offline" in url and "code_challenge_method=S256" in url and "state=st" in url


def test_exchange_error_is_readable_and_secretless():
    s = Session(Resp({"error": "invalid_grant", "error_description": "Bad Request"}, status=400))
    with pytest.raises(cloud_native.NativeError, match="Bad Request"):
        cloud_native.exchange("onedrive", {"client_id": "c", "client_secret": "SECRET"}, "code",
                              "v", "http://127.0.0.1:53682/", session=s)


def test_expired_token_refreshes_and_is_kept():
    kept = []
    s = Session(Resp({"access_token": "NEW", "expires_in": 3600}),
                Resp({"storageQuota": {"limit": "5000", "usage": "1000"}}))
    drive = cloud_native.GoogleDrive({"client_id": "c"}, {"access_token": "OLD", "refresh_token": "R",
                                                          "expires_at": time.time() - 5},
                                     on_token=kept.append, session=s)
    assert drive.about() == {"total": 5000, "used": 1000, "free": 4000}
    assert kept[0]["access_token"] == "NEW" and kept[0]["refresh_token"] == "R"
    assert s.sent[1][2]["headers"]["Authorization"] == "Bearer NEW"


def test_onedrive_lists_pages_and_quota():
    token = {"access_token": "A", "expires_at": time.time() + 999}
    s = Session(Resp({"value": [{"name": "snes", "folder": {}}],
                      "@odata.nextLink": "https://graph.microsoft.com/next"}),
                Resp({"value": [{"name": "a.zip", "size": 7, "file": {}}]}),
                Resp({"quota": {"total": 5368709120, "used": 10, "remaining": 5368709110}}))
    od = cloud_native.OneDrive({"client_id": "c"}, token, session=s)
    rows = od.list("ROMs")
    assert [(r["name"], r["is_dir"], r["path"]) for r in rows] == [
        ("snes", True, "ROMs/snes"), ("a.zip", False, "ROMs/a.zip")]
    assert od.about()["total"] == 5368709120


def test_onedrive_upload_session_gets_no_bearer_token(tmp_path):
    f = tmp_path / "g.zip"
    f.write_bytes(b"x" * 10)
    token = {"access_token": "A", "expires_at": time.time() + 999}
    s = Session(Resp({"id": "x"}, status=200),                      # ROMs/ exists
                Resp({"uploadUrl": "https://upload.example/session"}), Resp({}, status=201))
    cloud_native.OneDrive({"client_id": "c"}, token, session=s).upload(f, "ROMs/g.zip")
    put = s.sent[2]
    assert put[0] == "PUT" and "Authorization" not in put[2]["headers"]
    assert put[2]["headers"]["Content-Range"] == "bytes 0-9/10"


def test_native_paths_refuse_dot_segments():
    od = cloud_native.OneDrive({"client_id": "c"}, {"access_token": "A", "expires_at": time.time() + 99},
                               session=Session())
    with pytest.raises(cloud_native.NativeError):
        od.list("a/../b")



# -- CodeRabbit review fixes ------------------------------------------------------------


class Headed(Resp):
    def __init__(self, status, headers=None, body=None):
        super().__init__(body, status=status, headers=headers or {})


def test_drive_upload_follows_the_acknowledged_range(tmp_path):
    f = tmp_path / "g.bin"
    f.write_bytes(b"x" * (cloud_native.CHUNK + 100))
    s = Session(Headed(308, {"Range": "bytes=0-99"}),           # Drive kept only 100 bytes
                Headed(308, {"Range": f"bytes=0-{cloud_native.CHUNK - 1}"}),
                Headed(200))
    cloud_native._chunked_put(s, "https://up", f, f.stat().st_size, auth=dict, provider="gdrive")
    ranges = [kw["headers"]["Content-Range"] for _, _, kw in s.sent]
    assert ranges[1].startswith("bytes 100-")                  # resent from byte 100


def test_upload_is_not_success_without_the_providers_final_answer(tmp_path):
    f = tmp_path / "g.bin"
    f.write_bytes(b"x" * 10)
    s = Session(Headed(308, {"Range": "bytes=0-9"}))           # never a 200/201
    with pytest.raises(cloud_native.NativeError, match="did not confirm"):
        cloud_native._chunked_put(s, "https://up", f, 10, auth=dict, provider="gdrive")


def test_graph_upload_resumes_after_a_lost_response(tmp_path):
    f = tmp_path / "g.bin"
    f.write_bytes(b"x" * (cloud_native.CHUNK * 2))
    size = f.stat().st_size

    class Flaky(Session):
        def put(self, url, **kw):
            self.sent.append(("PUT", url, kw))
            if len(self.sent) == 1:
                raise cloud_native.requests.ConnectionError("lost")
            return self.answers.pop(0)
    s = Flaky(Resp({"nextExpectedRanges": [f"{cloud_native.CHUNK}-"]}),   # session status
              Resp({}, status=201))
    cloud_native._chunked_put(s, "https://up", f, size, auth=dict, provider="onedrive")
    assert s.sent[-1][2]["headers"]["Content-Range"].startswith(f"bytes {cloud_native.CHUNK}-")


def test_chunks_fit_both_providers():
    assert cloud_native.CHUNK % (256 * 1024) == 0 and cloud_native.CHUNK % (320 * 1024) == 0


def test_drive_refuses_an_ambiguous_name():
    token = {"access_token": "A", "expires_at": time.time() + 99}
    s = Session(Resp({"files": [{"id": "1", "name": "Game.zip"}, {"id": "2", "name": "Game.zip"}]}))
    drive = cloud_native.GoogleDrive({"client_id": "c"}, token, session=s)
    with pytest.raises(cloud_native.NativeError, match="two items"):
        drive._child("root", "Game.zip")


def test_onedrive_creates_missing_parent_folders(tmp_path):
    f = tmp_path / "g.zip"
    f.write_bytes(b"x" * 10)
    token = {"access_token": "A", "expires_at": time.time() + 99}
    s = Session(Resp({}, status=404), Resp({"id": "new"}, status=201),          # ROMs
                Resp({}, status=404), Resp({"id": "new2"}, status=201),         # ROMs/snes
                Resp({"uploadUrl": "https://upload.example/s"}), Resp({}, status=201))
    cloud_native.OneDrive({"client_id": "c"}, token, session=s).upload(f, "ROMs/snes/g.zip")
    creates = [(m, u) for m, u, _ in s.sent if m == "POST" and u.endswith("/children")]
    assert len(creates) == 2


def test_a_file_host_page_is_not_a_direct_file():
    direct = Direct()
    route = HosterRoute(direct=direct)
    assert not route.add("https://rapidgator.net/file/abc/Game.zip")
    assert direct.added == []


def test_queued_premium_jobs_carry_their_source_link():
    class SourceDirect(Direct):
        TAKES_SOURCE = True

        def add(self, url, name="", source=""):
            self.added.append((url, name, source))
            return True
    direct = SourceDirect()
    route = HosterRoute(direct=direct, premium=[Account()])
    assert route.add("https://rapidgator.net/file/abc/x.html", name="G")
    assert direct.added == [("https://dl.rapidgator.net/file", "G", "https://rapidgator.net/file/abc/x.html")]


def test_fresh_link_asks_the_account_again():
    hosters.register_accounts(lambda: [Account()])
    try:
        assert hosters.fresh_link("https://rapidgator.net/file/abc/x.html") == "https://dl.rapidgator.net/file"
        assert hosters.fresh_link("https://example.com/x") == ""
    finally:
        hosters.register_accounts(None)


def test_multiup_keeps_mirrors_that_are_plain_files():
    page = '<a href="https://cdn.example/files/game.zip">direct</a>'
    s = Session(Resp({"error": "success", "hosts": {}}), Resp(text=page))
    out = multiup_mirrors("https://multiup.io/en/mirror/0123456789abcdef0123456789abcdef/g.zip", session=s)
    assert out == ["https://cdn.example/files/game.zip"]
