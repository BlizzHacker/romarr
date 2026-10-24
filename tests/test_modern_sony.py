import pytest
from romarr.platforms import resolve
from romarr.library import import_rom
from romarr.selection import score
from test_selection import rel


@pytest.mark.parametrize("name,slug,ext", [
    ("Sony PlayStation 4", "ps4", ".pkg"),
    ("PS5", "ps5", ".pkg"),
    ("PS Vita", "psvita", ".vpk"),
    ("PlayStation Vita", "psvita", ".pkg"),
    ("vita", "psvita", ".zip"),
])
def test_modern_sony_import_preserves_package(tmp_path, name, slug, ext):
    platform = resolve(name)
    assert platform.slug == slug
    source = tmp_path / ("Homebrew" + ext)
    source.write_bytes(b"synthetic package for import test")
    results = import_rom(source, platform, tmp_path / "library")
    assert len(results) == 1 and results[0].ok
    assert results[0].destination.read_bytes() == source.read_bytes()
    assert slug in results[0].destination.parts


@pytest.mark.parametrize("slug,other", [("ps4", "ps5"), ("ps5", "ps4"), ("psvita", "psp")])
def test_modern_console_search_rejects_other_platform(slug, other):
    platform = resolve(slug)
    assert score(rel(f"Homebrew Game {slug}", seeders=10, size=64 * 1024 * 1024), "Homebrew Game", platform) > 0
    assert score(rel(f"Homebrew Game {other}", seeders=10, size=64 * 1024 * 1024), "Homebrew Game", platform) < 0
