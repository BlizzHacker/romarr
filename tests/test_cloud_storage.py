"""Cloud storage: rclone driving, sign-in safety, secrets, jobs and routes."""
from __future__ import annotations

import json
import os
import stat
import threading
import time
from pathlib import Path

import pytest

from romarr import cloud_jobs, cloud_rclone, cloud_routes
from romarr.cloud_jobs import CloudJobs, drive_id
from romarr.cloud_rclone import Authorizer, Rclone, RcloneError, _answer, _clean_error, fs_string
from romarr.cloud_storage import CloudError, CloudStorage, SecretStore, _clean_params


# -- fakes ---------------------------------------------------------------------------


class FakeStore:
    def __init__(self, path: Path, settings=None):
        self.path = path
        self.settings = settings or {}
        self.saves = 0

    def save(self, *, raise_on_error=False):
        if getattr(self, "fail", False) and raise_on_error:
            raise OSError("disk full")
        self.saves += 1

    def list_items(self, key):
        return list(self.settings.get(key) or [])


class FakeService:
    def __init__(self, tmp_path: Path):
        self.data = tmp_path / "data"
        self.data.mkdir()
        self.lib = tmp_path / "library"
        (self.lib / "snes").mkdir(parents=True)
        self.downloads = tmp_path / "downloads"
        self.downloads.mkdir()
        self.store = FakeStore(self.data / "romarr.json", {
            "libraries": [{"id": "l1", "name": "Main", "path": str(self.lib)}],
            "_api_key": "secret-api-key"})
        self.library = self.lib
        self._env = {}
        self.adopted = []

    def library_root(self, cfg):
        return Path(cfg.get("path") or self.library)

    def _host_roots(self):
        return [str(self.lib), str(self.downloads), str(self.data)]

    def library_for(self, slug):
        return ({"id": "l1", "path": str(self.lib)}, object())

    def default_library(self):
        return self.library_for("")

    def adopt(self, path, platform=""):
        self.adopted.append((Path(path).name, platform, Path(path).read_bytes()))
        return {"ok": True, "imported": [{"file": path}], "refused": []}

    def safe_settings(self):
        return {k: v for k, v in self.store.settings.items() if not k.startswith("_")}


class FakeRclone:
    """Records calls; answers like rclone's rc API would."""

    def __init__(self, providers=None):
        self.calls = []
        self.remotes = {}
        self._providers = providers or [
            {"type": "drive", "description": "Google Drive", "oauth": True, "options": []},
            {"type": "webdav", "description": "WebDAV", "oauth": False, "options": []}]
        self.files = {}
        self.mounted = []
        self._binary = ""

    available = True

    def binary(self):
        return "/usr/bin/rclone"

    def version(self):
        return "v1.76.0"

    def providers(self):
        return self._providers

    def provider(self, rtype):
        return next((p for p in self._providers if p["type"] == rtype), None)

    def create_remote(self, name, rtype, params, *, token="", shared_client=True):
        self.calls.append(("create", name, rtype, dict(params), token))
        self.remotes[name] = rtype

    def delete_remote(self, name):
        self.remotes.pop(name, None)

    def set_binary(self, path):
        self._binary = path

    running = True

    def list(self, remote, path="", *, recurse=False):
        prefix = path.strip("/")
        if recurse:
            return [{"name": k.rsplit("/", 1)[-1], "path": k, "size": len(v), "is_dir": False,
                     "modified": ""} for k, v in self.files.items()
                    if not prefix or k.startswith(prefix + "/")]
        out, seen = [], set()
        for full, data in self.files.items():
            if prefix and not full.startswith(prefix + "/"):
                continue
            rest = full[len(prefix):].lstrip("/")
            head = rest.split("/", 1)[0]
            if head in seen:
                continue
            seen.add(head)
            is_dir = "/" in rest
            out.append({"name": head, "path": f"{prefix}/{head}".strip("/"),
                        "size": None if is_dir else len(data), "is_dir": is_dir, "modified": ""})
        return out

    def copy(self, src, dst, *, mode="copy"):
        self.calls.append(("copy", src, dst, mode))
        return 1

    def copy_file(self, src_fs, src, dst_fs, dst, *, move=False):
        self.calls.append(("copyfile", src_fs, src, dst_fs, dst))
        if src_fs.startswith("dav:"):
            key = f"{src_fs[4:]}/{src}".strip("/")
            Path(dst_fs, dst).write_bytes(self.files[key])
        else:
            self.files[f"{dst_fs.split(':', 1)[1]}/{dst}".strip("/")] = Path(src_fs, src).read_bytes()
        return 2

    def job(self, jobid):
        return {"finished": True, "success": True, "error": "", "bytes": 3, "total_bytes": 3,
                "transfers": 1, "errors": 0, "output": {}}

    def delete_file(self, remote, path):
        self.calls.append(("delete", path))
        self.files.pop(path, None)

    def mounts(self):
        return [{"fs": "x", "mount_point": m} for m in self.mounted]

    def _running(self):
        return True

    def stop(self):
        pass


