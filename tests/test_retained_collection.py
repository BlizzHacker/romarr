"""Synthetic qBittorrent protocol and independent per-file completion proofs."""
from copy import deepcopy
from types import SimpleNamespace
import threading
import zipfile
import pytest
from romarr.clients import QBittorrent, QbitConfig
from romarr.store import QueueItem
from romarr.app import ROMarr
from romarr.selection import Release

HASH = "a" * 40
MAGNET = "magnet:?xt=urn:btih:" + HASH


class Response:
    ok = True
    status_code = 200
    text = "Ok."
    def __init__(self, body=None): self.body = body
    def json(self): return deepcopy(self.body)
    def raise_for_status(self): pass


class Session:
    def __init__(self):
        self.files = [dict(index=0, name="Collection/A.zip", size=100, priority=7, progress=1),
                      dict(index=1, name="Collection/B.zip", size=100, priority=0, progress=0),
                      dict(index=2, name="Collection/C.zip", size=100, priority=0, progress=0)]
        self.row = dict(hash=HASH, tags="romarr-collection-A", category="romarr",
                        state="uploading", name="Collection", save_path="/downloads", content_path="/downloads/Collection")
        self.posts = []
    def get(self, url, *, params=None, **kwargs):
        endpoint = url.split("/api/v2/")[-1]
        if endpoint == "torrents/files": return Response(self.files)
        if endpoint == "torrents/info":
            if params and params.get("filter") == "completed" and any(f["priority"] and f["progress"] != 1 for f in self.files):
                return Response([])
            return Response([self.row])
        raise AssertionError(endpoint)
    def post(self, url, *, data=None, **kwargs):
        endpoint = url.split("/api/v2/")[-1]
        self.posts.append((endpoint, deepcopy(data)))
        if endpoint == "torrents/filePrio":
            for index in map(int, data["id"].split("|")):
                self.files[index]["priority"] = int(data["priority"])
            self.row["state"] = "downloading"
        elif endpoint == "torrents/addTags":
            self.row["tags"] += "," + data["tags"]
        elif endpoint in ("torrents/resume", "torrents/start"):
            self.row["state"] = "downloading"
        else:
            raise AssertionError("destructive or unexpected operation: " + endpoint)
        return Response()


def make_client():
    session = Session()
    client = QBittorrent(QbitConfig("http://synthetic-qbit"), session=session)
    client.romarr_id = "qbit-one"
    return client, session


def owner(name="A", index=0, size=100, tag="romarr-collection-A"):
    return QueueItem(name, "gb", name, 1, "grabbed", download_id="qbit-tag:"+tag,
                     download_client="qbit-one", file_selection_required=True,
                     selected_file=dict(hash=HASH,index=index,path=f"Collection/{name}.zip",size=size))


def test_reuse_is_additive_and_preserves_priorities_no_add_delete_resume():
    client, session = make_client()
    assert client.add_magnet_selective(MAGNET, "Collection/B.zip", owned_selections=[owner()])
    assert [f["priority"] for f in session.files] == [7,1,0]
    assert [p[0] for p in session.posts] == ["torrents/filePrio", "torrents/addTags"]
    assert client.last_selected_file == dict(hash=HASH,index=1,path="Collection/B.zip",size=100)
    assert client.last_job_id != "qbit-tag:romarr-collection-A"


@pytest.mark.parametrize("mutation", ["category", "foreign-tag", "missing-owner", "wrong-client", "changed-file"])
def test_uncertain_ownership_preserves_everything(mutation):
    client, session = make_client()
    original = owner()
    owners = [original]
    if mutation == "category": session.row["category"] = "another-tool"
    if mutation == "foreign-tag": session.row["tags"] += ",another-tool"
    if mutation == "missing-owner": owners = []
    if mutation == "wrong-client": original.download_client = "other-client"
    if mutation == "changed-file": session.files[0]["size"] += 1
    before = deepcopy(session.files)
    assert not client.add_magnet_selective(MAGNET, "Collection/B.zip", owned_selections=owners)
    assert "review" in client.last_error
    assert session.posts == [] and session.files == before


def service_with_retained(tmp_path):
    service = ROMarr({"ROMARR_DATA":str(tmp_path/"state.json")})
    client, session = make_client()
    service.clients = [client]
    service.store.enqueue(owner())
    return service, client, session


