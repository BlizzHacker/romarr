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
