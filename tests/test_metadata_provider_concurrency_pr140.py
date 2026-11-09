"""PR #140: deterministic provider-state atomicity and concurrency regressions.

All HTTP tests use a loopback ephemeral port. No third-party provider is called.
"""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from http.server import ThreadingHTTPServer

import pytest

from romarr import app as app_module
from romarr import store as store_module
from romarr.app import ROMarr, make_handler
from romarr.metadata import GameInfo
from romarr.ops import make_backup


@contextmanager
def loopback_api(service):
    """Run the real threaded HTTP handler on loopback without external I/O."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(service))
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    url = f"http://127.0.0.1:{server.server_address[1]}"

    def request(path, method="GET", body=None):
        """Send one authenticated API request and return status plus JSON."""
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url + path, data=data, method=method,
                                     headers={"X-Api-Key": "local-test-key",
                                              "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=8) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as response:
            return response.code, json.loads(response.read().decode())

    try:
        yield request
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)


def new_service(tmp_path):
    """Create an isolated in-memory service with disposable local state."""
    return ROMarr({"ROMARR_DATA": str(tmp_path / "romarr.json"),
                   "ROMARR_API_KEY": "local-test-key"})


def assert_consistent(service):
    """Require the published chain, deep snapshot and persisted provider list to agree."""
    providers = service.store.list_items("metadata_providers")
    assert service.metadata.providers == providers
    assert service._metadata_provider_snapshot == providers
    assert len(service.metadata_revision) == 32


def test_provider_reload_noop_and_nested_change(tmp_path):
    """Unchanged or absent providers never rotate the opaque revision."""
    service = new_service(tmp_path)
    initial = service.metadata_revision
    service.reload_metadata()
    assert service.metadata_revision == initial
    saved = service.store.put_item("metadata_providers", {
        "type": "rawg", "name": "A", "enable": False,
        "api_key": "opaque-test-secret", "options": {"region": "us"},
    })
    service.reload_metadata()
    changed = service.metadata_revision
    assert changed != initial
    assert_consistent(service)
    service.reload_metadata()
    assert service.metadata_revision == changed
    assert service.store.delete_item("metadata_providers", "missing") is False
    service.reload_metadata()
    assert service.metadata_revision == changed
    changed_cfg = service.store.get_item("metadata_providers", saved["id"])
    changed_cfg["options"] = {"region": "eu"}
    service.store.put_item("metadata_providers", changed_cfg)
    service.reload_metadata()
    assert service.metadata_revision != changed
    assert_consistent(service)


def test_concurrent_get_waits_for_provider_save_and_revision(tmp_path, monkeypatch):
    """Do not return new saved settings paired with the old revision."""
    service = new_service(tmp_path)
    entered = threading.Event()
    release = threading.Event()
    original_put = service.store.put_item

    def paused_put(key, item, **kwargs):
        """Hold POST after the Store write but before Metadata reload."""
        out = original_put(key, item, **kwargs)
        if key == "metadata_providers" and item.get("name") == "A":
            entered.set()
            assert release.wait(8), "Provider save was never released"
        return out

    monkeypatch.setattr(service.store, "put_item", paused_put)
    with loopback_api(service) as request, ThreadPoolExecutor(max_workers=2) as pool:
        write_future = pool.submit(request, "/api/v1/metadataprovider", "POST", {
            "type": "rawg", "name": "A", "enable": False,
            "api_key": "opaque-test-secret",
        })
        try:
            assert entered.wait(5), "POST did not reach the coordinated state gap"
            get_future = pool.submit(request, "/api/v1/metadataprovider")
            # An event wait is bounded; unlike a sleep, it signals if the
            # read actually escaped while the writer was held mid-transaction.
            escaped = threading.Event()
            get_future.add_done_callback(lambda _: escaped.set())
            assert not escaped.wait(0.35), "GET escaped a partial provider update"
        finally:
            release.set()
        assert write_future.result(timeout=8)[0] == 200
        status, payload = get_future.result(timeout=8)
    assert status == 200
    assert payload["items"][0]["api_key"] == "********"
    assert payload["revision"] == service.metadata_revision
    assert_consistent(service)


def test_competing_provider_posts_publish_latest_chain(tmp_path, monkeypatch):
    """An older blocked reload must never overwrite a later provider save."""
    service = new_service(tmp_path)
    entered = threading.Event()
    release = threading.Event()
    second_saved = threading.Event()
    original_metadata = app_module.Metadata
    original_put = service.store.put_item

    def gated_metadata(providers):
        """Pause the first provider-chain rebuild at an explicit barrier."""
        names = {p.get("name") for p in providers}
        if "First" in names and "Second" not in names:
            entered.set()
            assert release.wait(8), "First provider reload never released"
        return original_metadata(providers)

    def tracked_put(key, item, **kwargs):
        """Signal when the competing provider write reaches Store."""
        out = original_put(key, item, **kwargs)
        if key == "metadata_providers" and item.get("name") == "Second":
            second_saved.set()
        return out

    monkeypatch.setattr(app_module, "Metadata", gated_metadata)
    monkeypatch.setattr(service.store, "put_item", tracked_put)
    with loopback_api(service) as request, ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(request, "/api/v1/metadataprovider", "POST",
                            {"type": "rawg", "name": "First", "enable": False})
        try:
            assert entered.wait(5), "First reload did not reach barrier"
            second = pool.submit(request, "/api/v1/metadataprovider", "POST",
                                 {"type": "rawg", "name": "Second", "enable": False})
            assert not second_saved.wait(0.35), "Second write bypassed in-flight reload"
        finally:
            release.set()
        assert first.result(timeout=8)[0] == 200
        assert second.result(timeout=8)[0] == 200
        _, payload = request("/api/v1/metadataprovider")
    assert len(payload["items"]) == 2
    assert {p["name"] for p in payload["items"]} == {"First", "Second"}
    assert payload["revision"] == service.metadata_revision
    assert_consistent(service)


def test_delete_and_restore_revisions_and_secret_masking(tmp_path):
    """Delete and backup restore publish the effective provider configuration."""
    service = new_service(tmp_path)
    with loopback_api(service) as request:
        initial = service.metadata_revision
        status, saved = request("/api/v1/metadataprovider", "POST", {
            "type": "rawg", "name": "Original", "enable": False,
            "api_key": "opaque-test-secret",
        })
        assert status == 200
        assert saved["api_key"] == "********"
        first = service.metadata_revision
        assert first != initial
        status, payload = request("/api/v1/metadataprovider")
        assert status == 200
        assert payload["revision"] == first
        assert payload["items"][0]["api_key"] == "********"
        status, deleted = request("/api/v1/metadataprovider/" + saved["id"], "DELETE")
        assert status == 200 and deleted["deleted"] is True
        removed = service.metadata_revision
        assert removed != first
        status, deleted = request("/api/v1/metadataprovider/" + saved["id"], "DELETE")
        assert status == 404 and deleted["deleted"] is False
        assert service.metadata_revision == removed
        restored_providers = [{"type": "rawg", "name": "Restored", "enable": False,
                               "api_key": "opaque-restored-secret"}]
        body = make_backup({**service.store.settings,
                            "metadata_providers": restored_providers}, include_secrets=True)
        status, result = request("/api/v1/restore", "POST", body)
        assert status == 200 and result["ok"] is True
        after = service.metadata_revision
        assert after != removed
        assert_consistent(service)
        status, payload = request("/api/v1/metadataprovider")
        assert status == 200
        assert payload["items"][0]["name"] == "Restored"
        assert payload["items"][0]["api_key"] == "********"
        assert payload["revision"] == after
        status, result = request("/api/v1/restore", "POST", body)
        assert status == 200 and result["ok"] is True
        assert service.metadata_revision == after


def test_identify_releases_lock_before_slow_provider_lookup(tmp_path, monkeypatch):
    """External Metadata I/O must not monopolize the provider-state lock."""
    service = new_service(tmp_path)
    entered = threading.Event()
    release = threading.Event()

    def delayed_identify(**_):
        """Simulate a slow external provider without network traffic."""
        entered.set()
        assert release.wait(8), "Slow provider was not released"
        return GameInfo(title="Local test", matched_by="filename")

    monkeypatch.setattr(service.metadata, "identify", delayed_identify)
    with ThreadPoolExecutor(max_workers=1) as pool:
        result = pool.submit(service.identify, "Local.Test.sfc")
        try:
            assert entered.wait(5), "Metadata lookup never started"
            assert service._metadata_lock.acquire(timeout=0.5), (
                "Provider-state lock stayed held during external Metadata lookup")
            service._metadata_lock.release()
        finally:
            release.set()
        assert result.result(timeout=8)["title"] == "Local test"


def test_masked_credentials_require_a_saved_value(tmp_path):
    """Reject invented masks and stale editors; preserve genuine saved secrets."""
    service = new_service(tmp_path)
    first_revision = service.metadata_revision
    with loopback_api(service) as request:
        status, invalid = request('/api/v1/metadataprovider', 'POST', {
            'type': 'rawg', 'name': 'New masked provider', 'enable': True,
            'api_key': '********',
        })
        assert status == 400 and 'error' in invalid
        assert service.store.list_items('metadata_providers') == []
        assert service.metadata_revision == first_revision

        status, saved = request('/api/v1/metadataprovider', 'POST', {
            'type': 'rawg', 'name': 'Valid', 'enable': True,
            'api_key': 'synthetic-test-key',
        })
        assert status == 200 and saved['api_key'] == '********'
        provider_id = saved['id']
        status, updated = request('/api/v1/metadataprovider', 'POST', {
            'id': provider_id, 'type': 'rawg', 'name': 'Renamed',
            'enable': True, 'api_key': '********',
        })
        assert status == 200 and updated['api_key'] == '********'
        actual = service.store.get_item('metadata_providers', provider_id)
        assert actual['api_key'] == 'synthetic-test-key'
        assert actual['name'] == 'Renamed'

        status, deleted = request('/api/v1/metadataprovider/' + provider_id, 'DELETE')
        assert status == 200 and deleted['deleted'] is True
        revision = service.metadata_revision
        status, stale = request('/api/v1/metadataprovider', 'POST', {
            'id': provider_id, 'type': 'rawg', 'name': 'Stale editor',
            'enable': True, 'api_key': '********',
        })
        assert status == 404 and 'error' in stale
        assert service.store.list_items('metadata_providers') == []
        assert service.metadata_revision == revision

        status, partial = request('/api/v1/metadataprovider', 'POST', {
            'type': 'igdb', 'name': 'Partially masked', 'enable': True,
            'client_id': '********', 'token': 'new-test-token',
        })
        assert status == 400 and 'error' in partial
        assert service.store.list_items('metadata_providers') == []


def test_failed_provider_rebuild_does_not_persist_or_publish(tmp_path, monkeypatch):
    """Validate the prospective chain before changing stored or active settings."""
    service = new_service(tmp_path)
    original_metadata = service.metadata
    original_snapshot = service._metadata_provider_snapshot
    original_revision = service.metadata_revision
    real_metadata = app_module.Metadata

    def reject_invalid(providers):
        """Deterministically simulate constructor failure on invalid input."""
        if any(p.get('name') == 'Rejected' for p in providers):
            raise ValueError('Synthetic test build failure')
        return real_metadata(providers)

    monkeypatch.setattr(app_module, 'Metadata', reject_invalid)
    with loopback_api(service) as request:
        status, result = request('/api/v1/metadataprovider', 'POST', {
            'type': 'rawg', 'name': 'Rejected', 'enable': False,
        })
    assert status == 400 and result['error'] == 'invalid metadata provider configuration'
    assert service.store.list_items('metadata_providers') == []
    assert service.metadata is original_metadata
    assert service._metadata_provider_snapshot is original_snapshot
    assert service.metadata_revision == original_revision
    assert 'Synthetic' not in str(result)


def test_failed_provider_edit_preserves_existing_configuration(tmp_path, monkeypatch):
    """A failed edit must preserve the prior secret and stable revision."""
    service = new_service(tmp_path)
    with loopback_api(service) as request:
        status, saved = request('/api/v1/metadataprovider', 'POST', {
            'type': 'rawg', 'name': 'Valid', 'enable': False,
            'api_key': 'synthetic-test-key',
        })
        assert status == 200
        original_cfg = service.store.get_item('metadata_providers', saved['id'])
        original_chain = service.metadata
        original_revision = service.metadata_revision
        real_metadata = app_module.Metadata

        def fail_rebuild(providers):
            """Reject only edits containing a simulated invalid entry."""
            if any(p.get('name') == 'Invalid edit' for p in providers):
                raise RecursionError('Simulated nested provider limit')
            return real_metadata(providers)

        monkeypatch.setattr(app_module, 'Metadata', fail_rebuild)
        status, result = request('/api/v1/metadataprovider', 'POST', {
            'id': saved['id'], 'type': 'rawg', 'name': 'Invalid edit',
            'enable': False, 'api_key': '********',
        })
    assert status == 400 and 'error' in result
    assert service.store.get_item('metadata_providers', saved['id']) == original_cfg
    assert service.metadata is original_chain
    assert service.metadata_revision == original_revision
    assert_consistent(service)


def test_reload_metadata_failure_preserves_published_tuple(tmp_path, monkeypatch):
    """An exception between deep copies cannot half-publish a metadata chain."""
    service = new_service(tmp_path)
    chain, snapshot, revision = (service.metadata, service._metadata_provider_snapshot,
                                 service.metadata_revision)
    service.store.put_item('metadata_providers', {
        'name': 'Changed', 'type': 'rawg', 'enable': False,
    })
    original_deepcopy = app_module.deepcopy
    call_count = 0

    def fail_second_copy(value):
        """Raise at the second deepcopy to test atomic publication."""
        nonlocal call_count
        call_count += 1
        if call_count == 2:
            raise RecursionError('Synthetic second-copy failure')
        return original_deepcopy(value)

    monkeypatch.setattr(app_module, 'deepcopy', fail_second_copy)
    with pytest.raises(RecursionError):
        service.reload_metadata()
    assert service.metadata is chain
    assert service._metadata_provider_snapshot is snapshot
    assert service.metadata_revision == revision



def test_store_saves_serialize_through_file_replace(tmp_path, monkeypatch):
    """A failed required provider write cannot leak through an older save."""
    service = new_service(tmp_path)
    store = service.store
    store.save()
    original_bytes = store.path.read_bytes()
    dump_entered = threading.Event()
    allow_dump = threading.Event()
    writer_attempted_lock = threading.Event()
    writer_finished = threading.Event()
    worker_errors = []
    expected_failure = []
    actual_dump = store_module.json.dump
    actual_replace = store_module.os.replace
    actual_lock = store._lock

    class ObservedLock:
        """Signal a required writer immediately before it tries the Store lock."""

        def __enter__(self):
            """Signal the competing writer before it attempts the shared lock."""
            if threading.current_thread().name == 'required-provider-writer':
                writer_attempted_lock.set()
            actual_lock.acquire()
            return self

        def __exit__(self, exc_type, exc, traceback):
            actual_lock.release()

    def blocking_dump(*args, **kwargs):
        """Hold an ordinary save during serialization to expose lock gaps."""
        if threading.current_thread().name == 'ordinary-store-writer':
            dump_entered.set()
            assert allow_dump.wait(8), 'Ordinary save was not released'
        return actual_dump(*args, **kwargs)

    def failed_replace(src, dst):
        """Fail only the required provider write, not the ordinary save."""
        if threading.current_thread().name == 'required-provider-writer':
            raise OSError('Synthetic failed required write')
        return actual_replace(src, dst)

    def ordinary_save():
        """Execute the ordinary save without swallowing unexpected failures."""
        try:
            store.save()
        except Exception as exc:
            worker_errors.append(exc)

    def required_save():
        """Attempt a required write while an ordinary save holds the lock."""
        try:
            store.put_item('metadata_providers', {
                'id': 'blocked-provider', 'name': 'Rejected',
            }, require_persist=True)
        except OSError as exc:
            expected_failure.append(str(exc))
        except Exception as exc:
            worker_errors.append(exc)
        finally:
            writer_finished.set()

    monkeypatch.setattr(store, '_lock', ObservedLock())
    monkeypatch.setattr(store_module.json, 'dump', blocking_dump)
    monkeypatch.setattr(store_module.os, 'replace', failed_replace)
    ordinary = threading.Thread(target=ordinary_save, name='ordinary-store-writer',
                                daemon=True)
    required = threading.Thread(target=required_save, name='required-provider-writer',
                                daemon=True)
    ordinary.start()
    try:
        assert dump_entered.wait(5), 'Ordinary save did not enter serialization'
        required.start()
        assert writer_attempted_lock.wait(5), 'Required save never attempted the lock'
        assert not writer_finished.wait(0.35), (
            'Required provider write escaped while an ordinary save was in progress')
    finally:
        allow_dump.set()
        ordinary.join(timeout=8)
        if required.ident is not None:
            required.join(timeout=8)

    assert not ordinary.is_alive() and not required.is_alive()
    assert not worker_errors
    assert expected_failure == ['Synthetic failed required write']
    assert store.settings['metadata_providers'] == []
    assert store.path.read_bytes() == original_bytes


def test_provider_save_failure_keeps_metadata_consistent(tmp_path, monkeypatch):
    """A failed durable update preserves memory, saved data, and revision."""
    service = new_service(tmp_path)
    with loopback_api(service) as request:
        status, saved = request('/api/v1/metadataprovider', 'POST', {
            'type': 'rawg', 'name': 'Original', 'enable': False,
            'api_key': 'synthetic-test-secret',
        })
        assert status == 200
        original_file = service.store.path.read_bytes()
        original_items = service.store.list_items('metadata_providers')
        original_metadata = service.metadata
        original_revision = service.metadata_revision
        original_collection = service.store.settings['metadata_providers']
        replace_destinations = []
        original_replace = store_module.os.replace

        def reject_replace(source, destination):
            """Reject only this store's write, leaving other services untouched."""
            if destination != service.store.path:
                return original_replace(source, destination)
            replace_destinations.append(destination)
            raise OSError('Synthetic settings-write failure')

        monkeypatch.setattr(store_module.os, 'replace', reject_replace)
        status, reply = request('/api/v1/metadataprovider', 'POST', {
            'id': saved['id'], 'type': 'rawg', 'name': 'Unsaved edit',
            'enable': False, 'api_key': '********',
        })
        assert status == 500 and reply['error'] == 'OSError: Synthetic settings-write failure'
        assert replace_destinations == [service.store.path]
        status, reply = request('/api/v1/metadataprovider', 'POST', {
            'type': 'rawg', 'name': 'Unsaved new row', 'enable': False,
        })
        assert status == 500 and reply['error'] == 'OSError: Synthetic settings-write failure'
        assert replace_destinations == [service.store.path, service.store.path]
        status, listed = request('/api/v1/metadataprovider')
        assert status == 200
        assert listed['revision'] == original_revision
        assert listed['items'][0]['name'] == 'Original'

    assert service.store.path.read_bytes() == original_file
    assert service.store.list_items('metadata_providers') == original_items
    assert service.store.settings['metadata_providers'] is original_collection
    assert service.metadata is original_metadata
    assert service.metadata_revision == original_revision
    assert_consistent(service)


