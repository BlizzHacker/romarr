from unittest.mock import Mock
import pytest
from romarr.downloaders import Transmission, TransmissionConfig, build_client

HASH = "a" * 40

def client():
    """Client."""
    c = Transmission(TransmissionConfig(base_url="http://localhost:9091", category="romarr", save_path="/downloads/romarr"))
    c._rpc_version = 17
    return c

def torrent(**changes):
    """Torrent."""
    changes["files"] = [{"length": 100, "bytesCompleted": 100, **entry}
                        for entry in changes.get("files", [{"name": "Demo.sfc"}])]
    return {"hashString": HASH, "name": "Demo.sfc", "labels": ["romarr"], "downloadDir": "/downloads/romarr", "leftUntilDone": 0, "percentDone": 1, "totalSize": 100, "metadataPercentComplete": 1, "files": [{"name": "Demo.sfc"}], **changes}

@pytest.mark.parametrize("changes", [{}, {"labels":["romarr-collection-existing"]}, {"hashString":"b"*40}, {"downloadDir":""}])
def test_foreign_duplicates_are_rejected_without_modification(changes):
    """Foreign duplicates are rejected without modification."""
    c = client()
    c._call = Mock(side_effect=[{"result":"success", "arguments":{"torrent-duplicate":{"hashString":HASH}}}, {"result":"success", "arguments":{"torrents":[torrent(**changes)]}}])
    assert not c.add("magnet:?xt=urn:btih:" + HASH)
    assert c.last_job_id == ""
    assert [call.args[0] for call in c._call.call_args_list] == ["torrent-add", "torrent-get"]

def test_failed_submission_clears_previous_identity():
    """Failed submission clears previous identity."""
    c = client(); c.last_job_id = HASH
    c._call = Mock(return_value={"result":"error"})
    assert not c.add("magnet:demo") and not c.last_job_id

def test_failed_polling_is_not_silently_reported_as_empty():
    """Failed polling is not silently reported as empty."""
    c = client(); c._call = Mock(return_value={"result":"error"})
    with pytest.raises(RuntimeError): c.completed()


@pytest.mark.parametrize("version", [16, 17])
def test_concurrent_submissions_keep_their_own_identity(version):
    """Concurrent submissions keep their own identity."""
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    c = client()
    c._rpc_version = None
    barrier = Barrier(2, timeout=10)
    def call(method, arguments):
        """Call."""
        if method == 'session-get':
            return success(**{'rpc-version': version})
        if method == 'torrent-get':
            return success(torrents=[torrent(hashString=arguments['ids'][0])])
        if method == 'torrent-set':
            return success()
        return success(**{'torrent-added': {'hashString': arguments['filename']}})
    c._call = call
    def submit(identity):
        """Submit."""
        if not c.add(identity):
            barrier.abort()
            raise AssertionError(f"submission {identity[:6]} failed")
        barrier.wait()
        return c.last_job_id
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert list(pool.map(submit, ["a" * 40, "b" * 40])) == ["a" * 40, "b" * 40]


def test_transmission_file_stats_are_normalized_and_selections_are_explicit():
    """Transmission file stats are normalized and selections are explicit."""
    c = client()
    c._call = Mock(return_value={"result": "success", "arguments": {"torrents": [{
        "hashString": HASH,
        "files": [{"name": "Set/A.bin", "length": 100}, {"name": "Set/B.bin", "length": 200}],
        "fileStats": [{"bytesCompleted": 100, "wanted": True, "priority": -1},
                      {"bytesCompleted": 50, "wanted": False, "priority": 1}]}]}})
    assert c.torrent_files(HASH) == [
        {"index": 0, "name": "Set/A.bin", "size": 100, "progress": 1.0, "priority": 2, "native_priority": -1},
        {"index": 1, "name": "Set/B.bin", "size": 200, "progress": 0.25, "priority": 0, "native_priority": 1}]
    c._call.return_value = {"result": "success"}
    assert c.set_file_priorities(HASH, {0: 2, 1: 0}, native_priorities={0: -1, 1: 1})
    assert c._call.call_args.args == ("torrent-set", {"ids": [HASH], "files-unwanted": [1],
                                                       "priority-low": [0], "priority-high": [1],
                                                       "files-wanted": [0]})


