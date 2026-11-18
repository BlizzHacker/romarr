"""The image must carry a reader for split RAR sets (#224).

bsdtar reads one RAR volume at a time, so usenet releases split into
.part1.rar/.r00/.001 volumes need 7-Zip (`7zz`) to import at all.
"""

from __future__ import annotations

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]


def test_the_image_installs_a_pinned_7zz():
    text = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "/usr/local/bin/7zz" in text, "the image no longer installs 7-Zip"
    assert "sha256sum -c" in text, "the 7-Zip download is not checksum-pinned"
    for arch in ("amd64", "arm64", "arm)"):
        assert arch in text, f"no 7-Zip build pinned for {arch}"
    assert len(re.findall(r"s=[0-9a-f]{64}", text)) >= 3
    assert "License.txt" in text, "7-Zip's licence must ship with the binary"


def test_7zz_is_a_name_romarr_looks_for():
    source = (ROOT / "romarr" / "library.py").read_text(encoding="utf-8")
    assert '"7zz"' in source
