"""The import sweep pairs a finished download with its queue row by id first.

Issue #38. The sweep matched on `item.release == name`, where `release` is the
indexer's title and `name` is whatever the download client calls the job. For
a torrent those are different strings as often as not (`Pokemon Emerald Rom`
on the indexer, `Pokemon Emerald (U).gba` in the client), so the download
finished, matched nothing, and the queue row stayed `grabbed` forever.

Every queue row already carries the infohash as `release_id`
(`btih:<hash>`), and usenet clients hand back an id when a job is added. Those
identify the download; the name is only the fallback.
"""

from __future__ import annotations

import pytest

from romarr.app import QueueItem, ROMarr
from romarr.downloaders import SabConfig, SABnzbd

HASH = "0123456789abcdef0123456789abcdef01234567"


def svc(tmp_path):
    return ROMarr({"ROMARR_DATA": str(tmp_path / "s.json")})


class FakeClient:
    name = "fake"
    configured = True

    def __init__(self, rows):
        self._rows = rows

    def completed(self):
        return list(self._rows)




def test_torrent_whose_name_differs_from_the_indexer_title_still_matches(tmp_path):
    s = svc(tmp_path)
    item = QueueItem("Pokemon Emerald", "gba", "Pokemon Emerald Rom", 5, "grabbed",
                     release_id=f"btih:{HASH}")
    s.queue.append(item)
    s.clients = [FakeClient([{"name": "Pokemon Emerald (U).gba", "hash": HASH.upper(),
                              "content_path": str(tmp_path / "nowhere")}])]
    # No library is configured, so a *matched* row is recorded as failed with a
    # reason; an unmatched one is skipped silently. That difference is the test.
    results = s.import_finished()
    assert item.state == "import-failed"
    assert results and results[0]["reason"] == "no library configured to import into"


def test_nzo_id_recorded_at_grab_time_matches_a_renamed_job(tmp_path):
    s = svc(tmp_path)
    item = QueueItem("Contra", "nes", "Contra (USA) nzb title", 0, "grabbed",
                     download_id="SABnzbd_nzo_abc123")
    s.queue.append(item)
    s.clients = [FakeClient([{"name": "Contra.USA", "id": "SABnzbd_nzo_abc123",
                              "content_path": str(tmp_path / "nowhere")}])]
    s.import_finished()
    assert item.state == "import-failed"


def test_name_is_still_the_fallback_when_no_id_is_known(tmp_path):
    """Rows queued before this change carry no download id and must still match."""
    s = svc(tmp_path)
    item = QueueItem("Contra", "nes", "Contra (U).nes", 1, "grabbed")
    s.queue.append(item)
    s.clients = [FakeClient([{"name": "Contra (U).nes", "hash": HASH,
                              "content_path": str(tmp_path / "nowhere")}])]
    s.import_finished()
    assert item.state == "import-failed"


def test_a_different_hash_does_not_claim_a_row_by_accident(tmp_path):
    s = svc(tmp_path)
    item = QueueItem("A", "nes", "Some Title", 1, "grabbed", release_id=f"btih:{HASH}")
    s.queue.append(item)
    s.clients = [FakeClient([{"name": "Other", "hash": "f" * 40,
                              "content_path": str(tmp_path / "x")}])]
    assert s.import_finished() == []
    assert item.state == "grabbed"


def test_queue_item_without_download_id_loads_from_old_state(tmp_path):
    from romarr.store import _rows
    rows = _rows([{"game": "g", "platform": "nes", "release": "r", "seeders": 1,
                   "state": "grabbed"}], QueueItem)
    assert rows[0].download_id == ""


class _Resp:
    def __init__(self, body):
        self._body = body

    def raise_for_status(self):
        pass

    def json(self):
        return self._body


class _Sess:
    def __init__(self, body):
        self.body = body

    def get(self, url, **kw):
        return _Resp(self.body)


def test_sabnzbd_reports_the_nzo_id_it_was_given_on_add():
    from romarr.downloaders import hand_off_with_id
    sab = SABnzbd(SabConfig(base_url="http://sab", api_key="k"),
                  session=_Sess({"status": True, "nzo_ids": ["SABnzbd_nzo_abc123"]}))
    assert hand_off_with_id(sab, "http://x/nzb", name="T") == (True, "SABnzbd_nzo_abc123")


