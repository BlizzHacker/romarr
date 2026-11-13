"""Named emulator requests include free proprietary software and manual tools."""
import pytest

from romarr.emulator_catalog import bundled
from romarr.emulators import EmulatorHub


@pytest.mark.parametrize("name,official_url,license_text", [
    ("redream", "https://redream.io/download", "Proprietary"),
    ("LaunchBox", "https://www.launchbox-app.com/download", "Proprietary"),
    ("AdvanceSCAN", "https://www.advancemame.it/scan-readme.html", "GPL-2.0"),
])
def test_requested_free_software_is_searchable_with_manual_setup(tmp_path, name, official_url, license_text):
    """Free versions must remain available without inventing managed installation."""
    matches = EmulatorHub(tmp_path).catalog(query=name)["items"]
    row = next(item for item in matches if item["name"] == name)
    assert row["website"] == official_url
    assert license_text in row["license"]
    assert row["manual_setup"] is True
    assert row["managed_releases"] is False
    assert row["free_use"]
    assert row["sources"] and row["license_source"]


def test_premium_frontend_features_are_not_promised_in_free_catalog():
    """LaunchBox's free entry must disclose the separate Big Box license."""
    row = next(item for item in bundled()["items"] if item["name"] == "LaunchBox")
    assert "Big Box" in row["description"]
    assert "paid license" in row["description"]


def test_catalog_coverage_count_matches_inventory():
    """The published total must include the named additions."""
    catalog = bundled()
    assert catalog["coverage"]["catalog_entries"] == len(catalog["items"])
