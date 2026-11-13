import zipfile
from pathlib import Path
from types import SimpleNamespace
import pytest
from romarr.import_intent import preview_import, choose_import
from romarr.romm_bundle import decorate_preview, route_selection
from romarr.library import import_rom
from romarr.platforms import resolve
from romarr.app import ROMarr
from romarr.store import QueueItem, Store, WantedItem


def bundle(tmp_path, names=("Fixture Game.nsp", "Fixture Game update.nsp", "Fixture Game DLC.nsp")):
    archive = tmp_path / "bundle.zip"
    with zipfile.ZipFile(archive,"w") as z:
        for i,name in enumerate(names): z.writestr(name, bytes([i+1])*32768)
    return archive


def plan(archive, root, layout="flat"):
    return decorate_preview(preview_import(archive,resolve("switch"),root,layout=layout),resolve("switch"),root,kind="romm",layout=layout)


def roles():
    return [{"primary":name,"role":role} for name,role in zip(["Fixture Game.nsp","Fixture Game update.nsp","Fixture Game DLC.nsp"],["base","update","dlc"])]


@pytest.mark.parametrize("layout,prefix",[("flat","switch"),("nested","switch/roms")])
def test_reviewed_roles_route_to_one_romm_game(tmp_path,layout,prefix):
    source=bundle(tmp_path);root=tmp_path/"library";preview=plan(source,root,layout)
    intent=choose_import(preview,roles());routes=route_selection(preview,intent)
    assert routes=={"Fixture Game.nsp":"Fixture Game/Fixture Game.nsp","Fixture Game update.nsp":"Fixture Game/update/Fixture Game update.nsp","Fixture Game DLC.nsp":"Fixture Game/dlc/Fixture Game DLC.nsp"}
    outcomes=import_rom(source,resolve("switch"),root,layout=layout,selected_primaries=list(routes),bundle_routes=routes)
    assert all(o.ok for o in outcomes)
    assert set(str(p.relative_to(root)) for p in root.rglob("*.nsp"))=={prefix+"/"+route for route in routes.values()}
    assert source.exists()


def test_optional_content_with_multiple_bases_requires_association(tmp_path):
    source=bundle(tmp_path,("A.nsp","B.nsp","Update.nsp"));preview=plan(source,tmp_path/"library")
    choices=[{"primary":n,"role":r} for n,r in [("A.nsp","base"),("B.nsp","additional-game"),("Update.nsp","update")]]
    with pytest.raises(ValueError,match="explicit association"):
        route_selection(preview,choose_import(preview,choices,"collection"))


def test_loose_base_requires_review_and_remains_untouched(tmp_path):
    source=bundle(tmp_path);root=tmp_path/"library";(root/"switch").mkdir(parents=True)
    loose=root/"switch/Fixture Game.nsp";loose.write_bytes(b"original")
    preview=plan(source,root)
    with pytest.raises(ValueError,match="loose file"):
        route_selection(preview,choose_import(preview,roles()))
    assert loose.read_bytes()==b"original" and not (root/"switch/Fixture Game").exists()


def test_collision_and_symlink_destinations_preserve_library(tmp_path):
    source=bundle(tmp_path);root=tmp_path/"library";target=root/"switch/Fixture Game/update"
    target.mkdir(parents=True);existing=target/"Fixture Game update.nsp";existing.write_bytes(b"old")
    preview=plan(source,root)
    with pytest.raises(ValueError,match="already exists"):
        route_selection(preview,choose_import(preview,roles()))
    assert existing.read_bytes()==b"old"
    existing.unlink();target.rmdir();target.symlink_to(tmp_path,target_is_directory=True)
    preview=plan(source,root)
    (target.parent/"Existing.nsp").write_bytes(b"base")
    preview=plan(source,root)
    with pytest.raises(ValueError,match="symlink"):
        route_selection(preview,choose_import(preview,[roles()[1]],existing_game="Fixture Game"))