def test_sabnzbd_history_rows_carry_nzo_id():
    body = {"history": {"slots": [{"name": "n", "storage": "/d/n", "status": "Completed",
                                  "nzo_id": "SABnzbd_nzo_abc123"}]}}
    sab = SABnzbd(SabConfig(base_url="http://sab", api_key="k"), session=_Sess(body))
    assert sab.completed()[0]["id"] == "SABnzbd_nzo_abc123"


def test_hand_off_with_id_works_for_clients_that_cannot_report_one():
    from romarr.downloaders import hand_off_with_id
    class Plain:
        def add(self, url):
            return True
    assert hand_off_with_id(Plain(), "magnet:?x") == (True, "")


def test_real_debrid_completed_reports_the_torrent_hash(tmp_path):
    from romarr.downloaders import RealDebrid, RealDebridConfig
    rd = RealDebrid(RealDebridConfig(api_token="t", save_path=str(tmp_path)))
    rd._ours = lambda: ["abc"]
    rd._get = lambda path, *a, **k: {"status": "downloaded", "hash": HASH,
                                    "filename": "Pokemon Emerald (U).gba", "links": ["L"]}
    rd._resolve = lambda link: ("Pokemon Emerald (U).gba", "http://dl")
    rd._download = lambda url, dest: dest.parent.mkdir(parents=True, exist_ok=True) or dest.write_bytes(b"x") or True
    rows = rd.completed()
    assert rows and rows[0]["hash"] == HASH


def test_qbittorrent_add_tags_the_job_and_exposes_the_same_identity():
    from romarr.clients import QBittorrent, QbitConfig

    class Response:
        status_code = 200
        text = "Ok."
        ok = True

    class Session:
        def __init__(self):
            self.posts = []
        def post(self, url, **kwargs):
            self.posts.append((url, kwargs))
            return Response()

    session = Session()
    client = QBittorrent(QbitConfig(base_url="http://qbit:8080",
                                    username="u", password="p"),
                         session=session)
    client._authed = True
    assert client.add("https://tracker.test/file.torrent")
    url, kwargs = session.posts[0]
    tag = kwargs["data"]["tags"]
    assert tag.startswith("romarr-")
    assert client.last_job_id == "qbit-tag:" + tag
    assert url.endswith("/api/v2/torrents/add")


def test_qbittorrent_add_attaches_a_verified_duplicate_magnet():
    """A qBittorrent 409 adopts exactly one matching torrent for import."""
    from romarr.clients import QBittorrent, QbitConfig

    infohash = "d2a64b88b086acfcb1c89f044f81764604ceab85"

    class Response:
        def __init__(self, status_code=200, payload=None, text="Ok."):
            self.status_code = status_code
            self.ok = 200 <= status_code < 400
            self.text = text
            self._payload = payload

        def json(self):
            return self._payload

        def raise_for_status(self):
            if not self.ok:
                raise RuntimeError(self.status_code)

    class Session:
        def __init__(self):
            self.row = {"hash": infohash, "name": "Fat Princess",
                        "tags": "", "category": "prowlarr"}
            self.posts = []

        def post(self, url, **kwargs):
            self.posts.append((url, kwargs))
            if url.endswith("/torrents/add"):
                return Response(409, text="Fails.")
            if url.endswith("/torrents/addTags"):
                self.row["tags"] = kwargs["data"]["tags"]
            if url.endswith("/torrents/setCategory"):
                self.row["category"] = kwargs["data"]["category"]
            return Response()

        def get(self, url, **kwargs):
            return Response(payload=[dict(self.row)])

    session = Session()
    client = QBittorrent(QbitConfig(base_url="http://qbit:8080",
                                    category="romarr"), session=session)
    client._authed = True

    assert client.add(f"magnet:?xt=urn:btih:{infohash}")
    assert client.last_job_id.startswith("qbit-tag:romarr-")
    assert session.row["tags"] == client.last_job_id.removeprefix("qbit-tag:")
    assert session.row["category"] == "romarr"
    assert any(url.endswith("/torrents/addTags") for url, _ in session.posts)
    assert any(url.endswith("/torrents/setCategory") for url, _ in session.posts)


