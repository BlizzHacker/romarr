"""Own a process-scoped ROMarr loopback server in Android private storage."""
import json
import logging
import os
import threading
import secrets
from pathlib import Path
from http.server import ThreadingHTTPServer

_server = None
_service = None
_lock = threading.Lock()


def start(root):
    """Start once per app process; never expose the listener to the LAN."""
    global _server, _service
    with _lock:
        if _server is None:
            root = Path(root).resolve(strict=True)
            library = root / "library"
            library.mkdir(parents=True, exist_ok=True)
            (root / "dats").mkdir(exist_ok=True)
            anchor = root.stat()
            native_env = {
                "ROMARR_ANDROID": "1", "ROMARR_ANDROID_FILES_ROOT": str(root),
                "ROMARR_ANDROID_FILES_ID": json.dumps([anchor.st_dev, anchor.st_ino]),
            }
            # Only the native Context supplies this private directory trust anchor.
            os.environ.update(native_env)
            env = dict(os.environ)
            env.update(ROMARR_DATA=str(root / "romarr.json"),
                       LIBRARY_PATH=str(library), DAT_PATH=str(root / "dats"),
                       ROMARR_HOST="127.0.0.1",
                       ROMARR_ANDROID="1", ROMARR_API_KEY=secrets.token_hex(32),
                       ROMARR_AUTH="enabled")
            from romarr.app import ROMarr, make_handler
            service = ROMarr(env)
            # Match the normal server's recovery after a prior process died.
            try:
                service.store.recover_seerr_dispatches()
            except Exception:
                logging.getLogger(__name__).exception(
                    "integration recovery skipped (non-fatal)")
            _service = service
            _server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(service))
            threading.Thread(target=_server.serve_forever, daemon=True,
                             name="ROMarr-loopback").start()
        return "http://127.0.0.1:" + str(_server.server_port)


def library_files(root):
    """Offer regular files while pruning quarantine and all ROMarr internal dirs."""
    base = (Path(root) / "library").resolve()
    found = []
    for directory, dirs, files in os.walk(base, followlinks=False):
        dirs[:] = [name for name in dirs if not name.casefold().startswith(".romarr")
                   and not (Path(directory) / name).is_symlink()]
        for name in files:
            path = Path(directory) / name
            if (not name.casefold().startswith(".romarr") and not path.is_symlink()
                    and path.is_file() and path.resolve().is_relative_to(base)):
                found.append(str(path.relative_to(base)))
                if len(found) >= 2000:
                    return json.dumps(sorted(found))
    return json.dumps(sorted(found))


def smoke_stdlib():
    """Exercise Android OpenSSL and core auth primitives in the packaged runtime."""
    import hashlib
    import ssl
    import requests
    from romarr.auth import Auth
    hashlib.scrypt(b"test", salt=b"fixture", n=16384, r=8, p=1)
    ssl.create_default_context()
    return "ok"

def refresh_dats(root):
    """Apply user-selected DAT files using the core's bounded DAT loader."""
    if _service is None:
        raise RuntimeError("Local server is not started")
    return json.dumps(_service.reload_dats(str(Path(root) / "dats")))

def runtime_facts():
    """Return non-secret runtime identity for device instrumentation proof."""
    import platform
    import sys
    anchor = Path(os.environ["ROMARR_ANDROID_FILES_ROOT"]).stat() if _service else None
    anchor_ok = bool(anchor and [anchor.st_dev, anchor.st_ino] ==
                     json.loads(os.environ["ROMARR_ANDROID_FILES_ID"]))
    return json.dumps({"python": platform.python_version(), "platform": sys.platform,
                       "android": os.environ.get("ROMARR_ANDROID") == "1",
                       "files_anchor_verified": anchor_ok,
                       "loopback": _server.server_address[0] if _server else None,
                       "scheduler_alive": bool(_service and _service.scheduler._thread
                                               and _service.scheduler._thread.is_alive()),
                       "tasks": [job["name"] for job in _service.scheduler.status()]
                                if _service else []})


