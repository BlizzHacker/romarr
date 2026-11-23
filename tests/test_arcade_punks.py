"""Arcade Punks: links, pack names, the zipped NZB, and which client gets what.

Arcade Punks offers each release four ways -- a zipped NZB and a .torrent on
arcade-punks.com, plus Mega and Rapidgator mirrors. Shapes below are copied
from a real post (TeknoBat LightGun, Nov 2026): a 10 MB zip holding one 40 MB
NZB posted to alt.binaries.cores as tb.lg.rEAP.partNNN.rar with PAR2 files.
No test touches the network; every HTTP call is a fake.
"""

from __future__ import annotations

import base64
import io
import json
import zipfile
from pathlib import Path

import pytest

from romarr import arcade_punks as ap
from romarr import hub
from romarr.downloaders import (
    NZBGet,
    NzbgetConfig,
    SabConfig,
    SABnzbd,
)

NZB_LINK = "https://arcade-punks.com/arcadepunks/2025-01/nzb/tb.lg.rEAP.zip"
TORRENT_LINK = "https://arcade-punks.com/arcadepunks/2025-01/torrent/TP-LG.torrent"
MEGA_LINK = "https://mega.nz/file/D9IGXTiR#vAZ49ymMZYW2JQ7eonDTBOULwXPJpTYiIrSEmGiwbYY"
RG_LINK = "https://rapidgator.net/folder/8993694/TBLG.html"

NZB = b"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE nzb PUBLIC "-//newzBin//DTD NZB 1.1//EN" "http://www.newzbin.com/DTD/nzb/nzb-1.1.dtd">
<nzb xmlns="http://www.newzbin.com/DTD/2003/nzb">
  <file poster="x@ngPost.com" date="1795026080" subject="[001/002] - &quot;tb.lg.rEAP.par2&quot; yEnc (1/1) 131992">
    <groups><group>alt.binaries.cores</group></groups>
    <segments><segment bytes="131992" number="1">a1@ngPost</segment></segments>
  </file>
  <file poster="x@ngPost.com" date="1795026153" subject="[002/002] - &quot;tb.lg.rEAP.part001.rar&quot; yEnc (1/2) 1433600">
    <groups><group>alt.binaries.cores</group></groups>
    <segments>
      <segment bytes="716800" number="1">b1@ngPost</segment>
      <segment bytes="716800" number="2">b2@ngPost</segment>
    </segments>
  </file>