@pytest.fixture
def svc(tmp_path):
    return FakeService(tmp_path)


@pytest.fixture
def cloud(svc):
    store = CloudStorage(svc, rclone=FakeRclone())
    yield store
    from romarr import hosters
    hosters.register_accounts(None)


# -- rclone helpers ------------------------------------------------------------------


def test_fs_string_rejects_bad_names_and_control_chars():
    assert fs_string("gdrive", "/ROMs/snes") == "gdrive:ROMs/snes"
    with pytest.raises(RcloneError):
        fs_string("bad name", "")
    with pytest.raises(RcloneError):
        fs_string("ok", "a\nb")


def test_answer_says_no_browser_here_and_hands_over_the_token():
    assert _answer({"Name": "config_is_local", "Default": True}, token="", shared_client=True) == "false"
    assert _answer({"Name": "config_token"}, token='{"a":1}', shared_client=True) == '{"a":1}'
    assert _answer({"Name": "config_token"}, token="", shared_client=True) is None
    assert _answer({"Name": "config_shared_client_id", "Default": False}, token="", shared_client=True) == "true"
    assert _answer({"Name": "config_shared_client_id"}, token="", shared_client=False) == "false"
    assert _answer({"Name": "config_driveid", "Default": "abc"}, token="", shared_client=True) == "abc"
    assert _answer({"Name": "x", "Default": False}, token="", shared_client=True) == "false"


def test_clean_error_hides_credentials():
    msg = _clean_error('failed: {"access_token":"ya29.SECRET","refresh_token":"1//R"} password=hunter2')
    assert "SECRET" not in msg and "1//R" not in msg and "hunter2" not in msg


def test_simplify_provider_marks_oauth_and_hides_token_fields():
    raw = {"Name": "drive", "Description": "Google Drive", "Options": [
        {"Name": "client_id", "Help": "id"}, {"Name": "client_secret", "IsPassword": False},
        {"Name": "token", "Help": "t"}, {"Name": "scope", "Advanced": False, "DefaultStr": "drive",
                                          "Examples": [{"Value": "drive", "Help": "Full"}]}]}
    out = cloud_rclone.simplify_provider(raw)
    assert out["oauth"] is True
    assert [o["name"] for o in out["options"]] == ["client_id", "client_secret", "scope"]
    assert out["options"][2]["examples"][0]["value"] == "drive"


def test_password_sign_in_backends_are_not_browser_sign_ins():
    raw = {"Name": "mailru", "Options": [{"Name": "client_id"}, {"Name": "token"},
                                         {"Name": "user"}, {"Name": "pass", "IsPassword": True}]}
    out = cloud_rclone.simplify_provider(raw)
    assert out["oauth"] is False
    assert [o["name"] for o in out["options"] if o["secret"]] == ["pass"]


class _ScriptedRclone(Rclone):
    def __init__(self, tmp_path, answers):
        super().__init__(tmp_path / "rclone.conf")
        self.sent = []
        self._answers = list(answers)

    def call(self, method, params=None, *, timeout=120):
        self.sent.append((method, json.loads(json.dumps(params))))
        if method == "config/delete":
            return {}
        return self._answers.pop(0)


def test_create_remote_walks_rclones_questions(tmp_path, monkeypatch):
    monkeypatch.setattr(cloud_rclone, "default_onedrive", lambda token, **kw: "")
    rc = _ScriptedRclone(tmp_path, [
        {"State": "s1", "Option": {"Name": "config_is_local", "Default": True}},
        {"State": "s2", "Option": {"Name": "config_token"}},
        {"State": "s3", "Option": {"Name": "config_driveid", "Default": "b!abc"}},
        {"State": ""}])
    rc.create_remote("od", "onedrive", {"region": "global", "token": "x", "BAD KEY": "1"},
                     token='{"access_token":"t"}')
    first = rc.sent[0][1]
    assert first["parameters"] == {"region": "global", "token": "x"}
    assert first["opt"]["nonInteractive"] is True
    replies = [s[1]["opt"]["result"] for s in rc.sent[1:]]
    assert replies == ["false", '{"access_token":"t"}', "b!abc"]
    assert all(s[0] == "config/update" and s[1]["opt"]["continue"] for s in rc.sent[1:])


def test_create_remote_without_token_removes_the_half_made_remote(tmp_path):
    rc = _ScriptedRclone(tmp_path, [{"State": "s1", "Option": {"Name": "config_token"}}])
    with pytest.raises(RcloneError, match="browser"):
        rc.create_remote("gd", "drive", {})
    assert rc.sent[-1][0] == "config/delete"


