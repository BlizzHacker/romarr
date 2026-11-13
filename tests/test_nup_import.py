"""The NUP header gate in the automatic import sweep.

A release title is a guess; a NUP's validated table of contents is not. Before
a finished download is routed, its loose `.nup` files are read: a Switch 2
title moves an inferred platform to Switch 2 or sends a requested-Switch row to
review, and an invalid container fails the import without blaming the release.
"""

from __future__ import annotations

import zipfile
from types import SimpleNamespace

import pytest

from romarr.app import ROMarr
from romarr.store import QueueItem
from tests.nup_builder import Pkg, build_nup

S2_BASE = 0x040074A01BF12000
S1_BASE = 0x0100F2C0115B6000


@pytest.fixture
def sweep(tmp_path, monkeypatch):
    """A service with one queue row and one finished download at `content`."""
    def make(platform: str, content, *, release: str = "Donkey Kong Bananza.nup"):
        service = ROMarr({"ROMARR_DATA": str(tmp_path / "state.json")})
        item = QueueItem("Donkey Kong Bananza", platform, release, 1, "grabbed",
                         download_id="qbit-tag:job")
        service.queue.append(item)
        done = {"name": release, "id": "qbit-tag:job", "content_path": str(content)}
        service.clients = [SimpleNamespace(configured=True, completed=lambda: [done],
                                           remove_download=lambda *a, **k: None)]
        cfg = {"path": str(tmp_path / "lib")}
        monkeypatch.setattr(service, "library_for",
                            lambda slug: (cfg, SimpleNamespace(rescan=lambda slug: None)))
        monkeypatch.setattr(service, "_virus_check", lambda *a: None)
        service.store.settings["reconcile_after_import"] = False
        return service, item
    return make


def nup_file(tmp_path, title_id: int, name: str = "Donkey Kong Bananza.nup"):
    """Write a one-package base container into a download folder."""
    downloads = tmp_path / "downloads"
    downloads.mkdir(exist_ok=True)
    path = downloads / name
    path.write_bytes(build_nup([Pkg(title_id)]))
    return path


def imported_under(tmp_path, slug: str) -> list[str]:
    return sorted(p.name for p in (tmp_path / "lib" / slug).rglob("*.nup"))


def failures(service) -> list[dict]:
    return service.store.history(kind="failed")


def test_an_inferred_switch_row_moves_to_switch_2(tmp_path, sweep):
    """Any-platform grab: the name says nothing, `.nup` falls back to Switch,
    and the 0x04 title ID corrects it before anything is routed."""
    service, item = sweep("", nup_file(tmp_path, S2_BASE))

    results = service.import_finished()

    assert results[0]["ok"], results
    assert item.platform == "switch-2"
    assert item.state == "imported"
    assert imported_under(tmp_path, "switch-2")
    assert not (tmp_path / "lib" / "switch").exists()


def test_a_requested_switch_row_goes_to_review(tmp_path, sweep):
    """The operator chose Switch; the header disagrees. Ask, do not override."""
    service, item = sweep("switch", nup_file(tmp_path, S2_BASE))

    results = service.import_finished()

    assert results[0]["ok"] is False
    assert item.state == "import-review"
    assert item.platform == "switch"
    assert "040074A01BF12000" in item.detail
    assert "Switch 2" in item.detail
    assert not (tmp_path / "lib").exists()


def test_review_is_recorded_once_across_sweeps(tmp_path, sweep):
    """Review rows are re-checked every sweep; History must not fill up."""
    service, item = sweep("switch", nup_file(tmp_path, S2_BASE))

    service.import_finished()
    service.import_finished(retry_failed=False)

    assert item.state == "import-review"
    assert len(failures(service)) == 1


def test_a_directory_download_is_gated_too(tmp_path, sweep):
    folder = tmp_path / "downloads" / "Donkey Kong Bananza"
    folder.mkdir(parents=True)
    (folder / "Donkey Kong Bananza.nup").write_bytes(build_nup([Pkg(S2_BASE)]))
    service, item = sweep("switch", folder, release="Donkey Kong Bananza")

    service.import_finished()

    assert item.state == "import-review"


def test_an_invalid_nup_fails_without_blaming_the_release(tmp_path, sweep):
    """A truncated file looks just like an incomplete download: no blocklist."""
    path = nup_file(tmp_path, S2_BASE)
    path.write_bytes(path.read_bytes()[:-8])
    service, item = sweep("switch-2", path)

    results = service.import_finished()

    assert results[0]["ok"] is False
    assert item.state == "import-failed"
    assert not item.release_fault
    assert "not a valid NUP" in item.detail
    assert not (tmp_path / "lib").exists()

    # Retryable: once the file is whole, the next manual sweep imports it.
    path.write_bytes(build_nup([Pkg(S2_BASE)]))
    assert service.import_finished()[0]["ok"]
    assert item.state == "imported"


