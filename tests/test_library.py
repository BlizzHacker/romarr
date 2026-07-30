import logging
import zipfile
from pathlib import Path

import pytest

from romarr.indexers import Prowlarr, sanitise_for_display
from romarr.library import import_rom, list_candidates, map_remote_path, safe_members
from romarr.platforms import by_slug
from romarr.selection import Release


SNES = by_slug("snes")


def make_zip(path, names):
    with zipfile.ZipFile(path, "w") as z:
        for n in names:
            z.writestr(n, b"\x00" * 2048)
    return path


# --- zip-slip -------------------------------------------------------------

def test_archive_entries_escaping_the_root_are_dropped(tmp_path):
    archive = make_zip(tmp_path / "evil.zip",
                       ["Game.smc", "../../escape.smc", "/abs/root.smc"])
    with zipfile.ZipFile(archive) as z:
        kept = safe_members(z, tmp_path)
    assert kept == ["Game.smc"]


def test_traversal_entry_never_reaches_the_filesystem(tmp_path):
    library = tmp_path / "library"
    downloads = tmp_path / "dl"
    downloads.mkdir()
    make_zip(downloads / "g.zip", ["../../pwned.smc", "Real Game (USA).smc"])

    result = import_rom(downloads / "g.zip", SNES, library)
    assert result.ok
    assert result.destination.name == "Real Game (USA).smc"
    assert not (tmp_path.parent / "pwned.smc").exists()


# --- importing ------------------------------------------------------------

def test_imports_the_rom_from_a_zip_into_the_platform_folder(tmp_path):
    library = tmp_path / "library"
    archive = make_zip(tmp_path / "g.zip", ["readme.nfo", "Super Mario World (USA).smc"])

    result = import_rom(archive, SNES, library)
    assert result.ok
    assert result.destination == library / "snes" / "Super Mario World (USA).smc"
    assert result.destination.read_bytes()[:2] == b"\x00\x00"


def test_imports_a_bare_rom_file(tmp_path):
    library = tmp_path / "library"
    rom = tmp_path / "Zelda (USA).smc"
    rom.write_bytes(b"\x01" * 1024)

    result = import_rom(rom, SNES, library)
    assert result.ok
    assert result.destination == library / "snes" / "Zelda (USA).smc"


def test_imports_from_a_directory_download(tmp_path):
    library = tmp_path / "library"
    folder = tmp_path / "Some Release"
    (folder / "extras").mkdir(parents=True)
    (folder / "extras" / "art.jpg").write_bytes(b"x")
    (folder / "Game (USA).smc").write_bytes(b"y" * 512)

    result = import_rom(folder, SNES, library)
    assert result.ok
    assert result.destination.name == "Game (USA).smc"


def test_refuses_to_overwrite_unless_told(tmp_path):
    library = tmp_path / "library"
    (library / "snes").mkdir(parents=True)
    existing = library / "snes" / "Game (USA).smc"
    existing.write_bytes(b"original")

    rom = tmp_path / "Game (USA).smc"
    rom.write_bytes(b"replacement")

    blocked = import_rom(rom, SNES, library)
    assert not blocked.ok
    assert "already" in blocked.reason
    assert existing.read_bytes() == b"original"

    forced = import_rom(rom, SNES, library, overwrite=True)
    assert forced.ok
    assert existing.read_bytes() == b"replacement"


def test_reports_when_a_download_has_no_rom(tmp_path):
    library = tmp_path / "library"
    archive = make_zip(tmp_path / "g.zip", ["readme.nfo", "cover.jpg"])

    result = import_rom(archive, SNES, library)
    assert not result.ok
    assert "no Super Nintendo ROM" in result.reason


def test_missing_download_is_reported_not_raised(tmp_path):
    result = import_rom(tmp_path / "nope.zip", SNES, tmp_path / "library")
    assert not result.ok
    assert "does not exist" in result.reason


def test_a_missing_download_names_both_ways_to_fix_it(tmp_path):
    """The overwhelmingly common cause is a wrong volume mount, not a lost
    download. Reporting only "does not exist" sends people hunting for a file
    that is sitting in their download client, so the reason has to name the
    container path that was tried and both remedies."""
    result = import_rom(tmp_path / "nope.zip", SNES, tmp_path / "library")
    assert "in this container" in result.reason
    assert str(tmp_path / "nope.zip") in result.reason
    assert "Mount" in result.reason and "remote path mapping" in result.reason


