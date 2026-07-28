import zipfile

import pytest

from rommarr.indexers import Prowlarr, sanitise_for_display
from rommarr.library import import_rom, list_candidates, safe_members
from rommarr.platforms import by_slug
from rommarr.selection import Release


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


# --- api key hygiene ------------------------------------------------------

def test_prowlarr_download_url_never_carries_the_api_key():
    row = {
        "title": "Super Mario World (USA)",
        "size": 524288,
        "seeders": 30,
        "categories": [{"id": 1030}],
        # Prowlarr hands back a link to ITSELF, api key included.
        "downloadUrl": "http://prowlarr:9696/1/download?apikey=SECRET123&link=x",
        "protocol": "torrent",
    }
    release = Prowlarr._to_release(row)
    assert "SECRET123" not in release.download_url
    assert "apikey" not in release.download_url


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
