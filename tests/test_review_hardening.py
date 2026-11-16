"""Hardening from the 1.11 review: archive integrity, video scoring, cache identity, plugin start-up."""
from __future__ import annotations

import gzip
import io
import json
import subprocess
import sys
import tarfile
import textwrap

import pytest

from romarr import nested_zip
from romarr.profiles import ReleaseProfile
from romarr.tar_archive import TarSource

PAYLOAD = b"ROMDATA-" * 64


def _tar_bytes(members):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as archive:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def test_a_corrupted_gzip_payload_is_refused_by_its_trailer(tmp_path):
    raw = gzip.compress(_tar_bytes({"Game.gba": PAYLOAD}), compresslevel=0)  # stored: payload bytes appear as-is
    at = raw.index(b"ROMDATA-")
    corrupted = raw[:at] + b"X" + raw[at + 1:]
    path = tmp_path / "game.tar.gz"
    path.write_bytes(corrupted)
    with pytest.raises(ValueError):
        TarSource(path)


def test_an_intact_gzip_tar_still_opens(tmp_path):
    path = tmp_path / "game.tar.gz"
    path.write_bytes(gzip.compress(_tar_bytes({"Game.gba": PAYLOAD}), compresslevel=0))
    source = TarSource(path)
    try:
        assert source.names() == ["Game.gba"]
    finally:
        source.close()


def test_solaris_extended_headers_share_the_metadata_budget(tmp_path, monkeypatch):
    monkeypatch.setattr(nested_zip, "MAX_DESCRIPTOR_BYTES", 64)
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.USTAR_FORMAT) as archive:
        header = tarfile.TarInfo("././@Solaris")
        header.type = tarfile.SOLARIS_XHDTYPE
        body = b"30 comment=" + b"x" * 400 + b"\n"
        header.size = len(body)
        archive.addfile(header, io.BytesIO(body))
        info = tarfile.TarInfo("Game.gba")
        info.size = 3
        archive.addfile(info, io.BytesIO(b"rom"))
    path = tmp_path / "solaris.tar"
    path.write_bytes(buf.getvalue())
    with pytest.raises(ValueError, match="metadata"):
        TarSource(path)


def _ntsc():
    return ReleaseProfile(video_standard="ntsc")


def test_a_matching_video_tag_needs_platform_evidence_to_score():
    reasons = [text for _points, text in _ntsc().adjustments("chrono trigger (ntsc)", "psx", platform_evidence=False)]
    assert not any("video standard" in text for text in reasons)
    scored = _ntsc().adjustments("chrono trigger (ntsc)", "psx", platform_evidence=True)
    assert any(points > 0 and "video standard" in text for points, text in scored)


def test_an_opposing_video_tag_costs_points_without_platform_evidence():
    scored = _ntsc().adjustments("chrono trigger (pal)", "psx", platform_evidence=False)
    assert any(points < 0 and "opposes" in text for points, text in scored)


def test_library_rows_are_dropped_when_the_library_server_changes(tmp_path):
    from romarr.app import ROMarr
    svc = ROMarr({"ROMARR_DATA": str(tmp_path / "romarr.json")})
    row = svc.store.put_item("libraries", {"type": "romm", "name": "RomM", "url": "http://romm-a.invalid"})
    svc.reload_libraries()
    svc._library_cache = (["a game from server A"], 1.0, "")
    svc.reload_libraries()                                     # same configuration: rows kept
    assert svc._library_cache[0] == ["a game from server A"]
    svc.store.put_item("libraries", dict(row, url="http://romm-b.invalid"))
    svc.reload_libraries()
    # A background refresh may already hold server B's answer; A's rows must be gone.
    assert "a game from server A" not in (svc._library_cache[0] or [])


def test_plugin_workers_can_import_a_user_site_seccomp_binding(tmp_path):
    pytest.importorskip("rom_hub.broker.host")
    user_site = tmp_path / "user-site"
    user_site.mkdir()
    (user_site / "pyseccomp.py").write_text("")
    script = textwrap.dedent(r"""
        import importlib.util, json, sys
        from importlib.machinery import ModuleSpec
        location = sys.argv[1] + "/pyseccomp.py"
        real = importlib.util.find_spec
        importlib.util.find_spec = lambda name, *a: ModuleSpec("pyseccomp", None, origin=location) if name == "pyseccomp" else real(name, *a)
        from romarr.plugin_safety import install_trusted_startup
        install_trusted_startup()
        import rom_hub.broker.host as host
        print(json.dumps({"included": sys.argv[1] in host.subprocess._bootstrap}))
    """)
    result = subprocess.run([sys.executable, "-c", script, str(user_site.resolve())],
                            capture_output=True, text=True, timeout=40)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.strip().splitlines()[-1]) == {"included": True}


def test_a_refresh_for_a_replaced_library_is_not_published(tmp_path):
    from romarr.app import ROMarr
    svc = ROMarr({"ROMARR_DATA": str(tmp_path / "romarr.json")})
    row = svc.store.put_item("libraries", {"type": "romm", "name": "RomM", "url": "http://romm-a.invalid"})
    svc.reload_libraries()
    started = getattr(svc, "_library_generation", 0)            # a pass begins reading server A
    svc.store.put_item("libraries", dict(row, url="http://romm-b.invalid"))
    svc.reload_libraries()
    before = svc._library_cache
    svc._publish_if_current(started, ["a game from server A"], "", partial=True)
    assert svc._library_cache is before


def test_format_points_need_platform_evidence():
    profile = ReleaseProfile(formats={"psx": ["chd", "cue"]})
    assert not profile.adjustments("chrono trigger (usa).chd", "psx", platform_evidence=False)
    assert profile.adjustments("chrono trigger (usa).chd", "psx", platform_evidence=True)


def _xz_tar(members):
    import lzma
    return lzma.compress(_tar_bytes(members), format=lzma.FORMAT_XZ)


def test_xz_stream_padding_is_accepted(tmp_path):
    import lzma
    raw = _tar_bytes({"Game.gba": PAYLOAD})
    half = len(raw) // 2
    two_streams = (lzma.compress(raw[:half], format=lzma.FORMAT_XZ) + b"\0" * 4
                   + lzma.compress(raw[half:], format=lzma.FORMAT_XZ) + b"\0" * 8)
    path = tmp_path / "game.tar.xz"
    path.write_bytes(two_streams)
    source = TarSource(path)
    try:
        assert source.names() == ["Game.gba"]
    finally:
        source.close()


def test_xz_padding_not_in_fours_is_refused(tmp_path):
    path = tmp_path / "game.tar.xz"
    path.write_bytes(_xz_tar({"Game.gba": PAYLOAD}) + b"\0" * 3)
    with pytest.raises(ValueError):
        TarSource(path)


def test_a_huge_global_pax_header_is_refused(tmp_path):
    buf = io.BytesIO()
    headers = {f"comment.{i}": "x" for i in range(300)}
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.PAX_FORMAT, pax_headers=headers) as archive:
        info = tarfile.TarInfo("Game.gba")
        info.size = 3
        archive.addfile(info, io.BytesIO(b"rom"))
    path = tmp_path / "pax.tar"
    path.write_bytes(buf.getvalue())
    with pytest.raises(ValueError, match="record limit"):
        TarSource(path)
