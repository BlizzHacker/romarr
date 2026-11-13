"""Native emulator package selection must not confuse host or symbol labels."""
import pytest

from romarr import emulators


@pytest.mark.parametrize("system,arch,name,wanted", [
    ("Windows", "AMD64", "emulator-darwin-x64.zip", False),
    ("Windows", "AMD64", "emulator-win-x64.zip", True),
    ("Windows", "AMD64", "emulator-win64.zip", True),
    ("Windows", "AMD64", "emulator-windows-x64.zip", True),
    ("Linux", "x86_64", "melonDS-1.1-appimage-x86_64.zip", True),
    ("Linux", "x86_64", "melonDS-1.1-AppImage-x86_64.zip", True),
    ("Linux", "x86_64", "DuckStation-x64.AppImage", True),
    ("Linux", "aarch64", "DuckStation-armhf.AppImage", False),
    ("Linux", "aarch64", "DuckStation-arm32.AppImage", False),
    ("Linux", "aarch64", "DuckStation-armv7l.AppImage", False),
    ("Linux", "aarch64", "DuckStation-arm64.AppImage", True),
    ("Linux", "x86_64", "emulator-linux-i686.zip", False),
    ("Linux", "x86_64", "emulator-linux-x86.zip", False),
    ("Windows", "AMD64", "emulator-win32.zip", False),
    ("Darwin", "arm64", "melonDS-1.1-macOS-universal.zip", True),
    ("Darwin", "arm64", "melonDS-1.1-macOS-universal2.zip", True),
    ("Darwin", "x86_64", "melonDS-1.1-macOS-universal.zip", True),
    ("Darwin", "arm64", "emulator-macos-x64.zip", False),
    ("Darwin", "arm64", "emulator-macos-arm64.zip", True),
    ("Darwin", "x86_64", "emulator-darwin-x64.zip", True),
    ("Linux", "x86_64", "emulator-linux-x86_64-pdb.zip", False),
    ("Linux", "x86_64", "emulator-linux-x86_64-PDBs.zip", False),
    ("Darwin", "arm64", "emulator-macos-universal-dSYMs.tar.gz", False),
    ("Linux", "x86_64", "emulator-linux-x86_64-dbg.zip", False),
    ("Linux", "x86_64", "emulator-linux-x86_64-debug.zip", False),
    ("Linux", "x86_64", "emulator-linux-x86_64-symbols.zip", False),
    ("Linux", "x86_64", "emulator-linux-x86_64-source.zip", False),
    ("Linux", "x86_64", "emulator-linux-x86_64.AppImage.zsync", False),
    ("Linux", "x86_64", "Resources-linux-x86_64.zip", True),
    ("Linux", "x86_64", "debugger-linux-x86_64.zip", True),
    ("Linux", "x86_64", "emulator-notlinux-x86_64.zip", False),
    ("Windows", "AMD64", "emulator-darwin-x64-symbolicator.zip", False),
])
def test_emulator_native_package_labels(monkeypatch, system, arch, name, wanted):
    """Real release label patterns must map only to their intended host."""
    monkeypatch.setattr(emulators.platform, "system", lambda: system)
    monkeypatch.setattr(emulators.platform, "machine", lambda: arch)
    assert emulators._matches_host(name) is wanted
