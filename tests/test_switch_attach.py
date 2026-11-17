"""Switch updates and DLC land inside their game's folder, never through a link."""
import os
import sys

import pytest

from romarr.import_intent import preview_import
from romarr.library import import_rom
from romarr.platforms import resolve
from romarr.switch_attach import attach_dir, is_real_directory_chain, title_id_of

BASE = "Bayonetta 2/Bayonetta 2 [01007960049A0000][v0].nsz"
UPDATE = "Bayonetta 2 Update [01007960049A0800][v196608].nsp"
DLC = "Bayonetta 2 Costume [01007960049A1001][v0].nsp"


def make(root, relative, data=b"x"):
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


needs_symlinks = pytest.mark.skipif(sys.platform == "win32", reason="symlinks need privileges on Windows")


def test_update_goes_into_the_games_update_folder(tmp_path):
    make(tmp_path, BASE)
    assert attach_dir(UPDATE, tmp_path) == tmp_path / "Bayonetta 2" / "update"


def test_dlc_goes_into_the_games_dlc_folder(tmp_path):
    make(tmp_path, BASE)
    assert attach_dir(DLC, tmp_path) == tmp_path / "Bayonetta 2" / "dlc"


def test_base_deeper_in_the_game_folder_still_counts(tmp_path):
    make(tmp_path, "Bayonetta 2/base/Bayonetta 2 [01007960049A0000][v0].nsp")
    assert attach_dir(UPDATE, tmp_path) == tmp_path / "Bayonetta 2" / "update"


@pytest.mark.parametrize("files", [
    [],                                                            # base game absent
    ["Other/Other [0100AAAA00010000][v0].nsz"],                    # a different game
    ["Bayonetta 2 [01007960049A0000][v0].nsz"],                    # base loose, no folder
    ["covers/Bayonetta 2 [01007960049A0000].nsp"],                 # media folder
    [BASE, "Bayonetta 2 (copy)/B2 [01007960049A0000][v0].nsp"],    # two folders: ambiguous
])
def test_keeps_the_normal_destination_when_unsure(tmp_path, files):
    for name in files:
        make(tmp_path, name)
    assert attach_dir(UPDATE, tmp_path) is None


@pytest.mark.parametrize("name", [
    "Bayonetta 2 [01007960049A0000][v0].nsp",                      # base itself
    "No title id.nsp",
    "Two [01007960049A0800][01007960049A1001].nsp",                # ambiguous
])
def test_only_single_tagged_updates_and_dlc_move(tmp_path, name):
    make(tmp_path, BASE)
    assert attach_dir(name, tmp_path) is None


def test_title_id_requires_exactly_one_bracketed_id():
    assert title_id_of(UPDATE) == "01007960049a0800"
    assert title_id_of("01007960049A0800.nsp") == ""


@needs_symlinks
def test_a_linked_game_folder_is_never_followed(tmp_path):
    outside = tmp_path / "outside"
    make(outside, "Bayonetta 2 [01007960049A0000][v0].nsz")
    shelf = tmp_path / "switch"
    shelf.mkdir()
    os.symlink(outside, shelf / "Bayonetta 2")
    assert attach_dir(UPDATE, shelf) is None


@needs_symlinks
def test_a_linked_update_folder_is_refused(tmp_path):
    make(tmp_path, BASE)
    elsewhere = tmp_path.parent / (tmp_path.name + "-elsewhere")
    elsewhere.mkdir()
    os.symlink(elsewhere, tmp_path / "Bayonetta 2" / "update")
    assert attach_dir(UPDATE, tmp_path) is None
    assert not is_real_directory_chain(tmp_path, tmp_path / "Bayonetta 2" / "update")


def test_directory_chain_rejects_paths_outside_the_root(tmp_path):
    assert not is_real_directory_chain(tmp_path / "a", tmp_path / "b")
    assert is_real_directory_chain(tmp_path, tmp_path / "new" / "update")


def _library_with_base(tmp_path):
    library = tmp_path / "library"
    make(library / "switch", BASE, b"base game")
    download = make(tmp_path / "downloads", UPDATE, b"update bytes")
    return library, download


def test_import_files_the_update_inside_the_game(tmp_path):
    library, download = _library_with_base(tmp_path)
    result, = import_rom(download, resolve("switch"), library, switch_extras=True)
    assert result.ok, result.reason
    assert result.destination == library / "switch" / "Bayonetta 2" / "update" / UPDATE
    assert result.destination.read_bytes() == b"update bytes"
    assert download.read_bytes() == b"update bytes"


def test_import_keeps_the_flat_layout_when_switched_off(tmp_path):
    library, download = _library_with_base(tmp_path)
    result, = import_rom(download, resolve("switch"), library, switch_extras=False)
    assert result.ok, result.reason
    assert result.destination == library / "switch" / UPDATE


@needs_symlinks
def test_folder_swapped_for_a_link_before_publication_is_refused(tmp_path, monkeypatch):
    library, download = _library_with_base(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    import romarr.library as library_module
    real_attach = library_module.switch_attach_dir

    def attach_then_swap(name, platform_folder):
        target = real_attach(name, platform_folder)
        (target.parent / "update").symlink_to(outside, target_is_directory=True)
        return target

    monkeypatch.setattr(library_module, "switch_attach_dir", attach_then_swap)
    result, = import_rom(download, resolve("switch"), library, switch_extras=True)
    assert not result.ok and "link" in result.reason
    assert not any(outside.iterdir())


def test_preview_shows_the_game_folder_destination(tmp_path):
    library, download = _library_with_base(tmp_path)
    preview = preview_import(download.parent, resolve("switch"), library, switch_extras=True)
    destinations = [item["destination"] for item in preview["items"]]
    assert str(library / "switch" / "Bayonetta 2" / "update" / UPDATE) in destinations
