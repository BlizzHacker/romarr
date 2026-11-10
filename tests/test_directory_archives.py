import struct
import subprocess
import zipfile
import zlib

import pytest

from romarr.import_intent import preview_import
from romarr.library import bsdtar_path, import_rom
from romarr.platforms import resolve


def make_archive(path, members):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.suffix == '.zip':
        with zipfile.ZipFile(path, 'w') as archive:
            for name, data in members.items():
                archive.writestr(name, data)
    elif path.suffix == '.7z':
        if not bsdtar_path():
            pytest.skip('libarchive unavailable')
        staging = path.parent / 'staging'
        staging.mkdir()
        for name, data in members.items():
            (staging / name).write_bytes(data)
        subprocess.run([bsdtar_path(), '--format=7zip', '-cf', str(path),
                        '-C', str(staging), *members], check=True)
        for entry in staging.iterdir():
            entry.unlink()
        staging.rmdir()
    else:
        if not bsdtar_path():
            pytest.skip('libarchive unavailable')
        def header(kind, flags, body):
            block = struct.pack('<BHH', kind, flags, 7 + len(body)) + body
            return struct.pack('<H', zlib.crc32(block) & 0xffff) + block
        data = b'Rar!\x1a\x07\x00' + header(0x73, 0, b'\x00' * 6)
        for name, payload in members.items():
            encoded = name.encode()
            body = struct.pack('<IIBIIBBHI', len(payload), len(payload), 3,
                               zlib.crc32(payload), 0, 20, 0x30, len(encoded), 0x20)
            data += header(0x74, 0x8000, body + encoded) + payload
        path.write_bytes(data + header(0x7b, 0, b''))
    return path


@pytest.mark.parametrize('suffix', ['.zip', '.7z', '.rar'])
def test_directory_archive_imports_complete_disc_and_preserves_download(tmp_path, suffix):
    download = tmp_path / 'download'
    archive = make_archive(download / 'nested' / ('game' + suffix), {
        'Game.cue': b'FILE "track.bin" BINARY\n', 'track.bin': b'track bytes'})
    (download / 'art.jpg').write_bytes(b'art')
    original = archive.read_bytes()
    preview = preview_import(download, resolve('dc'), tmp_path / 'library')
    assert not preview['requires_review']
    primary = 'nested/game' + suffix + '/Game.cue'
    assert preview['items'][0]['primary'] == primary
    result, = import_rom(download, resolve('dc'), tmp_path / 'library',
                         selected_primaries=[primary])
    assert result.ok, result.reason
    assert (result.destination / 'track.bin').read_bytes() == b'track bytes'
    assert (result.destination / 'Game.cue').read_bytes() == b'FILE "track.bin" BINARY\n'
    assert archive.read_bytes() == original
    assert (download / 'art.jpg').read_bytes() == b'art'
    assert result.import_method == 'extracted'


def test_multiple_archives_and_loose_rom_review_keeps_sources_separate(tmp_path):
    download = tmp_path / 'download'
    for name, payload in [('A', b'a'), ('B', b'b')]:
        make_archive(download / (name + '.zip'), {
            'Game.cue': b'FILE "track.bin" BINARY\n', 'track.bin': payload})
    (download / 'Loose.chd').write_bytes(b'loose')
    preview = preview_import(download, resolve('dc'), tmp_path / 'library')
    assert preview['requires_review']
    assert len(preview['items']) == 3
    result, = import_rom(download, resolve('dc'), tmp_path / 'library',
                         selected_primaries=['B.zip/Game.cue'])
    assert result.ok, result.reason
    assert (result.destination / 'track.bin').read_bytes() == b'b'
    assert len(list((tmp_path / 'library' / 'dc').iterdir())) == 1
    old_token = preview['token']
    make_archive(download / 'B.zip', {'Changed.chd': b'changed'})
    assert preview_import(download, resolve('dc'), tmp_path / 'library')['token'] != old_token
    failed, = import_rom(download, resolve('dc'), tmp_path / 'other',
                         selected_primaries=['B.zip/Game.cue'])
    assert not failed.ok
    assert not (tmp_path / 'other').exists()


def test_unreadable_archive_names_source_and_publishes_nothing(tmp_path):
    download = tmp_path / 'download'
    download.mkdir()
    (download / 'bad.zip').write_bytes(b'broken')
    (download / 'Good.chd').write_bytes(b'good')
    result, = import_rom(download, resolve('dc'), tmp_path / 'library')
    assert not result.ok and 'bad.zip' in result.reason
    assert not (tmp_path / 'library').exists()


@pytest.mark.parametrize("with_extra", [False, True])
def test_directory_source_refuses_symlink_escape(tmp_path, with_extra):
    download = tmp_path / 'download'
    download.mkdir()
    (tmp_path / 'outside.chd').write_bytes(b'outside')
    (download / 'Game.chd').symlink_to(tmp_path / 'outside.chd')
    if with_extra:
        (download / 'readme').write_bytes(b'readme')
    result, = import_rom(download, resolve('dc'), tmp_path / 'library')
    assert not result.ok and 'escapes' in result.reason
    assert not (tmp_path / 'library').exists()


def test_loose_playlist_does_not_hide_archived_game(tmp_path):
    download = tmp_path / 'download'
    make_archive(download / 'Game.zip', {'Archived.chd': b'archived'})
    (download / 'Loose.m3u').write_bytes(b'Loose.chd\n')
    (download / 'Loose.chd').write_bytes(b'loose')
    preview = preview_import(download, resolve('psx'), tmp_path / 'library')
    assert {item['primary'] for item in preview['items']} == {
        'Loose.m3u', 'Game.zip/Archived.chd'}
    assert preview['requires_review']
