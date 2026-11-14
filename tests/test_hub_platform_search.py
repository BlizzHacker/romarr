"""Platform-filtered interactive search must use the ROM Hub source vocabulary."""

import sys
import types

import pytest

from romarr import hub
from romarr.app import ROMarr
from romarr.downloaders import HubConfig, HubDownloader
from romarr.platforms import resolve


@pytest.fixture
def dispatch(monkeypatch):
    """Model exact-match archive sources at the actual Hub dispatcher seam."""
    import romarr.ia_auth as ia_auth
    calls = []
    plugins = [types.SimpleNamespace(slug=slug, enabled=True,
               manifest=types.SimpleNamespace(capabilities=("search",)))
               for slug in ("archive-org", "nointro-archive")]

    class Registry:
        """Expose installed searchable sources without reading user state."""

        def __init__(self, *args):
            """Accept the production registry's home argument."""

        def installed(self):
            """Return both affected sources."""
            return plugins

    class Fetcher:
        """Keep all source traffic offline."""

        def __init__(self, **kwargs):
            """Accept the production fetcher configuration."""

        def close(self):
            """Match the fetcher's cleanup interface."""

    def search_all(group, **kwargs):
        """Return results only for the supported source identifier."""
        calls.append(kwargs)
        rows = []
        if kwargs["platform"] in (None, "genesis"):
            rows = [dict(source_id="sonic", title="Sonic The Hedgehog (USA).md",
                         platform="genesis", size_bytes=524288,
                         url="https://archive.org/details/example-sonic",
                         plugin=p.slug, extra={}) for p in group]
        return types.SimpleNamespace(results=rows, statuses=[])

    modules = {
        "rom_hub": {"registry": None},
        "rom_hub.registry": {"Registry": Registry},
        "rom_hub.broker": {},
        "rom_hub.broker.fetcher": {"HttpxFetcher": Fetcher},
        "rom_hub.dispatcher": {"search_all": search_all},
    }
    for name, attrs in modules.items():
        module = types.ModuleType(name)
        module.__dict__.update(attrs)
        monkeypatch.setitem(sys.modules, name, module)
    sys.modules["rom_hub"].registry = sys.modules["rom_hub.registry"]
    monkeypatch.setattr(hub, "_unsandboxed", lambda: False)
    monkeypatch.setattr(hub, "searchable_plugins", lambda: [p.slug for p in plugins])
    monkeypatch.setattr(ia_auth, "status", lambda *args: {
        "configured": False, "external": False, "error": ""})
    return calls


@pytest.mark.parametrize("platform, expected", [
    ("genesis-slash-megadrive", "genesis"),
    ("Sega Genesis / Mega Drive", "genesis"),
    ("mega drive", "genesis"),
    ("turbografx16--1", "tg16"),
    ("odyssey-2-slash-videopac-g7000", "odyssey-2"),
    ("sinclair-zx81", "zx81"),
    ("apple2", "appleii"),
    ("snes", "snes"),
    ("switch-2", "switch-2"),
    ("future-plugin-platform", "future-plugin-platform"),
    (None, None),
])
def test_hub_dispatch_receives_source_platform(dispatch, platform, expected):
    """Translate known mismatches without widening or guessing other systems."""
    hub.search("Sonic", platform=platform)
    assert dispatch[0]["platform"] == expected
    assert dispatch[0]["limit"] == (100 if platform else 25)


@pytest.mark.parametrize("platform", ["genesis-slash-megadrive",
                                    "Sega Genesis / Mega Drive", "mega drive", ""])
def test_interactive_search_keeps_both_archive_sources(dispatch, tmp_path, platform):
    """Exercise candidate scoring and the real Hub bridge used by the UI."""
    service = ROMarr({"ROMARR_DATA": str(tmp_path / "state.json")})
    service.prowlarr = types.SimpleNamespace(search=lambda *args: [])
    service.clients = [HubDownloader(HubConfig(save_path=str(tmp_path / "hub")))]
    result = service.candidates("Sonic", platform)
    assert result["found"] == 2
    assert {r["indexer"] for r in result["items"]} == {
        "hub:archive-org", "hub:nointro-archive"}
    if platform:
        assert resolve(platform).slug == "genesis-slash-megadrive"
    assert dispatch[0]["platform"] == ("genesis" if platform else None)
