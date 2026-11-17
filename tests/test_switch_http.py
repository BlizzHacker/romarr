"""Authenticated Switch routes, cache behavior and persisted explicit ignores."""
from http.server import ThreadingHTTPServer
import threading
from types import SimpleNamespace
import requests
import pytest
from romarr.app import ROMarr, make_handler
from romarr.store import Store
from romarr.titled import TitleDBIndex, SwitchTitle

BASE="01007ef00011e000"
DLC="01007ef00011f001"


def service(tmp_path):
    """Minimal real service methods without startup schedulers or network."""
    app=ROMarr.__new__(ROMarr)
    app.store=Store(tmp_path / "state.json")
    app.library=tmp_path / "library"
    app.game_libraries=[]
    app._metadata_lock=threading.RLock()
    app._titled_lock=threading.RLock()
    app._switch_lock=threading.Lock()
    app._switch_cache=None
    app.env={"ROMARR_API_KEY":"switch-test-owner"}
    app.auth=SimpleNamespace(enabled=False)
    index=TitleDBIndex()
    index.add(SwitchTitle(BASE,versions={"65536":"today"}))
    index.add(SwitchTitle(DLC,"Expansion"))
    app.titled=index
    folder=app.library / "switch"
    folder.mkdir(parents=True)
    (folder / f"Example [{BASE}][v0].nsp").write_bytes(b"fixture")
    return app


def test_cached_gap_probe_never_scans_or_waits(tmp_path,monkeypatch):
    app=service(tmp_path)
    monkeypatch.setattr("romarr.app.build_switch_report",lambda *a:pytest.fail("scan started"))
    assert app.switch_updates(cached_only=True)["ready"] is False
    with app._switch_lock:
        assert app.switch_updates(cached_only=True)["ready"] is False


def test_report_ignore_persists_and_preserves_cached_facts(tmp_path):
    app=service(tmp_path)
    before=app.switch_updates()
    assert before["summary"]["dlc_gaps"]==1
    app.switch_ignore({"tid":DLC.upper(),"ignored":True})
    assert Store(app.store.path).settings["ignored_dlc"]==[DLC]
    after=app.switch_updates()
    assert after["summary"]["dlc_gaps"]==0
    assert after["items"][0]["dlc_ignored"][0]["tid"]==DLC.upper()
    assert len(app._switch_cache[2]["items"][0]["dlc_missing"])==1
    app.switch_ignore({"tid":DLC,"ignored":False})
    assert app.switch_updates()["summary"]["dlc_gaps"]==1


@pytest.mark.parametrize("body",[[],{}, {"tid":BASE,"ignored":True},{"tid":DLC,"ignored":"false"},{"tid":"../../secret","ignored":False}])
def test_ignore_rejects_ambiguous_inputs(tmp_path,body):
    with pytest.raises(ValueError):
        service(tmp_path).switch_ignore(body)


def test_actual_http_auth_and_input_validation(tmp_path):
    app=service(tmp_path)
    # Use the normal constructor to supply actual session/auth handler dependencies.
    real=ROMarr({"ROMARR_DATA":str(tmp_path/"http.json"),"ROMARR_API_KEY":"switch-test-owner","LIBRARY_PATH":str(app.library)})
    real.titled=app.titled
    server=ThreadingHTTPServer(("127.0.0.1",0),make_handler(real))
    worker=threading.Thread(target=server.serve_forever,daemon=True)
    worker.start()
    base=f"http://127.0.0.1:{server.server_port}"
    try:
        for method,path in [("get","updates"),("get","gaps"),("post","ignore")]:
            response=getattr(requests,method)(base+"/api/v1/switch/"+path,json={},timeout=5)
            assert response.status_code==401
        headers={"X-Api-Key":"switch-test-owner"}
        response=requests.get(base+"/api/v1/switch/updates",headers=headers,timeout=5)
        assert response.status_code==200 and response.json()["items"][0]["update"]=="missing"
        response=requests.post(base+"/api/v1/switch/ignore",headers=headers,json={"tid":BASE,"ignored":True},timeout=5)
        assert response.status_code==400
        response=requests.post(base+"/api/v1/switch/ignore",headers=headers,json={"tid":DLC,"ignored":True},timeout=5)
        assert response.status_code==200
    finally:
        server.shutdown();server.server_close();worker.join(timeout=5)
