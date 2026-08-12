"""qBittorrent deselected every file in the torrents ROMarr added.

Found with 64 releases sitting in a seeding state at 0%: `amount_left = 0`,
`completed = 0`, and every file priority 0. They looked finished, so nobody
noticed for three days.

Nothing in ROMarr set those priorities. qBittorrent has a global "Excluded
file names" filter that runs as a torrent is added and marks every match "do
not download", and the list on that instance -- 885 globs pasted in from the
video side of the *arr world -- names `*.zip`, `*.7z`, `*.rar`, `*.iso`,
`*.bin`, `*.cue`, `*.m3u`. Around a film those are junk. Here they are the
ROM, so a ROM torrent loses *every* file to the filter and qBittorrent then
considers it complete.

Reproduced on the live instance with two torrents identical but for the
extension: `romarr-probe-alpha.zip` came back with priorities `[0]` and
`size = 0`, `romarr-probe-beta.sfc` with `[1]` and its real size.

The fake below applies the same filter on add, so these tests fail against the
old client in exactly the way the live one did.
"""

from __future__ import annotations

import fnmatch
import json

from romarr.clients import QBittorrent, QbitConfig

# A few of the real ones, enough to swallow a ROM torrent whole.
EXCLUDED = ("*.zip", "*.7z", "*.rar", "*.iso", "*.bin", "*.cue", "*.nfo", "*.txt")


class _Response:
    def __init__(self, payload, status_code: int = 200):
        self.status_code = status_code
        self.ok = 200 <= status_code < 300
        self._payload = payload
        self.text = payload if isinstance(payload, str) else json.dumps(payload)

    def json(self):
        return self._payload

    def raise_for_status(self):
        if not self.ok:
            raise AssertionError(f"status {self.status_code}")


class _Qbittorrent:
    """A qBittorrent that excludes files on add, as the real one does."""

    def __init__(self, exclude=EXCLUDED):
        self._exclude = exclude
        self.torrents: dict[str, dict] = {}
        self.prio_calls: list[dict] = []

    # -- the bit under test: what the client does to a torrent as it arrives --

    def seed(self, hash_: str, name: str, files: list[tuple[str, int]],
             category: str = "romarr"):
        """Add a torrent, applying the exclusion filter exactly as qBittorrent
        does: any file whose name matches a glob is marked "do not download",
        and the torrent's `size` counts only what survived."""
        listing = []
        for index, (filename, size) in enumerate(files):
            keep = not any(fnmatch.fnmatch(filename, g) for g in self._exclude)
            listing.append({"index": index, "name": filename, "size": size,
                            "priority": 1 if keep else 0})
        self.torrents[hash_] = {
            "hash": hash_, "name": name, "category": category, "files": listing,
        }

    def _row(self, hash_: str) -> dict:
        t = self.torrents[hash_]
        total = sum(f["size"] for f in t["files"])
        selected = sum(f["size"] for f in t["files"] if f["priority"])
        return {"hash": hash_, "name": t["name"], "category": t["category"],
                # Nothing selected means nothing left to fetch, which is how
                # qBittorrent comes to call an untouched torrent complete.
                "size": selected, "total_size": total, "completed": 0,
                "amount_left": 0, "progress": 0.0, "state": "queuedUP",
                "content_path": f"/downloads/{t['name']}"}

    # -- requests.Session surface -------------------------------------------

    def get(self, url, params=None, **kwargs):
        params = params or {}
        if url.endswith("/torrents/info"):
            return _Response([self._row(h) for h, t in self.torrents.items()
                              if t["category"] == params.get("category")])
        if url.endswith("/torrents/files"):
            t = self.torrents.get(params.get("hash"))
            return _Response(t["files"] if t else [], 200 if t else 404)
        if url.endswith("/app/version"):
            return _Response("v5.1.4")
        raise AssertionError(f"unexpected GET {url}")

    def post(self, url, data=None, **kwargs):
        data = data or {}
        if url.endswith("/auth/login"):
            return _Response("Ok.")
        if url.endswith("/torrents/filePrio"):
            self.prio_calls.append(dict(data))
            t = self.torrents.get(data.get("hash"))
            if not t:
                return _Response("Not Found", 404)
            wanted = {int(i) for i in str(data.get("id", "")).split("|") if i != ""}
            for f in t["files"]:
                if f["index"] in wanted:
                    f["priority"] = int(data.get("priority", 1))
            return _Response("Ok.")
        raise AssertionError(f"unexpected POST {url}")