def session_cookie():
    """Issue a native-only session; never expose the API key to the WebView."""
    if _service is None:
        raise RuntimeError("Local server is not started")
    return ("romarr_session=" + _service.auth.issue_session() +
            "; HttpOnly; SameSite=Strict; Path=/")


def publish_import(root, staged_path, name, is_dat=False):
    """Publish private staged bytes atomically without replacing any existing file."""
    root = Path(root).resolve()
    staged = Path(staged_path)
    if (staged.is_symlink() or not staged.is_file()
            or staged.resolve().parent != (root / "staging").resolve()
            or not staged.name.startswith("import-")
            or not staged.name.endswith(".partial")):
        raise ValueError("Invalid private staging file")
    if not name or name in {".", ".."} or Path(name).name != name:
        raise ValueError("Invalid import filename")
    folder = root / "dats" if is_dat else root / "library" / "Imported"
    folder.mkdir(parents=True, exist_ok=True)
    for attempt in range(20):
        destination = folder / (name if attempt == 0 else secrets.token_hex(6) + "-" + name)
        try:
            _rename_noreplace(staged, destination)
        except FileExistsError:
            continue
        return str(destination)
    raise FileExistsError("Could not allocate a unique import filename")


def _rename_noreplace(source, destination):
    """Use Linux atomic non-replacing rename; unsupported kernels fail closed."""
    import ctypes
    import platform
    import errno
    libc = ctypes.CDLL(None, use_errno=True)
    args = (ctypes.c_int(-100), ctypes.c_char_p(os.fsencode(source)),
            ctypes.c_int(-100), ctypes.c_char_p(os.fsencode(destination)),
            ctypes.c_uint(1))  # AT_FDCWD and RENAME_NOREPLACE.
    if hasattr(libc, "renameat2"):
        rename = libc.renameat2
        rename.restype = ctypes.c_int
        result = rename(*args)
    else:
        numbers = {"x86_64": 316, "aarch64": 276}
        number = numbers.get(platform.machine())
        if number is None:
            raise OSError(errno.ENOSYS, "Atomic import is unavailable")
        libc.syscall.restype = ctypes.c_long
        result = libc.syscall(ctypes.c_long(number), *args)
    if result != 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code), str(destination))


def core_version():
    """Expose the packaged server version for native build-identity verification."""
    from romarr.app import VERSION
    return VERSION


def smoke_report_quarantine_restore(root):
    """Verify isolated Android report/quarantine/restore using harmless owned bytes."""
    import hashlib
    import tempfile
    import time
    from romarr.file_reports import FileReports
    with tempfile.TemporaryDirectory(prefix="android-report-test-", dir=root) as raw:
        base = Path(raw)
        library = base / "library"
        library.mkdir()
        fixture = library / "owned-fixture.rom"
        payload = b"ROMarr harmless Android report fixture"
        fixture.write_bytes(payload)
        manager = FileReports(lambda: library, base / "state", lambda: {},
                              {key: os.environ[key] for key in (
                                  "ROMARR_ANDROID", "ROMARR_ANDROID_FILES_ROOT",
                                  "ROMARR_ANDROID_FILES_ID")}, version=core_version())
        ident = manager.submit(fixture.name, "unwanted-file")["id"]
        deadline = time.monotonic() + 10
        while ident in manager.active and time.monotonic() < deadline:
            time.sleep(0.05)
        if ident in manager.active:
            raise AssertionError("Report review timed out")
        report = manager.list()["items"][0]
        assert report["sha256"] == hashlib.sha256(payload).hexdigest()
        assert report["status"] == "needs-review"
        manager.quarantine(ident)
        assert not fixture.exists()
        assert json.loads(library_files(base)) == []
        manager.restore(ident)
        assert fixture.read_bytes() == payload
    return "ok"
