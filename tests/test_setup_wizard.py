"""The guide is optional, private, and safe to skip."""
import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from romarr import app
from romarr.app import ROMarr, make_handler


def service(tmp_path):
    return ROMarr({"ROMARR_DATA": str(tmp_path / "state.json"),
                   "ROMARR_API_KEY": "testkey"})


def test_first_run_guide_skip_survives_restart(tmp_path):
    svc = service(tmp_path)
    assert svc.setup_status()["suggested"]
    svc.finish_setup_guide("download")
    state = service(tmp_path).setup_status()
    assert not state["suggested"] and state["dismissed"]
    assert state["mode"] == "download"


@pytest.mark.parametrize("key", ["libraries", "download_clients", "indexers"])
def test_existing_configuration_is_not_forced_through_setup(tmp_path, key):
    svc = service(tmp_path)
    svc.store.put_item(key, {"type": "custom", "name": "Existing", "enable": False})
    assert not svc.setup_status()["suggested"]


def test_status_only_returns_safe_checklist_fields(tmp_path):
    svc = service(tmp_path)
    for key in ("libraries", "download_clients", "indexers"):
        svc.store.put_item(key, {
            "type": "custom", "name": "My service", "url": "http://private",
            "api_key": "do-not-expose-key", "password": "do-not-expose-password",
        })
    result = svc.setup_status()
    assert "do-not-expose" not in json.dumps(result)
    assert "http://private" not in json.dumps(result)
    assert set(result["libraries"][0]) == {"id", "name", "type", "enabled"}


def test_dat_validation_explains_missing_or_empty_folders(tmp_path):
    svc = service(tmp_path)
    assert "Choose the folder" in svc.setup_dats("")["error"]
    assert "not available to ROMarr" in svc.setup_dats(str(tmp_path / "missing"))["error"]
    assert not svc.store.settings.get("dat_path")
    folder = tmp_path / "empty"
    folder.mkdir()
    assert "No readable DATs" in svc.setup_dats(str(folder))["error"]


def test_dat_loader_applies_and_persists_optional_folder(tmp_path):
    svc = service(tmp_path)
    folder = tmp_path / "dats"
    folder.mkdir()
    (folder / "test.dat").write_text(
        '<datafile><header><name>Test</name></header><game name="Game">'
        '<rom name="Game.bin" size="1" crc="aa"/></game></datafile>')
    assert svc.setup_dats(str(folder))["loaded"] == 1
    assert svc.setup_status()["dat_games"] == 1
    assert service(tmp_path).store.settings["dat_path"] == str(folder)


@pytest.fixture
def server(tmp_path):
    svc = service(tmp_path)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(svc))
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}", svc
    httpd.shutdown()
    httpd.server_close()


def request(base, path, method="GET", body=None, key=None):
    headers = {"Content-Type": "application/json"}
    if key:
        headers["X-Api-Key"] = key
    req = urllib.request.Request(base + path, method=method, headers=headers,
                                 data=None if body is None else json.dumps(body).encode())
    try:
        with urllib.request.urlopen(req, timeout=5) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read())


@pytest.mark.parametrize("path,method,body", [
    ("/api/v1/setup/status", "GET", None),
    ("/api/v1/setup/complete", "POST", {"mode": "download"}),
    ("/api/v1/setup/dats", "POST", {"directory": "/private"}),
])
def test_setup_routes_require_authentication(server, path, method, body):
    base, svc = server
    assert request(base, path, method, body)[0] == 401
    assert not svc.store.settings.get("_setup_guide")


def test_authenticated_guide_can_be_skipped_and_reopened(server):
    base, _ = server
    assert request(base, "/api/v1/setup/status", key="testkey")[1]["suggested"]
    assert request(base, "/api/v1/setup/complete", "POST", {"mode": "organize"},
                   "testkey") == (200, {"ok": True})
    assert request(base, "/api/v1/setup/status", key="testkey")[1]["dismissed"]


@pytest.mark.parametrize("env,host", [({}, "0.0.0.0"),
                                    ({"ROMARR_HOST": "127.0.0.1"}, "127.0.0.1")])
def test_explicit_loopback_preserves_container_default(monkeypatch, env, host):
    calls = []
    svc = SimpleNamespace(
        store=SimpleNamespace(recover_seerr_dispatches=lambda: None), library="/roms")
    monkeypatch.setattr(app, "ROMarr", lambda env: svc)
    monkeypatch.setattr(app, "ThreadingHTTPServer",
                        lambda address, handler: calls.append(address)
                        or SimpleNamespace(serve_forever=lambda: None))
    app.serve(6868, env)
    assert calls == [(host, 6868)]

