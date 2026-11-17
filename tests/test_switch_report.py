"""Pinned read-only Switch inventory, honest metadata gaps and profile ranking."""
import time
from pathlib import Path
from unittest.mock import Mock

import pytest
from romarr.switch_report import report, scan_folder
from romarr.titled import TitleDBIndex, SwitchTitle, classify_tid, base_tid_of, build_index, load_names_into

BASE = "01007ef00011e000"
PATCH = "01007ef00011e800"
DLC = "01007ef00011f001"


def catalogue():
    """Small deterministic regional catalogue; no copyrighted payloads."""
    index = TitleDBIndex()
    index.add(SwitchTitle(BASE, "Example", {"0": "a", "131072": "b"}, "US.en"))
    index.add(SwitchTitle(PATCH, versions={"131072": "b"}))
    index.add(SwitchTitle(DLC, "Expansion", {}, "US.en"))
    index.names_loaded = True
    index.region = "US.en"
    return index


def dump(root, name):
    """Write harmless fixture bytes whose names carry the only scanned facts."""
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"inventory fixture")
    return path


def test_loose_and_foldered_files_group_by_actual_family(tmp_path):
    original = dump(tmp_path, f"Example [{BASE}][v0].nsp")
    dump(tmp_path, f"Example/update [{PATCH}][v65536].nsz")
    data = report({"switch": tmp_path}, catalogue())
    row, = data["items"]
    assert row["base_tid"] == BASE.upper()
    assert row["update"] == "outdated"
    assert row["installed_version"] == 65536
    assert row["latest_version"] == 131072
    assert row["dlc_missing"] == [{"tid": DLC.upper(), "name": "Expansion"}]
    assert row["dlc_total"] == 1 and row["dlc_complete_list"] is False
    assert original.read_bytes() == b"inventory fixture"
    dump(tmp_path, f"addon [{DLC}].nsp")
    assert not report({"switch": tmp_path}, catalogue())["items"][0]["dlc_missing"]


def test_unknown_version_and_switch2_never_claim_current(tmp_path):
    dump(tmp_path, f"Example [{BASE}].nsp")
    dump(tmp_path, f"Patch [{PATCH}][v9999999999999999].nsp")
    for slug in ("switch", "switch-2"):
        row = report({slug: tmp_path}, catalogue())["items"][0]
        assert row["update"] == "unknown"
        assert row["installed_version"] is None
        if slug == "switch-2":
            assert row["latest_version"] is None and not row["dlc_missing"]


def test_missing_update_and_versions_only_dlc_coverage_unknown(tmp_path):
    dump(tmp_path, f"Example [{BASE}][v0].nsp")
    index = catalogue()
    index.names_loaded = False
    row = report({"switch": tmp_path}, index)["items"][0]
    assert row["update"] == "missing"
    assert row["dlc_total"] is None and not row["catalog_region"]


def test_empty_shelf_is_lazy(tmp_path):
    callback = Mock(side_effect=AssertionError("must not download"))
    assert report({"switch": tmp_path}, callback)["items"] == []
    callback.assert_not_called()


def test_skips_links_hidden_quarantine_media_and_nonfinal_extensions(tmp_path):
    outside = tmp_path / "outside"
    dump(outside, f"external [{BASE}].nsp")
    root = tmp_path / "switch"
    root.mkdir()
    (root / "linked").symlink_to(outside, target_is_directory=True)
    for folder in (".romarr-quarantine", ".internal", "covers", "soundtracks"):
        dump(root, f"{folder}/Example [{BASE}].nsp")
    dump(root, f"Example [{BASE}].nsp.exe")
    scan = scan_folder(root)
    assert scan["dumps"] == [] and scan["skipped_links"] == 1
    assert scan["complete"]
    linked_root = tmp_path / "root-link"
    linked_root.symlink_to(outside, target_is_directory=True)
    scan = scan_folder(linked_root)
    assert not scan["complete"] and not scan["dumps"]


def test_scan_limits_visible(tmp_path):
    for i in range(3):
        dump(tmp_path, f"{i} [{BASE}].nsp")
    scan = scan_folder(tmp_path, max_entries=1)
    assert not scan["complete"] and scan["warnings"]
    scan = scan_folder(tmp_path, deadline=time.monotonic()-1)
    assert not scan["complete"] and not scan["dumps"]


def test_classification_real_dlc_and_reserved_ids():
    assert classify_tid(DLC) == "dlc"
    assert base_tid_of(DLC) == BASE
    assert classify_tid("01007ef00011e001") == "unknown"
    assert classify_tid("01007ef00011f000") == "unknown"
    assert classify_tid("0100000000000001") == "system"


def test_versions_synthesize_patch_and_eshop_keys_use_row_id(monkeypatch):
    monkeypatch.setattr("romarr.titled.fetch_versions", lambda _: {BASE: {"0": "a", "65536": "b", "-1": "bad"}})
    index = build_index(with_names=False)
    assert index.lookup_by_id(PATCH).versions == {"65536": "b"}
    monkeypatch.setattr("romarr.titled._load_cached_or_fetch", lambda *args: {"70010000000001": {"id": BASE.upper(), "name": "Example", "size": "bad"}})
    assert load_names_into(index) == 1
    assert index.lookup_by_id(BASE).name == "Example"
    assert index.lookup_by_id(BASE).versions["65536"] == "b"
    assert index.lookup_by_id(BASE).size == 0


def test_root_swap_discards_snapshot_and_never_traverses_replacement(tmp_path, monkeypatch):
    """A race after listing cannot commit facts from an unpinned replacement."""
    from contextlib import contextmanager
    from romarr import switch_report
    root = tmp_path / "switch"
    dump(root, f"Example [{BASE}].nsp")
    real = switch_report.os.scandir
    swapped = False
    @contextmanager
    def replacing(path):
        nonlocal swapped
        with real(path) as entries:
            yield entries
        if not swapped:
            swapped = True
            root.rename(tmp_path / "old-switch")
            root.mkdir()
            dump(root, f"Replacement [{DLC}].nsp")
    monkeypatch.setattr(switch_report.os, "scandir", replacing)
    data = scan_folder(root)
    assert swapped and not data["complete"]
    assert not data["dumps"]