def test_rclone_binary_prefers_configured_then_env(tmp_path, monkeypatch):
    exe = tmp_path / "rclone"
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    monkeypatch.setenv("ROMARR_RCLONE", str(exe))
    assert Rclone(tmp_path / "c.conf").binary() == str(exe)
    monkeypatch.delenv("ROMARR_RCLONE")
    monkeypatch.setattr(cloud_rclone.shutil, "which", lambda _: None)
    assert Rclone(tmp_path / "c.conf").available is False


def test_start_puts_no_secret_on_the_command_line(tmp_path):
    seen = {}

    class Proc:
        def poll(self):
            return None

    def popen(cmd, **kw):
        seen["cmd"], seen["env"] = cmd, kw["env"]
        return Proc()

    exe = tmp_path / "rclone"
    exe.write_text("")
    exe.chmod(0o755)
    rc = Rclone(tmp_path / "rclone.conf", binary=str(exe), popen=popen)
    rc._post = lambda *a, **k: {}
    rc.start()
    assert seen["env"]["RCLONE_RC_PASS"] and seen["env"]["RCLONE_RC_PASS"] not in " ".join(seen["cmd"])
    assert seen["cmd"][1] == "rcd" and "127.0.0.1:" in seen["cmd"][3]
    assert stat.S_IMODE(os.stat(tmp_path / "rclone.conf").st_mode) == 0o600


class _Proc:
    """`rclone authorize`: exits once its listener has been called (`released`)."""

    def __init__(self):
        self.returncode = None
        self.released = threading.Event()

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        self.released.wait(timeout)
        if self.returncode is None:
            self.returncode = 0

    def terminate(self):
        self.returncode = -15
        self.released.set()

    kill = terminate


def _waiting_authorizer(tmp_path, stdout="", on_token=None):
    from romarr.cloud_rclone import _Attempt
    auth = Authorizer(Rclone(tmp_path / "c.conf"))
    proc = _Proc()
    attempt = _Attempt(proc, on_token)
    attempt.state, attempt.stdout = "goodstate", [stdout]
    auth._attempt = attempt
    threading.Thread(target=auth._watch, args=(attempt, None), daemon=True).start()

    class Session:
        def __init__(self):
            self.urls = []

        def get(self, url, **kw):
            self.urls.append(url)
            proc.released.set()   # rclone answers the redirect, then exits
    auth._session = Session()
    return auth


TOKEN_OUT = ('Paste the following into your remote machine --->\n'
             '{"access_token":"a","expiry":"x"}\n<---End paste\n')


def test_a_browser_reaching_the_listener_finishes_without_a_paste(tmp_path):
    got = []
    auth = _waiting_authorizer(tmp_path, TOKEN_OUT, on_token=got.append)
    attempt = auth._attempt
    attempt.proc.released.set()            # the browser delivered the code itself
    assert attempt.done.wait(5)
    assert len(got) == 1 and json.loads(got[0])["access_token"] == "a"
    assert auth._attempt is None


def test_a_replaced_sign_in_never_commits_its_token(tmp_path):
    got = []
    auth = _waiting_authorizer(tmp_path, TOKEN_OUT, on_token=got.append)
    old = auth._attempt
    auth._attempt = object()               # a newer sign-in took over
    old.proc.released.set()
    assert old.done.wait(5)
    assert got == [] and "newer sign-in" in old.error


def test_a_sign_in_without_a_token_reports_it(tmp_path):
    auth = _waiting_authorizer(tmp_path, "nothing useful\n")
    with pytest.raises(RcloneError, match="did not produce a token"):
        auth.finish("http://127.0.0.1:53682/?state=goodstate&code=abc")


def test_finish_refuses_foreign_hosts_and_wrong_state(tmp_path):
    auth = _waiting_authorizer(tmp_path)
    with pytest.raises(RcloneError, match="127.0.0.1:53682"):
        auth.finish("https://evil.example:53682/?state=goodstate&code=c")
    with pytest.raises(RcloneError, match="different sign-in"):
        auth.finish("http://127.0.0.1:53682/?state=other&code=c")
    with pytest.raises(RcloneError, match="no sign-in code"):
        auth.finish("http://127.0.0.1:53682/?state=goodstate")
    assert auth._session.urls == []


def test_finish_relays_only_the_oauth_fields_and_reads_the_token(tmp_path):
    auth = _waiting_authorizer(tmp_path, TOKEN_OUT)
    token = auth.finish("http://127.0.0.1:53682/?state=goodstate&code=abc&scope=s&extra=evil")
    assert json.loads(token)["access_token"] == "a"
    assert auth._session.urls == ["http://127.0.0.1:53682/?state=goodstate&code=abc&scope=s"]


def test_no_sign_in_waiting(tmp_path):
    with pytest.raises(RcloneError, match="no sign-in"):
        Authorizer(Rclone(tmp_path / "c.conf")).finish("http://127.0.0.1:53682/?state=a&code=b")


# -- secrets and settings ------------------------------------------------------------


