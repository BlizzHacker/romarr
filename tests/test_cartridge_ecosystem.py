"""Tests for Cartridge player auto-install and plug-and-play emulator ecosystem integration."""
from pathlib import Path
import hashlib
import json
import pytest

from romarr.cartridge import CartridgeHub, CARTRIDGE_EXPECTED_SHA256, CARTRIDGE_EXE_NAME


class MockResponse:
    def __init__(self, content: bytes, status_code: int = 200):
        self.content = content
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise ValueError(f"HTTP {self.status_code}")

    def iter_content(self, chunk_size=1024):
        yield self.content

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


class MockSession:
    def __init__(self, content: bytes):
        self.content = content

    def get(self, url, **kwargs):
        return MockResponse(self.content)


def test_cartridge_hub_status(tmp_path):
    hub = CartridgeHub(tmp_path)
    status = hub.status()
    assert status["installed"] is False
    assert status["download_url"].startswith("https://")
    assert "windows" in status["platforms"]
    assert "xbox" in status["platforms"]
    assert "android" in status["platforms"]
    assert status["platforms"]["xbox"]["store_id"] == "9MXC51W17LH4"
    assert "BlizzHacker/Cartridge" in status["platforms"]["android"]["repo"]


def test_cartridge_hub_install_with_valid_checksum(tmp_path):
    # Dummy binary matching expected SHA256
    dummy_payload = b"dummy cartridge binary content"
    real_sha = hashlib.sha256(dummy_payload).hexdigest()

    hub = CartridgeHub(tmp_path, session=MockSession(dummy_payload))
    # Temporarily monkeypatch expected sha for the dummy test
    import romarr.cartridge
    orig_sha = romarr.cartridge.CARTRIDGE_EXPECTED_SHA256
    romarr.cartridge.CARTRIDGE_EXPECTED_SHA256 = real_sha

    scanned = []
    try:
        result = hub.install(scanner=lambda p: scanned.append(p))
        assert result["ok"] is True
        assert result["version"] == "0.2.0"
        assert len(scanned) == 1
        assert (tmp_path / "tools" / "cartridge" / "Cartridge.exe").is_file()
        assert (tmp_path / "tools" / "cartridge" / "cartridge-install.json").is_file()

        # Re-running install reports already_installed
        res2 = hub.install()
        assert res2["already_installed"] is True
    finally:
        romarr.cartridge.CARTRIDGE_EXPECTED_SHA256 = orig_sha


def test_cartridge_hub_install_rejects_checksum_mismatch(tmp_path):
    hub = CartridgeHub(tmp_path, session=MockSession(b"corrupt content"))
    with pytest.raises(ValueError, match="SHA256 checksum mismatch"):
        hub.install()


def test_cartridge_plug_and_play_setup(tmp_path):
    hub = CartridgeHub(tmp_path)
    result = hub.plug_and_play_setup()
    assert result["ok"] is True
    assert result["ready"] is True
    assert result["systems_configured"] >= 10

    manifest = tmp_path / "plug-and-play.json"
    assert manifest.is_file()
    data = json.loads(manifest.read_text(encoding="utf-8"))
    assert data["configured"] is True
    assert "retroarch" in data
    assert (tmp_path / "tools" / "retroarch" / "cores").is_dir()
    assert (tmp_path / "tools" / "retroarch" / "saves").is_dir()


def test_cartridge_hub_launch_not_installed(tmp_path):
    hub = CartridgeHub(tmp_path)
    with pytest.raises(FileNotFoundError, match="Cartridge player is not installed yet"):
        hub.launch()


def test_cartridge_hub_launch_installed(tmp_path, monkeypatch):
    hub = CartridgeHub(tmp_path)
    cartridge_exe = tmp_path / "tools" / "cartridge" / "Cartridge.exe"
    cartridge_exe.parent.mkdir(parents=True, exist_ok=True)
    cartridge_exe.write_bytes(b"dummy exe")

    spawned = []
    monkeypatch.setattr("subprocess.Popen", lambda args, cwd: spawned.append((args, cwd)))

    result = hub.launch(port=6868, game_id="super-mario-64")
    assert result["ok"] is True
    assert result["launched"] is True
    assert len(spawned) == 1
    args, cwd = spawned[0]
    assert "--game" in args
    assert "super-mario-64" in args
