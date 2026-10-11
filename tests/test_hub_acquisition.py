"""ROM Hub plugins as a search source and download lane (issue #40).

The invariants: a hub release travels the same queue/import pipeline as any
other release; the lane stays silent until both halves exist (an enabled
searchable plugin AND a configured ROM Hub client); and nothing touches the
network in these tests -- the Hub bridge is stubbed at its seams.
"""

import json
from pathlib import Path

import pytest

from romarr import hub
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