def service_fixture(tmp_path,monkeypatch,names=None):
    source=bundle(tmp_path,names) if names else bundle(tmp_path)
    service=ROMarr({"ROMARR_DATA":str(tmp_path/"state.json"),"ROMARR_API_KEY":"testkey"})
    row=QueueItem("Fixture Game","switch","bundle",1,"grabbed",download_id="fixture")
    service.queue.append(row)
    service.clients=[SimpleNamespace(configured=True,completed=lambda:[{"name":"bundle","id":"fixture","content_path":str(source)}])]
    cfg={"path":str(tmp_path/"library"),"type":"romm"}
    monkeypatch.setattr(service,"library_for",lambda slug:(cfg,SimpleNamespace(rescan=lambda slug:None)))
    monkeypatch.setattr(service,"_virus_check",lambda *a:None)
    service.store.settings["reconcile_after_import"]=False
    return service,row,source


def test_hub_empty_preview_remains_accessible_after_review_pause(tmp_path, monkeypatch):
    """Verify hub bundles paused in import-review remain previewable even when items list is empty."""
    from romarr.downloaders import HubConfig, HubDownloader
    service, row, source = service_fixture(tmp_path, monkeypatch, ("manual.pdf",))
    row.download_id = ""
    client = HubDownloader(HubConfig(save_path=str(tmp_path)))
    client._write([{"name": "bundle", "state": "done", "file": str(source)}])
    service.clients = [client]
    service.store.settings["auto_import"] = False
    assert service.queue_rows()[0]["can_preview_import"]
    assert service.queue_import_plan(0, {})["preview"]["extras"] == ["manual.pdf"]
    assert not service.import_finished()[0]["ok"]
    assert row.state == "import-review" and row.import_preview["items"] == []
    assert service.queue_rows()[0]["can_preview_import"]
    assert service.queue_import_plan(0, {})["preview"]["extras"] == ["manual.pdf"]


def test_http_requires_final_tree_review_and_persists_roles(tmp_path,monkeypatch):
    import requests,threading
    from http.server import ThreadingHTTPServer
    from romarr.app import make_handler
    service,row,source=service_fixture(tmp_path,monkeypatch)
    service.store.save();before=(tmp_path/"state.json").read_bytes()
    server=ThreadingHTTPServer(("127.0.0.1",0),make_handler(service));threading.Thread(target=server.serve_forever,daemon=True).start()
    base=f"http://127.0.0.1:{server.server_address[1]}/api/v1/queue/import-plan";headers={"X-Api-Key":"testkey"}
    try:
        p=requests.get(base,params={"index":0},headers=headers).json()["preview"]
        body={"index":0,"token":p["token"],"choices":roles(),"mode":"single"}
        assert "Review the final" in requests.post(base,headers=headers,json={**body,"confirm":True}).json()["error"]
        reviewed=requests.post(base,headers=headers,json={**body,"review":True}).json()["preview"]
        assert reviewed["planned_destinations"]["Fixture Game DLC.nsp"].endswith("/Fixture Game/dlc/Fixture Game DLC.nsp")
        assert (tmp_path/"state.json").read_bytes()==before and not (tmp_path/"library").exists()
        answer=requests.post(base,headers=headers,json={**body,"confirm":True,"reviewed_destinations":reviewed["planned_destinations"]}).json()
        assert answer["ok"]
        assert Store(tmp_path/"state.json").queue[0].import_intent["choices"]==roles()
        assert service.import_finished()[0]["ok"]
        assert source.exists() and row.state=="imported"
    finally:server.shutdown();server.server_close()