def test_secret_store_is_private_and_atomic(tmp_path):
    store = SecretStore(tmp_path / "cloud-secrets.json")
    store.update("hosts", "1fichier", {"api_key": "k"})
    assert stat.S_IMODE(os.stat(store.path).st_mode) == 0o600
    assert store.get("hosts", "1fichier") == {"api_key": "k"}
    store.update("hosts", "1fichier", None)
    assert store.get("hosts", "1fichier") is None


def test_status_never_returns_secrets(cloud, svc):
    cloud.secrets.update("apps", "gdrive", {"client_id": "id-12345678", "client_secret": "S3CRET"})
    cloud.secrets.update("hosts", "rapidgator", {"login": "me@x", "password": "PW", "token": "TOK"})
    cloud.secrets.update("hosts", "1fichier", {"api_key": "APIKEY"})
    text = json.dumps(cloud.status())
    for secret in ("S3CRET", "PW", "TOK", "APIKEY"):
        assert secret not in text
    assert cloud.status()["apps"]["gdrive"] == {"client_id": "id-12345678", "secret_set": True}
    settings_text = json.dumps(svc.store.settings)
    for secret in ("S3CRET", "PW", "TOK", "APIKEY"):
        assert secret not in settings_text


def test_set_app_keeps_saved_secret_when_masked(cloud):
    cloud.set_app("gdrive", "client-123456", "first")
    cloud.set_app("gdrive", "client-123456", "********")
    assert cloud.secrets.get("apps", "gdrive")["client_secret"] == "first"
    cloud.set_app("gdrive", "other-1234567", "")
    assert cloud.secrets.get("apps", "gdrive")["client_secret"] == ""
    with pytest.raises(CloudError):
        cloud.set_app("dropbox", "x", "y")


def test_add_remote_refuses_browser_providers_and_bad_names(cloud):
    with pytest.raises(CloudError, match="browser"):
        cloud.add_remote({"name": "gd", "type": "drive", "params": {}})
    with pytest.raises(CloudError, match="Name"):
        cloud.add_remote({"name": "a b", "type": "webdav"})
    with pytest.raises(CloudError, match="reserved"):
        cloud.add_remote({"name": "n-x", "type": "webdav"})
    out = cloud.add_remote({"name": "dav", "type": "webdav", "label": "Box",
                            "params": {"url": "http://x", "token": "smuggled", "pass": "p"}})
    assert out["remote"]["id"] == "dav"
    assert cloud.rclone.calls[0][3] == {"url": "http://x", "pass": "p"}
    with pytest.raises(CloudError, match="already exists"):
        cloud.add_remote({"name": "dav", "type": "webdav"})


def test_service_account_drive_needs_no_browser(cloud):
    cloud.add_remote({"name": "sa", "type": "drive",
                      "params": {"service_account_file": "/data/sa.json"}})
    assert cloud.remote("sa")["type"] == "drive"


def test_delete_remote_refuses_while_a_job_uses_it(cloud, svc):
    cloud.add_remote({"name": "dav", "type": "webdav"})
    jobs = CloudJobs(cloud, svc)
    jobs.save_job({"kind": "settings", "remote": "dav", "remote_path": "b"})
    with pytest.raises(CloudError, match="Still used"):
        cloud.delete_remote("dav")


def test_clean_params_drops_tokens_and_odd_keys():
    assert _clean_params({"url": "x", "token": "t", "Bad": "1", "flag": True, "ctl": "a\x00"}) == {
        "url": "x", "flag": "true"}


def _fake_rclone_signin(cloud, monkeypatch, got=None):
    """start() remembers on_token; finish() delivers the token through it, as rclone does."""
    seen = {}

    def start(rtype, **kw):
        seen.update(kw)
        if got is not None:
            got.update({k: v for k, v in kw.items() if k != "on_token"})
        return "https://accounts.google.com/o/oauth2/auth?x"

    def finish(addr):
        seen["on_token"]('{"access_token":"a"}')
        return '{"access_token":"a"}'
    monkeypatch.setattr(cloud.authorizer, "start", start)
    monkeypatch.setattr(cloud.authorizer, "finish", finish)
    return seen


def test_rclone_signin_flow_creates_the_remote(cloud, monkeypatch):
    _fake_rclone_signin(cloud, monkeypatch)
    out = cloud.signin_start({"engine": "rclone", "type": "drive", "name": "gd", "label": "Drive"})
    assert out["mode"] == "paste" and out["url"].startswith("https://accounts.google.com/")
    done = cloud.signin_finish("http://127.0.0.1:53682/?state=s&code=c")
    assert done["remote"]["id"] == "gd"
    assert cloud.rclone.calls[-1][:3] == ("create", "gd", "drive") and cloud.rclone.calls[-1][4]