def test_transmission_rejects_unrepresentable_priority():
    """Transmission rejects unrepresentable priority."""
    c = client()
    with pytest.raises(ValueError, match="cannot represent"):
        c.set_file_priorities(HASH, {0: 7})


def test_transmission_waits_for_stop_before_selection(monkeypatch):
    """Transmission waits for stop before selection."""
    c = client()
    c._call = Mock(side_effect=[{"result": "success"},
        {"result": "success", "arguments": {"torrents": [{"hashString": HASH, "status": 4}]}},
        {"result": "success", "arguments": {"torrents": [{"hashString": HASH, "status": 0}]}}])
    monkeypatch.setattr("romarr.downloaders.time.sleep", lambda _: None)
    assert c.pause_torrent(HASH)
    assert [call.args[0] for call in c._call.call_args_list] == ["torrent-stop", "torrent-get", "torrent-get"]


def test_invalid_saved_native_priority_does_not_send_request():
    """Invalid saved native priority does not send request."""
    c = client()
    c._call = Mock()
    with pytest.raises(ValueError, match="invalid saved"):
        c.set_file_priorities(HASH, {0: 1}, native_priorities={0: 9})
    c._call.assert_not_called()


@pytest.mark.parametrize('files,relative', [
    ([{'name': 'Actual.sfc'}], 'Actual.sfc'),
    ([{'name': 'Set/A.cue'}, {'name': 'Set/A.bin'}], 'Set'),
    ([{'name': r'Set\A.cue'}, {'name': r'Set\A.bin'}], 'Set'),
])
def test_completion_uses_actual_files(files, relative):
    """Completion uses actual files."""
    c = client()
    c._call = Mock(return_value={'result': 'success', 'arguments': {'torrents': [
        torrent(name='Display title', files=files)]}})
    row, = c.completed()
    assert row['content_path'] == '/downloads/romarr/' + relative
    assert row['name'] == 'Display title'


@pytest.mark.parametrize('files', [
    [], [{'name': ''}], [{'name': '../Game.sfc'}], [{'name': '/Game.sfc'}],
    [{'name': r'C:\Game.sfc'}], [{'name': 'Set/./Game.sfc'}],
    [{'name': 'Set//Game.sfc'}], [{'name': 'Set/Game\x00.sfc'}],
    [{'name': 'A.cue'}, {'name': 'A.bin'}],
    [{'name': 'A/Game.sfc'}, {'name': 'B/Game.sfc'}],
])
def test_completion_rejects_unsafe_payload_paths(files):
    """Completion rejects unsafe payload paths."""
    c = client()
    c._call = Mock(return_value={'result': 'success', 'arguments': {'torrents': [torrent(files=files)]}})
    assert c.completed() == []


@pytest.mark.parametrize('changes', [
    {'leftUntilDone': 1}, {'percentDone': 0.9}, {'totalSize': 0}, {'labels': []},
    {'metadataPercentComplete': 0}, {'metadataPercentComplete': None}, {'hashString': ''},
])
def test_completion_excludes_unready_or_unowned_torrents(changes):
    """Completion requires a finished payload and a known torrent identity."""
    c = client()
    c._call = Mock(return_value=success(torrents=[torrent(**changes)]))
    assert c.completed() == []


def success(**arguments):
    """Success."""
    return {'result': 'success', 'arguments': arguments}


