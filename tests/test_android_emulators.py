"""Android catalogue metadata must not turn APKs into desktop installers."""
import pytest

from romarr.emulators import EmulatorHub, _matches_host
from romarr.emulator_catalog import bundled


def test_android_host_filter_exposes_only_verified_android_records(tmp_path):
    """Unknown host support remains visible in All, without invented Android support."""
    hub = EmulatorHub(tmp_path)
    result = hub.catalog(host_platform="android", limit=100)
    assert result["total"] >= 9
    assert result["host_platform"] == "android"
    assert "android" in result["host_platforms"]
    assert all("android" in row["host_platforms"] for row in result["items"])
    for name in ("PPSSPP", "Dolphin", "Lemuroid", "melonDS-android", "Flycast", "Azahar", "Vita3K", "redream"):
        row = next(row for row in result["items"] if row["name"] == name)
        assert row["android_package"] and row["android_downloads"].startswith("https://")
        assert row["android_verified_date"]
    assert hub.catalog(host_platform="android", query="PCSX2")["total"] == 0


def test_android_packages_are_searchable_without_network(tmp_path):
    """A user can find an app by its Android package identifier."""
    assert EmulatorHub(tmp_path).catalog(query="com.swordfish.lemuroid")["items"][0]["name"] == "Lemuroid"


def test_apk_only_apps_have_manual_setup_and_no_windows_host(tmp_path):
    """Android-only projects cannot advertise managed desktop installation."""
    hub = EmulatorHub(tmp_path)
    for name in ("Lemuroid", "melonDS-android"):
        row = next(row for row in hub.catalog(query=name)["items"] if row["name"] == name)
        assert row["manual_setup"] and not row["managed_releases"]
        assert row["host_platforms"] == ["android"]
        assert hub.release(row["id"])["assets"] == []


@pytest.mark.parametrize("name", ["azahar-android-arm64-v8a.zip", "android-Windows-arm64.zip", "flycast-2.7.apk", "android-latest.apk"])
def test_android_assets_do_not_match_desktop_hosts(monkeypatch, name):
    """Matching a desktop CPU never makes an Android build desktop-installable."""
    monkeypatch.setattr("romarr.emulators.platform.system", lambda: "Windows")
    monkeypatch.setattr("romarr.emulators.platform.machine", lambda: "ARM64")
    assert not _matches_host(name)


def test_invalid_host_filter_is_rejected(tmp_path):
    """A misspelled host must not silently claim broad support."""
    with pytest.raises(ValueError, match="host platform"):
        EmulatorHub(tmp_path).catalog(host_platform="android-tv-unknown")


def test_android_coverage_count_matches_data():
    """Published Android coverage counts only explicitly checked entries."""
    rows = bundled()["items"]
    assert bundled()["coverage"]["android_verified_entries"] == sum("android_verified_date" in row for row in rows)


def test_android_embedded_host_refuses_desktop_install_before_network(tmp_path, monkeypatch):
    """Linux as reported by Android cannot enable managed desktop binaries."""
    monkeypatch.setenv("ROMARR_ANDROID", "1")
    monkeypatch.setattr("romarr.emulators.platform.system", lambda: "Linux")
    monkeypatch.setattr("romarr.emulators.platform.machine", lambda: "aarch64")
    assert not _matches_host("PPSSPP-Linux-aarch64.AppImage")
    hub = EmulatorHub(tmp_path)
    with pytest.raises(ValueError, match="Android apps manually"):
        hub.install("ppsspp", "PPSSPP-Linux-aarch64.AppImage", "0" * 64)
    assert not hub.root.exists()