def test_own_app_signin_passes_client_and_stores_it_on_the_remote(cloud, monkeypatch):
    got = {}
    cloud.set_app("gdrive", "mine-123456.apps", "mysecret")
    _fake_rclone_signin(cloud, monkeypatch, got)
    cloud.signin_start({"engine": "rclone", "type": "drive", "name": "gd", "use_own_app": True})
    assert got == {"client_id": "mine-123456.apps", "client_secret": "mysecret"}
    cloud.signin_finish("http://127.0.0.1:53682/?state=s&code=c")
    assert cloud.rclone.calls[-1][3]["client_id"] == "mine-123456.apps"


def test_native_signin_needs_an_app_and_uses_pkce(cloud):
    with pytest.raises(CloudError, match="client id"):
        cloud.signin_start({"engine": "native", "type": "onedrive", "label": "OD"})
    cloud.set_app("onedrive", "00000000-aaaa-bbbb-cccc-000000000000", "")
    out = cloud.signin_start({"engine": "native", "type": "onedrive", "label": "OD",
                              "redirect": "callback", "base_url": "https://romarr.example"})
    assert out["mode"] == "callback"
    assert "code_challenge_method=S256" in out["url"]
    assert "redirect_uri=https%3A%2F%2Fromarr.example%2Fapi%2Fv1%2Fcloud%2Foauth%2Fcallback" in out["url"]
    with pytest.raises(CloudError, match="base_url"):
        cloud.signin_start({"engine": "native", "type": "onedrive", "redirect": "callback",
                            "base_url": "javascript:alert(1)"})


def test_native_callback_rejects_unknown_state(cloud):
    with pytest.raises(CloudError, match="unknown or expired"):
        cloud.native_callback({"state": "nope", "code": "c"})


def test_native_callback_exchanges_and_stores_token_out_of_settings(cloud, svc, monkeypatch):
    cloud.set_app("gdrive", "client-123456", "sec")
    start = cloud.signin_start({"engine": "native", "type": "gdrive", "label": "GD"})
    state = dict(p.split("=", 1) for p in start["url"].split("?", 1)[1].split("&"))["state"]
    from romarr import cloud_native
    monkeypatch.setattr(cloud_native, "exchange",
                        lambda *a, **k: {"access_token": "AT", "refresh_token": "RT", "expires_at": 1})
    out = cloud.signin_finish(f"http://127.0.0.1:53682/?state={state}&code=xyz")
    rid = out["remote"]["id"]
    assert rid.startswith("n-")
    assert cloud.secrets.get("native", rid)["token"]["refresh_token"] == "RT"
    assert "RT" not in json.dumps(svc.store.settings)


def test_host_accounts_register_for_grabs(cloud):
    from romarr import hosters
    cloud.secrets.update("hosts", "1fichier", {"api_key": "k"})
    cloud.secrets.update("hosts", "rapidgator", {"login": "me@x", "password": "p"})
    labels = sorted(a.label for a in hosters.premium_accounts())
    assert labels == ["1fichier", "Rapidgator"]


# -- jobs ----------------------------------------------------------------------------


@pytest.fixture
def jobs(cloud, svc):
    cloud.add_remote({"name": "dav", "type": "webdav"})
    return CloudJobs(cloud, svc)


def _wait_runs(jobs):
    for _ in range(200):
        if all(r["state"] != "running" for r in jobs.runs()):
            return jobs.runs()
        time.sleep(0.02)
    raise AssertionError("run did not finish")


def test_offload_needs_confirmation_and_a_library_folder(jobs, svc):
    with pytest.raises(CloudError, match="confirm"):
        jobs.save_job({"kind": "offload", "remote": "dav", "local_path": str(svc.lib / "snes")})
    with pytest.raises(CloudError, match="library folders only"):
        jobs.save_job({"kind": "offload", "remote": "dav", "local_path": str(svc.downloads),
                       "confirm": True})
    with pytest.raises(CloudError, match="outside"):
        jobs.save_job({"kind": "backup", "remote": "dav", "local_path": "/etc"})
    with pytest.raises(CloudError, match="data folder"):
        jobs.save_job({"kind": "sync", "remote": "dav", "local_path": str(svc.data)})
    ok = jobs.save_job({"kind": "offload", "remote": "dav", "local_path": str(svc.lib / "snes"),
                        "confirm": True})
    assert ok["job"]["local_path"] == os.path.realpath(svc.lib / "snes")


def test_backup_run_uses_rclone_copy(jobs, svc, cloud):
    job = jobs.save_job({"kind": "backup", "remote": "dav", "remote_path": "/ROMs/snes/",
                         "local_path": str(svc.lib / "snes")})["job"]
    jobs.run(job["id"])
    run = _wait_runs(jobs)[0]
    assert run["state"] == "done", run
    assert ("copy", os.path.realpath(svc.lib / "snes"), "dav:ROMs/snes", "copy") in cloud.rclone.calls
    for _ in range(100):                 # the outcome is saved just after the run ends
        if "done" in (jobs._job(job["id"]).get("last_result") or ""):
            break
        time.sleep(0.02)
    assert "done" in jobs._job(job["id"])["last_result"]