@pytest.mark.parametrize("platform", ["switch", "switch-2"])
def test_a_switch_1_id_is_imported_as_requested(tmp_path, sweep, platform):
    """0x01 is shared with Switch 2 Editions, so it never moves a row."""
    service, item = sweep(platform, nup_file(tmp_path, S1_BASE))

    assert service.import_finished()[0]["ok"]
    assert item.platform == platform
    assert imported_under(tmp_path, platform)


def test_a_switch_2_nup_requested_for_switch_2_is_untouched(tmp_path, sweep):
    service, item = sweep("switch-2", nup_file(tmp_path, S2_BASE))

    assert service.import_finished()[0]["ok"]
    assert item.platform == "switch-2"


def test_a_nup_inside_an_archive_is_not_gated(tmp_path, sweep):
    """Archives are not opened by the gate; the requested platform stands."""
    downloads = tmp_path / "downloads"
    downloads.mkdir()
    zipped = downloads / "Donkey Kong Bananza.zip"
    with zipfile.ZipFile(zipped, "w") as archive:
        archive.writestr("Donkey Kong Bananza.nup", build_nup([Pkg(S2_BASE)]))
    service, item = sweep("switch", zipped, release="Donkey Kong Bananza.zip")

    service.import_finished()

    assert item.state != "import-review"
    assert "NUP" not in item.detail


def test_delayed_scan_keeps_header_inference_for_next_sweep(tmp_path, sweep, monkeypatch):
    """A pending antivirus result must not turn a guessed platform into a request."""
    service, item = sweep("", nup_file(tmp_path, S2_BASE))
    monkeypatch.setattr(service, "_virus_check",
                        lambda *args: {"name": item.game, "ok": False, "reason": "waiting"})
    service.import_finished()
    assert item.platform == ""
    monkeypatch.setattr(service, "_virus_check", lambda *args: None)
    assert service.import_finished()[0]["ok"]
    assert item.platform == "switch-2"


def test_confirmed_selection_excludes_foreign_nup_and_rejects_stale_plan(tmp_path, sweep):
    """Only a current, confirmed selection can exclude a foreign container."""
    from romarr.import_intent import preview_import, choose_import, queue_identity
    from romarr.platforms import by_slug
    from romarr.romm_bundle import decorate_preview
    folder = tmp_path / "downloads"
    folder.mkdir()
    selected = folder / "Switch game.nup"
    selected.write_bytes(build_nup([Pkg(S1_BASE)]))
    foreign = folder / "Switch 2 game.nup"
    foreign.write_bytes(build_nup([Pkg(S2_BASE)]))
    service, item = sweep("switch", folder, release="Mixed download")
    platform = by_slug("switch")
    cfg, _ = service.library_for("switch")
    preview = preview_import(folder, platform, service.library_root(cfg),
                             layout=service.library_layout(cfg),
                             platform_folders=service.platform_folders(cfg),
                             queue_identity=queue_identity(item))
    preview = decorate_preview(preview, platform, service.library_root(cfg),
                               kind=cfg.get("type", ""), layout=service.library_layout(cfg),
                               platform_folders=service.platform_folders(cfg))
    item.import_intent = choose_import(preview, [{"primary": selected.name, "role": "base"}])
    saved_token = item.import_intent["token"]
    item.import_intent["token"] = "stale"
    assert not service.import_finished()[0]["ok"]
    assert item.state == "import-review"
    item.import_intent["token"] = saved_token
    result = service.import_finished()
    assert item.state == "imported", result
    assert imported_under(tmp_path, "switch") == [selected.name]
    assert foreign.exists()


def test_inferred_malformed_nup_retains_platform_for_review(tmp_path, sweep):
    """A bad companion can be excluded with the ordinary Queue review action."""
    from romarr.import_intent import preview_import, choose_import, queue_identity
    from romarr.platforms import by_slug
    from romarr.romm_bundle import decorate_preview
    folder = tmp_path / "downloads"
    folder.mkdir()
    good = folder / "Good.nup"
    good.write_bytes(build_nup([Pkg(S1_BASE)]))
    (folder / "Broken.nup").write_bytes(b"truncated")
    service, item = sweep("", folder, release="Mixed.nup")
    assert not service.import_finished()[0]["ok"]
    assert item.platform == "switch"
    platform = by_slug(item.platform)
    cfg, _ = service.library_for(item.platform)
    preview = preview_import(folder, platform, service.library_root(cfg),
                             layout=service.library_layout(cfg),
                             platform_folders=service.platform_folders(cfg),
                             queue_identity=queue_identity(item))
    preview = decorate_preview(preview, platform, service.library_root(cfg),
                               kind=cfg.get("type", ""), layout=service.library_layout(cfg),
                               platform_folders=service.platform_folders(cfg))
    item.import_intent = choose_import(preview, [{"primary": good.name, "role": "base"}])
    result = service.import_finished()
    assert item.state == "imported", result
    assert imported_under(tmp_path, "switch") == [good.name]
