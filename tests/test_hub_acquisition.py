"""ROM Hub plugins as a search source and download lane (issue #40).

The invariants: a hub release travels the same queue/import pipeline as any
other release; the lane stays silent until both halves exist (an enabled
searchable plugin AND a configured ROM Hub client); and nothing touches the
network in these tests -- the Hub bridge is stubbed at its seams.
"""

import json
from http.server import ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from romarr import hub
from romarr.app import ROMarr, make_handler
from romarr.downloaders import HubConfig, HubDownloader, build_client


# -- the hub:// URL -----------------------------------------------------------

ROW = {"source_id": "ia:metroid", "title": "Super Metroid (USA)",
       "platform": "snes", "size_bytes": 3145728,
       "url": "https://archive.org/details/metroid",
       "extra": {}, "plugin": "archive-org"}


def test_a_result_round_trips_through_a_hub_url():
    encoded = hub.encode_result(ROW)
    assert encoded.startswith("hub://")
    assert hub.decode_result(encoded) == ROW


def test_a_hub_url_carries_no_credential_shape():
    # It is stored in the queue and the ledger; a bare base64 of a row with
    # no secrets is the point of the encoding.
    encoded = hub.encode_result(ROW)
    assert " " not in encoded and "\n" not in encoded


def test_decode_refuses_a_foreign_or_corrupt_url():
    with pytest.raises(ValueError):
        hub.decode_result("https://example.com/rom.zip")
    with pytest.raises(ValueError):
        hub.decode_result("hub://!!!!not-base64!!!!")
    with pytest.raises(ValueError):
        hub.decode_result(hub.encode_result({"title": "no source_id"}))


# -- the download lane --------------------------------------------------------

def test_the_hub_client_is_a_download_client_type():
    client = build_client({"type": "hub", "save_path": "/downloads/hub"})
    assert isinstance(client, HubDownloader)
    assert client.protocol == "hub"
    assert client.configured
    assert build_client({"type": "hub", "save_path": ""}).configured is False


def test_client_api_prevents_duplicate_hub_setup_and_allows_edits(tmp_path, monkeypatch):
    monkeypatch.setattr(hub, "available", lambda: False)
    service = ROMarr({"ROMARR_DATA": str(tmp_path / "state.json"),
                      "ROMARR_API_KEY": "testkey"})
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(service))
    worker = Thread(target=server.serve_forever, daemon=True)
    worker.start()
    url = f"http://127.0.0.1:{server.server_address[1]}/api/v1/downloadclient"

    def call(body=None):
        request = Request(url, headers={"X-Api-Key": "testkey", "Content-Type": "application/json"},
                          data=json.dumps(body).encode() if body is not None else None)
        with urlopen(request, timeout=5) as response:
            return json.load(response)

    try:
        first = call({"type": "hub", "save_path": str(tmp_path / "first")})
        with pytest.raises(HTTPError) as duplicate:
            call({"type": "HUB", "save_path": str(tmp_path / "second")})
        assert duplicate.value.code == 409
        duplicate.value.close()
        revised_path = str(tmp_path / "moved")
        updated = call({**first, "save_path": revised_path})
        assert updated["id"] == first["id"]
        [current] = call()["items"]
        assert current["save_path"] == revised_path
    finally:
        server.shutdown()
        server.server_close()


def test_add_records_a_job_and_refuses_non_hub_urls(tmp_path):
    client = HubDownloader(HubConfig(save_path=str(tmp_path)))
    assert client.add("https://example.com/x.zip") is False
    assert client.add(hub.encode_result(ROW), name="Super Metroid (USA)")
    rows = json.loads((tmp_path / HubDownloader.LEDGER)
                      .read_text(encoding="utf-8"))
    assert rows[0]["state"] == "pending"
    assert rows[0]["name"] == "Super Metroid (USA)"


def test_add_refuses_an_undecodable_hub_url(tmp_path):
    client = HubDownloader(HubConfig(save_path=str(tmp_path)))
    assert client.add("hub://Z29uZQ==") is False


def test_the_sweep_fetches_through_the_plugin_and_reports_the_file(tmp_path):
    fetched = []

    def fake_fetch(row, dest):
        fetched.append((row, dest))
        dest = Path(dest)
        dest.mkdir(parents=True, exist_ok=True)
        f = dest / (row["title"] + ".sfc")
        f.write_bytes(b"\x00" * 16)
        return {"ok": True, "error": "", "files": [str(f)]}

    client = HubDownloader(HubConfig(save_path=str(tmp_path)),
                           fetch=fake_fetch)
    client.add(hub.encode_result(ROW), name="Super Metroid (USA)")
    done = client.completed()
    assert fetched and fetched[0][0] == ROW
    assert done == [{"name": "Super Metroid (USA)",
                     "content_path": str(tmp_path / "Super Metroid (USA).sfc"),
                     "save_path": str(tmp_path),
                     "state": "done"}]
    # The ledger row moved to done and survives a restart.
    rows = json.loads((tmp_path / HubDownloader.LEDGER)
                      .read_text(encoding="utf-8"))
    assert rows[0]["state"] == "done"


