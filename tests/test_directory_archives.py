from pathlib import Path
import subprocess
import zipfile

import pytest

from romarr.import_intent import preview_import
from romarr.library import bsdtar_path, import_rom
from romarr.platforms import resolve
from tests.test_import_naming import dats_for


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
        path.write_bytes(Path(__file__).with_name('fixtures').joinpath('disc.rar').read_bytes())
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


@pytest.mark.parametrize('overwrite', [False, True])
@pytest.mark.parametrize('disc', [False, True])
def test_colliding_archive_destinations_publish_nothing(tmp_path, overwrite, disc):
    download = tmp_path / 'download'
    originals = {}
    for title, payload in [('A', b'a'), ('B', b'b')]:
        members = {'Game.cue': b'FILE "track.bin" BINARY\n', 'track.bin': payload} if disc else {'Game.gb': payload}
        archive = make_archive(download / (title + '.zip'), members)
        originals[archive] = archive.read_bytes()
    primary = 'Game.cue' if disc else 'Game.gb'
    result, = import_rom(download, resolve('dc' if disc else 'gb'), tmp_path / 'library',
                         selected_primaries=['A.zip/' + primary, 'B.zip/' + primary],
                         overwrite=overwrite)
    assert not result.ok
    assert 'same destination' in result.reason
    assert 'A.zip/' + primary in result.reason
    assert 'B.zip/' + primary in result.reason
    assert not (tmp_path / 'library').exists()
    assert all(archive.read_bytes() == original for archive, original in originals.items())


@pytest.mark.parametrize('distinct_names', [False, True])
def test_archive_collisions_use_verified_destination_names(tmp_path, distinct_names):
    download = tmp_path / 'download'
    make_archive(download / 'A.zip', {('Game.gb' if distinct_names else 'First.gb'): b'a'})
    make_archive(download / 'B.zip', {('Game.gb' if distinct_names else 'Second.gb'): b'b'})
    second_name = 'Other.gb' if distinct_names else 'Canonical.gb'
    dats = dats_for([('Canonical.gb', b'a'), (second_name, b'b')])
    results = import_rom(download, resolve('gb'), tmp_path / 'library', dats=dats, rename=True)
    if distinct_names:
        assert all(result.ok for result in results)
        assert (tmp_path / 'library/gb/Canonical.gb').read_bytes() == b'a'
        assert (tmp_path / 'library/gb/Other.gb').read_bytes() == b'b'
    else:
        assert len(results) == 1 and not results[0].ok
        assert 'same destination' in results[0].reason
        assert not (tmp_path / 'library').exists()


@pytest.mark.parametrize('distinct_routes', [False, True])
def test_archive_collisions_use_reviewed_destinations(tmp_path, distinct_routes):
    download = tmp_path / 'download'
    for title, payload in [('A', b'a'), ('B', b'b')]:
        make_archive(download / (title + '.zip'), {'Game.gb': payload})
    routes = {'A.zip/Game.gb': 'A/Game.gb',
              'B.zip/Game.gb': ('B' if distinct_routes else 'A') + '/Game.gb'}
    results = import_rom(download, resolve('gb'), tmp_path / 'library',
                         selected_primaries=list(routes), bundle_routes=routes)
    if distinct_routes:
        assert all(result.ok for result in results)
        assert (tmp_path / 'library/gb/A/Game.gb').read_bytes() == b'a'
        assert (tmp_path / 'library/gb/B/Game.gb').read_bytes() == b'b'
    else:
        assert len(results) == 1 and not results[0].ok
        assert 'same destination' in results[0].reason
        assert not (tmp_path / 'library').exists()


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