# --- remote path mapping -----------------------------------------------------
#
# The client reports paths in ITS filesystem. Getting this wrong is the single
# most common Docker misconfiguration for this kind of app, and it presents as a
# download that completes and then silently never appears.

def test_an_unmapped_path_is_returned_unchanged():
    assert map_remote_path("/downloads/complete/Game", []) \
        == Path("/downloads/complete/Game")
    assert map_remote_path("/downloads/x", None) == Path("/downloads/x")


def test_a_mapping_rewrites_only_the_prefix():
    mappings = [{"remote": "/downloads", "local": "/mnt/dl"}]
    assert map_remote_path("/downloads/complete/Game.zip", mappings) \
        == Path("/mnt/dl/complete/Game.zip")
    # An exact match maps to the local root rather than appending an empty part.
    assert map_remote_path("/downloads", mappings) == Path("/mnt/dl")


def test_the_longest_matching_prefix_wins_regardless_of_order():
    # Otherwise a broad mapping added first shadows the specific one that was
    # added to correct it.
    mappings = [
        {"remote": "/downloads", "local": "/mnt/all"},
        {"remote": "/downloads/complete", "local": "/mnt/done"},
    ]
    assert map_remote_path("/downloads/complete/G", mappings) == Path("/mnt/done/G")
    assert map_remote_path("/downloads/incomplete/G", mappings) == Path("/mnt/all/incomplete/G")


def test_incomplete_mapping_rows_are_ignored():
    # A half-filled row in the settings UI must not swallow every path.
    mappings = [{"remote": "/downloads", "local": ""}, {"remote": "", "local": "/mnt"}]
    assert map_remote_path("/downloads/G", mappings) == Path("/downloads/G")


def test_a_windows_style_remote_path_maps(tmp_path):
    mappings = [{"remote": "D:\\torrents", "local": "/downloads"}]
    assert map_remote_path("D:\\torrents\\Game", mappings) == Path("/downloads/Game")


def test_an_unopenable_result_warns_and_names_both_paths(caplog):
    """The warning is the only place both paths appear together, and the
    difference between them is the entire diagnosis."""
    with caplog.at_level(logging.WARNING):
        map_remote_path("/downloads/complete/Game", [])
    assert "/downloads/complete/Game" in caplog.text
    assert "no remote path mapping covers it" in caplog.text

    caplog.clear()
    with caplog.at_level(logging.WARNING):
        map_remote_path("/downloads/G", [{"remote": "/downloads", "local": "/mnt/dl"}])
    # Both the reported path and what the mapping made of it, so a mapping whose
    # local side is wrong is distinguishable from a missing mount.
    assert "/downloads/G" in caplog.text
    assert str(Path("/mnt/dl/G")) in caplog.text


def test_a_path_that_does_exist_warns_about_nothing(tmp_path, caplog):
    real = tmp_path / "here.zip"
    real.write_bytes(b"x")
    with caplog.at_level(logging.WARNING):
        assert map_remote_path(str(real), []) == real
    assert caplog.text == ""


# --- api key hygiene ------------------------------------------------------

def test_a_keyed_url_is_kept_for_the_download_client():
    """Prowlarr's link carries its API key and there is no keyless alternative
    for usenet -- a magnet has no NZB equivalent. Refusing keyed URLs, as this
    once did, protected nothing and meant usenet never worked at all. Radarr
    and Sonarr both hand this URL to the download client, which fetches it
    server-side; the key must simply never reach a browser or a log."""
    row = {
        "title": "Super Mario World (USA)",
        "size": 524288,
        "seeders": 30,
        "categories": [{"id": 1030}],
        "downloadUrl": "http://prowlarr:9696/1/download?apikey=SECRET123&link=x",
        "protocol": "torrent",
    }
    release = Prowlarr._to_release(row)
    assert release.download_url.startswith("http://prowlarr:9696/")
    # ...and it is unusable in anything user-facing.
    assert "SECRET123" not in sanitise_for_display(release.download_url)