def test_a_failed_fetch_is_recorded_with_its_reason(tmp_path):
    def boom(row, dest):
        return {"ok": False, "error": "DownloadError: refused host",
                "files": []}

    client = HubDownloader(HubConfig(save_path=str(tmp_path)), fetch=boom)
    client.add(hub.encode_result(ROW), name="Super Metroid (USA)")
    assert client.completed() == []
    rows = json.loads((tmp_path / HubDownloader.LEDGER)
                      .read_text(encoding="utf-8"))
    assert rows[0]["state"] == "failed"
    assert "refused host" in rows[0]["detail"]


def test_one_fetch_per_sweep(tmp_path):
    calls = []

    def fetch(row, dest):
        calls.append(row["source_id"])
        dest = Path(dest)
        dest.mkdir(parents=True, exist_ok=True)
        f = dest / f"{row['source_id']}.sfc"
        f.write_bytes(b"\x00")
        return {"ok": True, "error": "", "files": [str(f)]}

    client = HubDownloader(HubConfig(save_path=str(tmp_path), max_active=1),
                           fetch=fetch)
    client.add(hub.encode_result(dict(ROW, source_id="a")))
    client.add(hub.encode_result(dict(ROW, source_id="b")))
    client.completed()
    assert calls == ["a"]
    client.completed()
    assert calls == ["a", "b"]


def test_reachable_says_what_is_missing(tmp_path, monkeypatch):
    client = HubDownloader(HubConfig(save_path=str(tmp_path)))
    monkeypatch.setattr(hub, "available", lambda: False)
    assert client.reachable() is False
    assert "not installed" in client.detail

    monkeypatch.setattr(hub, "available", lambda: True)
    monkeypatch.setattr(hub, "searchable_plugins", lambda: [])
    assert client.reachable() is False
    assert "no enabled plugin" in client.detail

    monkeypatch.setattr(hub, "searchable_plugins", lambda: ["archive-org"])
    assert client.reachable() is True
    assert "archive-org" in client.detail


# -- the search lane ----------------------------------------------------------

class _FakePlatform:
    slug = "snes"
    name = "Super Nintendo"
    aliases = ()
    native_markers = ()
    media = "cartridge"


def _service(tmp_path, monkeypatch, rows, clients=()):
    from romarr.app import ROMarr
    svc = ROMarr(env={"ROMARR_DATA": str(tmp_path / "s.json")})
    svc.clients = list(clients)
    monkeypatch.setattr(hub, "searchable_plugins", lambda: ["archive-org"])
    monkeypatch.setattr(hub, "search",
                        lambda q, platform=None, limit=25, timeout=30.0:
                        (rows, ""))
    return svc


def _hub_client(tmp_path):
    return HubDownloader(HubConfig(save_path=str(tmp_path / "hub")))


def test_the_lane_is_quiet_without_both_halves(tmp_path, monkeypatch):
    svc = _service(tmp_path, monkeypatch, [ROW])
    assert svc._hub_enabled() is False
    assert svc._hub_releases("Super Metroid", _FakePlatform()) == []

    # A client with no searchable plugin is still not enough.
    monkeypatch.setattr(hub, "searchable_plugins", lambda: [])
    svc.clients = [_hub_client(tmp_path)]
    assert svc._hub_enabled() is False


def test_hub_results_become_releases_on_the_same_pipeline(tmp_path, monkeypatch):
    svc = _service(tmp_path, monkeypatch, [ROW], [_hub_client(tmp_path)])
    releases = svc._hub_releases("Super Metroid", _FakePlatform())
    assert len(releases) == 1
    r = releases[0]
    assert r.title == "Super Metroid (USA)"
    assert r.protocol == "hub"
    assert r.indexer == "hub:archive-org"
    assert r.size == 3145728
    assert r.download_url.startswith("hub://")
    assert hub.decode_result(r.download_url) == ROW


