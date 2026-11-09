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

    def paused_put(key, item):
        """Hold POST after the Store write but before Metadata reload."""
        out = original_put(key, item)
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

    def tracked_put(key, item):
        """Signal when the competing provider write reaches Store."""
        out = original_put(key, item)
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
