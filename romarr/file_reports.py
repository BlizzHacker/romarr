"""Local file reports with bounded snapshot scanning and explicit reversible quarantine."""
from __future__ import annotations

import copy
import contextlib
import stat
import hashlib
import os
import sys
import time
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode

import requests
from . import scanners
from .quarantine import _atomic_json

MAX_BYTES = 4 * 1024 ** 3
CATEGORIES = {"malware", "not-a-game", "unwanted-file", "false-positive"}
ENGINES = {"clamav_enabled": "ClamAV", "scanner_defender_enabled": "Microsoft Defender",
           "scanner_mwm_enabled": "Configured antivirus service", "scanner_vt_enabled": "VirusTotal hash reputation"}


def _now():
    """Timestamp evidence in UTC rather than depending on host locale."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def issue_draft(title, body):
    """Prepare a fixed-repository GitHub draft without uploading files or credentials."""
    title = str(title or "").strip()
    body = str(body or "").strip()
    if not title or len(title) > 180 or not body or len(body) > 4500:
        raise ValueError("Use a title up to 180 characters and details up to 4500 characters")
    return "https://github.com/BlizzHacker/romarr/issues/new?" + urlencode({"title": title, "body": body})


def _move_no_replace(source, destination, *, source_dir_fd=None, destination_dir_fd=None):
    """Atomically rename without replacement, anchored to pinned parents on Linux/Android."""
    if sys.platform == "win32":
        _windows_move(source, destination, source_dir_fd, destination_dir_fd)
        return
    if sys.platform.startswith("linux") or sys.platform == "android":
        import ctypes
        import errno
        import platform
        libc = ctypes.CDLL(None, use_errno=True)
        args = (source_dir_fd if source_dir_fd is not None else -100, os.fsencode(source),
                destination_dir_fd if destination_dir_fd is not None else -100, os.fsencode(destination), 1)
        if hasattr(libc, "renameat2"):
            fn = libc.renameat2
            fn.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
            result = fn(*args)
        else:
            number = {"aarch64": 276, "arm64": 276, "x86_64": 316, "armv7l": 382}.get(platform.machine().lower())
            if number is None:
                raise OSError(errno.ENOSYS, "Non-overwriting move is unavailable")
            result = libc.syscall(ctypes.c_long(number), ctypes.c_int(args[0]), ctypes.c_char_p(args[1]),
                                  ctypes.c_int(args[2]), ctypes.c_char_p(args[3]), ctypes.c_uint(args[4]))
        if result:
            code = ctypes.get_errno()
            raise OSError(code, os.strerror(code))
        return
    raise ValueError("Secure non-overwriting moves are unavailable on this platform")


def _parts(relative):
    """Accept plain relative components without platform aliases or parent traversal."""
    if not isinstance(relative, str) or not relative or "\\" in relative or ":" in relative:
        raise ValueError("Unsafe library file path")
    parts = relative.split("/")
    if any(part in ("", ".", "..") or "\x00" in part for part in parts):
        raise ValueError("Unsafe library file path")
    if sys.platform == "win32" and any(part.rstrip(" .") != part for part in parts):
        raise ValueError("Windows path aliases are not allowed")
    return parts


def _object_id(observation):
    """Keep the persistent root identity independent of mutable directory timestamps."""
    return [observation.st_dev, observation.st_ino]


def _windows_handle(path, directory, parent=None, create=False):
    """Open a non-reparse Windows object, relative to its pinned parent when supplied."""
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel.CreateFileW
    create_file.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                       wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    create_file.restype = wintypes.HANDLE
    # Directory handles include traverse access for native relative opens/renames.
    if parent is not None:
        handle = _windows_relative_open(parent, Path(path).name, directory, create=create)
    else:
        handle = create_file(str(path), 0xa0 if directory else 0x80000000,
                        3 if directory else 1, None, 3, 0x00200000 | (0x02000000 if directory else 0), None)
    if handle == wintypes.HANDLE(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    class Information(ctypes.Structure):
        """Native BY_HANDLE_FILE_INFORMATION layout for reparse and type checks."""
        _fields_ = [("attributes", wintypes.DWORD), ("created", wintypes.FILETIME),
                    ("accessed", wintypes.FILETIME), ("modified", wintypes.FILETIME),
                    ("volume", wintypes.DWORD), ("size_high", wintypes.DWORD),
                    ("size_low", wintypes.DWORD), ("links", wintypes.DWORD),
                    ("index_high", wintypes.DWORD), ("index_low", wintypes.DWORD)]
    info = Information()
    query = kernel.GetFileInformationByHandle
    query.argtypes = [wintypes.HANDLE, ctypes.POINTER(Information)]
    query.restype = wintypes.BOOL
    close = kernel.CloseHandle
    close.argtypes = [wintypes.HANDLE]
    close.restype = wintypes.BOOL
    if not query(handle, ctypes.byref(info)) or info.attributes & 0x400 or bool(info.attributes & 0x10) != directory:
        close(handle)
        raise ValueError("Windows links and unexpected object types are not allowed")
    return handle


def _windows_relative_open(parent, name, directory, *, create=False, deleting=False):
    """Open one entry through an existing directory handle, without reparse traversal."""
    import ctypes
    from ctypes import wintypes
    if Path(name).name != name or name in ("", ".", "..") or any(c in name for c in "\\/:\x00"):
        raise ValueError("Unsafe native entry name")
    class UnicodeString(ctypes.Structure):
        """Native counted UTF-16 name."""
        _fields_ = [("length", wintypes.USHORT), ("maximum", wintypes.USHORT), ("buffer", wintypes.LPWSTR)]
    class ObjectAttributes(ctypes.Structure):
        """Native handle-relative object lookup."""
        _fields_ = [("length", wintypes.ULONG), ("root", wintypes.HANDLE),
                    ("name", ctypes.POINTER(UnicodeString)), ("attributes", wintypes.ULONG),
                    ("security", ctypes.c_void_p), ("quality", ctypes.c_void_p)]
    class StatusBlock(ctypes.Structure):
        """Pointer-sized NT I/O completion status."""
        _fields_ = [("status", ctypes.c_void_p), ("information", ctypes.c_size_t)]
    length = len(name.encode("utf-16-le"))
    text = ctypes.create_unicode_buffer(name, length // 2 + 1)
    counted = UnicodeString(length, length + 2, ctypes.cast(text, wintypes.LPWSTR))
    attributes = ObjectAttributes(ctypes.sizeof(ObjectAttributes), parent, ctypes.pointer(counted), 0x40, None, None)
    handle = wintypes.HANDLE()
    io = StatusBlock()
    native = ctypes.WinDLL("ntdll")
    open_file = native.NtCreateFile
    open_file.argtypes = [ctypes.POINTER(wintypes.HANDLE), wintypes.ULONG, ctypes.POINTER(ObjectAttributes),
                          ctypes.POINTER(StatusBlock), ctypes.c_void_p, wintypes.ULONG, wintypes.ULONG,
                          wintypes.ULONG, wintypes.ULONG, ctypes.c_void_p, wintypes.ULONG]
    open_file.restype = wintypes.LONG
    access = (0xa0 if directory else 0x80000000) | (0x10000 if deleting else 0) | 0x100000
    options = 0x00200000 | 0x20 | (1 if directory else 0x40)
    status = open_file(ctypes.byref(handle), access, ctypes.byref(attributes), ctypes.byref(io), None,
                       0, 3 if directory else 1, 3 if create else 1, options, None, 0)
    if status < 0:
        error = native.RtlNtStatusToDosError
        error.argtypes = [wintypes.LONG]
        error.restype = wintypes.ULONG
        raise ctypes.WinError(error(status))
    return handle.value


def _windows_move(source, destination, source_parent, destination_parent):
    """Rename an opened source to one name under a pinned destination directory."""
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    close = kernel.CloseHandle
    close.argtypes = [wintypes.HANDLE]
    owned = []
    try:
        if source_parent is None:
            source_parent = _windows_handle(Path(source).parent, True)
            owned.append(source_parent)
        if destination_parent is None:
            destination_parent = _windows_handle(Path(destination).parent, True)
            owned.append(destination_parent)
        source_handle = _windows_relative_open(source_parent, Path(source).name, False, deleting=True)
        owned.append(source_handle)
        name = Path(destination).name
        if _parts(name) != [name]:
            raise ValueError("Unsafe native destination name")
        class RenameInformation(ctypes.Structure):
            """Native FILE_RENAME_INFORMATION, replacement disabled."""
            _fields_ = [("replace", wintypes.BOOLEAN), ("root", wintypes.HANDLE),
                        ("length", wintypes.ULONG), ("name", wintypes.WCHAR * (len(name.encode("utf-16-le")) // 2 + 1))]
        class StatusBlock(ctypes.Structure):
            """Native I/O status for the rename request."""
            _fields_ = [("status", ctypes.c_void_p), ("information", ctypes.c_size_t)]
        information = RenameInformation(False, destination_parent, len(name.encode("utf-16-le")), name)
        io = StatusBlock()
        native = ctypes.WinDLL("ntdll")
        rename = native.NtSetInformationFile
        rename.argtypes = [wintypes.HANDLE, ctypes.POINTER(StatusBlock), ctypes.c_void_p,
                           wintypes.ULONG, wintypes.ULONG]
        rename.restype = wintypes.LONG
        status = rename(source_handle, ctypes.byref(io), ctypes.byref(information), ctypes.sizeof(information), 10)
        if status < 0:
            error = native.RtlNtStatusToDosError
            error.argtypes = [wintypes.LONG]
            error.restype = wintypes.ULONG
            raise ctypes.WinError(error(status))
    finally:
        for handle in reversed(owned):
            close(handle)


class _PinnedLibrary:
    """Pin the recorded root and every ancestor; all later operations use those bindings."""

    def __init__(self, root, expected=None):
        """Never resolve a recorded root again: a replacement link must be rejected."""
        self.root = Path(os.path.abspath(root))
        self.expected = expected
        self.descriptors = []
        self.handles = []
        self.bindings = []
        self.windows = sys.platform == "win32"
        self.root_fd = None

    def __enter__(self):
        """Open from the filesystem anchor, rejecting links in all root ancestors."""
        try:
            current = Path(self.root.anchor)
            components = self.root.parts[1:]
            android_identity = None
            if sys.platform == "android":
                import json
                anchor = os.environ.get("ROMARR_ANDROID_FILES_ROOT", "")
                if not anchor or not Path(anchor).is_absolute():
                    raise ValueError("A trusted native Android files anchor is required")
                current = Path(anchor)
                try:
                    components = self.root.relative_to(current).parts
                    android_identity = json.loads(os.environ.get("ROMARR_ANDROID_FILES_ID", ""))
                except (ValueError, TypeError) as exc:
                    raise ValueError("Library is outside the trusted Android files anchor") from exc
                if (not isinstance(android_identity, list) or len(android_identity) != 2
                        or any(type(item) is not int for item in android_identity)):
                    raise ValueError("Invalid trusted Android files identity")
            parent = self._pin(current, None, None)
            if android_identity is not None and _object_id(os.fstat(parent)) != android_identity:
                raise ValueError("The trusted Android files anchor changed")
            for component in components:
                current /= component
                parent = self._pin(current, parent, component)
            self.root_fd = parent
            self.root_id = _object_id(os.stat(self.root) if self.windows else os.fstat(parent))
            if self.expected is not None and list(self.expected) != self.root_id:
                raise ValueError("The recorded library root changed; submit a new report")
            self.validate()
            return self
        except Exception:
            self.__exit__(None, None, None)
            raise

    def _pin(self, path, parent, component, create=False):
        """Hold one directory and remember its binding to its already pinned parent."""
        if self.windows:
            handle = _windows_handle(path, True, parent=parent, create=create)
            self.handles.append(handle)
            identity = _object_id(os.stat(path, follow_symlinks=False))
            self.bindings.append((path, identity))
            return handle
        if not (os.open in os.supports_dir_fd and hasattr(os, "O_NOFOLLOW")):
            raise ValueError("Secure library operations are unavailable on this platform")
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        descriptor = os.open(component if parent is not None else path, flags,
                             **({"dir_fd": parent} if parent is not None else {}))
        self.descriptors.append(descriptor)
        if parent is not None:
            self.bindings.append((parent, component, descriptor))
        return descriptor

    def parent(self, relative, create_quarantine=False):
        """Return a pinned parent and final name without following mutable parent paths."""
        components = _parts(relative)
        parent, path = self.root_fd, self.root
        for component in components[:-1]:
            path /= component
            if create_quarantine and component == ".romarr-file-quarantine" and path.parent == self.root:
                try:
                    if not self.windows:
                        os.mkdir(component, mode=0o700, dir_fd=parent)
                except FileExistsError:
                    pass
            parent = self._pin(path, parent, component, create=(self.windows and create_quarantine
                               and component == ".romarr-file-quarantine" and path.parent == self.root))
        self.validate()
        return parent, path, components[-1]

    def validate(self):
        """Refuse a detached, replaced or linked directory before any read or publication."""
        if self.windows:
            for path, identity in self.bindings:
                observation = os.stat(path, follow_symlinks=False)
                if _object_id(observation) != identity or getattr(observation, "st_file_attributes", 0) & 0x400:
                    raise ValueError("A pinned library directory changed")
        else:
            for parent, component, descriptor in self.bindings:
                observation = os.stat(component, dir_fd=parent, follow_symlinks=False)
                if not stat.S_ISDIR(observation.st_mode) or _object_id(observation) != _object_id(os.fstat(descriptor)):
                    raise ValueError("A pinned library directory changed")

    def entry_stat(self, location):
        """Inspect the final directory entry itself, never its symlink destination."""
        descriptor, path, name = location
        return os.stat(path / name, follow_symlinks=False) if self.windows else os.stat(name, dir_fd=descriptor, follow_symlinks=False)

    def exists(self, location):
        """Treat dangling links as occupied destinations too."""
        try:
            self.entry_stat(location)
            return True
        except FileNotFoundError:
            return False

    @contextlib.contextmanager
    def open(self, location):
        """Read a regular non-linked final object while its ancestors remain pinned."""
        descriptor, path, name = location
        self.validate()
        if not stat.S_ISREG(self.entry_stat(location).st_mode):
            raise ValueError("Only regular non-linked files can be reviewed")
        source = None
        fd = None
        try:
            if self.windows:
                import ctypes
                import msvcrt
                from ctypes import wintypes
                handle = _windows_handle(path / name, False, parent=descriptor)
                try:
                    final = ctypes.WinDLL("kernel32", use_last_error=True).GetFinalPathNameByHandleW
                    final.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD]
                    final.restype = wintypes.DWORD
                    buffer = ctypes.create_unicode_buffer(32768)
                    size = final(handle, buffer, len(buffer), 0)
                    if not size or size >= len(buffer):
                        raise ValueError("Could not validate the opened file")
                    actual = buffer.value
                    if actual.startswith("\\\\?\\UNC\\"):
                        actual = "\\\\" + actual[8:]
                    elif actual.startswith("\\\\?\\"):
                        actual = actual[4:]
                    try:
                        Path(actual).relative_to(self.root)
                    except ValueError as exc:
                        raise ValueError("The opened file escaped the recorded library") from exc
                    fd = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
                    handle = None
                finally:
                    if handle is not None:
                        close = ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle
                        close.argtypes = [wintypes.HANDLE]
                        close(handle)
            else:
                fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0), dir_fd=descriptor)
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise ValueError("Only regular files can be reviewed")
            source = os.fdopen(fd, "rb"); fd = None
            self.validate()
            yield source
        finally:
            if source is not None:
                source.close()
            if fd is not None:
                os.close(fd)

    def move(self, source, destination):
        """Publish a non-replacing rename relative to the same pinned library root."""
        self.validate()
        if self.windows:
            _move_no_replace(source[2], destination[2], source_dir_fd=source[0], destination_dir_fd=destination[0])
        else:
            _move_no_replace(source[2], destination[2], source_dir_fd=source[0], destination_dir_fd=destination[0])
            os.fsync(source[0]); os.fsync(destination[0])

    def __exit__(self, *_):
        """Release file-system pins in reverse order, including partial setup failures."""
        if self.windows:
            import ctypes
            from ctypes import wintypes
            close = ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle
            close.argtypes = [wintypes.HANDLE]
            for handle in reversed(self.handles):
                close(handle)
            self.handles.clear()
        for descriptor in reversed(self.descriptors):
            os.close(descriptor)
        self.descriptors.clear()


@contextlib.contextmanager
def _open_source(root, relative, root_id=None):
    """Bind snapshot reads to the recorded root and reject changes before scanning."""
    with _PinnedLibrary(root, root_id) as pins:
        location = pins.parent(relative)
        with pins.open(location) as source:
            yield source
            pins.validate()
            observation = pins.entry_stat(location)
            if not stat.S_ISREG(observation.st_mode) or _object_id(observation) != _object_id(os.fstat(source.fileno())):
                raise ValueError("The reviewed directory entry changed")


def _digest(source):
    """Hash an opened regular file and refuse content changes during hashing."""
    before = _fd_identity(source)
    if before[2] > MAX_BYTES:
        raise ValueError("The opened file exceeds the review size limit")
    h = hashlib.sha256()
    remaining = MAX_BYTES + 1
    for chunk in iter(lambda: source.read(min(1024 * 1024, remaining)), b""):
        remaining -= len(chunk)
        if remaining <= 0:
            raise ValueError("The opened file exceeds the review size limit")
        h.update(chunk)
    if _fd_identity(source) != before:
        raise ValueError("The opened file changed")
    return h.hexdigest(), before


def _fd_identity(source):
    """Compare the opened object before and after copying its bytes."""
    return _stat_identity(os.fstat(source.fileno()))


def _stat_identity(observation):
    """Compare consistent change metadata; Windows stat/fstat ctime meanings differ."""
    return (observation.st_dev, observation.st_ino, observation.st_size, observation.st_mtime_ns,
            0 if sys.platform == "win32" else observation.st_ctime_ns)


class FileReports:
    """Persist evidence locally; scanner availability is never a clean verdict."""

    def __init__(self, library, data_dir, settings, env, version=""):
        """Accept live configuration readers without starting background work on boot."""
        self.library = library
        self.settings = settings
        self.env = env
        self.version = version
        self.directory = Path(data_dir) / "file-reports"
        self.lock = threading.RLock()
        self.slots = threading.BoundedSemaphore(2)
        self.active = set()
        self.last_issue = 0.0

    def _record_path(self, ident):
        """Accept only generated identifiers, never user-selected storage paths."""
        if not isinstance(ident, str) or len(ident) != 32 or any(c not in "0123456789abcdef" for c in ident):
            raise ValueError("Invalid report ID")
        return self.directory / (ident + ".json")

    def _read(self, ident):
        """Load one durable report without accepting arbitrary JSON paths."""
        import json
        with self._record_path(ident).open(encoding="utf-8") as source:
            report = json.load(source)
        if not isinstance(report, dict) or report.get("id") != ident:
            raise ValueError("Invalid report record")
        if report.get("quarantine_state") in {"moving", "restoring"} and report.get("_root_id"):
            try:
                with _PinnedLibrary(report["_library"], report["_root_id"]) as pins:
                    held = pins.parent(".romarr-file-quarantine/" + ident)
                    original = pins.parent(report["file"])
                    held_exists, original_exists = pins.exists(held), pins.exists(original)
                    candidate = held if held_exists and not original_exists else original if original_exists and not held_exists else None
                    if candidate is not None:
                        with pins.open(candidate) as source:
                            digest, identity = _digest(source)
                            observation = pins.entry_stat(candidate)
                            if not stat.S_ISREG(observation.st_mode) or _object_id(observation) != list(identity[:2]):
                                raise ValueError("Recovery entry changed")
                        pins.validate()
                        if digest == report.get("sha256"):
                            report.update(quarantined=held_exists, quarantine_state="quarantined" if held_exists else "restored")
                            self._save(report)
            except (OSError, ValueError):
                # Preserve both bytes and the unresolved state; never hash a newly linked path.
                pass
        return report

    def _save(self, report):
        """Save evidence atomically before acknowledging a state change."""
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        _atomic_json(self._record_path(report["id"]), report)

    def _public(self, report):
        """Remove absolute private paths and distinguish interrupted reviews after restart."""
        out = copy.deepcopy({k: v for k, v in report.items() if not k.startswith("_")})
        if out["status"] in {"queued", "scanning"} and out["id"] not in self.active:
            out["status"] = "interrupted"
        return out

    def list(self):
        """Return bounded local reports and whether direct GitHub submission is configured."""
        with self.lock:
            records = []
            for p in sorted(self.directory.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:200]:
                try:
                    records.append(self._public(self._read(p.stem)))
                except (OSError, ValueError, KeyError, TypeError):
                    continue
            return {"items": records, "version": self.version, "direct_github": bool(self.env.get("ROMARR_GITHUB_ISSUES_TOKEN"))}

    def submit(self, relative, category, notes=""):
        """Queue a report only for a regular non-linked file in the configured library."""
        if not isinstance(category, str) or category not in CATEGORIES:
            raise ValueError("Choose malware, not-a-game, unwanted-file or false-positive")
        if not isinstance(relative, str) or len(relative) > 2000 or "\\" in relative or ":" in relative:
            raise ValueError("Enter a relative library file path")
        if len(str(notes)) > 2000:
            raise ValueError("Keep notes below 2000 characters")
        root = Path(self.library()).resolve()
        with _PinnedLibrary(root) as pins:
            location = pins.parent(relative)
            with pins.open(location) as source:
                size = os.fstat(source.fileno()).st_size
            root_id = pins.root_id
        if any(p.startswith(".romarr") for p in Path(relative).parts):
            raise ValueError("Internal ROMarr storage cannot be reported")
        if size > MAX_BYTES:
            raise ValueError("This file exceeds the 4 GiB review snapshot limit")
        if not self.slots.acquire(blocking=False):
            raise ValueError("Two reviews are already running; wait for one to finish")
        report = {"id": uuid.uuid4().hex, "file": relative, "category": category,
                  "notes": str(notes), "created": _now(), "status": "queued", "scanners": [],
                  "_library": str(root), "_root_id": root_id, "claim": "User report; not a confirmed detection"}
        try:
            with self.lock:
                self.active.add(report["id"])
                self._save(report)
            threading.Thread(target=self._review, args=(report,), daemon=True,
                             name="ROMarr-file-review").start()
        except Exception:
            with self.lock:
                self.active.discard(report["id"])
            self.slots.release()
            raise
        return self._public(report)

    def _review(self, report):
        """Scan a private snapshot with configured engines; never execute or attach its bytes."""
        import tempfile
        try:
            root = Path(report["_library"])
            report["status"] = "scanning"
            with self.lock:
                self._save(report)
            with tempfile.TemporaryDirectory(prefix="scan-", dir=self.directory) as raw:
                snapshot = Path(raw) / "reported-file"
                digest = hashlib.sha256()
                count = 0
                with _open_source(root, report["file"], report["_root_id"]) as src, snapshot.open("xb") as dest:
                    before = _fd_identity(src)
                    os.chmod(snapshot, 0o600)
                    for chunk in iter(lambda: src.read(1024 * 1024), b""):
                        count += len(chunk)
                        if count > MAX_BYTES:
                            raise ValueError("Snapshot size limit exceeded")
                        digest.update(chunk); dest.write(chunk)
                    if _fd_identity(src) != before:
                        raise ValueError("Opened source changed during the snapshot")
                report.update(sha256=digest.hexdigest(), size_bytes=count)
                cfg = dict(self.settings())
                if scanners.defender_path():
                    cfg["scanner_defender_enabled"] = True
                if cfg.get("clamav_address"):
                    cfg["clamav_enabled"] = True
                evidence = []
                for flag, name in ENGINES.items():
                    if not cfg.get(flag):
                        continue
                    single = dict(cfg)
                    for key in ENGINES:
                        single[key] = key == flag
                    item = {"engine": name, "checked": _now()}
                    try:
                        found, skipped = scanners.scan(single, self.env, snapshot)
                        signatures = [str(f.signature)[:300] for f in found]
                        real = [signature for signature in signatures if not signature.casefold().startswith("heuristics.limits.exceeded")]
                        limited = len(real) != len(signatures)
                        item.update(status="detected" if real else ("incomplete" if skipped or limited else "no-detections"), signatures=real)
                        if limited:
                            item["incomplete"] = True
                            item["limit_signatures"] = [signature for signature in signatures if signature not in real]
                            item["detail"] = "Scanner limits prevented a complete scan; limit signatures are not malware detections"
                    except scanners.ScanUnavailable:
                        item.update(status="unavailable", detail="Scanner did not complete; check Media Management scanner settings")
                    except Exception:
                        item.update(status="error", detail="Scanner returned no usable verdict")
                    evidence.append(item)
                report["scanners"] = evidence
                actual = [x for x in evidence if x["status"] == "detected" and x["engine"] != "VirusTotal hash reputation"]
                if actual and all(e.get("signatures") and all("eicar" in signature.casefold() for signature in e["signatures"]) for e in actual):
                    report["status"] = "antivirus-test-fixture"
                elif actual:
                    report["status"] = "detection-reported"
                elif any(x["status"] == "detected" for x in evidence):
                    report["status"] = "reputation-flagged"
                elif evidence and all(x["status"] == "no-detections" for x in evidence):
                    report["status"] = "no-detections"
                else:
                    report["status"] = "needs-review"
                report["claim"] = "Scanner evidence applies to this SHA-256 snapshot, not a guarantee of safety or proof that it is a game"
            report["reviewed"] = _now()
        except Exception:
            report.update(status="review-failed", detail="File review failed or the source changed. Check availability and submit it again.")
        finally:
            try:
                with self.lock:
                    self._save(report)
            finally:
                with self.lock:
                    self.active.discard(report["id"])
                self.slots.release()

    def _transfer(self, report, restoring):
        """Move under pinned ancestors, then verify the moved inode and bytes before applying evidence."""
        if not report.get("_root_id"):
            raise ValueError("This report predates secure root tracking; submit a new report")
        held = ".romarr-file-quarantine/" + report["id"]
        with _PinnedLibrary(report["_library"], report["_root_id"]) as pins:
            original = pins.parent(report["file"])
            quarantine = pins.parent(held, create_quarantine=not restoring)
            source, destination = (quarantine, original) if restoring else (original, quarantine)
            if pins.exists(destination):
                raise ValueError("Restore refused: destination exists" if restoring else "Quarantine destination already exists")
            with pins.open(source) as opened:
                digest, before = _digest(opened)
                if digest != report["sha256"]:
                    raise ValueError("File changed since review; scan it again")
                pins.validate()
                observation = pins.entry_stat(source)
                if not stat.S_ISREG(observation.st_mode) or _object_id(observation) != list(before[:2]):
                    raise ValueError("File changed before quarantine or restore")
            # Reopen the final source by its pinned parent for the native rename.
            report["quarantine_state"] = "restoring" if restoring else "moving"
            self._save(report)
            pins.validate()
            current = pins.entry_stat(source)
            if not stat.S_ISREG(current.st_mode) or _stat_identity(current) != before:
                raise ValueError("File changed before quarantine or restore")
            pins.move(source, destination)
            try:
                pins.validate()
                with pins.open(destination) as moved:
                    moved_digest, moved_identity = _digest(moved)
                    if moved_identity[:4] != before[:4] or moved_digest != report["sha256"]:
                        raise ValueError("Directory entry changed during the move")
                    observation = pins.entry_stat(destination)
                    if _object_id(observation) != list(moved_identity[:2]) or not stat.S_ISREG(observation.st_mode):
                        raise ValueError("Moved directory entry changed")
            except (OSError, ValueError) as exc:
                # A hostile final-name replacement may itself have been moved. Preserve it;
                # never assign the reviewed file's scanner verdict to these different bytes.
                report.update(quarantined=False, quarantine_state="conflict", action_conflict=True,
                              action_detail="The moved entry changed; bytes were preserved for manual review")
                self._save(report)
                raise ValueError("Move conflict: preserved bytes require manual review") from exc
            report.update(quarantined=not restoring, quarantine_state="restored" if restoring else "quarantined")
            self._save(report)
            return self._public(report)

    def quarantine(self, ident):
        """Move only reviewed bytes after explicit consent, without overwriting a destination."""
        with self.lock:
            report = self._read(ident)
            if report["id"] in self.active or not report.get("sha256"):
                raise ValueError("Wait for the file review to finish")
            if report.get("quarantined"):
                raise ValueError("This file is already quarantined")
            if report.get("action_conflict"):
                raise ValueError("This report has a move conflict; review preserved bytes manually")
            return self._transfer(report, False)

    def restore(self, ident):
        """Restore reviewed bytes under the original root without replacing any current file."""
        with self.lock:
            report = self._read(ident)
            if not report.get("quarantined") or report.get("action_conflict"):
                raise ValueError("This report is not quarantined or needs manual conflict review")
            return self._transfer(report, True)

    def evidence_text(self, ident):
        """Build a shareable summary without notes, absolute paths or source credentials."""
        with self.lock:
            r = self._read(ident)
            if ident in self.active:
                raise ValueError("Wait for the review to finish before sharing its evidence")
        lines = ["File report (user claim, subject to review)", "Category: " + r["category"],
                 "File name: " + Path(r["file"]).name, "SHA-256: " + r.get("sha256", "not yet computed"),
                 "Review status: " + r["status"], "Created: " + r["created"]]
        for e in r.get("scanners", []):
            lines.append(e["engine"] + ": " + e["status"] + " " + ", ".join(e.get("signatures", [])))
        lines.append("No file bytes are attached to this issue. No-detections is not a guarantee of safety.")
        return "\n".join(lines)

    def github(self, title, body, confirm=False, report_id=""):
        """Create a reviewed issue only with an explicit configured token and user consent."""
        title, body = str(title or "").strip(), str(body or "").strip()
        if self.version:
            body = "ROMarr version: " + self.version + "\n\n" + body
        if report_id:
            body = str(body or "") + "\n\n" + self.evidence_text(report_id)
        url = issue_draft(title, body)
        token = str(self.env.get("ROMARR_GITHUB_ISSUES_TOKEN") or "")
        if not token or not confirm:
            return {"mode": "browser", "url": url, "requires_review": True}
        with self.lock:
            if self.last_issue and time.monotonic() - self.last_issue < 30:
                raise ValueError("Wait 30 seconds between GitHub submissions")
            self.last_issue = time.monotonic()
        response = requests.post("https://api.github.com/repos/BlizzHacker/romarr/issues",
            headers={"Authorization": "Bearer " + token, "Accept": "application/vnd.github+json"},
            json={"title": title, "body": body}, timeout=(5, 25), allow_redirects=False)
        if response.status_code != 201:
            raise ValueError("GitHub did not accept the issue (HTTP " + str(response.status_code) + ")")
        link = response.json().get("html_url", "")
        if not link.startswith("https://github.com/BlizzHacker/romarr/issues/"):
            raise ValueError("GitHub returned an unexpected issue URL")
        return {"mode": "submitted", "url": link}
