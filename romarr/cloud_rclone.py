"""rclone as ROMarr's cloud storage engine.

rclone already speaks to seventy-odd storage systems -- Google Drive,
OneDrive, Dropbox, MEGA, Box, pCloud, S3 and everything S3-shaped (Wasabi,
Backblaze B2, Cloudflare R2, MinIO), WebDAV, SFTP, SMB, iCloud Drive, Proton
Drive and more -- with retries, chunked uploads, hash checks and OAuth token
refresh that took years to get right. ROMarr drives it rather than
reimplementing any of that.

How it is driven, and why:

* **One `rclone rcd` per ROMarr**, bound to 127.0.0.1 on a random port with a
  random user and password passed through the environment. Every operation
  is a JSON call to it. Nothing secret ever goes on a command line, where any
  local user could read it from the process list: a token or a password goes
  in a request body, and an OAuth client id/secret goes in the environment of
  the one `rclone authorize` that needs it.
* **ROMarr's own config file** (`rclone.conf` beside `romarr.json`, mode
  0600), never the operator's `~/.config/rclone`. Remotes an operator made by
  hand for something else are not ROMarr's to list or change.
* **Sign-in without a browser on the server.** `rclone authorize` listens on
  127.0.0.1:53682 *on the server*. ROMarr reads the provider's consent URL
  from it and gives that to the operator's browser. After they approve, the
  provider sends their browser to ``http://127.0.0.1:53682/?state=…&code=…``,
  which does not load on their machine. They paste that address into ROMarr,
  which checks the state and delivers it to the waiting listener on the
  server. rclone finishes the exchange and prints the token, which goes
  straight into `config/create`.
"""
from __future__ import annotations

import json
import logging
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit

import requests

log = logging.getLogger(__name__)

AUTH_PORT = 53682
AUTH_SECONDS = 600
START_SECONDS = 15
MAX_STATES = 12
_AUTH_LINK = re.compile(r"http://127\.0\.0\.1:53682/auth\?state=([A-Za-z0-9_-]+)")
_TOKEN_BLOCK = re.compile(r"--->\s*(\{.*?\})\s*<---", re.S)
REMOTE_NAME = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
MAX_DEPTH = 8

#: The providers people mean by "cloud storage", shown first. Every other
#: backend rclone has is still offered, under "All providers".
FEATURED = ("drive", "onedrive", "dropbox", "mega", "box", "pcloud", "s3", "b2",
            "iclouddrive", "protondrive", "yandex", "koofr", "webdav",
            "sftp", "smb", "ftp", "azureblob", "google cloud storage", "filen",
            "internxt", "putio", "premiumizeme", "pixeldrain", "gofile", "fichier",
            "zoho", "hidrive", "opendrive", "seafile", "storj", "crypt")

#: Plumbing rclone needs internally and an operator never picks as storage.
HIDDEN = frozenset({"memory", "local", "cache", "tardigrade", "alias", "union",
                    "combine", "chunker", "hasher", "compress", "archive"})

#: Backends that keep an OAuth token internally but sign in with a password or
#: a login token typed into the form, never through a browser.
FORM_SIGN_IN = frozenset({"mailru", "jottacloud"})


class RcloneError(RuntimeError):
    """rclone refused or failed; the message is safe to show."""


def _launcher() -> list[str]:
    """`setpriv --pdeathsig TERM --` on Linux, so rclone dies with ROMarr.

    Without it, killing ROMarr leaves an orphaned `rclone rcd` holding mounts
    and a port. The signal is set by setpriv itself, after the fork, rather
    than by a `preexec_fn`: Python warns that a `preexec_fn` in a threaded
    process can deadlock the child before exec. PR_SET_PDEATHSIG fires when
    the *thread* that forked exits, which is why every rclone is started from
    `_SPAWNER`'s one long-lived thread rather than a request thread.
    """
    if sys.platform.startswith("linux"):
        setpriv = shutil.which("setpriv")
        if setpriv:
            return [setpriv, "--pdeathsig", "TERM", "--"]
    return []


