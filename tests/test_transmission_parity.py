"""Transmission attachment, progress and soundtrack identity boundaries."""
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import Mock
import threading

import pytest

from romarr.clients import QBittorrent, QbitConfig
from romarr.downloaders import Transmission, match_queue_item
from romarr.queue_progress import annotate
from romarr.store import QueueItem
from test_collection_torrent import _service
from test_transmission_completion import HASH, client, torrent, success

MAGNET = 'magnet:?xt=urn:btih:' + HASH


@pytest.mark.parametrize("magnet,release_fault", [(MAGNET, False), ("magnet:?xt=broken", True)])
def test_direct_magnet_rejection_only_marks_invalid_release(tmp_path, monkeypatch, magnet, release_fault):
    """A syntactically valid release remains eligible after a client rejection."""
    from romarr.selection import Release

    service = _service(tmp_path, monkeypatch)
    c = client()
    c._rpc_version = 17
    c._call = Mock(return_value={"result": "error"})
    service.clients = [c]
    service.prowlarr = SimpleNamespace(is_prowlarr_download_url=lambda _: False)
    release = Release("Demo (USA).sfc", 100, 1, (), magnet, "torrent")
    assert not service.grab(release, "Demo", "snes")["ok"]
    assert service.queue[-1].release_fault is release_fault


@pytest.mark.parametrize('version', [16, 17])
def test_attach_preserves_external_directory_and_labels(version):
    """An unowned magnet can be claimed in its existing download directory."""
    c = client()
    c._rpc_version = version
    before = torrent(labels=['personal'], downloadDir='/existing')
    after = dict(before, labels=['personal', 'romarr'])
    c._call = Mock(side_effect=[success(**{'torrent-duplicate': {'hashString': HASH}}),
                               success(torrents=[before]), success(), success(torrents=[after])])
    assert c.add(MAGNET)
    assert c.last_job_id == HASH
    assert c._call.call_args_list[2].args == ('torrent-set', {'ids': [HASH], 'labels': ['personal', 'romarr']})
    c._call = Mock(return_value=success(torrents=[after]))
    assert c.completed()[0]['content_path'] == '/existing/Demo.sfc'


@pytest.mark.parametrize('confirmation', ['wrong-directory', 'missing-label', 'error', 'exception'])
def test_failed_attach_rolls_back_only_its_label(confirmation):
    """Failure preserves unrelated labels, including concurrently added labels."""
    c = client()
    before = torrent(labels=['personal'])
    reply = {'wrong-directory': success(torrents=[torrent(downloadDir='/moved')]),
             'missing-label': success(torrents=[before]), 'error': {'result': 'error'},
             'exception': RuntimeError('disconnected')}[confirmation]
    c._call = Mock(side_effect=[success(**{'torrent-duplicate': {'hashString': HASH}}),
                               success(torrents=[before]), success(), reply,
                               success(torrents=[torrent(labels=['personal', 'new', 'romarr'])]), success()])
    assert not c.add(MAGNET)
    assert not c.last_job_id
    assert c._call.call_args.args == ('torrent-set', {'ids': [HASH], 'labels': ['personal', 'new']})


def test_two_requests_cannot_attach_the_same_torrent():
    """Concurrent submissions through one client claim a torrent once."""
    c = client()
    labels = []
    def call(method, arguments):
        """Model the daemon's current labels across serialized requests."""
        if method == 'torrent-add':
            return success(**{'torrent-duplicate': {'hashString': HASH}})
        if method == 'torrent-set':
            labels[:] = arguments['labels']
            return success()
        return success(torrents=[torrent(labels=list(labels))])
    c._call = call
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: c.add(MAGNET), range(2)))
    assert sorted(results) == [False, True]
    assert labels == ['romarr']


def test_progress_uses_identity_category_and_shared_deadline():
    """Progress cannot leak from another category or malformed RPC values."""
    c = client()
    c.romarr_id = 'transmission-one'
    c._call = Mock(return_value=success(torrents=[
        torrent(percentDone=.42, eta=90, rateDownload=2048, status=4),
        torrent(hashString='b'*40, percentDone=2, eta=-2, rateDownload=-10),
        torrent(hashString='c'*40, percentDone=float('nan')),
        torrent(hashString='d'*40, labels=['other']), None]))
    service = SimpleNamespace(_lock=threading.RLock(), clients=[c])
    row = {'state': 'grabbed', 'download_client': c.romarr_id, 'download_id': HASH}
    annotated = annotate(service, [row])[0]
    assert annotated['progress'] == 42
    assert annotated['speed'] == 2048 and annotated['eta'] == 90
    assert isinstance(c._call.call_args.kwargs['_deadline'], float)
    progress = c.progress()
    assert len(progress) == 2
    assert progress['b'*40]['eta'] is None and progress['b'*40]['speed'] == 0


@pytest.mark.parametrize('directory', ['relative', '/downloads/../other', '/bad\x00dir'])
def test_unsafe_download_directory_cannot_be_adopted_or_imported(directory):
    """Remote paths are validated before ownership or import changes."""
    c = client()
    row = torrent(downloadDir=directory, labels=[])
    c._call = Mock(side_effect=[success(**{'torrent-duplicate': {'hashString': HASH}}),
                               success(torrents=[row])])
    assert not c.add(MAGNET)
    assert [call.args[0] for call in c._call.call_args_list] == ['torrent-add', 'torrent-get']
    assert c._content_path(row) == ''


