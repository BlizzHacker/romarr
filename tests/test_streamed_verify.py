"""Verification hashes a member without holding it in memory.

`verify_set` used to `read` every member of a set and hash the bytes. For a
cartridge that is nothing; for a PS3 disc it is the whole image in RAM, and a
multi-disc archive pushed a 92 GB host into its OOM killer. These pin the
streamed path to the exact answers `hash_bytes` gives, headers included.
"""

from __future__ import annotations

import io
import os
import shutil
import subprocess
import time
import zipfile
from pathlib import Path

import pytest

from romarr.dat import UNKNOWN, hash_bytes, hash_stream
from romarr import library
from romarr.library import _PathSource, _ZipSource, verify_set

# (suffix, payload): headered and unheadered variants of every header rule.
CASES = [
    (".bin", bytes(range(256)) * 300),
    (".nes", b"NES\x1a" + b"\x00" * 12 + b"\x11" * 32768),   # iNES header
    (".nes", b"\x22" * 8),                                    # too short to skip
    (".smc", b"\x00" * 512 + b"\x33" * 65536),               # copier header
    (".sfc", b"\x44" * 65536),                               # headerless
    (".lnx", b"LYNX" + b"\x00" * 60 + b"\x55" * 4096),
    (".a78", b"\x00" * 128 + b"\x66" * 4096),
    ("", b""),
]


@pytest.mark.parametrize("suffix,data", CASES)
@pytest.mark.parametrize("chunk", [1, 7, 512, 513, 1 << 20])
def test_hash_stream_matches_hash_bytes(suffix, data, chunk):
    assert hash_stream(io.BytesIO(data), suffix=suffix, chunk=chunk) == \
        hash_bytes(data, suffix=suffix)


@pytest.mark.parametrize("suffix,data", CASES)
def test_sources_hash_like_hash_bytes(tmp_path: Path, suffix, data):
    name = f"game{suffix}"
    (tmp_path / name).write_bytes(data)
    zpath = tmp_path / "game.zip"
    with zipfile.ZipFile(zpath, "w") as z:
        z.writestr(name, data)
    expected = hash_bytes(data, suffix=suffix)
    assert _PathSource(tmp_path / name).hash(name, suffix) == expected
    assert _PathSource(tmp_path).hash(name, suffix) == expected
    assert _ZipSource(zpath).hash(name, suffix) == expected


@pytest.mark.skipif(not shutil.which("bsdtar"), reason="bsdtar not installed")
def test_bsdtar_source_streams(tmp_path: Path):
    data = b"\x00" * 512 + b"\x77" * 65536
    (tmp_path / "game.smc").write_bytes(data)
    archive = tmp_path / "game.tar"
    subprocess.run(["bsdtar", "-cf", str(archive), "-C", str(tmp_path), "game.smc"],
                   check=True)
    src = library._BsdtarSource(archive, shutil.which("bsdtar"))
    assert src.hash("game.smc", ".smc") == hash_bytes(data, suffix=".smc")


class _NoWholeReads(_PathSource):
    def read(self, name):  # a disc image must never come through here
        raise AssertionError("verify_set read a whole member into memory")


def test_verify_set_does_not_read_members_whole(tmp_path: Path):
    (tmp_path / "Game (USA).cue").write_text('FILE "Game (USA).bin" BINARY\n')
    (tmp_path / "Game (USA).bin").write_bytes(b"\x00" * 4096)

    class Dats:
        seen = []

        def lookup(self, **hashes):
            self.seen.append(hashes)
            from romarr.dat import Match
            return Match(UNKNOWN)

    dats = Dats()
    verify_set(_NoWholeReads(tmp_path), ["Game (USA).cue", "Game (USA).bin"], dats)
    assert [h["size"] for h in dats.seen] == [
        len('FILE "Game (USA).bin" BINARY\n'), 4096]


def _fake_bsdtar(tmp_path: Path, body: str) -> str:
    tool = tmp_path / "fake-bsdtar"
    tool.write_text("#!/bin/sh\n" + body + "\n")
    tool.chmod(0o755)
    return str(tool)


@pytest.mark.skipif(os.name == "nt", reason="fake bsdtar helper uses a POSIX shell")
def test_stalled_bsdtar_is_killed_and_reported_unknown(tmp_path: Path):
    """A bsdtar that stops producing output must not hang verification."""
    tool = _fake_bsdtar(tmp_path, "printf 'partial'; exec sleep 60")
    src = library._BsdtarSource(tmp_path / "game.7z", tool)
    src.STALL_TIMEOUT = 1

    started = time.monotonic()
    with pytest.raises(TimeoutError):
        src.hash("game.iso", ".iso")
    assert verify_set(src, ["game.iso"], object()).status == UNKNOWN
    assert time.monotonic() - started < 15


@pytest.mark.skipif(os.name == "nt", reason="fake bsdtar helper uses a POSIX shell")
def test_slow_but_steady_bsdtar_is_not_a_stall(tmp_path: Path):
    """Only silence counts: a stream that keeps trickling is left alone."""
    tool = _fake_bsdtar(tmp_path, "for i in 1 2 3 4; do printf 'abcd'; sleep 0.6; done")
    src = library._BsdtarSource(tmp_path / "game.7z", tool)
    src.STALL_TIMEOUT = 1
    assert src.hash("game.bin", ".bin") == hash_bytes(b"abcd" * 4)


@pytest.mark.skipif(os.name == "nt", reason="fake bsdtar helper uses a POSIX shell")
def test_failing_bsdtar_raises(tmp_path: Path):
    tool = _fake_bsdtar(tmp_path, "exit 3")
    src = library._BsdtarSource(tmp_path / "game.7z", tool)
    with pytest.raises(subprocess.CalledProcessError):
        src.hash("game.bin", ".bin")


@pytest.mark.skipif(os.name == "nt", reason="fake bsdtar helper uses a POSIX shell")
def test_bsdtar_that_closes_stdout_but_hangs_returns_unknown(tmp_path: Path):
    """EOF must not let a still-running bsdtar hang verification."""
    tool = _fake_bsdtar(tmp_path, "exec 1>&-; exec sleep 60")
    src = library._BsdtarSource(tmp_path / "game.7z", tool)
    src.STALL_TIMEOUT = 1

    started = time.monotonic()
    assert verify_set(src, ["game.iso"], object()).status == UNKNOWN
    assert time.monotonic() - started < 15