_SPAWNER = ThreadPoolExecutor(max_workers=1, thread_name_prefix="romarr-rclone-spawn")


def _spawn(popen, cmd: list, **kwargs):
    if popen is subprocess.Popen:
        cmd = _launcher() + list(cmd)
    return _SPAWNER.submit(popen, cmd, **kwargs).result()


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _env_name(rtype: str, option: str) -> str:
    """rclone reads backend option defaults from RCLONE_<TYPE>_<OPTION>."""
    return f"RCLONE_{re.sub(r'[^A-Za-z0-9]', '_', rtype).upper()}_{option.upper()}"


def fs_string(remote: str, path: str = "") -> str:
    """`remote:path` with the path cleaned of leading slashes and control chars."""
    if not REMOTE_NAME.match(remote or ""):
        raise RcloneError("invalid remote name")
    clean = str(path or "").replace("\\", "/").strip().lstrip("/")
    if any(ord(c) < 32 for c in clean):
        raise RcloneError("invalid path")
    return f"{remote}:{clean}"


class Rclone:
    """A supervised `rclone rcd` and the calls ROMarr makes to it."""

    def __init__(self, config_path, *, binary: str = "", cache_dir=None,
                 session: requests.Session | None = None, popen=subprocess.Popen):
        self.config_path = Path(config_path)
        self.cache_dir = Path(cache_dir) if cache_dir else self.config_path.parent / "rclone-cache"
        self._binary = binary
        self._session = session or requests.Session()
        self._popen = popen
        self._proc = None
        self._url = ""
        self._auth = ("", "")
        self._lock = threading.RLock()
        self._providers: list | None = None
        self._version: str | None = None
        #: Bumped each time a daemon starts. rclone numbers jobs from 1 again
        #: after a restart, so a job id is only meaningful with its generation.
        self._generation = 0
        self._jobs: dict[int, int] = {}

    # -- the binary and the daemon -------------------------------------------

    def binary(self) -> str:
        """The rclone to run: the configured path, $ROMARR_RCLONE, then PATH."""
        for candidate in (self._binary, os.environ.get("ROMARR_RCLONE", "")):
            if candidate and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                return candidate
        return shutil.which("rclone") or ""

    @property
    def available(self) -> bool:
        return bool(self.binary())

    @property
    def running(self) -> bool:
        return self._running()

    def set_binary(self, path: str) -> None:
        """Use another rclone: stop the daemon and forget what the old one said."""
        self.stop()
        self._binary = path
        self._providers = None
        self._version = None

    def _ensure_config(self) -> None:
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        if not self.config_path.exists():
            fd = os.open(self.config_path, os.O_WRONLY | os.O_CREAT, 0o600)
            os.close(fd)
        try:
            os.chmod(self.config_path, 0o600)
        except OSError:
            pass

    def _running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def start(self) -> None:
        with self._lock:
            if self._running():
                return
            binary = self.binary()
            if not binary:
                raise RcloneError("rclone is not installed; see docs/CLOUD-STORAGE.md")
            self._ensure_config()
            port = _free_port()
            user, password = secrets.token_hex(8), secrets.token_urlsafe(24)
            env = {**os.environ, "RCLONE_RC_USER": user, "RCLONE_RC_PASS": password,
                   "RCLONE_CONFIG": str(self.config_path)}
            cmd = [binary, "rcd", "--rc-addr", f"127.0.0.1:{port}",
                   "--cache-dir", str(self.cache_dir), "--log-level", "NOTICE"]
            self._proc = _spawn(self._popen, cmd, env=env, stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self._generation += 1
            self._url = f"http://127.0.0.1:{port}/"
            self._auth = (user, password)
            deadline = time.monotonic() + START_SECONDS
            while time.monotonic() < deadline:
                if not self._running():
                    raise RcloneError("rclone exited while starting")
                try:
                    self._post("rc/noop", {}, timeout=2)
                    return
                except (requests.RequestException, RcloneError):
                    time.sleep(0.2)
            self.stop()
            raise RcloneError("rclone did not start in time")

    def stop(self) -> None:
        with self._lock:
            proc, self._proc = self._proc, None
            if proc is not None and proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()

    def _post(self, method: str, params: dict, *, timeout: float = 60):
        response = self._session.post(self._url + method, json=params,
                                      auth=self._auth, timeout=timeout)
        try:
            body = response.json()
        except ValueError:
            body = {}
        if response.status_code >= 400:
            raise RcloneError(_clean_error(body.get("error") or f"HTTP {response.status_code}"))
        return body

    def call(self, method: str, params: dict | None = None, *, timeout: float = 120) -> dict:
        """One rc call; starts the daemon on first use and once more if it died."""
        for attempt in (1, 2):
            self.start()
            try:
                return self._post(method, params or {}, timeout=timeout)
            except requests.ConnectionError:
                if attempt == 2:
                    raise RcloneError("rclone is not answering")
                self.stop()
        raise RcloneError("rclone is not answering")

    def version(self) -> str:
        """`rclone version`, read once; never starts the daemon just to show it."""
        if self._version is None:
            binary = self.binary()
            try:
                out = subprocess.run([binary, "version"], capture_output=True, text=True,
                                     timeout=15).stdout if binary else ""
            except (OSError, subprocess.SubprocessError):
                out = ""
            first = (out.splitlines() or [""])[0]
            self._version = first.split()[-1] if first.startswith("rclone ") else ""
        return self._version

    # -- providers -------------------------------------------------------------

    def providers(self) -> list[dict]:
        """Every storage backend this rclone has, reduced to what a form needs."""
        if self._providers is None:
            raw = self.call("config/providers").get("providers") or []
            self._providers = [simplify_provider(p) for p in raw
                               if p.get("Name") not in HIDDEN]
            rank = {name: i for i, name in enumerate(FEATURED)}
            self._providers.sort(key=lambda p: (rank.get(p["type"], 999), p["type"]))
        return self._providers

    def provider(self, rtype: str) -> dict | None:
        return next((p for p in self.providers() if p["type"] == rtype), None)

    # -- remotes ---------------------------------------------------------------

    def remotes(self) -> list[str]:
        return list(self.call("config/listremotes").get("remotes") or [])

    def create_remote(self, name: str, rtype: str, params: dict, *, token: str = "",
                      shared_client: bool = True) -> None:
        """Create (or replace) a remote, answering rclone's questions for it.

        rclone asks follow-up questions per backend -- "use a browser here?",
        "which OneDrive?", "a shared drive?". Each is answered with the token
        ROMarr already has, "no browser on this machine", or rclone's own
        default, so every backend is created the same way.
        """
        if not REMOTE_NAME.match(name):
            raise RcloneError("Use letters, digits, - and _ for the name (40 at most)")
        clean = {str(k): str(v) for k, v in (params or {}).items()
                 if v not in (None, "") and re.match(r"^[a-z0-9_]{1,64}$", str(k))}
        opt = {"nonInteractive": True, "obscure": True}
        result = self.call("config/create", {"name": name, "type": rtype,
                                             "parameters": clean, "opt": opt})
        for _ in range(MAX_STATES):
            if result.get("Error"):
                raise RcloneError(_clean_error(result["Error"]))
            state = result.get("State") or ""
            if not state:
                return
            option = result.get("Option") or {}
            answer = _answer(option, token=token, shared_client=shared_client)
            if rtype == "onedrive" and option.get("Name") == "config_driveid" and token:
                # rclone offers the account's drives and defaults to the first,
                # which for some personal accounts is one Graph then refuses
                # ("ObjectHandle is Invalid"). The account's own drive is the
                # one people mean.
                answer = default_onedrive(token) or answer
            if answer is None:
                self.delete_remote(name)
                raise RcloneError("this provider needs a browser sign-in first")
            result = self.call("config/update", {"name": name, "parameters": {}, "opt": {
                **opt, "continue": True, "state": state, "result": answer}})
        self.delete_remote(name)
        raise RcloneError("rclone kept asking questions; create this remote by hand")

    def delete_remote(self, name: str) -> None:
        if REMOTE_NAME.match(name or ""):
            self.call("config/delete", {"name": name})

    # -- files -----------------------------------------------------------------

    def list(self, remote: str, path: str = "", *, recurse: bool = False) -> list[dict]:
        """One folder, or with `recurse` every file under it, in one call."""
        base = _rel(path)
        params = {"fs": fs_string(remote), "remote": base,
                  "opt": {"recurse": True, "filesOnly": True} if recurse else {"recurse": False}}
        if recurse:
            params["_config"] = {"MaxDepth": MAX_DEPTH}
        rows = self.call("operations/list", params, timeout=600).get("list") or []
        # Path is relative to the remote's root, so it already includes `base`.
        return [{"name": r.get("Name", ""), "path": r.get("Path", ""),
                 "size": int(r.get("Size") or 0) if not r.get("IsDir") else None,
                 "is_dir": bool(r.get("IsDir")), "modified": r.get("ModTime", "")}
                for r in rows]

    def about(self, remote: str) -> dict:
        raw = self.call("operations/about", {"fs": fs_string(remote)}, timeout=60)
        return {"total": raw.get("total"), "used": raw.get("used"), "free": raw.get("free")}

    def copy(self, src_fs: str, dst_fs: str, *, mode: str = "copy") -> int:
        """Start copy/move/sync between two fs strings; returns rclone's job id."""
        method = {"copy": "sync/copy", "move": "sync/move", "sync": "sync/sync"}[mode]
        params = {"srcFs": src_fs, "dstFs": dst_fs, "_async": True}
        if mode == "move":
            params["deleteEmptySrcDirs"] = True
        return self._async(method, params)

    def copy_file(self, src_fs: str, src: str, dst_fs: str, dst: str, *,
                  move: bool = False) -> int:
        method = "operations/movefile" if move else "operations/copyfile"
        return self._async(method, {"srcFs": src_fs, "srcRemote": src, "dstFs": dst_fs,
                                    "dstRemote": dst, "_async": True})

    def check(self, src_fs: str, dst_fs: str) -> int:
        return self._async("operations/check", {"srcFs": src_fs, "dstFs": dst_fs,
                                                "oneWay": True, "_async": True})

    def _async(self, method: str, params: dict) -> int:
        with self._lock:
            jobid = int(self.call(method, params)["jobid"])
            self._jobs[jobid] = self._generation
            return jobid

    def _own(self, jobid: int) -> None:
        """Refuse a job id from a daemon that has since restarted."""
        if self._jobs.get(int(jobid)) != self._generation or not self._running():
            raise RcloneError("rclone restarted while this transfer ran; run it again")

    def job(self, jobid: int) -> dict:
        self._own(jobid)
        status = self.call("job/status", {"jobid": int(jobid)})
        stats = {}
        try:
            stats = self.call("core/stats", {"group": f"job/{int(jobid)}"})
        except RcloneError:
            pass
        return {"finished": bool(status.get("finished")), "success": bool(status.get("success")),
                "error": _clean_error(status.get("error") or ""),
                "output": status.get("output") or {},
                "bytes": int(stats.get("bytes") or 0), "total_bytes": int(stats.get("totalBytes") or 0),
                "transfers": int(stats.get("transfers") or 0), "speed": float(stats.get("speed") or 0),
                "errors": int(stats.get("errors") or 0)}

    def stop_job(self, jobid: int) -> None:
        self._own(jobid)
        self.call("job/stop", {"jobid": int(jobid)})

    def delete_file(self, remote: str, path: str) -> None:
        self.call("operations/deletefile", {"fs": fs_string(remote), "remote": _rel(path)})

    def public_link(self, remote: str, path: str) -> str:
        return str(self.call("operations/publiclink", {"fs": fs_string(remote),
                                                       "remote": _rel(path)}).get("url", ""))

    def copy_id(self, remote: str, file_id: str, dest: str) -> None:
        """Google Drive only: server-side copy of a shared file or folder by id."""
        if not re.match(r"^[A-Za-z0-9_-]{10,200}$", file_id or ""):
            raise RcloneError("that is not a Google Drive file id")
        self.call("backend/command", {"command": "copyid", "fs": fs_string(remote),
                                      "arg": [file_id, fs_string(remote, dest).rstrip("/") + "/"]},
                  timeout=600)

    @staticmethod
    def folder_fs(remote: str, folder_id: str) -> str:
        """A Drive remote rooted at a shared folder's id (folder share links)."""
        if not REMOTE_NAME.match(remote or "") or not re.match(r"^[A-Za-z0-9_-]{10,200}$",
                                                                 folder_id or ""):
            raise RcloneError("that is not a Google Drive folder id")
        return f"{remote},root_folder_id={folder_id}:"

    # -- mounts ----------------------------------------------------------------

    def mount(self, fs: str, mount_point: str, *, cache_mode: str = "full",
              allow_other: bool = False, cache_max_gb: float = 10) -> None:
        if cache_mode not in ("off", "minimal", "writes", "full"):
            raise RcloneError("cache mode must be off, minimal, writes or full")
        vfs = {"CacheMode": cache_mode}
        if cache_max_gb and cache_max_gb > 0:
            vfs["CacheMaxSize"] = f"{int(cache_max_gb * 1024)}M"
        self.call("mount/mount", {"fs": fs, "mountPoint": mount_point,
                                  "vfsOpt": vfs,
                                  "mountOpt": {"AllowOther": bool(allow_other)}}, timeout=60)

    def unmount(self, mount_point: str) -> None:
        self.call("mount/unmount", {"mountPoint": mount_point}, timeout=60)

    def mounts(self) -> list[dict]:
        try:
            rows = self.call("mount/listmounts").get("mountPoints") or []
        except RcloneError:
            return []
        return [{"fs": r.get("Fs", ""), "mount_point": r.get("MountPoint", "")} for r in rows]


def default_onedrive(token: str, *, session: requests.Session | None = None) -> str:
    """The id of the signed-in account's own OneDrive (Graph /me/drive), or ""."""
    try:
        access = json.loads(token).get("access_token", "")
        response = (session or requests).get(
            "https://graph.microsoft.com/v1.0/me/drive", params={"$select": "id"},
            headers={"Authorization": f"Bearer {access}"}, timeout=20)
        return str(response.json().get("id") or "") if response.status_code == 200 else ""
    except (requests.RequestException, ValueError, AttributeError):
        return ""


def fuse_available() -> tuple[bool, str]:
    """Whether this host can mount anything with FUSE, and if not, why."""
    if os.name == "nt":
        return False, "mounting needs WinFsp on Windows; ROMarr does not manage it"
    if not os.path.exists("/dev/fuse"):
        return False, ("/dev/fuse is missing. In a Proxmox container enable "
                       "Options -> Features -> FUSE; in Docker add --device /dev/fuse "
                       "--cap-add SYS_ADMIN")
    if not (shutil.which("fusermount3") or shutil.which("fusermount")):
        return False, "fusermount is missing; install fuse3"
    # fusermount3 looks its caller up and refuses an unnamed uid with "could
    # not determine username" -- the state of every numeric Docker PUID before
    # the entrypoint started naming it, and still of `--user` runs.
    try:
        import pwd
        pwd.getpwuid(os.getuid())
    except ImportError:
        pass
    except KeyError:
        return False, (f"uid {os.getuid()} has no /etc/passwd entry, which "
                       "fusermount requires. Run the container with PUID/PGID "
                       "rather than --user, or mount a passwd file that names it")
    return True, ""


def _rel(path: str) -> str:
    clean = str(path or "").replace("\\", "/").strip().lstrip("/")
    if any(ord(c) < 32 for c in clean):
        raise RcloneError("invalid path")
    return clean


def _clean_error(text) -> str:
    """rclone's error text, without anything that looks like a credential."""
    msg = str(text or "")
    msg = re.sub(r'("?(?:access_token|refresh_token|token|password|pass|secret)"?\s*[:=]\s*)'
                 r'("[^"]*"|\S+)', r"\1[hidden]", msg, flags=re.I)
    return msg[:500]


def _answer(option: dict, *, token: str, shared_client: bool):
    """The reply to one of rclone's configuration questions; None = cannot answer."""
    name = option.get("Name") or ""
    if name == "config_is_local":
        return "false"
    if name == "config_token":
        return token or None
    if name == "config_shared_client_id":
        return "true" if shared_client else "false"
    default = option.get("Default")
    if isinstance(default, bool):
        return "true" if default else "false"
    if default not in (None, ""):
        return str(default)
    examples = option.get("Examples") or []
    if examples and option.get("Required"):
        return str(examples[0].get("Value", ""))
    return ""


def simplify_provider(raw: dict) -> dict:
    options = []
    names = set()
    for opt in raw.get("Options") or []:
        name = opt.get("Name") or ""
        names.add(name)
        if name in ("token", "auth_url", "token_url", "client_credentials") or opt.get("Hide"):
            continue
        options.append({
            "name": name,
            "help": (opt.get("Help") or "").split("\n")[0][:300],
            "default": opt.get("DefaultStr") if opt.get("DefaultStr") is not None else "",
            "required": bool(opt.get("Required")),
            "advanced": bool(opt.get("Advanced")),
            "secret": bool(opt.get("IsPassword") or opt.get("Sensitive")),
            "provider": opt.get("Provider") or "",
            "type": opt.get("Type") or "string",
            "examples": [{"value": e.get("Value", ""), "help": (e.get("Help") or "")[:120],
                          "provider": e.get("Provider") or ""}
                         for e in (opt.get("Examples") or [])][:60],
        })
    return {"type": raw.get("Name", ""), "description": raw.get("Description", ""),
            "oauth": ("token" in names and "client_id" in names
                      and raw.get("Name") not in FORM_SIGN_IN),
            "options": options}


class _Attempt:
    """One `rclone authorize` run: its process, output and outcome."""

    def __init__(self, proc, on_token):
        self.proc = proc
        self.on_token = on_token
        self.state = ""
        self.stdout: list[str] = []
        self.link_seen = threading.Event()
        self.done = threading.Event()
        self.token = ""
        self.error = ""
        self.started = time.monotonic()


class Authorizer:
    """One browser sign-in at a time, through `rclone authorize` on the server.

    The provider sends the browser to ``127.0.0.1:53682``. Two ways reach the
    listener: the operator pastes that address into ROMarr (`finish`), or the
    browser reaches it directly -- ROMarr on the same machine as the browser,
    or a tunnel to it. Either way rclone exits with the token, and `_watch`
    hands it to `on_token` exactly once.

    Everything about one run lives on its `_Attempt`, so a run that ends after
    a newer one started can neither read the newer run's output nor commit
    its token: only the attempt that is still current may complete.
    """

    def __init__(self, rclone: Rclone, *, popen=subprocess.Popen,
                 session: requests.Session | None = None):
        self._rclone = rclone
        self._popen = popen
        self._session = session or requests.Session()
        self._lock = threading.Lock()
        self._attempt: _Attempt | None = None
        self.rtype = ""

    @property
    def active(self) -> bool:
        a = self._attempt
        return (a is not None and a.proc.poll() is None
                and time.monotonic() - a.started < AUTH_SECONDS)

    def start(self, rtype: str, *, client_id: str = "", client_secret: str = "",
              on_token=None) -> str:
        """Start `rclone authorize`; return the provider URL to open."""
        with self._lock:
            self.cancel()
            binary = self._rclone.binary()
            if not binary:
                raise RcloneError("rclone is not installed")
            env = dict(os.environ)
            if client_id:
                env[_env_name(rtype, "client_id")] = client_id
            if client_secret:
                env[_env_name(rtype, "client_secret")] = client_secret
            proc = _spawn(self._popen, [binary, "authorize", rtype, "--auth-no-open-browser"],
                          env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, text=True)
            attempt = self._attempt = _Attempt(proc, on_token)
            self.rtype = rtype
            # An abandoned sign-in must not hold port 53682 until the next one.
            timer = threading.Timer(AUTH_SECONDS, self._expire, args=(attempt,))
            timer.daemon = True
            timer.start()
            threading.Thread(target=self._read, args=(attempt, proc.stderr, True),
                             daemon=True).start()
            reader = threading.Thread(target=self._read, args=(attempt, proc.stdout, False),
                                      daemon=True)
            reader.start()
            threading.Thread(target=self._watch, args=(attempt, reader),
                             name="romarr-cloud-signin", daemon=True).start()
            if not attempt.link_seen.wait(15) or not attempt.state:
                self.cancel()
                raise RcloneError("rclone did not start a sign-in (is port 53682 in use?)")
            local = f"http://127.0.0.1:{AUTH_PORT}/auth?state={attempt.state}"
            try:
                response = self._session.get(local, allow_redirects=False, timeout=10)
            except requests.RequestException:
                self.cancel()
                raise RcloneError("could not reach rclone's sign-in listener")
            target = response.headers.get("Location", "")
            if not target.startswith("https://"):
                self.cancel()
                raise RcloneError("rclone gave no provider sign-in address")
            return target

    @staticmethod
    def _read(attempt: _Attempt, stream, is_err: bool) -> None:
        for line in iter(stream.readline, ""):
            if is_err:
                found = _AUTH_LINK.search(line)
                if found and not attempt.state:
                    attempt.state = found.group(1)
                    attempt.link_seen.set()
            else:
                attempt.stdout.append(line)
        if is_err:
            attempt.link_seen.set()

    def _watch(self, attempt: _Attempt, reader) -> None:
        """When rclone exits, hand over its token -- however the code reached it."""
        attempt.proc.wait()
        if reader is not None:
            reader.join(5)
        found = _TOKEN_BLOCK.search("".join(attempt.stdout))
        token = ""
        if found:
            try:
                json.loads(found.group(1))   # it must be JSON; never echoed
                token = found.group(1)
            except ValueError:
                token = ""
        with self._lock:
            current = self._attempt is attempt
            if current:
                self._attempt = None
        attempt.token = token
        if not token:
            attempt.error = "the sign-in did not produce a token; start again"
        elif not current:
            attempt.error = "a newer sign-in replaced this one"
        elif attempt.on_token is not None:
            try:
                attempt.on_token(token)
            except Exception as err:   # noqa: BLE001 - reported to whoever waits
                attempt.error = str(err)[:300]
        attempt.done.set()

    def finish(self, pasted: str) -> str:
        """Deliver the pasted redirect address to rclone; return the token JSON."""
        with self._lock:
            attempt = self._attempt
            if attempt is None or not self.active:
                raise RcloneError("no sign-in is waiting; start again")
            parts = urlsplit(str(pasted or "").strip())
            if parts.hostname not in ("127.0.0.1", "localhost") or parts.port != AUTH_PORT:
                raise RcloneError("paste the whole address that starts with http://127.0.0.1:53682/")
            query = dict(parse_qsl(parts.query))
            if query.get("state") != attempt.state:
                raise RcloneError("that address belongs to a different sign-in; start again")
            if query.get("error"):
                self.cancel()
                raise RcloneError(f"the provider refused: {query['error'][:100]}")
            if not query.get("code"):
                raise RcloneError("the address has no sign-in code in it")
            keep = {k: v for k, v in query.items() if k in ("state", "code", "scope")}
            try:
                self._session.get(f"http://127.0.0.1:{AUTH_PORT}/?{urlencode(keep)}", timeout=30)
            except requests.RequestException:
                pass  # rclone may close the socket after answering
        if not attempt.done.wait(90):
            with self._lock:
                if self._attempt is attempt:
                    self.cancel()
            raise RcloneError("rclone did not finish the sign-in")
        if attempt.error or not attempt.token:
            raise RcloneError(attempt.error or "the sign-in did not produce a token; start again")
        return attempt.token

    def _expire(self, attempt: _Attempt) -> None:
        with self._lock:
            if self._attempt is attempt:
                self.cancel()

    def cancel(self) -> None:
        attempt, self._attempt = self._attempt, None
        proc = attempt.proc if attempt else None
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