def test_qbittorrent_tag_matches_a_renamed_completed_torrent():
    from romarr.downloaders import match_queue_item
    item = QueueItem("Tony Hawk 3", "ps2", "long indexer title", 0,
                     "grabbed", download_id="qbit-tag:romarr-abc")
    assert match_queue_item([item], {
        "name": "Tony Hawk's Pro Skater 3 (USA).7z",
        "tags": "other, romarr-abc"}) is item


def test_exact_size_alone_does_not_establish_download_identity():
    from romarr.downloaders import match_queue_item
    item = QueueItem("Tony Hawk 3", "ps2", "indexer release title", 0,
                     "grabbed", size=2731101058)
    assert match_queue_item([item], {
        "name": "different torrent name", "total_size": "2731101058"}) is None


def test_ambiguous_exact_size_does_not_claim_a_download():
    from romarr.downloaders import match_queue_item
    rows = [QueueItem("A", "snes", "release a", 0, "grabbed", size=123),
            QueueItem("B", "nes", "release b", 0, "grabbed", size=123)]
    assert match_queue_item(rows, {"name": "unknown", "total_size": 123}) is None


def test_unmatched_romarr_category_completion_is_logged(tmp_path, caplog):
    s = svc(tmp_path)
    class CategorizedClient(FakeClient):
        _config = type("Config", (), {"category": "romarr"})()
    s.clients = [CategorizedClient([{"name": "unmatched",
                                     "category": "romarr",
                                     "content_path": str(tmp_path / "missing")}])]
    with caplog.at_level("WARNING"):
        assert s.import_finished() == []
    assert "could not be matched to a ROMarr queue row" in caplog.text


def test_qbittorrent_tag_takes_precedence_over_another_rows_title():
    from romarr.downloaders import match_queue_item
    tagged = QueueItem("A", "snes", "indexer release A", 0, "grabbed",
                       download_id="qbit-tag:romarr-a")
    named = QueueItem("B", "nes", "torrent name B", 0, "grabbed")
    assert match_queue_item([named, tagged], {
        "name": "torrent name B", "tags": "romarr-a"}) is tagged


@pytest.mark.parametrize("state", ["grabbed", "import-review", "import-failed"])
def test_conflicting_download_identity_cannot_claim_an_exact_title(state):
    """Refuse fallback title matching when recorded download identity conflicts."""
    from romarr.downloaders import match_queue_item
    rows_and_downloads = [
        (QueueItem("A", "nes", "same title", 0, state,
                   release_id=f"btih:{HASH}"), {"hash": "f" * 40}),
        (QueueItem("A", "nes", "same title", 0, state,
                   download_id="SABnzbd_nzo_one"), {"id": "SABnzbd_nzo_two"}),
        (QueueItem("A", "nes", "same title", 0, state,
                   download_id="qbit-tag:romarr-one"), {"tags": "romarr-two"}),
    ]
    for item, identity in rows_and_downloads:
        assert match_queue_item([item], {"name": "same title", **identity}) is None


@pytest.mark.parametrize("state", ["grabbed", "import-review", "import-failed"])
def test_ambiguous_titles_cannot_claim_a_download(state):
    """Refuse fallback title matching when multiple rows share the same release title."""
    from romarr.downloaders import match_queue_item
    rows = [QueueItem(game, "gb", "same title", 1, state) for game in ("A", "B")]
    assert match_queue_item(rows, {"name": "same title"}) is None


@pytest.mark.parametrize("older_state", ["import-review", "import-failed"])
def test_grabbed_row_takes_precedence_over_older_review_or_failed_title(older_state):
    """Prefer a fresh grabbed row when an older import-failed or import-review row shares its title."""
    from romarr.downloaders import match_queue_item
    older = QueueItem("A", "gb", "same title", 1, older_state)
    fresh = QueueItem("A", "gb", "same title", 1, "grabbed")
    assert match_queue_item([older, fresh], {"name": "same title"}) is fresh


def test_unrelated_hash_and_same_size_cannot_claim_a_grabbed_row():
    from romarr.downloaders import match_queue_item
    item = QueueItem("A", "nes", "release A", 0, "grabbed", size=123,
                     release_id=f"btih:{HASH}")
    assert match_queue_item([item], {
        "name": "other", "hash": "f" * 40, "total_size": 123}) is None


