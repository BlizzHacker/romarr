"""Install metadata stays public and constant while authenticated data stays online."""
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import shutil
import struct
import subprocess
import threading
from urllib.request import urlopen
from urllib.error import HTTPError

import pytest
from romarr.app import ROMarr, make_handler
from romarr import ui

ROOT = Path(__file__).resolve().parents[1]


def test_public_install_assets_do_not_open_private_routes(tmp_path):
    service = ROMarr({"ROMARR_DATA": str(tmp_path / "state.json"), "ROMARR_API_KEY": "private-test-key"})
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(service))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    origin = f"http://127.0.0.1:{server.server_port}"
    try:
        for path, content_type in [("/manifest.webmanifest", "application/manifest+json"),
                                   ("/service-worker.js", "application/javascript"),
                                   ("/app-icon-192.png", "image/png"), ("/app-icon-512.png", "image/png")]:
            with urlopen(origin + path) as response:
                body = response.read()
                assert response.headers.get_content_type() == content_type
                assert b"private-test-key" not in body
        with pytest.raises(HTTPError) as error:
            urlopen(origin + "/api/v1/settings")
        assert error.value.code == 401
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_manifest_identity_icons_and_online_only_worker():
    manifest = json.loads(ui.web_manifest())
    assert manifest["id"] == manifest["start_url"] == manifest["scope"] == "/"
    assert manifest["display"] == "standalone"
    assert {icon["sizes"] for icon in manifest["icons"]} == {"192x192", "512x512"}
    for size in (192, 512):
        png = ui.app_icon(size)
        assert png.startswith(b"\x89PNG\r\n\x1a\n")
        assert struct.unpack(">II", png[16:24]) == (size, size)
    worker = ui.service_worker()
    assert "fetch" not in worker
    assert "caches" not in worker
    assert "localStorage" not in worker
    assert 'rel="manifest"' in ui.page()
    assert 'aria-label="Filter current page"' in ui.page()


def test_worker_never_intercepts_requests_or_writes_browser_storage():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is needed to execute the worker")
    script = """const vm=require('node:vm');const events={};let claimed=0,skipped=0;
    const self={addEventListener:(name,fn)=>events[name]=fn,skipWaiting:()=>skipped++,clients:{claim:()=>{claimed++;return Promise.resolve();}}};
    vm.runInNewContext(WORKER,{self});
    if(Object.keys(events).sort().join(',')!=='activate,install')throw Error('Unexpected worker interception');
    events.install();events.activate({waitUntil:p=>{}});
    if(claimed!==1||skipped!==1)throw Error('Lifecycle failed');
    """.replace("WORKER", json.dumps(ui.service_worker()))
    subprocess.run([node, "-e", script], check=True)