def test_a_usenet_release_is_grabbable_at_all():
    """The whole point: an NZB result used to come back with an empty
    download_url and be refused as "no plain magnet"."""
    row = {
        "title": "Chrono Trigger (USA)",
        "size": 4194304,
        "seeders": 0,
        "categories": [{"id": 1030}],
        "downloadUrl": "https://indexer.example/getnzb?id=abc&apikey=SECRET",
        "protocol": "usenet",
    }
    release = Prowlarr._to_release(row)
    assert release.protocol == "usenet"
    assert release.download_url, "a usenet release must be grabbable"


def test_a_magnet_is_still_preferred_over_a_keyed_url():
    row = {
        "title": "Zelda (USA)", "size": 1024, "seeders": 5,
        "categories": [{"id": 1030}],
        "magnetUrl": "magnet:?xt=urn:btih:abc",
        "downloadUrl": "http://prowlarr:9696/1/download?apikey=SECRET123",
        "protocol": "torrent",
    }
    assert Prowlarr._to_release(row).download_url == "magnet:?xt=urn:btih:abc"


def test_a_nonsense_download_link_is_dropped():
    """Anything that is not a magnet or an http(s) URL cannot be handed to a
    client, and passing it on would just fail later with a worse message."""
    row = {
        "title": "X", "size": 1, "seeders": 1, "categories": [{"id": 1030}],
        "downloadUrl": "javascript:alert(1)", "protocol": "torrent",
    }
    assert Prowlarr._to_release(row).download_url == ""


def test_plain_magnet_is_kept():
    row = {
        "title": "Zelda (USA)", "size": 1024, "seeders": 5,
        "categories": [{"id": 1030}],
        "magnetUrl": "magnet:?xt=urn:btih:abc",
        "protocol": "torrent",
    }
    assert Prowlarr._to_release(row).download_url == "magnet:?xt=urn:btih:abc"


def test_sanitiser_redacts_keys_for_logs():
    dirty = "http://prowlarr:9696/1/download?apikey=SECRET123&link=x"
    assert "SECRET123" not in sanitise_for_display(dirty)
    assert sanitise_for_display("") == ""


# --- service wiring -------------------------------------------------------

def test_request_rejects_an_unknown_platform(tmp_path):
    from romarr.app import ROMarr
    svc = ROMarr(env={"ROMARR_DATA": str(tmp_path / "r.json")})
    out = svc.request("Super Mario World", "PlayStation 5")
    assert not out["ok"]
    assert "unknown platform" in out["error"]


def test_a_release_without_a_plain_magnet_is_refused_not_leaked(monkeypatch, tmp_path):
    """Prowlarr's own download links carry its API key, so a release that has
    no plain magnet must be refused rather than passed to a download client."""
    from romarr.app import ROMarr
    from romarr.selection import Release

    svc = ROMarr(env={"QBITTORRENT_URL": "http://qbit:8090",
                       "ROMARR_DATA": str(tmp_path / "r.json")})
    unusable = Release(title="Super Mario World (USA)", size=524288, seeders=50,
                       categories=(1030,), download_url="", protocol="torrent")
    monkeypatch.setattr(svc.prowlarr, "search", lambda *a, **k: [unusable])

    grabbed = []
    for c in svc.clients:
        monkeypatch.setattr(c, "add", lambda *a, **k: grabbed.append(a) or True)

    out = svc.request("Super Mario World", "snes")
    assert not out["ok"]
    assert "no usable download link" in out["error"]
    assert grabbed == [], "nothing to grab means nothing is handed to a client"
    assert svc.queue[-1].state == "failed"


def test_a_healthy_release_is_grabbed_and_queued(monkeypatch, tmp_path):
    from romarr.app import ROMarr
    from romarr.selection import Release

    svc = ROMarr(env={"QBITTORRENT_URL": "http://qbit:8090",
                       "ROMARR_DATA": str(tmp_path / "r.json")})
    good = Release(title="Super Mario World (USA)", size=524288, seeders=120,
                   categories=(1030,), download_url="magnet:?xt=urn:btih:abc",
                   protocol="torrent")
    monkeypatch.setattr(svc.prowlarr, "search", lambda *a, **k: [good])
    sent = []
    for c in svc.clients:
        monkeypatch.setattr(c, "add", lambda url, **k: sent.append(url) or True)

    out = svc.request("Super Mario World", "Super Nintendo")
    assert out["ok"]
    assert sent == ["magnet:?xt=urn:btih:abc"]
    assert svc.queue[-1].state == "grabbed"
    assert svc.queue[-1].platform == "snes"


