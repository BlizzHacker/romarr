"""MiNERVA rides the ROM Hub plugin layer, not ROMarr's core.

The hardcoded `minerva` indexer driver this replaces was the wrong shape:
every new source welded into indexers.py is one more driver, one more
registry entry, one more set of fields in Settings -- fifteen sources from
fifteen people, none of them sandboxed, all of them maintained here. The ROM
Hub plugin protocol exists precisely so a source is a manifest and a
capability instead.

These tests pin the general half of that in ROMarr: a plugin result whose
payload is a *torrent* gets resolved through the plugin's own `torrent`
capability -- host-gated, tracker-checked -- and handed to the operator's
torrent client like any magnet. Nothing here knows the word MiNERVA.
"""

import base64
import json
import sys
import types

import pytest

from romarr import hub


# resolve_torrents imports rom_hub.* lazily; CI images have the SDK, this
# workstation does not. A minimal stand-in module tree lets the tests pin
# ROMarr's side of the contract either way -- the real SDK behavior is
# proven by the plugin's own tests (rom-hub-minerva) and live runs.
def _ensure_fake_sdk():
    try:
        import rom_hub  # noqa: F401
        return                      # the real SDK is here; use it
    except ImportError:
        pass
    root = types.ModuleType("rom_hub")
    registry = types.ModuleType("rom_hub.registry")
    registry.Registry = object          # replaced per-test
    broker = types.ModuleType("rom_hub.broker")
    host = types.ModuleType("rom_hub.broker.host")
    host.PluginProcess = object         # replaced per-test
    fetcher = types.ModuleType("rom_hub.broker.fetcher")
    fetcher.HttpxFetcher = object       # replaced per-test
    types_mod = types.ModuleType("rom_hub.types")

    class SearchResult:
        def __init__(self, **kw):
            self.__dict__.update(kw)

    types_mod.SearchResult = SearchResult
    root.registry = registry
    root.broker = broker
    broker.host = host
    broker.fetcher = fetcher
    root.types = types_mod
    sys.modules["rom_hub"] = root
    sys.modules["rom_hub.registry"] = registry
    sys.modules["rom_hub.broker"] = broker
    sys.modules["rom_hub.broker.host"] = host
    sys.modules["rom_hub.broker.fetcher"] = fetcher
    sys.modules["rom_hub.types"] = types_mod


_ensure_fake_sdk()


def _row(plugin="minerva-archive", **kw):
    row = {"source_id": "magnet:?xt=urn:btih:"
                        "c5c78ab0432dcc14fec568ba5daad2d9cc98f4dc"
                        "&dn=Minerva_Myrient",
           "title": "Chrono Trigger (1995)(Square)(US).zip",
           "platform": None, "size_bytes": None,
           "url": "https://minerva-archive.org/rom?id=823208",
           "extra": {}, "plugin": plugin}
    row.update(kw)
    return row


class _FakeSource:
    def __init__(self, source, info_hash):
        self.kind = "magnet"
        self.source = source
        self.info_hash = info_hash


class _FakeProc:
    """Stands in for PluginProcess: records rows, answers with magnets."""

    resolved = []
    answer = None          # _FakeSource, or Exception to raise
    calls = 0

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def resolve_torrent(self, result):
        _FakeProc.calls += 1
        if isinstance(_FakeProc.answer, Exception):
            raise _FakeProc.answer
        return _FakeProc.answer


@pytest.fixture(autouse=True)
def _reset():
    _FakeProc.resolved = []
    _FakeProc.calls = 0
    _FakeProc.answer = None
    yield


def _wire(monkeypatch, declares_torrent=True, proc=None):
    """Point hub.resolve_torrents at fakes: registry + PluginProcess."""
    class _Manifest:
        capabilities = {"torrent": "x.y:Z"} if declares_torrent else {}
        network = ["minerva-archive.org"]

    class _Plugin:
        slug = "minerva-archive"
        path = "/fake"
        manifest = _Manifest()
        enabled = True
        config = {}

    class _Reg:
        def __init__(self, home):
            pass

        def get(self, slug):
            if slug != "minerva-archive":
                raise KeyError(slug)
            return _Plugin()

    import rom_hub.registry as registry_mod
    monkeypatch.setattr(registry_mod, "Registry", _Reg)
    import rom_hub.broker.host as host_mod
    monkeypatch.setattr(host_mod, "PluginProcess",
                        lambda **kw: proc or _FakeProc())
    import rom_hub.broker.fetcher as fetcher_mod
    monkeypatch.setattr(fetcher_mod, "HttpxFetcher",
                        lambda **kw: _NoopFetcher())


class _NoopFetcher:
    def close(self):
        pass


# -- resolve_torrents -----------------------------------------------------------