def test_import_job_takes_only_new_files(jobs, svc, cloud):
    cloud.rclone.files = {"inbox/Game A (USA).sfc": b"aaa", "inbox/sub/Game B (USA).sfc": b"bb"}
    job = jobs.save_job({"kind": "import", "remote": "dav", "remote_path": "inbox",
                         "platform": "snes", "interval_hours": 1})["job"]
    jobs.run(job["id"])
    assert _wait_runs(jobs)[0]["message"] == "imported 2"
    assert sorted(a[0] for a in svc.adopted) == ["Game A (USA).sfc", "Game B (USA).sfc"]
    assert not (svc.lib / cloud_jobs.STAGING).exists()
    jobs.run(job["id"])
    assert _wait_runs(jobs)[0]["message"] == "nothing new to import"


def test_settings_backup_masks_and_prunes(jobs, svc, cloud):
    for stamp in ("20200101-000000", "20200102-000000", "20200103-000000"):
        cloud.rclone.files[f"b/romarr-settings-{stamp}.json"] = b"{}"
    job = jobs.save_job({"kind": "settings", "remote": "dav", "remote_path": "b", "keep": 2})["job"]
    jobs.run(job["id"])
    assert _wait_runs(jobs)[0]["state"] == "done"
    uploaded = [k for k in cloud.rclone.files if not k.startswith("b/romarr-settings-2020")]
    assert len(uploaded) == 1 and b"secret-api-key" not in cloud.rclone.files[uploaded[0]]
    deleted = [c[1] for c in cloud.rclone.calls if c[0] == "delete"]
    assert deleted == ["b/romarr-settings-20200101-000000.json", "b/romarr-settings-20200102-000000.json"]


def test_tick_runs_only_due_jobs(jobs, svc):
    job = jobs.save_job({"kind": "settings", "remote": "dav", "remote_path": "b",
                         "interval_hours": 1})["job"]
    assert jobs.scheduled()
    assert "started" in jobs.tick()
    _wait_runs(jobs)
    assert jobs.tick() == "nothing due"


def test_drive_id_parses_share_links():
    assert drive_id("https://drive.google.com/file/d/1AbCdEfGhIjKlMnOp/view?usp=sharing") == "1AbCdEfGhIjKlMnOp"
    assert drive_id("https://drive.google.com/drive/folders/0BxYz123456789ab?resourcekey=1") == "0BxYz123456789ab"
    assert drive_id("https://drive.google.com/open?id=1234567890abcdef") == "1234567890abcdef"
    assert drive_id("https://evil.example/file/d/1234567890abcdef") == ""


def test_import_link_needs_a_drive_connection(jobs):
    with pytest.raises(CloudError, match="Google Drive connection"):
        jobs.import_now({"remote": "dav", "link": "https://drive.google.com/file/d/1234567890abcdef"})
    with pytest.raises(CloudError, match="share link"):
        jobs.import_now({"remote": "dav", "link": "https://example.com/x"})


def test_mount_points_are_confined(jobs, svc):
    with pytest.raises(CloudError):
        jobs.save_mount({"remote": "dav", "mount_point": "/etc/cloud"})
    with pytest.raises(CloudError, match="data folder"):
        jobs.save_mount({"remote": "dav", "mount_point": str(svc.data / "m")})
    out = jobs.save_mount({"remote": "dav", "mount_point": str(svc.lib / "cloud"), "cache_mode": "full"})
    assert out["mount"]["auto"] is True
    with pytest.raises(CloudError, match="already uses"):
        jobs.save_mount({"remote": "dav", "mount_point": str(svc.lib / "cloud")})
    with pytest.raises(CloudError, match="Cache mode"):
        jobs.save_mount({"remote": "dav", "mount_point": str(svc.lib / "c2"), "cache_mode": "huge"})


def test_mount_refused_without_fuse(jobs, svc, monkeypatch):
    m = jobs.save_mount({"remote": "dav", "mount_point": str(svc.lib / "cloud")})["mount"]
    monkeypatch.setattr(cloud_jobs, "fuse_available", lambda: (False, "/dev/fuse is missing"))
    with pytest.raises(CloudError, match="/dev/fuse"):
        jobs.mount(m["id"])


def test_mount_permission_error_becomes_readable_cloud_error(jobs, svc, monkeypatch):
    m = jobs.save_mount({"remote": "dav", "mount_point": str(svc.lib / "cloud")})["mount"]
    monkeypatch.setattr(cloud_jobs, "fuse_available", lambda: (True, ""))
    monkeypatch.setattr(cloud_jobs.os, "makedirs",
                        lambda *a, **kw: (_ for _ in ()).throw(PermissionError(13, "Permission denied")))
    with pytest.raises(CloudError, match="Cannot create or read mount folder"):
        jobs.mount(m["id"])


# -- routes --------------------------------------------------------------------------