@pytest.mark.parametrize('entry', [
    {'name': 'Set/Other.sfc', 'length': 100, 'bytesCompleted': 0},
    {'name': 'Set/Other.sfc', 'length': 100, 'bytesCompleted': 99},
    {'name': 'Set/Other.sfc', 'length': None},
])
def test_wanted_completion_requires_every_payload_file(entry):
    """A finished wanted subset cannot expose incomplete unwanted files."""
    c = client()
    row = torrent(files=[{'name': 'Set/Game.sfc'}, entry])
    c._call = Mock(return_value=success(torrents=[row]))
    assert c.completed() == []
    assert c.torrents()[0]['content_path'] == ''


def test_metainfo_duplicate_is_refused_without_mutation():
    """Only a magnet can establish identity for attaching an existing torrent."""
    c = client()
    c._call = Mock(return_value=success(**{'torrent-duplicate': {'hashString': HASH}}))
    assert not c.add_torrent_file(b'torrent metainfo')
    assert c.last_job_id == ''
    assert [call.args[0] for call in c._call.call_args_list] == ['torrent-add']


def test_connection_failure_does_not_log_credentials(caplog):
    """Transport exceptions can contain credentials and must remain private."""
    c = client()
    c._call = Mock(side_effect=RuntimeError('password=SUPERSECRET'))
    assert not c.reachable()
    assert 'SUPERSECRET' not in caplog.text


@pytest.mark.parametrize('elapsed', [.75, 1.1])
def test_progress_handshake_reuses_the_remaining_deadline(monkeypatch, elapsed):
    """A 409 challenge cannot restart the queue's RPC time budget."""
    import json
    from tests.test_more_clients import FakeResponse

    now, calls = [100.0], []
    monkeypatch.setattr('romarr.downloaders.time.monotonic', lambda: now[0])
    def post(url, **kwargs):
        """Charge the handshake against the same absolute deadline."""
        calls.append(kwargs)
        if len(calls) == 1:
            now[0] += elapsed
            return FakeResponse(409, {}, {'X-Transmission-Session-Id': 'challenge'})
        return FakeResponse(200, success(torrents=[torrent()]))
    c = Transmission(TransmissionConfig('http://fixture'), Mock(post=post))
    progress = c.progress(deadline=101.0)
    assert calls[0]['timeout'] == 1.0
    assert all(json.loads(call['data'])['method'] == 'torrent-get' for call in calls)
    if elapsed < 1:
        assert calls[1]['timeout'] == .25
        assert HASH in progress
    else:
        assert len(calls) == 1 and progress == {}


@pytest.mark.parametrize('version', [16, 17])
def test_version_aware_labels(version):
    """Version aware labels."""
    c = build_client({'type': 'transmission', 'host': 'localhost', 'save_path': '/downloads/romarr'})
    replies = [success(**{'rpc-version': version}), success(**{'torrent-added': {'hashString': HASH}})]
    if version == 16:
        replies.extend([success(), success(torrents=[torrent()])])
    c._call = Mock(side_effect=replies)
    assert c.reachable()
    assert c.add('magnet:demo') and c.last_job_id == HASH
    calls = c._call.call_args_list
    assert calls[0].args == ('session-get', {})
    args = calls[1].args[1]
    assert args['download-dir'] == '/downloads/romarr'
    if version == 17:
        assert args['labels'] == ['romarr']
        assert [call.args[0] for call in calls] == ['session-get', 'torrent-add']
    else:
        assert 'labels' not in args
        assert calls[2].args == ('torrent-set', {'ids': [HASH], 'labels': ['romarr']})
        assert [call.args[0] for call in calls] == ['session-get', 'torrent-add', 'torrent-set', 'torrent-get']


def test_connection_check_still_probes_after_cached_success():
    """Connection check still probes after cached success."""
    c = client()
    c._call = Mock(return_value={'result': 'connection failed'})
    assert not c.reachable()
    c._call.assert_called_once_with('session-get', {})