def test_queue_preview_tracks_selected_file_completion_without_writes(tmp_path):
    """Verify queue_rows() reflects per-file completion without mutating qBittorrent or store state."""
    service, client, session = service_with_retained(tmp_path)
    service.queue.append(owner("B", 1, tag="romarr-collection-B"))
    session.row["tags"] += ",romarr-collection-B"
    session.files[1]["priority"] = 1
    original = (tmp_path / "state.json").read_bytes()
    assert [r["can_preview_import"] for r in service.queue_rows()] == [True, False]
    assert session.posts == []
    assert client._selective_hashes == set()
    assert (tmp_path / "state.json").read_bytes() == original


def release(name):
    return Release(title=name,size=100,seeders=1,categories=(1000,),download_url=MAGNET,
                   protocol="torrent",indexer="hub:minerva-archive",
                   torrent_file_path=f"Collection/{name}.zip",file_selection_required=True)


def test_stopped_torrent_requires_explicit_resume_and_preserves_reference(tmp_path):
    service, client, session = service_with_retained(tmp_path)
    session.row["state"] = "stoppedUP"
    result = service.grab(release("B"), "B", "gb")
    assert not result["ok"] and "Resume retained torrent" in client.last_error
    assert session.posts == []
    assert service.queue[-1].retained_torrent_hash == HASH
    assert service.queue_action(1,"resume-retained")["ok"]
    assert session.posts[-1][0] == "torrents/resume"
    assert service.grab(release("B"), "B", "gb")["ok"]
    assert [f["priority"] for f in session.files] == [7,1,0]


def test_overlapping_grabs_serialize_through_durable_ownership(tmp_path):
    service, client, session = service_with_retained(tmp_path)
    outcomes = []
    threads = [threading.Thread(target=lambda n=n: outcomes.append(service.grab(release(n),n,"gb"))) for n in ["B","C"]]
    for t in threads: t.start()
    for t in threads: t.join()
    assert len(outcomes) == 2 and all(r["ok"] for r in outcomes)
    assert [f["priority"] for f in session.files] == [7,1,1]
    assert not any(p[0] in {"torrents/add","torrents/delete","torrents/pause","torrents/resume"} for p in session.posts)
    reloaded = ROMarr({"ROMARR_DATA":str(tmp_path/"state.json")})
    assert len(reloaded.store.settings["retained_collection_owners"]) == 3
    assert len({q.download_id for q in reloaded.queue}) == 3


def test_clearing_imported_queue_preserves_ownership_after_restart(tmp_path):
    service, client, session = service_with_retained(tmp_path)
    service._remember_collection_owners()
    service.queue[0].state = "imported"
    service.clear_queue()
    service.store.save()
    reloaded = ROMarr({"ROMARR_DATA":str(tmp_path/"state.json")})
    reloaded.clients = [client]
    assert not reloaded.queue
    assert reloaded.grab(release("B"),"B","gb")["ok"]
    assert [f["priority"] for f in session.files] == [7,1,0]


def test_per_file_completion_imports_a_before_b_and_never_deletes_shared_sources(tmp_path,monkeypatch):
    service, client, session = service_with_retained(tmp_path)
    source = tmp_path/"downloads"/"Collection"
    source.mkdir(parents=True)
    for name in ["A","B"]:
        archive = source/f"{name}.zip"
        with zipfile.ZipFile(archive,"w") as z: z.writestr(f"{name}.gb",name.encode()*32768)
        session.files[0 if name=="A" else 1]["size"] = archive.stat().st_size
    service.queue[0].selected_file["size"] = session.files[0]["size"]
    session.row.update(save_path=str(source.parent),content_path=str(source))
    service.store.settings.update(retain_downloads_after_import=False,reconcile_after_import=False)
    cfg = {"path":str(tmp_path/"library")}
    monkeypatch.setattr(service,"library_for",lambda slug:(cfg,SimpleNamespace(rescan=lambda slug:None)))
    assert service.grab(release("B"),"B","gb")["ok"]
    # Recreate service and client: no in-memory ownership survives.
    service = ROMarr({"ROMARR_DATA":str(tmp_path/"state.json")})
    client = QBittorrent(QbitConfig("http://synthetic-qbit"),session=session)
    client.romarr_id = "qbit-one"
    service.clients = [client]
    monkeypatch.setattr(service,"library_for",lambda slug:(cfg,SimpleNamespace(rescan=lambda slug:None)))
    assert service.import_finished(retry_failed=False)[0]["ok"]
    assert [q.state for q in service.queue] == ["imported","grabbed"]
    assert (tmp_path/"library"/"gb"/"A.gb").exists()
    assert not (tmp_path/"library"/"gb"/"B.gb").exists()
    session.files[1]["progress"] = 1
    assert service.import_finished(retry_failed=False)[0]["ok"]
    assert [q.state for q in service.queue] == ["imported","imported"]
    assert (tmp_path/"library"/"gb"/"B.gb").exists()
    assert all((source/f"{name}.zip").exists() for name in ["A","B"])
    assert not any(p[0] == "torrents/delete" for p in session.posts)