def test_routes_map_errors_to_400_and_never_leak(cloud, svc):
    svc.cloud, svc.cloud_jobs = cloud, CloudJobs(cloud, svc)
    code, body = cloud_routes.post(svc, "/api/v1/cloud/remotes", {"name": "x y", "type": "webdav"})
    assert code == 400 and "Name" in body["error"]
    code, body = cloud_routes.post(svc, "/api/v1/cloud/nope", {})
    assert code == 404
    cloud.secrets.update("hosts", "1fichier", {"api_key": "TOPSECRET"})
    code, body = cloud_routes.get(svc, "/api/v1/cloud", {})
    assert code == 200 and "TOPSECRET" not in json.dumps(body)
    assert set(body["kinds"]) == {"backup", "sync", "offload", "import", "settings"}
    ok, msg = cloud_routes.oauth_callback(svc, {"state": "x", "code": "y"})
    assert not ok and "expired" in msg


def test_every_listed_cloud_route_is_handled(cloud, svc):
    svc.cloud, svc.cloud_jobs = cloud, CloudJobs(cloud, svc)
    for path in cloud_routes.POST_PATHS:
        if path.endswith("/cancel"):
            continue
        assert cloud_routes.post(svc, path, {})[0] != 404, path
    for path in cloud_routes.GET_PATHS:
        if path.endswith("/callback"):
            continue
        assert cloud_routes.get(svc, path, {})[0] != 404, path


def test_an_abandoned_sign_in_stops_its_listener(tmp_path):
    auth = _waiting_authorizer(tmp_path)
    attempt = auth._attempt
    auth._expire(object())          # a timer from an older sign-in: ignored
    assert auth._attempt is attempt
    auth._expire(attempt)
    assert auth._attempt is None and attempt.proc.poll() is not None   # terminated


def test_a_sign_in_that_finished_by_itself_shows_up_without_a_paste(cloud, monkeypatch):
    seen = _fake_rclone_signin(cloud, monkeypatch)
    cloud.signin_start({"engine": "rclone", "type": "drive", "name": "od", "label": "Drive"})
    seen["on_token"]('{"access_token":"a"}')          # browser reached the listener directly
    assert cloud.remote("od")["type"] == "drive"
    assert cloud.status()["last_signin"]["remote"]["id"] == "od"
    assert "access_token" not in json.dumps(cloud.status())
    assert cloud.signin_finish("http://127.0.0.1:53682/?state=s&code=c")["remote"]["id"] == "od"


def test_onedrive_uses_the_accounts_own_drive(tmp_path, monkeypatch):
    rc = _ScriptedRclone(tmp_path, [
        {"State": "s1", "Option": {"Name": "config_is_local", "Default": True}},
        {"State": "s2", "Option": {"Name": "config_token"}},
        {"State": "s3", "Option": {"Name": "config_type", "Default": "onedrive"}},
        {"State": "s4", "Option": {"Name": "config_driveid", "Default": "b!wrong"}},
        {"State": ""}])
    monkeypatch.setattr(cloud_rclone, "default_onedrive", lambda token, **kw: "a1b2c3d4e5f6")
    rc.create_remote("od", "onedrive", {}, token='{"access_token":"t"}')
    assert [s[1]["opt"]["result"] for s in rc.sent[1:]] == [
        "false", '{"access_token":"t"}', "onedrive", "a1b2c3d4e5f6"]


def test_default_onedrive_reads_me_drive():
    class Resp:
        status_code = 200

        def json(self):
            return {"id": "a1b2c3d4e5f6"}

    class S:
        def get(self, url, **kw):
            assert url.endswith("/me/drive") and kw["headers"]["Authorization"] == "Bearer t"
            return Resp()
    assert cloud_rclone.default_onedrive('{"access_token":"t"}', session=S()) == "a1b2c3d4e5f6"
    assert cloud_rclone.default_onedrive("not json") == ""



# -- CodeRabbit review fixes ------------------------------------------------------------


def test_an_unreadable_secret_store_is_never_overwritten(tmp_path):
    store = SecretStore(tmp_path / "cloud-secrets.json")
    store.path.write_text("{not json", encoding="utf-8")
    with pytest.raises(CloudError, match="damaged"):
        store.update("hosts", "1fichier", {"api_key": "k"})
    assert store.path.read_text(encoding="utf-8") == "{not json"


def test_status_reports_a_broken_secret_store(cloud):
    cloud.secrets.path.write_text("[]", encoding="utf-8")
    assert "secret store" in cloud.status()["secrets_problem"]


def test_a_failed_settings_save_is_reported_and_undone(cloud, svc):
    svc.store.fail = True
    with pytest.raises(CloudError, match="could not be saved"):
        cloud.add_remote({"name": "dav", "type": "webdav"})
    assert "cloud_storage" not in svc.store.settings or not svc.store.settings["cloud_storage"].get("remotes")


