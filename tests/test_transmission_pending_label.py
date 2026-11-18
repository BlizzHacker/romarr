"""An RPC 16 grab whose label step failed after torrent-add can still be recovered."""

from __future__ import annotations

from unittest.mock import Mock

import pytest

from romarr.downloaders import Transmission, TransmissionConfig
from test_collection_torrent import _service

HASH = "a" * 40


def make_client():
    c = Transmission(TransmissionConfig(base_url="http://t:9091", category="romarr"))
    c._rpc_version = 16
    return c


def test_a_retry_completes_the_label_instead_of_refusing_the_duplicate():
    c = make_client()
    state = {"set_ok": False, "labels": []}

    def call(method, arguments, **kw):
        if method == "torrent-add":
            key = "torrent-duplicate" if state.get("added") else "torrent-added"
            state["added"] = True
            return {"result": "success", "arguments": {key: {"hashString": HASH}}}
        if method == "torrent-set":
            if not state["set_ok"]:
                return {"result": "error"}
            state["labels"] = arguments["labels"]
            return {"result": "success"}
        if method == "torrent-get":
            return {"result": "success", "arguments": {"torrents": [
                {"hashString": HASH, "labels": state["labels"], "downloadDir": "/dl"}]}}
        return {"result": "success", "arguments": {}}

    c._call = call
    assert c.add_torrent_file(b"d4:infod4:name1:xee") is False   # label step failed
    state["set_ok"] = True                                        # connection restored
    assert c.add_torrent_file(b"d4:infod4:name1:xee") is True     # retry recovers it
    assert c.last_job_id == HASH


def test_an_unrelated_duplicate_metainfo_is_still_refused():
    c = make_client()

    def call(method, arguments, **kw):
        if method == "torrent-add":
            return {"result": "success", "arguments": {"torrent-duplicate": {"hashString": HASH}}}
        return {"result": "success", "arguments": {"torrents": []}}

    c._call = call
    assert c.add_torrent_file(b"d4:infod4:name1:xee") is False


def test_a_pending_label_is_not_recovered_by_a_different_request():
    c = make_client()
    state = {"set_ok": False, "labels": [], "added": False}

    def call(method, arguments, **kw):
        if method == "torrent-add":
            key = "torrent-duplicate" if state["added"] else "torrent-added"
            state["added"] = True
            return {"result": "success", "arguments": {key: {"hashString": HASH}}}
        if method == "torrent-set":
            if not state["set_ok"]:
                return {"result": "error"}
            state["labels"] = arguments["labels"]
            return {"result": "success"}
        return {"result": "success", "arguments": {"torrents": [
            {"hashString": HASH, "labels": state["labels"], "downloadDir": "/dl"}]}}

    c._call = call
    assert c.add_torrent_file(b"d4:infod4:name1:xee") is False
    state["set_ok"] = True
    # Same torrent, different category: an unrelated duplicate, refused.
    assert c.add_torrent_file(b"d4:infod4:name1:xee", category="romarr-soundtracks") is False
    assert state["labels"] == []


@pytest.mark.parametrize("conflict", ["directory", "label", "source", "queue"])
def test_pending_retry_checks_ownership_before_label_write(conflict):
    """A pending record cannot overwrite a moved or newly owned torrent."""
    c = make_client()
    c._pending_labels[HASH] = ("romarr", "", "/dl")
    c.owned_torrents_provider = lambda: {HASH} if conflict == "source" else set()
    c.queued_torrents_provider = lambda: {HASH} if conflict == "queue" else set()
    c._call = Mock(side_effect=[
        {"result": "success", "arguments": {"torrent-duplicate": {"hashString": HASH}}},
        {"result": "success", "arguments": {"torrents": [{
            "hashString": HASH, "downloadDir": "/moved" if conflict == "directory" else "/dl",
            "labels": ["external"] if conflict == "label" else [],
        }]}},
    ])
    assert not c.add_torrent_file(b"fixture")
    assert all(call.args[0] != "torrent-set" for call in c._call.call_args_list)


def test_pending_label_survives_only_a_matching_configured_endpoint(tmp_path, monkeypatch):
    """Reload retains retry information through a credential or spelling edit."""
    svc = _service(tmp_path, monkeypatch)
    config = {"id": "t1", "type": "transmission", "host": "fixture", "save_path": "/dl"}
    svc.store.put_item("download_clients", config)
    svc.reload_clients()
    c, = svc.clients
    c._rpc_version = 16
    c._call = Mock(side_effect=[
        {"result": "success", "arguments": {"torrent-added": {"hashString": HASH}}},
        {"result": "error"},
    ])
    assert not c.add_torrent_file(b"fixture")

    svc.store.put_item("download_clients", dict(config, host="FIXTURE:80", password="changed"))
    svc.reload_clients()
    rebuilt, = svc.clients
    rebuilt._rpc_version = 16
    before = {"hashString": HASH, "downloadDir": "/dl", "labels": []}
    rebuilt._call = Mock(side_effect=[
        {"result": "success", "arguments": {"torrent-duplicate": {"hashString": HASH}}},
        {"result": "success", "arguments": {"torrents": [before]}},
        {"result": "success"},
        {"result": "success", "arguments": {"torrents": [dict(before, labels=["romarr"])]}},
    ])
    assert rebuilt.add_torrent_file(b"fixture")

    rebuilt._pending_labels[HASH] = ("romarr", "/dl", "/dl")
    svc.store.put_item("download_clients", dict(config, host="replacement"))
    svc.reload_clients()
    replacement, = svc.clients
    replacement._rpc_version = 16
    replacement._call = Mock(return_value={
        "result": "success", "arguments": {"torrent-duplicate": {"hashString": HASH}},
    })
    assert not replacement.add_torrent_file(b"fixture")
    assert [call.args[0] for call in replacement._call.call_args_list] == ["torrent-add"]