def test_shared_remove_is_refused():
    client,session=make_client()
    session.row["tags"] += ",romarr-collection-B"
    assert not client.remove_download("qbit-tag:romarr-collection-A",delete_files=True)
    assert session.posts == []


def test_ownership_loss_becomes_visible_review(tmp_path):
    service,client,session=service_with_retained(tmp_path)
    session.row["category"] = "foreign"
    results=service.import_finished(retry_failed=False)
    assert results and not results[0]["ok"]
    assert "review" in service.queue[0].detail
    assert session.posts == []


def test_interrupted_additive_mutation_retains_prior_selection_and_can_retry(monkeypatch):
    import requests
    client, session = make_client()
    post = session.post
    def interrupted(url, **kwargs):
        response = post(url, **kwargs)
        if url.endswith("torrents/filePrio"):
            raise requests.ConnectionError("synthetic interrupted response")
        return response
    monkeypatch.setattr(session,"post",interrupted)
    assert not client.add_magnet_selective(MAGNET,"Collection/B.zip",owned_selections=[owner()])
    assert [f["priority"] for f in session.files] == [7,1,0]
    assert not any(p[0] == "torrents/delete" for p in session.posts)
    monkeypatch.setattr(session,"post",post)
    assert client.add_magnet_selective(MAGNET,"Collection/B.zip",owned_selections=[owner()])
    assert [f["priority"] for f in session.files] == [7,1,0]


def test_changed_tags_during_selection_are_not_silently_adopted(monkeypatch):
    client,session=make_client()
    post=session.post
    def takeover(url,**kwargs):
        response=post(url,**kwargs)
        if url.endswith("torrents/filePrio"):
            session.row["tags"] += ",foreign-controller"
        return response
    monkeypatch.setattr(session,"post",takeover)
    assert not client.add_magnet_selective(MAGNET,"Collection/B.zip",owned_selections=[owner()])
    assert "review" in client.last_error
    assert [f["priority"] for f in session.files] == [7,1,0]
    assert not any(p[0] in {"torrents/delete","torrents/addTags"} for p in session.posts)


def test_shared_collection_preview_uses_per_file_completion(tmp_path,monkeypatch):
    service,client,session=service_with_retained(tmp_path)
    source=tmp_path/"downloads"/"Collection"
    source.mkdir(parents=True)
    archive=source/"A.zip"
    with zipfile.ZipFile(archive,"w") as z:z.writestr("A.gb",b"a"*32768)
    session.files[0]["size"]=archive.stat().st_size
    service.queue[0].selected_file["size"]=session.files[0]["size"]
    session.row.update(save_path=str(source.parent),content_path=str(source))
    cfg={"path":str(tmp_path/"library")}
    monkeypatch.setattr(service,"library_for",lambda slug:(cfg,SimpleNamespace(rescan=lambda slug:None)))
    assert service.grab(release("B"),"B","gb")["ok"]
    assert client.completed()==[]
    preview=service.queue_import_plan(0,{})
    assert preview["ok"] and preview["preview"]["items"][0]["primary"]=="A.gb"
    assert not (tmp_path/"library").exists()


def test_rejected_nonmagnet_does_not_inherit_previous_retained_job():
    client,session=make_client()
    session.row["state"]="stoppedUP"
    assert not client.add_magnet_selective(MAGNET,"Collection/B.zip",owned_selections=[owner()])
    assert client.last_retained_hash==HASH
    assert not client.add_magnet_selective("https://example.test/not-a-magnet","Collection/C.zip",owned_selections=[owner()])
    assert client.last_retained_hash=="" and client.last_selected_file=={} and client.last_job_id==""
    assert session.posts==[]