def test_library_save_restore_race_preserves_restored_backends(tmp_path, monkeypatch):
    """A restore cannot be overtaken by an older library backend rebuild."""
    from types import SimpleNamespace

    service = new_service(tmp_path)
    entered = threading.Event()
    release = threading.Event()
    restore_parsed = threading.Event()
    original_read_backup = app_module.read_backup

    def tracked_backup(body):
        result = original_read_backup(body)
        restore_parsed.set()
        return result

    def library_factory(cfg):
        if cfg.get('name') == 'Before restore':
            entered.set()
            assert release.wait(8), 'Library rebuild was not released'
        return SimpleNamespace(name=cfg['name'])

    monkeypatch.setattr(app_module, 'read_backup', tracked_backup)
    monkeypatch.setattr(app_module, 'build_library_from_config', library_factory)
    restored = make_backup({**service.store.settings,
                            'libraries': [{
                                'id': 'restored-library', 'type': 'folder',
                                'name': 'After restore', 'enable': True,
                                'path': str(tmp_path / 'restored-roms'),
                                'is_default': True,
                            }]}, include_secrets=True)

    with loopback_api(service) as request, ThreadPoolExecutor(max_workers=2) as pool:
        edit = pool.submit(request, '/api/v1/library', 'POST', {
            'id': 'before-library', 'type': 'folder', 'name': 'Before restore',
            'enable': True, 'path': str(tmp_path / 'before-roms'),
            'is_default': True,
        })
        try:
            assert entered.wait(5), 'Library edit did not enter rebuild'
            restore = pool.submit(request, '/api/v1/restore', 'POST', restored)
            assert restore_parsed.wait(5), 'Restore did not parse backup'
            escaped = threading.Event()
            restore.add_done_callback(lambda _: escaped.set())
            assert not escaped.wait(0.35), 'Restore bypassed in-progress edit'
        finally:
            release.set()
        assert edit.result(timeout=8)[0] == 200
        status, result = restore.result(timeout=8)

    assert status == 200 and result['ok'] is True
    assert [x['name'] for x in service.store.list_items('libraries')] == ['After restore']
    assert [cfg['name'] for cfg, _ in service.game_libraries] == ['After restore']
    assert service.library == tmp_path / 'restored-roms'