def test_ambiguous_qbittorrent_tags_cannot_claim_a_download():
    from romarr.downloaders import match_queue_item
    rows = [QueueItem("A", "nes", "release A", 0, "grabbed",
                      download_id="qbit-tag:romarr-a"),
            QueueItem("B", "nes", "release B", 0, "grabbed",
                      download_id="qbit-tag:romarr-b")]
    assert match_queue_item(rows, {
        "name": "release A", "tags": "romarr-a,romarr-b"}) is None


@pytest.mark.parametrize("state", ["grabbed", "import-review", "import-failed"])
def test_title_fallback_rejects_different_download_client(state):
    """Refuse untagged fallback title matches from a different client id or protocol."""
    from romarr.downloaders import match_queue_item

    qbit_row = QueueItem("Zelda", "nes", "Zelda (USA)", 1, state,
                         download_id="qbit-tag:romarr-q1", download_client="qbit-1")
    # Different client_id and protocol.
    assert match_queue_item([qbit_row], {"name": "Zelda (USA)"},
                            client_id="nzb-1", client_protocol="usenet") is None
    # Same protocol rejection even when download_client is blank on an older qbit row.
    legacy_qbit_row = QueueItem("Zelda", "nes", "Zelda (USA)", 1, state,
                                download_id="qbit-tag:romarr-q1")
    assert match_queue_item([legacy_qbit_row], {"name": "Zelda (USA)"},
                            client_id="nzb-1", client_protocol="usenet") is None
    # Same client fallback still matches when no tags/hash are present.
    assert match_queue_item([qbit_row], {"name": "Zelda (USA)"},
                            client_id="qbit-1", client_protocol="torrent") is qbit_row


def test_import_finished_does_not_cross_match_client_fallback(tmp_path):
    """Verify ROMarr.import_finished() never imports an NZBGet title under a qBittorrent queue item."""
    from types import SimpleNamespace

    s = svc(tmp_path)
    s.library_for = lambda slug: ({"path": str(tmp_path / "lib")}, SimpleNamespace(rescan=lambda slug: None))
    qbit_row = QueueItem("Zelda", "nes", "Zelda (USA)", 1, "import-review",
                         download_id="qbit-tag:romarr-q1", download_client="qbit-1")
    s.queue = [qbit_row]

    class FakeNzbClient(FakeClient):
        """Stub usenet client ensuring cross-protocol matches are rejected."""
        name = "NZBGet"
        protocol = "usenet"
        romarr_id = "nzb-1"

    s.clients = [FakeNzbClient([{"name": "Zelda (USA)", "content_path": str(tmp_path / "nowhere")}])]
    assert s.import_finished() == []
    assert qbit_row.state == "import-review"


def test_direct_hash_and_job_matches_respect_download_client():
    """Verify direct hash, job id, and qbit-tag lookups never cross-match a different client."""
    from romarr.downloaders import match_queue_item

    row_a = QueueItem("Zelda", "nes", "Zelda (USA)", 1, "grabbed",
                      release_id=f"btih:{HASH}", download_id="qbit-tag:romarr-a",
                      download_client="qbit-a")
    row_b = QueueItem("Zelda", "nes", "Zelda (USA)", 2, "grabbed",
                      release_id=f"btih:{HASH}", download_id="qbit-tag:romarr-b",
                      download_client="qbit-b")

    # Client B reports by hash without tags -> must select Row B, not Row A.
    assert match_queue_item([row_a, row_b], {"hash": HASH},
                            client_id="qbit-b", client_protocol="torrent") is row_b
    # Client C reports the same hash when only A and B exist -> must not claim either.
    assert match_queue_item([row_a, row_b], {"hash": HASH},
                            client_id="qbit-c", client_protocol="torrent") is None
    # Reporting by job id similarly respects download_client ownership.
    sab_a = QueueItem("Contra", "nes", "Contra A", 0, "grabbed",
                      download_id="job-42", download_client="sab-a")
    sab_b = QueueItem("Contra", "nes", "Contra B", 0, "grabbed",
                      download_id="job-42", download_client="sab-b")
    assert match_queue_item([sab_a, sab_b], {"id": "job-42"},
                            client_id="sab-b", client_protocol="usenet") is sab_b
    assert match_queue_item([sab_a], {"id": "job-42"},
                            client_id="sab-b", client_protocol="usenet") is None
    # Exact client row takes precedence over a legacy unassigned row sharing the hash.
    legacy = QueueItem("Zelda", "nes", "Zelda Legacy", 1, "grabbed", release_id=f"btih:{HASH}")
    assert match_queue_item([legacy, row_b], {"hash": HASH},
                            client_id="qbit-b", client_protocol="torrent") is row_b