@pytest.mark.parametrize('reply', [
    {'result': 'error'}, success(),
    success(**{'rpc-version': '17'}), success(**{'rpc-version': True}),
    success(**{'rpc-version': 15}),
    RuntimeError('disconnected'),
])
def test_failed_or_unsupported_discovery_refuses_submission(reply, caplog):
    """Failed or unsupported discovery refuses submission."""
    c = client()
    c._rpc_version = None
    c.last_job_id = HASH
    c._call = Mock(side_effect=[reply])
    assert not c.add('magnet:demo')
    assert c.last_job_id == ''
    c._call.assert_called_once_with('session-get', {})
    assert 'add failed' in caplog.text


def test_failed_discovery_is_retried():
    """Failed discovery is retried."""
    c = client()
    c._rpc_version = None
    c._call = Mock(side_effect=[{'result': 'error'}, success(**{'rpc-version': 17}),
                               success(**{'torrent-added': {'hashString': HASH}})])
    assert not c.add('magnet:demo')
    assert c.add('magnet:demo')
    assert [call.args[0] for call in c._call.call_args_list] == ['session-get', 'session-get', 'torrent-add']


@pytest.mark.parametrize('version', [16, 17])
def test_duplicate_owned_by_romarr_is_only_inspected(version):
    """A second request cannot claim an already owned torrent."""
    c = client()
    c._rpc_version = version
    c._call = Mock(side_effect=[success(**{'torrent-duplicate': {'hashString': HASH}}),
                               success(torrents=[torrent()])])
    assert not c.add('magnet:?xt=urn:btih:' + HASH)
    assert not c.last_job_id
    assert [call.args[0] for call in c._call.call_args_list] == ['torrent-add', 'torrent-get']


@pytest.mark.parametrize('reply', [{'result': 'error'}, RuntimeError('disconnected')])
def test_post_add_labelling_failure_leaves_torrent_intact(reply, caplog):
    """Post add labelling failure leaves torrent intact."""
    c = client()
    c._rpc_version = 16
    c._call = Mock(side_effect=[success(**{'torrent-added': {'hashString': HASH}}), reply])
    assert not c.add('magnet:demo')
    assert c.last_job_id == ''
    assert [call.args[0] for call in c._call.call_args_list] == ['torrent-add', 'torrent-set']
    assert 'torrent left intact' in caplog.text


@pytest.mark.parametrize('reply', [
    success(torrents=[]), success(torrents=[torrent(), torrent()]),
    success(torrents=[torrent(labels=[])]), success(torrents=[torrent(downloadDir='/other')]),
    success(torrents=[torrent(hashString='b' * 40)]),
])
def test_post_add_verification_failure_leaves_torrent_intact(reply, caplog):
    """Post add verification failure leaves torrent intact."""
    c = client()
    c._rpc_version = 16
    c._call = Mock(side_effect=[success(**{'torrent-added': {'hashString': HASH}}), success(), reply])
    assert not c.add('magnet:demo')
    assert c.last_job_id == ''
    assert [call.args[0] for call in c._call.call_args_list] == ['torrent-add', 'torrent-set', 'torrent-get']
    assert 'torrent left intact' in caplog.text


def test_unlabelled_submission_does_not_require_label_capabilities():
    """Unlabelled submission does not require label capabilities."""
    c = Transmission(TransmissionConfig('http://localhost', category=''))
    c._call = Mock(return_value=success(**{'torrent-added': {'hashString': HASH}}))
    assert c.add('magnet:demo')
    c._call.assert_called_once_with('torrent-add', {'filename': 'magnet:demo'})


@pytest.mark.parametrize('status', [401, 409, 500])
def test_http_failure_cannot_confirm_capabilities(status):
    """Http failure cannot confirm capabilities."""
    from tests.test_more_clients import FakeResponse, FakeSession

    session = FakeSession([FakeResponse(status, success(**{'rpc-version': 17})),
                           FakeResponse(status, success(**{'rpc-version': 17}))])
    c = Transmission(TransmissionConfig('http://localhost'), session)
    assert not c.add('magnet:demo')
    assert c.last_job_id == ''
    import json
    assert all(json.loads(call['data'])['method'] == 'session-get' for call in session.calls)
