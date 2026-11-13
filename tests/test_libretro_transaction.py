"""A failed core update preserves the previously usable binary and receipt."""
import io
import json
import zipfile
import zlib
from pathlib import Path

import pytest

from romarr import libretro
from romarr.libretro import CoreHub


class Response:
    """Supply official-shaped package bytes without remote requests."""

    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def raise_for_status(self):
        pass

    def iter_content(self, size):
        yield self.payload


def core_hub(tmp_path, monkeypatch):
    """Build one deterministic replacement core and an official-shaped index."""
    payload = b"replacement-core"
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as package:
        package.writestr("2048_libretro.so", payload)
    crc = format(zlib.crc32(payload) & 0xffffffff, "08x")
    monkeypatch.setattr(libretro, "host_build", lambda: ("linux/x86_64/latest/", ".so"))

    class Session:
        def get(self, url, **kwargs):
            return Response(("2026-11-12 " + crc + " 2048_libretro.so.zip\n").encode()
                            if url.endswith(".index-extended") else archive.getvalue())
    return CoreHub(tmp_path, session=Session())


def prior_installation(hub):
    """Store a working older core and receipt whose exact bytes must survive."""
    binary = hub.root / "cores" / "2048_libretro.so"
    receipt = hub.root / "receipts" / "2048_libretro.json"
    binary.parent.mkdir(parents=True)
    receipt.parent.mkdir()
    binary.write_bytes(b"working-old-core")
    receipt.write_bytes(b'{"sha256":"previous-receipt"}\n')
    return binary, receipt


@pytest.mark.parametrize("existing", [True, False])
def test_receipt_publish_failure_restores_core_and_receipt(tmp_path, monkeypatch, existing):
    """A receipt permission failure cannot leave a new binary with an old ledger."""
    hub = core_hub(tmp_path, monkeypatch)
    if existing:
        binary, receipt = prior_installation(hub)
    else:
        binary = hub.root / "cores" / "2048_libretro.so"
        receipt = hub.root / "receipts" / "2048_libretro.json"
    original_replace = libretro.os.replace

    def fail_receipt(source, destination):
        if Path(destination) == receipt and Path(source).name == "receipt.json":
            raise PermissionError("receipt destination denied")
        return original_replace(source, destination)

    monkeypatch.setattr(libretro.os, "replace", fail_receipt)
    with pytest.raises(PermissionError, match="receipt destination denied"):
        hub.install("2048_libretro")
    if existing:
        assert binary.read_bytes() == b"working-old-core"
        assert receipt.read_bytes() == b'{"sha256":"previous-receipt"}\n'
    else:
        assert not binary.exists()
        assert not receipt.exists()
    assert not list(hub.root.glob(".core-*"))


@pytest.mark.parametrize("operation", ["readiness", "configure"])
def test_setup_failure_happens_before_publishing_core(tmp_path, monkeypatch, operation):
    """A failed firmware check or config write leaves the prior pair untouched."""
    hub = core_hub(tmp_path, monkeypatch)
    binary, receipt = prior_installation(hub)

    def fail(*args):
        raise OSError("setup failed")

    monkeypatch.setattr(hub, operation, fail)
    with pytest.raises(OSError, match="setup failed"):
        hub.install("2048_libretro")
    assert binary.read_bytes() == b"working-old-core"
    assert receipt.read_bytes() == b'{"sha256":"previous-receipt"}\n'
    assert not list(hub.root.glob(".core-*"))


def test_receipt_write_failure_happens_before_publishing_core(tmp_path, monkeypatch):
    """An unwritable staged receipt must fail before touching a working core."""
    hub = core_hub(tmp_path, monkeypatch)
    binary, receipt = prior_installation(hub)

    def fail_sync(fd):
        raise OSError("receipt flush failed")

    monkeypatch.setattr(libretro.os, "fsync", fail_sync)
    with pytest.raises(OSError, match="receipt flush failed"):
        hub.install("2048_libretro")
    assert binary.read_bytes() == b"working-old-core"
    assert receipt.read_bytes() == b'{"sha256":"previous-receipt"}\n'
    assert not list(hub.root.glob(".core-*"))


def test_failed_rollback_retains_recovery_files(tmp_path, monkeypatch):
    """A second filesystem failure cannot delete the only saved working binary."""
    hub = core_hub(tmp_path, monkeypatch)
    binary, receipt = prior_installation(hub)
    original_replace = libretro.os.replace

    def fail_publish_and_rollback(source, destination):
        if Path(source).name in ("receipt.json", "previous-core"):
            raise PermissionError("filesystem refused update")
        return original_replace(source, destination)

    monkeypatch.setattr(libretro.os, "replace", fail_publish_and_rollback)
    with pytest.raises(OSError, match="recovery copies retained"):
        hub.install("2048_libretro")
    recovery = list(hub.root.glob(".core-*"))
    assert len(recovery) == 1
    assert (recovery[0] / "RECOVERY.txt").is_file()
    assert (recovery[0] / "previous-core").read_bytes() == b"working-old-core"
    assert (recovery[0] / "previous-receipt.json").read_bytes() == receipt.read_bytes()


def test_successful_update_publishes_matching_pair(tmp_path, monkeypatch):
    """The successful path replaces the old binary and records its new digest."""
    import hashlib

    hub = core_hub(tmp_path, monkeypatch)
    binary, receipt = prior_installation(hub)
    result = hub.install("2048_libretro")
    assert binary.read_bytes() == b"replacement-core"
    assert json.loads(receipt.read_text())["sha256"] == hashlib.sha256(binary.read_bytes()).hexdigest()
    assert result["ok"]
    assert not list(hub.root.glob(".core-*"))