def test_native_connections_cannot_offload_or_mirror(cloud, svc):
    cloud._add_record({"id": "n-1", "engine": "native", "type": "gdrive", "label": "GD"})
    jobs = CloudJobs(cloud, svc)
    for kind in ("offload", "sync"):
        with pytest.raises(CloudError, match="needs an rclone connection"):
            jobs.save_job({"kind": kind, "remote": "n-1", "local_path": str(svc.lib / "snes"),
                           "confirm": True})


def test_staging_paths_cannot_escape(tmp_path):
    staging = tmp_path / "stage"
    staging.mkdir()
    assert cloud_jobs._inside(staging, "a/b.sfc") == staging / "a" / "b.sfc"
    for bad in ("../x.sfc", "a/../../x", "./.", ""):
        assert cloud_jobs._inside(staging, bad) is None


def test_a_refused_import_is_tried_again_next_time(jobs, svc, cloud):
    cloud.rclone.files = {"inbox/Bad (USA).sfc": b"bad"}
    svc.adopt = lambda path, platform="": {"ok": False, "reason": "bad dump",
                                           "refused": [{"reason": "bad dump"}]}
    job = jobs.save_job({"kind": "import", "remote": "dav", "remote_path": "inbox"})["job"]
    jobs.run(job["id"])
    assert "bad dump" in _wait_runs(jobs)[0]["message"]
    for _ in range(100):
        if jobs._job(job["id"]).get("last_run"):
            break
        time.sleep(0.02)
    assert jobs._job(job["id"]).get("seen", []) == []


def test_settings_backup_is_restorable_and_has_no_2fa_codes(jobs, svc, cloud):
    svc.store.settings.update({"_totp_secret": "TOTPSECRET", "_totp_backup": ["code-1", "code-2"]})
    job = jobs.save_job({"kind": "settings", "remote": "dav", "remote_path": "b"})["job"]
    jobs.run(job["id"])
    assert _wait_runs(jobs)[0]["state"] == "done"
    raw = next(v for k, v in cloud.rclone.files.items() if k.startswith("b/romarr-settings-"))
    from romarr.ops import read_backup
    settings, _warning = read_backup(json.loads(raw))
    assert b"TOTPSECRET" not in raw and b"code-1" not in raw and b"secret-api-key" not in raw
    assert "libraries" in settings


def test_a_late_token_from_a_replaced_sign_in_saves_nothing(cloud, monkeypatch):
    seen = _fake_rclone_signin(cloud, monkeypatch)
    cloud.signin_start({"engine": "rclone", "type": "drive", "name": "first"})
    first = seen["on_token"]
    cloud.signin_start({"engine": "rclone", "type": "drive", "name": "second"})
    first('{"access_token":"a"}')        # the first sign-in's token arrives late
    assert [r["id"] for r in cloud.cfg()["remotes"]] == []


def test_a_refreshed_rapidgator_token_never_lands_on_another_account(cloud):
    cloud.secrets.update("hosts", "rapidgator", {"login": "a@x", "password": "p", "gen": "g1"})
    cloud._keep_rg_token("TOKEN-A", "g1")
    assert cloud.secrets.get("hosts", "rapidgator")["token"] == "TOKEN-A"
    cloud.secrets.update("hosts", "rapidgator", {"login": "b@x", "password": "q", "gen": "g2"})
    cloud._keep_rg_token("LATE-A", "g1")
    assert "token" not in cloud.secrets.get("hosts", "rapidgator")


def test_a_remote_rclone_cannot_delete_keeps_its_record(cloud, monkeypatch):
    cloud.add_remote({"name": "dav", "type": "webdav"})

    def refuse(name):
        raise RcloneError("config locked")
    monkeypatch.setattr(cloud.rclone, "delete_remote", refuse)
    monkeypatch.setattr(cloud.rclone, "remotes", lambda: ["dav"], raising=False)
    with pytest.raises(CloudError, match="still saved"):
        cloud.delete_remote("dav")
    assert cloud.remote("dav")


def test_jobs_from_a_restarted_daemon_are_refused(tmp_path):
    rc = Rclone(tmp_path / "c.conf")
    rc._proc = type("P", (), {"poll": lambda self: None})()
    rc._generation = 1
    rc.call = lambda method, params=None, **kw: {"jobid": 7}
    jobid = rc._async("sync/copy", {})
    rc._generation = 2                     # rclone restarted; ids start again at 1
    with pytest.raises(RcloneError, match="restarted"):
        rc.job(jobid)


def test_drive_folder_links_read_through_a_folder_rooted_remote():
    assert Rclone.folder_fs("gd", "1AbCdEfGhIjKlMnOp") == "gd,root_folder_id=1AbCdEfGhIjKlMnOp:"
    with pytest.raises(RcloneError):
        Rclone.folder_fs("gd", "../etc")


def test_the_web_cannot_choose_the_rclone_executable(cloud, svc):
    svc.cloud, svc.cloud_jobs = cloud, CloudJobs(cloud, svc)
    assert cloud_routes.post(svc, "/api/v1/cloud/rclone", {"path": "/bin/sh"})[0] == 404