def test_extension_rejects_unsafe_profiles_and_packages_reproducibly(tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is needed to execute extension validation")
    script = """const {serverURL,serverProfile}=require('./desktop/chrome/validation.js');
    const assert=require('node:assert/strict');
    for(const value of ['javascript:alert(1)','file:///tmp','https://user:secret@host/','https://host/?apikey=secret','https://host/#key','https://host/?','https://host/#','https://host/\\\\evil','https://host/with space','https://host/\\n'])assert.throws(()=>serverURL(value),value);
    assert.equal(serverURL('https://example.com/base'),'https://example.com/base/');
    assert.equal(serverURL('http://192.168.0.6:8877'),'http://192.168.0.6:8877/');
    assert.equal(serverURL('http://[::1]:8877/'),'http://[::1]:8877/');
    assert.throws(()=>serverProfile({name:' ',url:'https://host/'}));
    assert.equal(serverProfile({name:' Home ',url:'https://host/'}).name,'Home');
    """
    subprocess.run([node, "-e", script], cwd=ROOT, check=True)
    manifest = json.loads((ROOT / "desktop/chrome/manifest.json").read_text())
    assert manifest["manifest_version"] == 3
    assert manifest["permissions"] == ["storage"]
    assert not manifest.get("host_permissions")
    import importlib.util
    spec = importlib.util.spec_from_file_location("chrome_package", ROOT / "desktop/chrome/package.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    first, second = tmp_path / "a.zip", tmp_path / "b.zip"
    module.package(first)
    module.package(second)
    assert first.read_bytes() == second.read_bytes()
    import zipfile
    with zipfile.ZipFile(first) as archive:
        assert "manifest.json" in archive.namelist()
        assert "package.py" not in archive.namelist()


def test_popup_serializes_deferred_storage_and_requires_successful_initialization():
    """Run the shipped popup against delayed storage, including failure and retry."""
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is needed to execute the popup")
    script = r"""
const vm=require('node:vm'),fs=require('node:fs'),assert=require('node:assert/strict');
class Element {
  constructor(tag='div'){this.tag=tag;this.children=[];this.disabled=false;this.hidden=false;this.value='';}
  append(...children){this.children.push(...children);}
  replaceChildren(...children){this.children=[...children];}
  set textContent(value){this.text=value;this.children=[];}
  get textContent(){return this.text||'';}
  setAttribute(){}
  querySelectorAll(selector){
    const tags=selector.split(',');
    return this.children.flatMap(child=>[...(tags.includes(child.tag)?[child]:[]),...child.querySelectorAll(selector)]);
  }
  reset(){this.resets=(this.resets||0)+1;}
}
function deferred(){let resolve,reject;const promise=new Promise((yes,no)=>{resolve=yes;reject=no;});return {promise,resolve,reject};}
const tick=()=>new Promise(resolve=>setImmediate(resolve));
function setup(){
  const elements=Object.fromEntries(['profiles','status','add','name','url','retry'].map(id=>[id,new Element()]));
  elements.name.tag=elements.url.tag='input';elements.add.append(elements.name,elements.url,new Element('button'));
  const reads=[],writes=[];
  const storage={
    setAccessLevel:async()=>{},
    get:()=>{const wait=deferred();reads.push(wait);return wait.promise;},
    set:value=>{const wait=deferred();writes.push({wait,value:structuredClone(value)});return wait.promise;}
  };
  const context=vm.createContext({URL,chrome:{storage:{local:storage}},document:{getElementById:id=>elements[id],createElement:tag=>new Element(tag)}});
  vm.runInContext(fs.readFileSync('desktop/chrome/validation.js','utf8'),context);
  vm.runInContext(fs.readFileSync('desktop/chrome/popup.js','utf8'),context);
  const submit=()=>elements.add.onsubmit({preventDefault(){},target:elements.add});
  const setInput=(name,url)=>{elements.name.value=name;elements.url.value=url;};
  const names=()=>elements.profiles.children.map(article=>article.children[0].textContent);
  return {elements,reads,writes,submit,setInput,names,buttons:()=>elements.profiles.querySelectorAll('button')};
}
const fixtures=['A','B','C'].map(name=>({name,url:'https://'+name.toLowerCase()+'.example/'}));
const watchdog=setTimeout(()=>{console.error('popup harness timed out');process.exit(1);},5000);
(async()=>{
  const x=setup();await tick();
  assert.equal(x.elements.name.disabled,true);
  x.setInput('D','https://d.example/');await x.submit();assert.equal(x.writes.length,0);
  x.reads[0].resolve({profiles:fixtures});await tick();assert.deepEqual(x.names(),['A','B','C']);
  const remove=x.buttons()[0], first=remove.onclick();
  assert.equal(x.writes.length,1);assert.equal(remove.disabled,true);assert.equal(x.elements.name.disabled,true);
  await remove.onclick();x.setInput('D','https://d.example/');await x.submit();assert.equal(x.writes.length,1);
  assert.deepEqual(x.names(),['A','B','C']); // Keep the last durable snapshot until save succeeds.
  x.writes[0].wait.resolve();await first;assert.deepEqual(x.names(),['B','C']);
  assert.equal(x.elements.name.disabled,false);
  const failed=x.buttons()[0].onclick();x.writes[1].wait.reject(Error('storage failed'));await failed;
  assert.deepEqual(x.names(),['B','C']);assert.match(x.elements.status.textContent,/storage failed/);
  x.setInput('D','https://d.example/');const add=x.submit();await x.buttons()[0].onclick();
  assert.equal(x.writes.length,3);assert.deepEqual(x.writes[2].value.profiles.map(p=>p.name),['B','C','D']);
  x.writes[2].wait.resolve();await add;assert.deepEqual(x.names(),['B','C','D']);
  x.setInput('E','https://e.example/');const failedAdd=x.submit();x.writes[3].wait.reject(Error('write rejected'));await failedAdd;
  assert.deepEqual(x.names(),['B','C','D']);assert.equal(x.elements.name.disabled,false);

  const y=setup();await tick();y.reads[0].reject(Error('read failed'));await tick();
  assert.equal(y.elements.retry.hidden,false);assert.equal(y.elements.name.disabled,true);
  y.setInput('New','https://new.example/');await y.submit();assert.equal(y.writes.length,0);
  const retry=y.elements.retry.onclick();await tick();assert.equal(y.elements.retry.disabled,true);
  y.reads[1].resolve({profiles:fixtures});await retry;assert.deepEqual(y.names(),['A','B','C']);
  assert.equal(y.elements.retry.hidden,true);assert.equal(y.elements.name.disabled,false);
  y.setInput('New','https://new.example/');const afterRetry=y.submit();
  assert.deepEqual(y.writes[0].value.profiles.map(p=>p.name),['A','B','C','New']);
  y.writes[0].wait.resolve();await afterRetry;

  const full=setup();await tick();
  full.reads[0].resolve({profiles:Array.from({length:20},(_,i)=>({name:'S'+i,url:'https://s'+i+'.example/'}))});await tick();
  full.setInput('Extra','https://extra.example/');await full.submit();
  assert.equal(full.writes.length,0);assert.match(full.elements.status.textContent,/20 maximum/);
  console.log('popup deferred initialization, retry, double-remove, overlapping add/remove, rollback, and 20-profile limit passed');
})().then(()=>clearTimeout(watchdog)).catch(error=>{clearTimeout(watchdog);console.error(error);process.exitCode=1;});
"""
    subprocess.run([node, "-e", script], cwd=ROOT, check=True, timeout=15)