</nzb>
"""


def _zip(members: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in members.items():
            z.writestr(name, data)
    return buf.getvalue()


class FakeResponse:
    def __init__(self, body: bytes = b"", status: int = 200, payload=None, headers=None):
        self.body = body
        self.status_code = status
        self.payload = payload
        self.headers = headers or {}
        self.text = json.dumps(payload) if payload is not None else body.decode("latin-1")
        self.closed = False

    def iter_content(self, size):
        for i in range(0, len(self.body), size):
            yield self.body[i:i + size]

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            raise requests.HTTPError(str(self.status_code))

    def json(self):
        return self.payload

    def close(self):
        self.closed = True


class FakeSession:
    """Answers GETs from a table and records every request."""

    def __init__(self, gets=None, posts=None):
        self.gets = gets or {}
        self.posts = posts or []
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        return self.gets[url]

    def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        return self.posts.pop(0)


# --- links ------------------------------------------------------------------

@pytest.mark.parametrize("url,kind,name", [
    (NZB_LINK, "nzb", "tb.lg.rEAP"),
    ("https://arcade-punks.com/arcadepunks/2026-11/nzb/Some.Pack.rEAP.nzb", "nzb", "Some.Pack.rEAP"),
    (TORRENT_LINK, "torrent", "TP-LG"),
    (MEGA_LINK, "mega", "mega-D9IGXTiR"),
    ("https://mega.nz/folder/AbCdEf12#0123456789abcdef", "mega", "mega-AbCdEf12"),
    (RG_LINK, "rapidgator", "TBLG"),
    ("https://rapidgator.net/file/abc123/tb.lg.rEAP.part001.rar.html", "rapidgator",
     "tb.lg.rEAP.part001.rar"),
])
def test_the_four_link_kinds(url, kind, name):
    link = ap.classify(url)
    assert (link.kind, link.name, link.url) == (kind, name, url)


@pytest.mark.parametrize("url", [
    "",
    "http://arcade-punks.com/arcadepunks/2025-01/nzb/tb.lg.rEAP.zip",
    "https://evil.example/arcadepunks/2025-01/nzb/tb.lg.rEAP.zip",
    "https://arcade-punks.com.evil.example/arcadepunks/2025-01/nzb/x.zip",
    "https://user:pw@arcade-punks.com/arcadepunks/2025-01/nzb/x.zip",
    "https://arcade-punks.com:8443/arcadepunks/2025-01/nzb/x.zip",
    "https://arcade-punks.com/arcadepunks/2025-01/nzb/x.exe",
    "https://arcade-punks.com/arcadepunks/2025-01/torrent/x.zip",
    "https://arcade-punks.com/arcadepunks/2025-01/nzb/..%2F..%2Fetc.zip",
    "https://arcade-punks.com/arcadepunks/2025-01/nzb/x.zip?token=1",
    "https://arcade-punks.com/wp-admin/x.zip",
    "https://mega.nz/file/D9IGXTiR",
    "https://mega.nz/#!D9IGXTiR!key",
    "https://rapidgator.net/download/captcha",
    "magnet:?xt=urn:btih:" + "a" * 40,
])
def test_links_that_are_refused(url):
    with pytest.raises(ap.LinkError):
        ap.classify(url)


# --- pack names -------------------------------------------------------------

@pytest.mark.parametrize("title,kind,frontend,device,capacity,platform,game", [
    ("TeknoBat LightGun: Every TeknoParrot Light Gun Game in One RetroBat Build",
     "build", "TeknoParrot", "", 0, "", ""),
    ("Hondacera v42 – 256GB Batocera Build with over 70,000 ROMs",
     "build", "Batocera", "", 256 * 1000 ** 3, "", ""),
    ("20TB RetroBat Cloud Build – 129+ Systems & 54,439+ Games!",
     "build", "RetroBat", "", 20 * 1000 ** 4, "", ""),
    ("CoinOps ATARASHII Forgotten Worlds for The Raspberry Pi 5",
     "build", "CoinOps", "Raspberry Pi", 0, "", ""),
    ("Anbernic RG35XX SP KNULLI 512GB from BooBooMan",
     "build", "Knulli", "Anbernic", 512 * 1000 ** 3, "", ""),
    ("The Rumble Fish 2 is now available for the Sega Dreamcast- Atomiswave conversion from YZB",
     "game", "Atomiswave conversion", "", 0, "dreamcast", "The Rumble Fish 2"),
    ("Savage Quest Teknoparrot Dump Found Online!",
     "game", "TeknoParrot", "", 0, "", "Savage Quest"),
    ("Hurstys 4:3 Video Media Pack Systems and Collections",
     "media", "", "", 0, "", ""),
    ("Pinball Makers Once Built the Machines of the Casino Floor",
     "other", "", "", 0, "", ""),
])
def test_pack_names(title, kind, frontend, device, capacity, platform, game):
    pack = ap.parse_pack(title)
    assert (pack.kind, pack.frontend, pack.device, pack.capacity_bytes,
            pack.platform, pack.game) == (kind, frontend, device, capacity, platform, game)


def test_word_boundaries_not_substrings():
    # "Pinball" is not a Raspberry Pi; "Baseball" is not a base build.
    assert ap.parse_pack("Pinball Wizard").device == ""
    assert ap.parse_pack("Baseball Stars").kind == "other"


def test_release_tag_and_version():
    pack = ap.parse_pack("tb.lg.rEAP")
    assert pack.tagged
    assert ap.parse_pack("Hondacera v42 Batocera").version == "42"


def test_the_atomiswave_platform_resolves_in_romarr():
    from romarr.platforms import resolve
    assert resolve(ap.parse_pack(
        "Dolphin Blue just released for the Sega Dreamcast (Atomiswave conversion)").platform)


# --- the zipped NZB ---------------------------------------------------------

def _nzb_session(body: bytes, status=200, url=NZB_LINK, headers=None):
    return FakeSession(gets={url: FakeResponse(body, status, headers=headers)})


def test_fetch_unzips_and_validates_the_nzb():
    session = _nzb_session(_zip({"tb.lg.rEAP.nzb": NZB}))
    doc = ap.fetch_nzb(ap.classify(NZB_LINK), session)
    assert doc.name == "tb.lg.rEAP"
    assert doc.data == NZB
    assert doc.files == 2
    assert doc.groups == ("alt.binaries.cores",)
    assert doc.size_bytes == 131992 + 2 * 716800
    _, url, kwargs = session.calls[0]
    assert url == NZB_LINK
    assert kwargs["allow_redirects"] is False and kwargs["stream"] is True


def test_a_bare_nzb_link_is_validated_too():
    url = "https://arcade-punks.com/arcadepunks/2026-11/nzb/tb.lg.rEAP.nzb"
    doc = ap.fetch_nzb(ap.classify(url), _nzb_session(NZB, url=url))
    assert doc.files == 2


@pytest.mark.parametrize("members,why", [
    ({}, "holds 0 NZB"),
    ({"a.nzb": NZB, "b.nzb": NZB}, "holds 2 NZB"),
    ({"!!.nzb": NZB}, "unusable name"),
    ({"x.nzb": b"<html>not an nzb</html>"}, "not a usable NZB"),
    ({"x.nzb": b'<?xml version="1.0"?><!DOCTYPE nzb [<!ENTITY a "aaaa">]><nzb>&a;</nzb>'},
     "not a usable NZB"),
])
def test_bad_zips_are_refused(members, why):
    with pytest.raises(ap.FetchError, match=why):
        ap.fetch_nzb(ap.classify(NZB_LINK), _nzb_session(_zip(members)))


def test_a_path_in_the_zip_is_only_a_label():
    # Nothing is written to disk; the member's basename names the job.
    doc = ap.fetch_nzb(ap.classify(NZB_LINK), _nzb_session(_zip({"../dir/tb.lg.rEAP.nzb": NZB})))
    assert doc.name == "tb.lg.rEAP"


def test_not_a_zip_redirect_and_errors_are_refused():
    link = ap.classify(NZB_LINK)
    with pytest.raises(ap.FetchError, match="not return a zip"):
        ap.fetch_nzb(link, _nzb_session(b"<html>login</html>"))
    with pytest.raises(ap.FetchError, match="redirected"):
        ap.fetch_nzb(link, _nzb_session(b"", status=302))
    with pytest.raises(ap.FetchError, match="HTTP 404"):
        ap.fetch_nzb(link, _nzb_session(b"", status=404))


def test_size_limits(monkeypatch):
    link = ap.classify(NZB_LINK)
    with pytest.raises(ap.FetchError, match="limit"):
        ap.fetch_nzb(link, _nzb_session(b"x", headers={"Content-Length": str(ap.MAX_ZIP_BYTES + 1)}))
    monkeypatch.setattr(ap, "MAX_ZIP_BYTES", 10)
    with pytest.raises(ap.FetchError, match="limit"):
        ap.fetch_nzb(link, _nzb_session(_zip({"x.nzb": NZB})))
    monkeypatch.setattr(ap, "MAX_ZIP_BYTES", 64 * 1024 * 1024)
    monkeypatch.setattr(ap, "MAX_NZB_BYTES", 100)
    with pytest.raises(ap.FetchError, match="larger than"):
        ap.fetch_nzb(link, _nzb_session(_zip({"x.nzb": NZB})))


def test_fetch_torrent_checks_the_payload():
    link = ap.classify(TORRENT_LINK)
    good = b"d8:announce3:udp4:infod4:name5:TP-LGee"
    assert ap.fetch_torrent(link, FakeSession(gets={TORRENT_LINK: FakeResponse(good)})) == good
    with pytest.raises(ap.FetchError):
        ap.fetch_torrent(link, FakeSession(gets={TORRENT_LINK: FakeResponse(b"<html>")}))


# --- clients ----------------------------------------------------------------

def test_nzbget_add_nzb_sends_base64_content():
    session = FakeSession(posts=[FakeResponse(payload={"result": 7})])
    client = NZBGet(NzbgetConfig(base_url="http://nzbget:6789", category="romarr"), session)
    assert client.add_nzb(NZB, name="tb.lg.rEAP") is True
    _, url, kwargs = session.calls[0]
    assert url == "http://nzbget:6789/jsonrpc"
    body = kwargs["json"]
    assert body["method"] == "append"
    filename, content, category = body["params"][:3]
    assert filename == "tb.lg.rEAP.nzb"
    assert base64.b64decode(content) == NZB
    assert category == "romarr"


def test_nzbget_add_by_url_is_unchanged():
    session = FakeSession(posts=[FakeResponse(payload={"result": 8})])
    client = NZBGet(NzbgetConfig(base_url="http://nzbget:6789"), session)
    assert client.add("https://indexer.example/get/1", name="X")
    assert session.calls[0][2]["json"]["params"][1] == "https://indexer.example/get/1"


def test_sabnzbd_add_nzb_uploads_the_file():
    session = FakeSession(posts=[FakeResponse(payload={"status": True, "nzo_ids": ["SAB_1"]})])
    client = SABnzbd(SabConfig(base_url="http://sab:8080", api_key="k", category="romarr"), session)
    assert client.add_nzb(NZB, name="tb.lg.rEAP") is True
    assert client.last_job_id == "SAB_1"
    _, url, kwargs = session.calls[0]
    assert url == "http://sab:8080/api"
    assert kwargs["params"]["mode"] == "addfile"
    assert kwargs["params"]["cat"] == "romarr"
    assert kwargs["params"]["nzbname"] == "tb.lg.rEAP"
    filename, data, _ = kwargs["files"]["name"]
    assert (filename, data) == ("tb.lg.rEAP.nzb", NZB)


def test_sabnzbd_refusal_is_false():
    session = FakeSession(posts=[FakeResponse(payload={"status": False})])
    client = SABnzbd(SabConfig(base_url="http://sab:8080", api_key="k"), session)
    assert client.add_nzb(NZB, name="x") is False


def test_builtin_usenet_client_takes_the_document(tmp_path):
    from romarr.nntp import BuiltinNzbClient
    client = BuiltinNzbClient.__new__(BuiltinNzbClient)
    seen = {}
    client.add = lambda source, name="": seen.update(source=source, name=name) or True
    assert BuiltinNzbClient.add_nzb(client, NZB, name="tb.lg.rEAP")
    assert seen["source"].lstrip().startswith("<?xml") and seen["name"] == "tb.lg.rEAP"
















# --- the lane ---------------------------------------------------------------

class FakeUsenet:
    protocol = "usenet"
    name = "NZBGet"
    romarr_id = "nzbget-1"
    configured = True

    def __init__(self, ok=True):
        self.ok = ok
        self.got = []
        self.last_job_id = ""

    def add_nzb(self, data, *, name=""):
        self.got.append((data, name))
        self.last_job_id = "17" if self.ok else ""
        return self.ok


class FakeTorrent:
    protocol = "torrent"
    name = "qBittorrent"
    romarr_id = "qbit-1"
    configured = True
    TAKES_NAME = True
    last_job_id = "romarr-abc"

    def __init__(self):
        self.got = []

    def add(self, url, *, name=""):
        self.got.append((url, name))
        return True


class FakeDebrid:
    protocol = "torrent"
    name = "Real-Debrid"
    romarr_id = "rd-1"
    configured = True
    magnet_only = True
    takes_hoster_links = True
    TAKES_NAME = True

    def __init__(self):
        self.links = []
        self.files = []

    def add(self, url, *, name=""):
        self.links.append((url, name))
        return True

    def add_torrent_file(self, data):
        self.files.append(data)
        return True


def test_each_kind_goes_to_its_client():
    usenet, torrent, debrid = FakeUsenet(), FakeTorrent(), FakeDebrid()
    lane = ap.ArcadePunksLane([usenet, torrent, debrid],
                              session=_nzb_session(_zip({"tb.lg.rEAP.nzb": NZB})))
    assert lane.routes() == {"nzb": "NZBGet", "torrent": "qBittorrent",
                             "mega": "Real-Debrid", "rapidgator": "Real-Debrid"}

    assert lane.add(NZB_LINK, name="tb.lg.rEAP")
    assert usenet.got == [(NZB, "tb.lg.rEAP")]
    assert (lane.last_job_id, lane.romarr_id, lane.delegate_name) == ("17", "nzbget-1", "NZBGet")

    assert lane.add(TORRENT_LINK, name="TP-LG")
    assert torrent.got == [(TORRENT_LINK, "TP-LG")]
    assert lane.romarr_id == "qbit-1"

    assert lane.add(MEGA_LINK, name="TBLG") and lane.add(RG_LINK, name="TBLG")
    assert debrid.links == [(MEGA_LINK, "TBLG"), (RG_LINK, "TBLG")]


def test_a_magnet_only_torrent_client_gets_the_file():
    debrid = FakeDebrid()
    good = b"d8:announce3:udp4:infod4:name5:TP-LGee"
    lane = ap.ArcadePunksLane([debrid], session=FakeSession(gets={TORRENT_LINK: FakeResponse(good)}))
    assert lane.add(TORRENT_LINK, name="TP-LG")
    assert debrid.files == [good]


def test_missing_clients_and_bad_links_explain_themselves():
    lane = ap.ArcadePunksLane([FakeTorrent()])
    assert not lane.add(NZB_LINK)
    assert "Usenet client" in lane.last_error
    assert not lane.add(MEGA_LINK)
    assert "Real-Debrid or AllDebrid" in lane.last_error
    assert not lane.add("https://evil.example/x.zip")
    assert "not an Arcade Punks" in lane.last_error


def test_a_fetch_failure_reaches_the_operator():
    lane = ap.ArcadePunksLane([FakeUsenet()], session=_nzb_session(b"", status=403))
    assert not lane.add(NZB_LINK)
    assert "HTTP 403" in lane.last_error


def test_setup_hints_follow_the_guides_and_hold_no_secret():
    hints = ap.setup_hints({"NZBGET_CATEGORY": "games", "NZBGET_PASS": "hunter2"})
    text = json.dumps(hints)
    assert hints["provider"]["port"] == 563 and hints["provider"]["encryption"] is True
    assert hints["newsgroups"] == ["alt.binaries.cores"]
    assert "'games'" in text
    assert "hunter2" not in text
    assert "a_aid" not in text  # no affiliate links


# --- ROMarr: grab, search, status ---------------------------------------------

def _service(tmp_path, clients):
    from romarr.app import ROMarr
    svc = ROMarr(env={"ROMARR_DATA": str(tmp_path / "s.json")})
    svc.clients = list(clients)
    return svc


def test_a_conversion_is_queued_against_its_client(tmp_path, monkeypatch):
    usenet = FakeUsenet()
    svc = _service(tmp_path, [usenet])
    monkeypatch.setattr(ap.requests, "Session",
                        lambda: _nzb_session(_zip({"tb.lg.rEAP.nzb": NZB})))
    out = svc.grab_arcade_punks(NZB_LINK, game="The Rumble Fish 2", platform_slug="dreamcast")
    assert out["ok"] and out["queued"] and out["client"] == "NZBGet"
    row = svc.queue[-1]
    assert (row.game, row.platform, row.release, row.state) == (
        "The Rumble Fish 2", "dc", "tb.lg.rEAP", "grabbed")
    assert (row.download_id, row.download_client) == ("17", "nzbget-1")


def test_a_build_is_downloaded_but_not_queued(tmp_path):
    torrent = FakeTorrent()
    svc = _service(tmp_path, [torrent])
    out = svc.grab_arcade_punks(TORRENT_LINK,
                                game="TeknoBat LightGun: Every TeknoParrot Light Gun Game in One RetroBat Build")
    assert out["ok"] and out["queued"] is False
    assert out["pack"]["kind"] == "build"
    assert torrent.got == [(TORRENT_LINK, "TP-LG")]
    assert svc.queue == []


def test_grab_refusals(tmp_path):
    svc = _service(tmp_path, [])
    assert "Usenet client" in svc.grab_arcade_punks(NZB_LINK)["error"]
    assert not svc.grab_arcade_punks("https://evil.example/a.zip")["ok"]
    svc = _service(tmp_path, [FakeTorrent()])
    assert "unknown platform" in svc.grab_arcade_punks(TORRENT_LINK, platform_slug="nope")["error"]


def test_a_refused_hand_off_reports_the_client_reason(tmp_path, monkeypatch):
    svc = _service(tmp_path, [FakeUsenet(ok=False)])
    monkeypatch.setattr(ap.requests, "Session",
                        lambda: _nzb_session(_zip({"tb.lg.rEAP.nzb": NZB})))
    out = svc.grab_arcade_punks(NZB_LINK, platform_slug="dreamcast")
    assert not out["ok"]
    assert "NZBGet" in out["error"]
    assert svc.queue[-1].state == "failed"


def test_browser_only_plugin_rows_never_become_releases(tmp_path, monkeypatch):
    from romarr.downloaders import HubConfig, HubDownloader
    rows = [{"source_id": "207272", "title": "TeknoBat LightGun", "plugin": "arcade-punks",
             "url": "https://www.arcadepunks.com/teknobat/",
             "extra": {"downloadable": "browser", "login_required": "true"}},
            {"source_id": "x", "title": "Super Metroid (USA)", "plugin": "archive-org",
             "extra": {}}]
    svc = _service(tmp_path, [HubDownloader(HubConfig(save_path=str(tmp_path / "hub")))])
    monkeypatch.setattr(hub, "searchable_plugins", lambda: ["arcade-punks", "archive-org"])
    monkeypatch.setattr(hub, "search", lambda q, platform=None, limit=25, timeout=30.0: (rows, ""))
    titles = [r.title for r in svc._hub_releases("Super Metroid", None)]
    assert titles == ["Super Metroid (USA)"]


def test_search_asks_only_the_arcade_punks_plugin(monkeypatch):
    seen = {}

    def fake_search(q, platform=None, limit=25, timeout=90.0, only=None):
        seen.update(q=q, limit=limit, only=only)
        return ([{"title": "Hondacera v42 – 256GB Batocera Build", "plugin": ap.SLUG,
                  "url": "https://www.arcadepunks.com/hondacera/", "extra": {"kind": "build"}},
                 {"title": "elsewhere", "plugin": "other"}], "")

    monkeypatch.setattr(hub, "search", fake_search)
    out = ap.search("batocera", limit=500)
    assert seen == {"q": "batocera", "limit": 50, "only": (ap.SLUG,)}
    assert [i["title"] for i in out["items"]] == ["Hondacera v42 – 256GB Batocera Build"]
    assert out["items"][0]["pack"]["capacity_bytes"] == 256 * 1000 ** 3
    assert ap.search("  ") == {"items": [], "error": ""}


def test_hub_search_only_filters_plugins(monkeypatch):
    rom_hub = pytest.importorskip("rom_hub")
    del rom_hub

    class P:
        def __init__(self, slug):
            self.slug, self.enabled = slug, True
            self.manifest = type("M", (), {"capabilities": {"search": "x"}})()

    class Reg:
        def __init__(self, home):
            pass

        def installed(self):
            return [P("arcade-punks"), P("archive-org")]

    seen = {}

    def fake_search_all(group, **kwargs):
        seen["slugs"] = [p.slug for p in group]
        return type("O", (), {"results": [], "statuses": []})()

    from rom_hub import dispatcher, registry
    monkeypatch.setattr(registry, "Registry", Reg)
    monkeypatch.setattr(dispatcher, "search_all", fake_search_all)
    hub.search("x", only=("arcade-punks",))
    assert seen["slugs"] == ["arcade-punks"]


def test_status_is_secret_free(tmp_path, monkeypatch):
    monkeypatch.setattr(ap, "plugin_enabled", lambda home=None: True)
    status = ap.status({"NZBGET_PASS": "hunter2"}, [FakeUsenet()])
    assert status["plugin_enabled"] is True
    assert status["routes"]["nzb"] == "NZBGet"
    assert "hunter2" not in json.dumps(status)


def test_preinstall_uses_the_shared_marker(tmp_path, monkeypatch):
    from romarr import preinstalled_plugins as pp
    seen = {}

    def fake(slug, repo, ref, **kwargs):
        seen.update(slug=slug, repo=repo, ref=ref, **kwargs)
        return "installed"

    monkeypatch.setattr(pp, "preinstall", fake)
    assert ap.preinstall(home=tmp_path, env={}) == "installed"
    assert (seen["slug"], seen["repo"], seen["ref"]) == (ap.SLUG, ap.PLUGIN_REPO, ap.PLUGIN_REF)


def test_a_rapidgator_account_unlocks_rapidgator_links(monkeypatch):
    from romarr import hosters

    class Direct:
        protocol, name, configured, TAKES_NAME = "direct", "Direct HTTP", True, True

        def __init__(self):
            self.got = []

        def add(self, url, *, name=""):
            self.got.append((url, name))
            return True

    class Account:
        label, configured = "Rapidgator", True

        def supports_host(self, host):
            return host in ("rapidgator.net", "rg.to")

        def resolve(self, url):
            return "https://dl.rapidgator.net/file.rar"

    hosters.register_accounts(lambda: [Account()])
    try:
        direct = Direct()
        lane = ap.ArcadePunksLane([direct])
        assert lane.routes()["rapidgator"] == "Rapidgator account"
        assert lane.add(RG_LINK, name="TBLG")
        assert direct.got == [("https://dl.rapidgator.net/file.rar", "TBLG")]
    finally:
        hosters.register_accounts(None)


def test_dockerfile_bundles_the_tagged_plugin():
    text = (Path(__file__).resolve().parents[1] / "Dockerfile").read_text()
    assert f"ARG ARCADE_PUNKS_PLUGIN_REF={ap.PLUGIN_REF}" in text
    assert ap.PLUGIN_REPO in text and "/opt/romarr/bundled-plugins/arcade-punks" in text