def test_a_magnet_result_is_resolved_through_its_plugin(monkeypatch):
    _wire(monkeypatch)
    _FakeProc.answer = _FakeSource(
        "magnet:?xt=urn:btih:c5c78ab0432dcc14fec568ba5daad2d9cc98f4dc"
        "&tr=udp://tracker.opentrackr.org:1337/announce",
        "c5c78ab0432dcc14fec568ba5daad2d9cc98f4dc")

    rows = [_row()]
    out = hub.resolve_torrents(rows)

    assert out[0]["magnet"].startswith("magnet:?xt=urn:btih:c5c78ab0")
    assert "tr=" in out[0]["magnet"], "the plugin's tracker-augmented magnet"
    assert _FakeProc.calls == 1


def test_a_plugin_without_the_torrent_capability_is_left_alone(monkeypatch):
    _wire(monkeypatch, declares_torrent=False)
    rows = [_row(plugin="archive-org")]
    out = hub.resolve_torrents(rows)
    assert "magnet" not in out[0]
    assert _FakeProc.calls == 0


def test_a_non_magnet_source_id_is_not_resolved(monkeypatch):
    # archive.org identifiers, item ids, https pages: only magnets ride this
    # path. Anything else keeps going through hub.fetch's FetchPlan lane.
    _wire(monkeypatch)
    rows = [_row(source_id="MINERVA3")]
    out = hub.resolve_torrents(rows)
    assert "magnet" not in out[0]
    assert _FakeProc.calls == 0


def test_a_refusal_falls_back_to_the_raw_magnet(monkeypatch):
    # The search row already carries a working magnet; a resolve failure
    # degrades to DHT-only rather than losing the result entirely.
    _wire(monkeypatch)
    _FakeProc.answer = RuntimeError("plugin refused")
    raw = _row()["source_id"]
    out = hub.resolve_torrents([_row()])
    assert out[0]["magnet"] == raw


def test_one_process_serves_every_row_of_its_plugin(monkeypatch):
    # Spawning a sandboxed subprocess per result would be absurd; rows are
    # grouped by plugin and resolved in one process.
    _wire(monkeypatch)
    _FakeProc.answer = _FakeSource("magnet:?xt=urn:btih:aa", "aa")
    rows = [_row(source_id=f"magnet:?xt=urn:btih:{i:040x}")
            for i in range(5)]
    out = hub.resolve_torrents(rows)
    assert len(out) == 5
    assert all("magnet" in r for r in out)
    assert _FakeProc.calls == 5


def test_a_broken_registry_never_raises_out(monkeypatch):
    _wire(monkeypatch)

    class _Boom:
        def __init__(self, home):
            raise RuntimeError("registry on fire")

    import rom_hub.registry as registry_mod
    monkeypatch.setattr(registry_mod, "Registry", _Boom)
    out = hub.resolve_torrents([_row()])
    assert out and "magnet" not in out[0], (
        "the row survives, unresolved, and the hub:// lane still has it")


# -- the release shape ROMarr builds from a resolved row -------------------------

def test_a_resolved_magnet_row_becomes_a_torrent_release(tmp_path, monkeypatch):
    """The end-to-end contract at the app layer: a hub row carrying a
    resolved magnet becomes a Release with protocol="torrent" and the magnet
    as its download_url -- so pick_client hands it to qBittorrent like any
    other magnet, with no hub:// fetch lane involved."""
    from romarr.app import ROMarr
    from romarr.selection import Release

    svc = ROMarr(env={"ROMARR_DATA": str(tmp_path / "s.json")})
    row = _row()
    row["magnet"] = ("magnet:?xt=urn:btih:c5c78ab0432dcc14fec568ba5daad2d9cc98f4dc"
                     "&tr=udp://tracker.opentrackr.org:1337/announce")
    monkeypatch.setattr(svc, "_hub_enabled", lambda: True)
    monkeypatch.setattr(hub, "search", lambda *a, **k: ([row], ""))
    monkeypatch.setattr(hub, "resolve_torrents", lambda rows: rows)

    out = svc._hub_releases("chrono trigger", None)
    assert len(out) == 1
    rel = out[0]
    assert isinstance(rel, Release)
    assert rel.protocol == "torrent"
    assert rel.download_url.startswith("magnet:")
    assert rel.indexer == "hub:minerva-archive"
    assert rel.info_url.endswith("/rom?id=823208")


def test_an_unresolved_hub_row_still_rides_the_hub_lane(tmp_path, monkeypatch):
    from romarr.app import ROMarr

    svc = ROMarr(env={"ROMARR_DATA": str(tmp_path / "s.json")})
    row = _row(plugin="archive-org", source_id="MINERVA3")
    monkeypatch.setattr(svc, "_hub_enabled", lambda: True)
    monkeypatch.setattr(hub, "search", lambda *a, **k: ([row], ""))
    monkeypatch.setattr(hub, "resolve_torrents", lambda rows: rows)

    out = svc._hub_releases("minerva 3", None)
    assert out[0].protocol == "hub"
    assert out[0].download_url.startswith("hub://")