def test_attachment_retains_base_and_does_not_fulfil_missing_request(tmp_path,monkeypatch):
    service,row,source=service_fixture(tmp_path,monkeypatch,("Later update.nsp",))
    game=tmp_path/"library/switch/Existing Game";game.mkdir(parents=True);base=game/"Existing Game.nsp";base.write_bytes(b"unchanged base")
    service.store.wanted.append(WantedItem("Fixture Game","switch"))
    row.external_request_id="missing-base-request"
    calls=[];monkeypatch.setattr(service.store,"update_seerr_request",lambda *a,**kw:calls.append(a));monkeypatch.setattr(service.store,"fulfil",lambda *a:calls.append(a));monkeypatch.setattr(service,"reconcile_platform",lambda *a:calls.append(a))
    p=service.queue_import_plan(0,{})["preview"]
    body={"token":p["token"],"choices":[{"primary":"Later update.nsp","role":"update"}],"existing_game":"Existing Game"}
    reviewed=service.queue_import_plan(0,body)["preview"]
    assert service.queue_import_plan(0,{**body,"confirm":True,"reviewed_destinations":reviewed["planned_destinations"]})["ok"]
    assert service.import_finished()[0]["ok"]
    assert base.read_bytes()==b"unchanged base"
    assert (game/"update/Later update.nsp").exists() and source.exists()
    assert not calls and service.store.wanted


@pytest.mark.parametrize("name",["NUL.nsp","bad\x00.nsp","bad\nname.nsp"])
def test_unsafe_game_folder_names_rejected_before_writes(tmp_path,name):
    from romarr.romm_bundle import _folder
    with pytest.raises(ValueError):_folder(Path(name).stem)


def test_new_base_cannot_silently_join_another_existing_base(tmp_path):
    source=bundle(tmp_path);root=tmp_path/"library";folder=root/"switch/Fixture Game";folder.mkdir(parents=True);old=folder/"Different base.nsp";old.write_bytes(b"original")
    preview=plan(source,root)
    with pytest.raises(ValueError,match="already exists"):
        route_selection(preview,choose_import(preview,roles()))
    assert old.read_bytes()==b"original" and len(list(folder.iterdir()))==1


def test_non_directory_parent_is_reviewable_error(tmp_path):
    source=bundle(tmp_path);root=tmp_path/"library";(root/"switch").mkdir(parents=True);parent=root/"switch/Fixture Game";parent.write_bytes(b"not a folder")
    preview=plan(source,root)
    with pytest.raises(ValueError,match="not a directory"):
        route_selection(preview,choose_import(preview,roles()))
    assert parent.read_bytes()==b"not a folder"


def test_translation_preview_matches_actual_destination(tmp_path):
    source=bundle(tmp_path);root=tmp_path/"library";platform=resolve("switch")
    preview=decorate_preview(preview_import(source,platform,root),platform,root,kind="romm",translation=True)
    routes=route_selection(preview,choose_import(preview,roles()))
    outcomes=import_rom(source,platform,root,translation=True,selected_primaries=list(routes),bundle_routes=routes)
    assert all(o.ok for o in outcomes)
    assert {str(o.destination) for o in outcomes}=={str(Path(preview["platform_root"])/relative) for relative in routes.values()}


def test_route_directory_permission_failure_returns_failed_outcome(tmp_path,monkeypatch):
    source=bundle(tmp_path);root=tmp_path/"library";preview=plan(source,root)
    routes={"Fixture Game update.nsp":"Fixture Game/update/Fixture Game update.nsp"}
    original=Path.mkdir
    def denied(path,*a,**kw):
        if path.name=="update":raise PermissionError("synthetic denied directory")
        return original(path,*a,**kw)
    monkeypatch.setattr(Path,"mkdir",denied)
    result,=import_rom(source,resolve("switch"),root,selected_primaries=list(routes),bundle_routes=routes)
    assert not result.ok and "destination preparation failed" in result.reason
    assert source.exists() and not (root/"switch/Fixture Game/update/Fixture Game update.nsp").exists()


def test_a_nup_is_not_an_existing_game_package():
    """RomM bundles are routed around .nsp/.xci packages; a NUP is a whole
    dump in one file, so it never marks a folder as an existing game."""
    from romarr.romm_bundle import PACKAGE_SUFFIXES

    assert ".nup" not in PACKAGE_SUFFIXES