def test_soundtrack_watch_refuses_shared_root_and_is_scoped_to_client(tmp_path):
    """A completed hash cannot import another client's files or a shared root."""
    from romarr.soundtracks import Soundtracks

    store = Soundtracks(tmp_path / 'soundtracks.db')
    store.add_watch('42', 'transmission:' + HASH, 'Album', client_id='owner')
    store._import_torrent = Mock()
    row = {'hash': HASH, 'progress': 1, 'save_path': str(tmp_path), 'content_path': ''}
    resolve = lambda _: ({'id': '42'}, Mock())
    store.process_watches([row], resolve, lambda path: path, client_id='other')
    assert store.watch_for('42')['state'] == 'downloading'
    store.process_watches([row], resolve, lambda path: path, client_id='owner')
    assert store.watch_for('42')['state'] == 'failed'
    assert 'safe torrent payload path' in store.watch_for('42')['message']
    store._import_torrent.assert_not_called()


def test_unlabelled_queue_ownership_is_unique_and_client_scoped(tmp_path, monkeypatch):
    """An unlabelled torrent needs one active, concrete match on its own client."""
    service = _service(tmp_path, monkeypatch)
    c = service._build_client({'type': 'transmission', 'host': 'fixture', 'id': 'owner'})
    c.romarr_id = 'owner'
    c._rpc_version = 16
    row = torrent(labels=[], status=6)
    c._call = Mock(return_value=success(torrents=[row]))
    title_only = QueueItem('Demo', 'snes', row['name'], 1, 'grabbed', download_client='owner')
    wrong_client = QueueItem('Demo', 'snes', row['name'], 1, 'grabbed',
                             download_client='other', download_id=HASH)
    service.queue = [title_only, wrong_client]
    assert c.completed() == [] and c.progress() == {}

    owner = QueueItem('Demo', 'snes', 'different title', 1, 'grabbed',
                      download_client='owner', release_id='btih:' + HASH)
    service.store.enqueue(owner)
    conflicting = QueueItem('Demo', 'snes', 'different download', 1, 'grabbed',
                            download_client='owner', release_id='btih:' + HASH, download_id='b' * 40)
    service.store.enqueue(conflicting)
    done, = c.completed()
    assert match_queue_item(service.queue, done, client_id='owner') is owner
    service.clients = [c]
    rows = annotate(service, service.store.queue_rows())
    assert rows[2]['progress'] == 100
    assert 'progress' not in rows[3]
    assert match_queue_item([title_only, wrong_client], done, client_id='owner') is None
    assert match_queue_item([owner, owner], done, client_id='owner') is None

    c._call.return_value = success(torrents=[dict(row, labels=['foreign'])])
    assert c.completed() == [] and c.progress() == {}
    c._call.return_value = success(**{'torrent-duplicate': {'hashString': HASH}})
    c._call.reset_mock()
    assert not c.add(MAGNET, category='romarr-soundtracks')
    assert [call.args[0] for call in c._call.call_args_list] == ['torrent-add']


@pytest.mark.parametrize('incomplete', [
    {'metadataPercentComplete': 0}, {'status': 2},
    {'files': [{'name': 'Album/01.mp3', 'bytesCompleted': 20}]},
])
def test_soundtrack_watch_waits_for_the_complete_payload(tmp_path, incomplete):
    """A wanted percentage of 100 cannot prematurely fail an incomplete watch."""
    from romarr.soundtracks import Soundtracks

    store = Soundtracks(tmp_path / 'soundtracks.db')
    store.add_watch('42', 'transmission:' + HASH, 'Album', client_id='owner')
    store._import_torrent = Mock()
    c = client()
    c._call = Mock(return_value=success(torrents=[torrent(**incomplete)]))
    resolve = lambda _: ({'id': '42'}, Mock())
    store.process_watches(c.torrents(), resolve, str, client_id='owner')
    assert store.watch_for('42')['state'] == 'downloading'
    store._import_torrent.assert_not_called()
    c._call.return_value = success(torrents=[torrent(files=[{'name': 'Album/01.mp3'}])])
    store.process_watches(c.torrents(), resolve, str, client_id='owner')
    assert store._import_torrent.call_args.args[3] == '/downloads/romarr/Album/01.mp3'


def test_soundtrack_poll_continues_after_another_client_fails(tmp_path, monkeypatch):
    """A disconnected qBittorrent cannot starve a live Transmission watch."""
    service = _service(tmp_path, monkeypatch)
    qbit = QBittorrent(QbitConfig(base_url='http://fixture'))
    qbit.romarr_id = 'offline'
    qbit.torrents = Mock(side_effect=RuntimeError('offline'))
    transmission = client()
    transmission.romarr_id = 'online'
    transmission._call = Mock(return_value=success(torrents=[torrent(labels=['romarr-soundtracks'])]))
    service.clients = [qbit, transmission]
    service._soundtrack_libraries = lambda: [('scope',)]
    store = Mock()
    store.active_watches.return_value = [{'client_id': 'offline'}, {'client_id': 'online'}]
    service.soundtrack_store = lambda _: store
    completed = threading.Event()
    store.process_watches.side_effect = lambda *args, **kwargs: completed.set()
    assert service.soundtrack_watch_tick() == '2 soundtrack client watches queued'
    assert completed.wait(2)
    assert store.process_watches.call_args.kwargs['client_id'] == 'online'
