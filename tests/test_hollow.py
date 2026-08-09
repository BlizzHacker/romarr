"""A file that is the right size and contains nothing.

Found in a real library while working out why some PlayStation images could
not be identified: seven of them, 81 to 669 MB, were zeros from the first byte
to the last 64 KB. 3.1 GB of games that look complete by name and by size and
would never boot. Nothing in ROMarr noticed, because every check upstream asks
what a file *is* rather than whether it contains anything.

That is what an interrupted download leaves when the client pre-allocates the
file and dies: real size, no content.
"""

from __future__ import annotations

import pytest

from romarr.sniff import looks_hollow

BIG = 20 * 1024 * 1024      # over the size floor
PROBE = 64 * 1024


def write(path, size, *, content_at=()):
    """A sparse-ish file of `size` bytes with content only where asked."""
    with path.open("wb") as handle:
        handle.truncate(size)
        for offset in content_at:
            handle.seek(offset)
            handle.write(b"\xde\xad\xbe\xef" * (PROBE // 4))
    return path


def test_an_image_with_content_only_at_the_end_is_flagged(tmp_path):
    """The exact shape of all seven: allocated, then abandoned."""
    path = write(tmp_path / "Game.bin", BIG, content_at=[BIG - PROBE])
    verdict = looks_hollow(path)
    assert verdict is not None
    assert "interrupted download" in verdict


def test_a_completely_empty_image_is_flagged(tmp_path):
    assert looks_hollow(write(tmp_path / "Game.bin", BIG)) is not None


def test_a_healthy_image_is_left_alone(tmp_path):
    """Content throughout, which is every working game in the library."""
    path = write(tmp_path / "Game.bin", BIG,
                 content_at=[0, BIG // 4, BIG // 2, (BIG * 3) // 4,
                             BIG - PROBE])
    assert looks_hollow(path) is None


def test_content_at_the_start_is_enough_to_stay_quiet(tmp_path):
    """Long runs of zeros are ordinary inside a disc image. Only a file that
    is empty *at the front* is worth suspecting, so this must not fire on a
    game with a large empty middle."""
    path = write(tmp_path / "Game.bin", BIG, content_at=[0])
    assert looks_hollow(path) is None


def test_small_files_are_not_judged(tmp_path):
    """A BIOS is a few hundred KB and mostly content; the probe says nothing
    useful about it. The library's scph*.bin files must not be flagged."""
    path = write(tmp_path / "scph1001.bin", 512 * 1024)
    assert looks_hollow(path) is None


def test_a_missing_file_is_not_an_error(tmp_path):
    assert looks_hollow(tmp_path / "nope.bin") is None


def test_a_directory_is_not_an_error(tmp_path):
    (tmp_path / "adir").mkdir()
    assert looks_hollow(tmp_path / "adir") is None


def test_it_seeks_rather_than_reading_the_whole_file(tmp_path):
    """Against 25 real images totalling several gigabytes this took 0.0s.
    Reading them would have taken minutes."""
    import time
    path = write(tmp_path / "Game.bin", 200 * 1024 * 1024,
                 content_at=[200 * 1024 * 1024 - PROBE])
    started = time.monotonic()
    assert looks_hollow(path) is not None
    assert time.monotonic() - started < 2


def test_the_verdict_says_how_big_the_file_claims_to_be(tmp_path):
    """"Empty" is not actionable on its own; the size is what makes it
    obvious the download never finished."""
    path = write(tmp_path / "Game.bin", BIG)
    assert "20 MB" in looks_hollow(path)


def test_formats_that_are_sparse_by_nature_are_never_flagged(tmp_path):
    """CD subchannel data is largely empty when perfectly healthy.

    Found in a real library: a .sub beside a game matched the broken-download
    pattern exactly, and calling it a failed download would have been wrong
    every time. The pattern is only evidence for formats that carry content.
    """
    for suffix in ('.sub', '.swp', '.sbi'):
        path = write(tmp_path / ('data' + suffix), BIG)
        assert looks_hollow(path) is None, suffix