# --- protocol routing ------------------------------------------------------

def test_a_usenet_release_goes_to_the_usenet_client(monkeypatch, tmp_path):
    """Accepting only torrents made every usenet indexer in Prowlarr dead
    weight: results scored fine and were then refused."""
    from romarr.app import ROMarr
    from romarr.selection import Release

    svc = ROMarr(env={
        "QBITTORRENT_URL": "http://qbit:8090",
        "SABNZBD_URL": "http://sab:8080", "SABNZBD_API_KEY": "k",
        "ROMARR_DATA": str(tmp_path / "r.json"),
    })
    nzb = Release(title="Chrono Trigger (USA)", size=4 << 20, seeders=0,
                  categories=(1030,), download_url="https://idx/get?id=1",
                  protocol="usenet")
    monkeypatch.setattr(svc.prowlarr, "search", lambda *a, **k: [nzb])

    to_sab, to_qbit = [], []
    for c in svc.clients:
        sink = to_sab if c.protocol == "usenet" else to_qbit
        monkeypatch.setattr(c, "add", lambda url, _s=sink, **k: _s.append(url) or True)

    out = svc.request("Chrono Trigger", "snes")
    assert out["ok"], out
    assert to_sab == ["https://idx/get?id=1"]
    assert to_qbit == [], "a usenet release must not go to a torrent client"


def test_a_torrent_release_goes_to_the_torrent_client(monkeypatch, tmp_path):
    from romarr.app import ROMarr
    from romarr.selection import Release

    svc = ROMarr(env={
        "QBITTORRENT_URL": "http://qbit:8090",
        "SABNZBD_URL": "http://sab:8080", "SABNZBD_API_KEY": "k",
        "ROMARR_DATA": str(tmp_path / "r.json"),
    })
    tor = Release(title="Zelda (USA)", size=1 << 20, seeders=40,
                  categories=(1030,), download_url="magnet:?xt=urn:btih:abc",
                  protocol="torrent")
    monkeypatch.setattr(svc.prowlarr, "search", lambda *a, **k: [tor])

    to_sab, to_qbit = [], []
    for c in svc.clients:
        sink = to_sab if c.protocol == "usenet" else to_qbit
        monkeypatch.setattr(c, "add", lambda url, _s=sink, **k: _s.append(url) or True)

    assert svc.request("Zelda", "snes")["ok"]
    assert to_qbit == ["magnet:?xt=urn:btih:abc"]
    assert to_sab == []


def test_a_protocol_with_no_client_says_so_rather_than_failing_vaguely(monkeypatch, tmp_path):
    from romarr.app import ROMarr
    from romarr.selection import Release

    # Torrent client only; a usenet result has nowhere to go.
    svc = ROMarr(env={"QBITTORRENT_URL": "http://qbit:8090",
                       "ROMARR_DATA": str(tmp_path / "r.json")})
    nzb = Release(title="Metroid (USA)", size=1 << 20, seeders=0,
                  categories=(1030,), download_url="https://idx/get?id=2",
                  protocol="usenet")
    monkeypatch.setattr(svc.prowlarr, "search", lambda *a, **k: [nzb])

    out = svc.request("Metroid", "nes")
    assert not out["ok"]
    assert "usenet" in out["error"]
    # And it stays in Wanted, so configuring a client later retries it.
    assert any(w["game"] == "Metroid" for w in svc.store.missing())


def test_an_unconfigured_client_is_skipped_not_tried():
    from romarr.downloaders import SABnzbd, SabConfig, pick_client
    from romarr.clients import QBittorrent, QbitConfig

    qbit = QBittorrent(QbitConfig(base_url="http://qbit:8090"))
    sab_off = SABnzbd(SabConfig(base_url="", api_key=""))
    assert pick_client("torrent", [qbit, sab_off]) is qbit
    assert pick_client("usenet", [qbit, sab_off]) is None

    sab_on = SABnzbd(SabConfig(base_url="http://sab:8080", api_key="k"))
    assert pick_client("usenet", [qbit, sab_off, sab_on]) is sab_on
