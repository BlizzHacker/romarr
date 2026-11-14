"""Own a process-scoped ROMarr loopback server in Android private storage."""
import json
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
            root = Path(root)
            library = root / "library"
            library.mkdir(parents=True, exist_ok=True)
            (root / "dats").mkdir(exist_ok=True)
            env = dict(os.environ)
            env.update(ROMARR_DATA=str(root / "romarr.json"),
                       LIBRARY_PATH=str(library), DAT_PATH=str(root / "dats"),
                       ROMARR_HOST="127.0.0.1",
                       ROMARR_ANDROID="1", ROMARR_API_KEY=secrets.token_hex(32),
                       ROMARR_AUTH="enabled")
            os.environ["ROMARR_ANDROID"] = "1"
            from romarr.app import ROMarr, make_handler
            service = ROMarr(env)
            _service = service
            _server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(service))
            threading.Thread(target=_server.serve_forever, daemon=True,
                             name="ROMarr-loopback").start()
        return "http://127.0.0.1:" + str(_server.server_port)


def library_files(root):
    """List only resolved regular files beneath the private ROM library."""
    base = (Path(root) / "library").resolve()
    return json.dumps([str(p.relative_to(base)) for p in sorted(base.rglob("*"))
                       if p.is_file() and not p.is_symlink()
                       and p.resolve().is_relative_to(base)][:2000])

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
    return json.dumps({"python": platform.python_version(), "platform": sys.platform,
                       "android": os.environ.get("ROMARR_ANDROID") == "1",
                       "loopback": _server.server_address[0] if _server else None})


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
