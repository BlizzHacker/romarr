"""An RPC 16 grab whose label step failed after torrent-add can still be recovered."""

from __future__ import annotations

from romarr.downloaders import Transmission, TransmissionConfig

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
