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


def test_qbittorrent_tag_matches_a_renamed_completed_torrent():
    from romarr.downloaders import match_queue_item
    item = QueueItem("Tony Hawk 3", "ps2", "long indexer title", 0,
                     "grabbed", download_id="qbit-tag:romarr-abc")
    assert match_queue_item([item], {
        "name": "Tony Hawk's Pro Skater 3 (USA).7z",
        "tags": "other, romarr-abc"}) is item


def test_exact_size_fallback_matches_only_one_grabbed_row():
    from romarr.downloaders import match_queue_item
    item = QueueItem("Tony Hawk 3", "ps2", "indexer release title", 0,
                     "grabbed", size=2731101058)
    assert match_queue_item([item], {
        "name": "different torrent name", "total_size": "2731101058"}) is item


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