def test_existing_rom_path_skips_download_steps_and_finish_is_optional():
    import shutil
    import subprocess
    from romarr import ui

    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js needed for the guide interaction check")
    start = ui.JS.index("const SETUP=")
    end = ui.JS.index("RENDER.getstarted=", start)
    harness = r"""
const assert=require('assert');
const RENDER={},nodes={},modes=['organize','download'].map(mode=>({dataset:{setupMode:mode}}));
const state={libraries:[],download_clients:[],indexers:[],dat_path:'',dat_loaded:0,hub_available:false,mode:'organize'};
const calls=[];
const $=selector=>nodes[selector]||(nodes[selector]={innerHTML:'',value:'',className:'',disabled:false});
const document={querySelectorAll:selector=>selector==='[data-setup-mode]'?modes:[]};
const j=async(url,options)=>{calls.push([url,options]);return url.endsWith('/status')?state:{ok:true};};
const esc=x=>String(x??''); const fmt=x=>String(x); const toast=()=>{};
const go=page=>calls.push(['go',page]);
const editItem=()=>{};const addItem=()=>{};
"""
    checks = r"""
(async()=>{
 await RENDER.setup();
 assert($('#page').innerHTML.includes('I already have ROMs'));
 assert(!$('#page').innerHTML.includes('3. Downloads'));
 $('#setup-next').onclick(); await new Promise(setImmediate);
 assert($('#page').innerHTML.includes('Where should your ROMs live?'));
 $('#setup-next').onclick(); await new Promise(setImmediate);
 assert($('#page').innerHTML.includes('Add confidence and more sources'));
 assert(!calls.some(c=>c[0]==='/api/v1/setup/complete'));
 SETUP.step=0; await RENDER.setup();
 modes[1].onclick();await new Promise(setImmediate);
 assert($('#page').innerHTML.includes('3. Downloads'));
 $('#setup-dismiss').onclick();await new Promise(setImmediate);
 assert(calls.some(c=>c[0]==='/api/v1/setup/complete'));
 assert(calls.some(c=>c[0]==='go'&&c[1]==='library'));
})().catch(error=>{console.error(error);process.exitCode=1;});
"""
    result = subprocess.run([node, "-e", harness + ui.JS[start:end] + checks],
                            text=True, capture_output=True)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("malformed", [False, True])
def test_failed_optional_dat_check_preserves_working_index_and_path(tmp_path, malformed):
    svc = service(tmp_path)
    working = tmp_path / "working-dats"
    working.mkdir()
    (working / "system.dat").write_text(
        '<datafile><header><name>Working</name></header><game name="Game">'
        '<rom name="Game.bin" size="1" crc="aa"/></game></datafile>')
    assert svc.setup_dats(str(working))["ok"]
    previous_index = svc.dats
    rejected = tmp_path / "other-dats"
    rejected.mkdir()
    if malformed:
        (rejected / "broken.dat").write_text("this is not a DAT")
    result = svc.setup_dats(str(rejected))
    assert result["ok"] is False
    assert svc.dats is previous_index
    assert svc.setup_status()["dat_loaded"] == 1
    assert svc.store.settings["dat_path"] == str(working)
    assert service(tmp_path).store.settings["dat_path"] == str(working)


@pytest.mark.parametrize("malformed", [False, True])
def test_failed_optional_dat_candidate_is_never_published(tmp_path, monkeypatch, malformed):
    svc = service(tmp_path)
    working = tmp_path / "working-dats"
    working.mkdir()
    (working / "system.dat").write_text(
        '<datafile><header><name>Working</name></header><game name="Game">'
        '<rom name="Game.bin" size="1" crc="aa"/></game></datafile>')
    assert svc.setup_dats(str(working))["ok"]
    rejected = tmp_path / "other-dats"
    rejected.mkdir()
    if malformed:
        (rejected / "broken.dat").write_text("this is not a DAT")
    publications = []
    assign = ROMarr.__setattr__

    def observe_assignment(instance, name, value):
        if instance is svc and name == "dats":
            publications.append(value)
        assign(instance, name, value)

    class ObservedSettings(dict):
        def __setitem__(self, key, value):
            if key == "dat_path":
                publications.append(value)
            super().__setitem__(key, value)

    svc.store.settings = ObservedSettings(svc.store.settings)
    monkeypatch.setattr(ROMarr, "__setattr__", observe_assignment)
    monkeypatch.setattr(svc, "reload_titled", lambda: pytest.fail(
        "failed DAT setup must not refresh unrelated state"))

    assert svc.setup_dats(str(rejected))["ok"] is False
    assert publications == [], "failed index/path must never become active, even temporarily"


def test_explicit_dat_selection_survives_environment_default_on_restart(tmp_path):
    seed, chosen = tmp_path / "environment-dats", tmp_path / "chosen-dats"
    for folder, name in [(seed, "Seed"), (chosen, "Chosen")]:
        folder.mkdir()
        (folder / "test.dat").write_text(
            '<datafile><header><name>' + name + '</name></header>'
            '<game name="Game"><rom name="Game.bin" size="1" crc="aa"/>'
            '</game></datafile>')
    env = {"ROMARR_DATA": str(tmp_path / "state.json"),
           "ROMARR_API_KEY": "testkey", "DAT_PATH": str(seed)}
    svc = ROMarr(env)
    svc.scheduler.stop()
    assert svc.dats.dats[0].name == "Seed"
    assert svc.setup_dats(str(chosen))["ok"]
    restarted = ROMarr(env)
    restarted.scheduler.stop()
    assert restarted.store.settings["dat_path"] == str(chosen)
    assert restarted.dats.dats[0].name == "Chosen"