@pytest.mark.parametrize("tags", ["romarr-0123456789abcdef01234567",
                                  "romarr-collection-0123456789abcdef01234567"])
def test_qbittorrent_duplicate_already_tracked_by_romarr_is_not_adopted(tags):
    """An existing request or retained collection keeps sole ownership of its torrent."""
    from romarr.clients import QBittorrent, QbitConfig

    infohash = "d2a64b88b086acfcb1c89f044f81764604ceab85"

    class Response:
        def __init__(self, status_code=200, payload=None, text="Ok."):
            self.status_code, self.ok, self.text = status_code, 200 <= status_code < 400, text
            self._payload = payload

        def json(self):
            return self._payload

        def raise_for_status(self):
            if not self.ok:
                raise RuntimeError(self.status_code)

    class Session:
        def __init__(self):
            self.row = {"hash": infohash, "name": "Fat Princess", "tags": tags, "category": "romarr"}
            self.posts = []

        def post(self, url, **kwargs):
            self.posts.append(url)
            return Response(409, text="Fails.") if url.endswith("/torrents/add") else Response()

        def get(self, url, **kwargs):
            return Response(payload=[dict(self.row)])

    session = Session()
    client = QBittorrent(QbitConfig(base_url="http://qbit:8080", category="romarr"), session=session)
    client._authed = True

    assert not client.add(f"magnet:?xt=urn:btih:{infohash}")
    assert not any(url.endswith(("/torrents/addTags", "/torrents/setCategory")) for url in session.posts)


def test_qbittorrent_duplicate_cleans_up_tag_on_category_or_verify_failure_and_allows_retry():
    """Failed duplicate adoption removes only this attempt's tag so a retry succeeds."""
    from romarr.clients import QBittorrent, QbitConfig

    infohash = "d2a64b88b086acfcb1c89f044f81764604ceab85"

    class Response:
        def __init__(self, status_code=200, payload=None, text="Ok."):
            self.status_code, self.ok, self.text = status_code, 200 <= status_code < 400, text
            self._payload = payload

        def json(self):
            return self._payload

        def raise_for_status(self):
            if not self.ok:
                raise RuntimeError(self.status_code)

    class Session:
        def __init__(self):
            self.row = {"hash": infohash, "name": "Fat Princess",
                        "tags": "cross-seed", "category": "prowlarr"}
            self.fail_category = True
            self.fail_verify = False
            self.drop_category_update = False
            self.removed = []

        def post(self, url, **kwargs):
            data = kwargs.get("data") or {}
            if url.endswith("/torrents/add"):
                return Response(409, text="Fails.")
            if url.endswith("/torrents/addTags"):
                current = [t.strip() for t in self.row["tags"].split(",") if t.strip()]
                current.append(data["tags"])
                self.row["tags"] = ", ".join(current)
                return Response()
            if url.endswith("/torrents/removeTags"):
                self.removed.append(data["tags"])
                current = [t.strip() for t in self.row["tags"].split(",") if t.strip()]
                self.row["tags"] = ", ".join(t for t in current if t != data["tags"])
                return Response()
            if url.endswith("/torrents/setCategory"):
                if self.fail_category:
                    return Response(500, text="Error.")
                if not self.drop_category_update:
                    self.row["category"] = data["category"]
                return Response()
            return Response()

        def get(self, url, **kwargs):
            params = kwargs.get("params") or {}
            if params.get("tag"):
                if self.fail_verify:
                    return Response(payload=[])
                tag = params["tag"]
                tags = {t.strip() for t in self.row["tags"].split(",") if t.strip()}
                return Response(payload=[dict(self.row)] if tag in tags else [])
            return Response(payload=[dict(self.row)])

    session = Session()
    client = QBittorrent(QbitConfig(base_url="http://qbit:8080", category="romarr"), session=session)
    client._authed = True
    magnet = f"magnet:?xt=urn:btih:{infohash}"

    # Category failure rolls back only this attempt's tag and keeps unrelated tags.
    assert not client.add(magnet)
    assert client.last_job_id == ""
    assert len(session.removed) == 1
    assert session.row["tags"] == "cross-seed"

    # Verification failure also rolls back only this attempt's tag and restores original category.
    session.fail_category = False
    session.fail_verify = True
    assert not client.add(magnet)
    assert client.last_job_id == ""
    assert len(session.removed) == 2
    assert session.row["tags"] == "cross-seed"
    assert session.row["category"] == "prowlarr"

    # Uncategorized original torrent is also restored to "" on verification failure.
    session.row["category"] = ""
    assert not client.add(magnet)
    assert session.row["tags"] == "cross-seed"
    assert session.row["category"] == ""

    # Confirmation rejects the row if the category does not match the configured category.
    session.fail_verify = False
    session.row["category"] = "prowlarr"
    session.drop_category_update = True
    assert not client.add(magnet)
    assert client.last_job_id == ""
    assert len(session.removed) == 4
    assert session.row["tags"] == "cross-seed"
    assert session.row["category"] == "prowlarr"

    # Retry succeeds once category update and verification succeed.
    session.drop_category_update = False
    assert client.add(magnet)
    assert client.last_job_id.startswith("qbit-tag:romarr-")
    attached_tag = client.last_job_id.removeprefix("qbit-tag:")
    final_tags = [t.strip() for t in session.row["tags"].split(",")]
    assert "cross-seed" in final_tags
    assert [t for t in final_tags if t.startswith("romarr-")] == [attached_tag]
    assert session.row["category"] == "romarr"


