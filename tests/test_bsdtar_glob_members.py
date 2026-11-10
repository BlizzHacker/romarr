"""Bracketed scene names must extract through the bsdtar source (issue #144).

bsdtar parses every trailing member argument as a shell-style glob, so
`Title [010012101468C000][v0][US].nsp` -- the guaranteed shape of every
Switch scene release -- was read as a character class that could never match
itself, and extraction died with "Not found in archive" even though `-tf`
listed the member. `bsdtar_pattern` escapes the metacharacters so the pattern
means the literal name; these tests pin both the escaping and the real
extract, on every path that feeds a member name to bsdtar.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from romarr import library, sniff
from romarr.dat import hash_bytes
from romarr.library import _BsdtarSource, bsdtar_pattern

SCENE = "Metroid Prime REMASTERED [010012101468C000][v0][US].nsp"
BODY = b"PFS0" + b"\x11" * 4096


@pytest.mark.parametrize("name,expected", [
    (SCENE, r"Metroid Prime REMASTERED \[010012101468C000\]\[v0\]\[US\].nsp"),
    ("game [Scene].iso", r"game \[Scene\].iso"),
    ("a*b?c[d].bin", r"a\*b\?c\[d\].bin"),
    ("back\\slash.nes", "back\\\\slash.nes"),
    ("plain name.smc", "plain name.smc"),
    ("", ""),
])
def test_bsdtar_pattern_escapes_glob_metachars(name, expected):
    assert bsdtar_pattern(name) == expected


def test_sniff_uses_the_same_escaping(monkeypatch):
    """sniff streams a member through bsdtar too -- it must escape as well."""
    seen = []

    class FakeProc:
        stdout = None

    def fake_popen(cmd, **kw):
        seen.append(cmd)
        raise OSError("stop here")

    monkeypatch.setattr(library, "bsdtar_path", lambda: "/usr/bin/bsdtar")
    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **kw: type(
                            "R", (), {"returncode": 0, "stdout": SCENE})())
    sniff._identify_via_bsdtar(Path("game.7z"))
    assert seen and seen[0][-1] == bsdtar_pattern(SCENE)


bsdtar = shutil.which("bsdtar")


@pytest.mark.skipif(bsdtar is None, reason="bsdtar not installed")
@pytest.mark.skipif(os.name == "nt", reason="fake-archive member naming")
def test_bracketed_member_round_trips(tmp_path: Path):
    """read, copy and hash all reach a bracketed member inside a real archive."""
    (tmp_path / SCENE).write_bytes(BODY)
    archive = tmp_path / "Metroid Prime REMASTERED.7z"
    # bsdtar writes the container by extension; .tar keeps this hermetic
    # (no 7z codec dependency) while preserving the member-name semantics
    # that broke -- the glob matching is on the member argument, not the
    # container format.
    tarball = tmp_path / "game.tar"
    subprocess.run([bsdtar, "-cf", str(tarball), "-C", str(tmp_path), SCENE],
                   check=True)
    src = _BsdtarSource(tarball, bsdtar)

    assert SCENE in src.names()
    assert src.read(SCENE) == BODY

    out = tmp_path / "extracted.nsp"
    src.copy(SCENE, out)
    assert out.read_bytes() == BODY

    assert src.hash(SCENE) == hash_bytes(BODY)


@pytest.mark.skipif(bsdtar is None, reason="bsdtar not installed")
@pytest.mark.skipif(os.name == "nt", reason="fake-archive member naming")
def test_unescaped_name_fails_without_the_fix(tmp_path: Path):
    """The bug is real on this bsdtar: the raw name must NOT match.

    If libarchive ever changes its pattern semantics this test fails loudly
    instead of silently testing nothing.
    """
    (tmp_path / SCENE).write_bytes(BODY)
    tarball = tmp_path / "game.tar"
    subprocess.run([bsdtar, "-cf", str(tarball), "-C", str(tmp_path), SCENE],
                   check=True)
    proc = subprocess.run([bsdtar, "-xOf", str(tarball), SCENE],
                          capture_output=True)
    assert proc.returncode != 0
    assert b"Not found in archive" in proc.stderr