def test_client_save_and_restore_publish_latest_clients(tmp_path, monkeypatch):
    """A blocked client reload cannot overwrite clients published by restore."""
    from types import SimpleNamespace

    service = new_service(tmp_path)
    first_reload = threading.Event()
    release = threading.Event()
    restore_parsed = threading.Event()
    original_read_backup = app_module.read_backup

    def tracked_backup(body):
        result = original_read_backup(body)
        restore_parsed.set()
        return result

    monkeypatch.setattr(app_module, 'read_backup', tracked_backup)

    def client_factory(cfg):
        """Avoid external download clients and coordinate a deterministic race."""
        if cfg.get('name') == 'Before restore':
            first_reload.set()
            assert release.wait(8), 'Client reload was never released'
        return SimpleNamespace(name=cfg['name'])

    monkeypatch.setattr(app_module, 'build_client', client_factory)
    restored = make_backup({**service.store.settings,
                            'download_clients': [{
                                'id': 'restored-client', 'type': 'qbittorrent',
                                'name': 'After restore', 'enable': True,
                            }]}, include_secrets=True)
    with loopback_api(service) as request, ThreadPoolExecutor(max_workers=2) as pool:
        client_request = pool.submit(request, '/api/v1/downloadclient', 'POST', {
            'id': 'before-client', 'type': 'qbittorrent',
            'name': 'Before restore', 'enable': True,
        })
        try:
            assert first_reload.wait(5), 'Client update did not reach rebuild'
            restore_request = pool.submit(request, '/api/v1/restore', 'POST', restored)
            assert restore_parsed.wait(5), 'Restore did not parse backup'
            escaped = threading.Event()
            restore_request.add_done_callback(lambda _: escaped.set())
            assert not escaped.wait(0.35), 'Restore escaped while client reload was blocked'
        finally:
            release.set()
        assert client_request.result(timeout=8)[0] == 200
        status, result = restore_request.result(timeout=8)
    assert status == 200 and result['ok'] is True
    clients = service.store.list_items('download_clients')
    assert [c['name'] for c in clients] == ['After restore']
    assert [c.romarr_name for c in service.clients] == ['After restore']


def test_failed_provider_delete_preserves_saved_chain_and_revision(tmp_path, monkeypatch):
    service = new_service(tmp_path)
    saved = service.save_metadata_provider({"type": "rawg", "name": "Original", "enable": False})
    revision = service.metadata_revision
    original = service.store.path.read_bytes()

    def fail_create(**kwargs):
        raise PermissionError("Cannot create a replacement state file")

    with loopback_api(service) as request, monkeypatch.context() as patch:
        patch.setattr(store_module.tempfile, "mkstemp", fail_create)
        service.store.save()
        status, _ = request("/api/v1/metadataprovider/" + saved["id"], "DELETE")
        assert status == 500
        assert service.store.path.read_bytes() == original
        assert service.metadata_revision == revision
        assert service.store.list_items("metadata_providers") == [saved]
        assert_consistent(service)