def test_qbittorrent_concurrent_duplicate_attaches_are_serialized():
    """Concurrent 409 attaches for one magnet leave exactly one ROMarr owner tag."""
    import threading
    from romarr.clients import QBittorrent, QbitConfig

    infohash = "d2a64b88b086acfcb1c89f044f81764604ceab85"

    class Response:
        def __init__(self, status_code=200, payload=None, text="Ok."):
            self.status_code, self.ok, self.text = status_code, 200 <= status_code < 400, text
            self._payload = payload

        def json(self):
            return self._payload

        def raise_for_status(self):
            if not self.ok:
                raise RuntimeError(self.status_code)

    class Session:
        def __init__(self):
            self.state_lock = threading.Lock()
            self.add_barrier = threading.Barrier(2)
            self.row = {"hash": infohash, "name": "Fat Princess", "tags": "", "category": "prowlarr"}
            self.add_tags_calls = 0

        def post(self, url, **kwargs):
            data = kwargs.get("data") or {}
            if url.endswith("/torrents/add"):
                self.add_barrier.wait(timeout=5)
                return Response(409, text="Fails.")
            with self.state_lock:
                if url.endswith("/torrents/addTags"):
                    self.add_tags_calls += 1
                    current = [t.strip() for t in self.row["tags"].split(",") if t.strip()]
                    current.append(data["tags"])
                    self.row["tags"] = ", ".join(current)
                elif url.endswith("/torrents/setCategory"):
                    self.row["category"] = data["category"]
            return Response()

        def get(self, url, **kwargs):
            with self.state_lock:
                return Response(payload=[dict(self.row)])

    session = Session()
    client = QBittorrent(QbitConfig(base_url="http://qbit:8080", category="romarr"), session=session)
    client._authed = True
    magnet = f"magnet:?xt=urn:btih:{infohash}"

    results = []
    job_ids = []

    def worker():
        ok = client.add(magnet)
        results.append(ok)
        job_ids.append(client.last_job_id)

    t1 = threading.Thread(target=worker)
    t2 = threading.Thread(target=worker)
    t1.start()
    t2.start()
    t1.join(timeout=5)
    t2.join(timeout=5)

    assert sorted(results) == [False, True]
    assert session.add_tags_calls == 1
    non_empty_jobs = [jid for jid in job_ids if jid]
    assert len(non_empty_jobs) == 1
    assert session.row["tags"] == non_empty_jobs[0].removeprefix("qbit-tag:")