def client(server):
    return QBittorrent(QbitConfig(base_url="http://qbit:8090", category="romarr"),
                       session=server)


# -- the regression ---------------------------------------------------------

def test_a_rom_torrent_is_left_with_at_least_one_file_selected():
    """The whole bug in one assertion: every file was excluded and ROMarr
    left it that way, so the torrent could never download a byte."""
    server = _Qbittorrent()
    server.seed("aaa", "Some Game [PSX]", [("Some Game.bin", 700), ("Some Game.cue", 1)])

    client(server).completed()

    priorities = [f["priority"] for f in server.torrents["aaa"]["files"]]
    assert any(priorities), "every file still marked do-not-download"


def test_a_single_file_archive_release_is_recovered():
    """Most ROM releases are one `.rar` or `.7z`, so the single-file case is
    the common one rather than an edge."""
    server = _Qbittorrent()
    server.seed("bbb", "Nascar Racing (Edge3D).7z", [("Nascar Racing (Edge3D).7z", 5627228)])

    client(server).completed()

    assert [f["priority"] for f in server.torrents["bbb"]["files"]] == [1]


def test_an_emptied_torrent_is_not_offered_to_the_importer():
    """It reports as finished with nothing on disk. Passing it on is how the
    importer comes to look for a file that was never fetched."""
    server = _Qbittorrent()
    server.seed("ccc", "Vagrant Story [PSX]", [("Vagrant Story.iso", 89_000)])

    assert client(server).completed() == []


def test_the_repaired_torrent_reports_its_real_size_afterwards():
    """`size` is what qBittorrent will fetch, so it moving off zero is the
    proof that the release is actually going to download now."""
    server = _Qbittorrent()
    server.seed("ddd", "Tekken 3 [PSX]", [("Tekken 3.bin", 480), ("readme.txt", 20)])

    client(server).completed()

    assert server._row("ddd")["size"] == 500


def test_a_genuinely_finished_download_still_reaches_the_importer():
    """The guard must not swallow the ordinary case it sits in front of."""
    server = _Qbittorrent()
    server.seed("eee", "Chrono Trigger (USA).sfc", [("Chrono Trigger (USA).sfc", 4_194_304)])

    done = client(server).completed()

    assert [row["name"] for row in done] == ["Chrono Trigger (USA).sfc"]
    assert server.prio_calls == []


def test_a_partly_selected_torrent_is_left_alone():
    """Somebody deselecting the bonus disc by hand is a choice, not this bug,
    and re-selecting it behind their back would download what they refused."""
    server = _Qbittorrent()
    server.seed("fff", "Parasite Eve [PSX]",
                [("Disc 1.iso", 700), ("Disc 2.sfc", 700)])
    server.torrents["fff"]["files"][1]["priority"] = 1

    client(server).completed()

    assert [f["priority"] for f in server.torrents["fff"]["files"]] == [0, 1]
    assert server.prio_calls == []


def test_every_file_index_is_sent_not_just_the_first():
    """A multi-part release re-selected one file at a time would still stall
    on the parts nobody asked for."""
    server = _Qbittorrent()
    server.seed("ggg", "Ys The Oath In Felghana",
                [(f"b-ysfe.part{n}.rar", 100) for n in range(1, 7)])

    client(server).completed()

    assert server.prio_calls[0]["id"] == "0|1|2|3|4|5"
    assert all(f["priority"] == 1 for f in server.torrents["ggg"]["files"])


def test_a_torrent_from_another_category_is_not_touched():
    """ROMarr repairs its own downloads. Sonarr's exclusions are Sonarr's
    business, and this instance has one of those too."""
    server = _Qbittorrent()
    server.seed("hhh", "Some Show S01E01", [("Some Show S01E01.zip", 900)],
                category="sonarr")

    client(server).completed()

    assert [f["priority"] for f in server.torrents["hhh"]["files"]] == [0]