def test_interactive_search_shows_hub_results(tmp_path, monkeypatch):
    svc = _service(tmp_path, monkeypatch, [ROW], [_hub_client(tmp_path)])
    # The no-platform branch queries Prowlarr AND the hub lane; stub it so
    # the test drives the hub half without a live indexer.
    class _NoProwlarr:
        def search(self, term):
            return []

    monkeypatch.setattr(svc, "prowlarr", _NoProwlarr())
    out = svc.candidates("Super Metroid", "")
    items = out["items"]
    assert any(i["protocol"] == "hub" and i["indexer"] == "hub:archive-org"
               for i in items), items


def test_a_failing_hub_does_not_sink_the_search(tmp_path, monkeypatch):
    from romarr.app import ROMarr
    svc = ROMarr(env={"ROMARR_DATA": str(tmp_path / "s.json")})
    svc.clients = [_hub_client(tmp_path)]
    monkeypatch.setattr(hub, "searchable_plugins", lambda: ["archive-org"])

    def explode(*a, **k):
        raise RuntimeError("hub on fire")

    monkeypatch.setattr(hub, "search", explode)
    assert svc._hub_releases("Super Metroid", _FakePlatform()) == []


def test_repacks_plugin_searches_and_feeds_rss_sync_with_a_torrent_client(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from romarr.app import ROMarr
    from romarr.platforms import resolve

    svc = ROMarr(env={"ROMARR_DATA": str(tmp_path / "s.json")})
    added = []
    torrent_client = SimpleNamespace(
        name="qBittorrent",
        protocol="torrent",
        configured=True,
        add=lambda url: added.append(url) or True,
    )
    svc.clients = [torrent_client]
    monkeypatch.setattr(hub, "searchable_plugins", lambda: ["repacks"])
    repack_row = {
        "source_id": "repack:fitgirl:c1389977986a805eac49bc5ad4608cbeab5cf177",
        "title": "Cyberpunk 2077 (v2.12 + All DLCs, MULTi19) [FitGirl Repack]",
        "platform": "win",
        "size_bytes": 60 * 1024**3,
        "url": "https://fitgirl-repacks.site/cyberpunk-2077/",
        "plugin": "repacks",
        "magnet": "magnet:?xt=urn:btih:c1389977986a805eac49bc5ad4608cbeab5cf177",
        "extra": {"repacker": "FitGirl", "downloadable": "torrent"},
    }
    seen_calls = []

    def fake_search(q, platform=None, **kw):
        seen_calls.append((q, platform, kw.get("only")))
        return [dict(repack_row)], ""

    monkeypatch.setattr(hub, "search", fake_search)
    monkeypatch.setattr(hub, "resolve_torrents", lambda rows: rows)

    # Works without a ROM Hub download client when a torrent client is configured.
    releases = svc._hub_releases("Cyberpunk 2077", resolve("win"))
    assert len(releases) == 1
    assert releases[0].indexer == "hub:repacks"
    assert releases[0].protocol == "torrent"
    assert seen_calls[-1] == ("Cyberpunk 2077", "win", "repacks")

    # rss_sync automatically polls the subscribed FitGirl & DODI RSS feeds for monitored PC titles.
    svc.store.want("Cyberpunk 2077", "win")
    summary = svc.rss_sync()
    assert "1 feed item(s) from 1 source(s), grabbed 1" in summary
    assert added == ["magnet:?xt=urn:btih:c1389977986a805eac49bc5ad4608cbeab5cf177"]


def test_authentication_status_without_shared_rom_hub(monkeypatch):
    import sys
    monkeypatch.setitem(sys.modules, "romarr.ia_auth", None)
    assert hub.archive_org_auth_status() == {
        "configured": False, "external": False,
        "error": "Install the pinned ROM Hub version for Archive.org authentication",
    }


def test_authentication_configure_without_shared_rom_hub(tmp_path, monkeypatch):
    import sys
    from types import SimpleNamespace
    monkeypatch.setattr(hub, "HOME", tmp_path / "rom-hub")
    monkeypatch.setitem(sys.modules, "romarr.ia_auth", None)
    monkeypatch.setattr("rom_hub.registry.Registry.get",
                        lambda self, slug: SimpleNamespace(slug=slug))
    res = hub.archive_org_auth_configure("a@b.com", "pw")
    assert res == {
        "ok": False,
        "error": "Install the pinned ROM Hub version for Archive.org authentication",
    }


def test_partial_hub_search_warning_keeps_healthy_rows(tmp_path, monkeypatch):
    from romarr.app import ROMarr
    svc = ROMarr(env={"ROMARR_DATA": str(tmp_path / "s.json")})
    svc.clients = [_hub_client(tmp_path)]
    monkeypatch.setattr(hub, "searchable_plugins", lambda: ["homebrew", "archive-org"])
    healthy_row = dict(ROW, plugin="homebrew")
    monkeypatch.setattr(hub, "search",
                        lambda q, platform=None: ([healthy_row], "Archive.org authentication: bad key"))
    monkeypatch.setattr(hub, "resolve_torrents", lambda rows: rows)
    releases = svc._hub_releases("Super Metroid", _FakePlatform())
    assert len(releases) == 1
    assert releases[0].indexer == "hub:homebrew"


def test_search_falls_back_when_ia_modules_are_absent(tmp_path, monkeypatch):
    import sys
    from types import SimpleNamespace
    monkeypatch.setattr(hub, "HOME", tmp_path / "rom-hub")
    monkeypatch.setitem(sys.modules, "romarr.ia_auth", None)
    monkeypatch.setitem(sys.modules, "romarr.ia_session", None)
    plugin = SimpleNamespace(slug="homebrew", enabled=True,
                             manifest=SimpleNamespace(capabilities=("search",)))
    monkeypatch.setattr("rom_hub.registry.Registry.installed", lambda self: [plugin])
    monkeypatch.setattr("rom_hub.dispatcher.search_all",
                        lambda group, **kw: SimpleNamespace(
                            statuses=[],
                            results=[SimpleNamespace(model_dump=lambda: dict(ROW, plugin="homebrew"))]))
    # Search plumbing, not confinement: hosts without pyseccomp refuse plugins
    # unless the operator opts in, so opt in explicitly here.
    monkeypatch.setenv("ROMARR_ALLOW_UNSANDBOXED_PLUGINS", "1")
    rows, err = hub.search("Super Metroid")
    assert err == ""
    assert len(rows) == 1 and rows[0]["plugin"] == "homebrew"


def test_clear_authentication_works_after_plugin_uninstall(tmp_path, monkeypatch):
    monkeypatch.setattr(hub, "HOME", tmp_path / "rom-hub")

    def missing(_self, slug):
        raise KeyError(slug)

    monkeypatch.setattr("rom_hub.registry.Registry.get", missing)
    monkeypatch.setattr("romarr.ia_auth.configure",
                        lambda home, email="", password="", clear=False: {"ok": True, "cleared": clear})
    assert hub.archive_org_auth_configure("a@b.com", "pw") == {
        "ok": False,
        "error": "Install the Archive.org plugin first",
    }
    assert hub.archive_org_auth_configure(clear=True) == {"ok": True, "cleared": True}


def test_fetch_uses_the_brokers_operator_grant(monkeypatch, tmp_path):
    """App staging uses the same grant that validated the plugin's plan."""
    from types import SimpleNamespace

    import httpx

    from rom_hub import registry
    from rom_hub.broker.fetcher import HttpxFetcher
    from rom_hub.manifest import parse_manifest

    shop = "http://127.0.0.1:8080"
    monkeypatch.setenv("ROM_HUB_NETWORK_TEST_SHOP", shop)
    manifest = parse_manifest("""
[plugin]
slug = "test-shop"
name = "Fixture Shop"
version = "1"
rpp_version = "1"

[capabilities]
importer = "shop:Import"

[permissions]
network_from_env = true
""")
    (tmp_path / "shop.py").write_text("""
from rom_hub_sdk import FetchFile, FetchPlan, ImportProvider

class Import(ImportProvider):
    def plan(self, result):
        return FetchPlan(platform="nes", files=[
            FetchFile(url=self.ctx.config["shop"] + "/fixture.nes", filename="fixture.nes")
        ])
""", encoding="utf-8")

    plugin = SimpleNamespace(path=tmp_path, manifest=manifest, slug=manifest.slug,
                             config={"shop": shop}, enabled=True)
    monkeypatch.setattr(registry, "Registry", lambda _: SimpleNamespace(get=lambda _: plugin))
    monkeypatch.setattr(hub, "_plugin_secrets", lambda _: {})
    monkeypatch.setattr(hub, "_unsandboxed", lambda: True)
    calls = []

    def serve(request):
        """Serve the fixture through the host's downloader."""
        assert request.extensions.get("rom_hub_bypass_solver") is True
        calls.append(str(request.url))
        return httpx.Response(200, content=b"fixture")

    transport = httpx.MockTransport(serve)
    monkeypatch.setattr(hub, "_build_fetcher", lambda *args, **kwargs: HttpxFetcher(transport=transport))
    monkeypatch.setattr(hub, "_wrap_download_transport", lambda _: transport)
    row = dict(ROW, plugin=manifest.slug, title="Fixture", platform="nes")
    result = hub.fetch(row, tmp_path / "out")

    assert result["ok"], result
    assert calls == [shop + "/fixture.nes"]
    assert Path(result["files"][0]).read_bytes() == b"fixture"
