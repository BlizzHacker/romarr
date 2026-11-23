"""What ROMarr does with a cloud connection: jobs, imports and mounts.

Job kinds, each run on demand or every N hours by the scheduler:

* **backup** -- copy a library (or platform) folder to the cloud. New and
  changed files go up; nothing is deleted on either side.
* **sync** -- mirror a folder: like backup, but files gone locally are
  removed from the cloud copy too.
* **offload** -- move a library folder to the cloud: each file is deleted
  locally only after it has been uploaded and checked. For freeing disk;
  pair it with a mount if the games should stay playable.
* **import** -- bring games *from* a cloud folder into the library through
  the normal import (`ServiceBase.adopt`): DAT verification, renaming,
  platform folders. A scheduled import only takes files it has not taken
  before, so a shared "ROM inbox" folder can be watched.
* **settings** -- write ROMarr's settings, with every credential masked, to
  a cloud folder and keep the newest N.

Imports also take a Google Drive share link (a file or a folder somebody
shared), copied through the operator's own Drive account.

A **mount** (rclone + FUSE) makes a cloud folder look like a local one, so
it can be a library root. It needs /dev/fuse on the host.

Local folders are confined to the folders the operator already gave ROMarr
(libraries, downloads); offload, which deletes, only to library folders.
"""
from __future__ import annotations

import calendar
import json
import logging
import os
import re
import secrets
import shutil
import tempfile
import threading
import time
from collections import OrderedDict, deque
from pathlib import Path

from . import cloud_native as native
from .cloud_rclone import RcloneError, fs_string, fuse_available
from .cloud_storage import CloudError, _clean_label
from .path_guard import confined, env_roots, usable_root

log = logging.getLogger(__name__)

KINDS = {"backup": "Back up a folder", "sync": "Mirror a folder",
         "offload": "Offload (move) a folder", "import": "Import from a cloud folder",
         "settings": "Back up ROMarr settings"}
CACHE_MODES = ("off", "minimal", "writes", "full")
MAX_RUNS = 50
MAX_IMPORT_FILES = 500
MAX_SEEN = 5000
MAX_DEPTH = 8
POLL = 2.0
FIRST_POLL = 0.25   # small files finish in well under a second
STAGING = ".romarr-cloud-import"
_DRIVE_ID = re.compile(r"(?:/file/d/|/folders/|[?&]id=)([A-Za-z0-9_-]{10,200})")


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def drive_id(url: str) -> str:
    """The file or folder id in a Google Drive share link, or ""."""
    text = str(url or "")
    if not re.match(r"^https://(drive|docs)\.google\.com/", text):
        return ""
    found = _DRIVE_ID.search(text)
    return found.group(1) if found else ""


class CloudJobs:
    def __init__(self, cloud, service):
        self.cloud = cloud
        self.svc = service
        self._runs: OrderedDict[str, dict] = OrderedDict()
        self._lock = threading.RLock()

    # -- folders ROMarr may touch ------------------------------------------------

    def _library_roots(self) -> list[str]:
        roots = [str(self.svc.library_root(cfg)) for cfg in self.svc.store.list_items("libraries")]
        roots.append(str(self.svc.library))
        return [r for r in dict.fromkeys(roots) if usable_root(r)]

    def _local(self, path: str, *, kind: str) -> str:
        if not str(path or "").strip():
            raise CloudError("Choose a local folder")
        roots = self._library_roots() if kind == "offload" else self.svc._host_roots()
        try:
            real = confined(path, roots)
        except ValueError:
            raise CloudError("That folder is outside the library and download folders "
                             "ROMarr was given" + (" (offload: library folders only)"
                                                   if kind == "offload" else "")) from None
        data_dir = os.path.realpath(str(self.cloud.data_dir))
        if kind in ("offload", "sync") and (real == data_dir or real.startswith(data_dir + os.sep)):
            raise CloudError("ROMarr's own data folder cannot be offloaded or mirrored")
        if not os.path.isdir(real):
            raise CloudError("That local folder does not exist")
        return real

    # -- job records --------------------------------------------------------------

    def save_job(self, body: dict) -> dict:
        kind = str(body.get("kind") or "")
        if kind not in KINDS:
            raise CloudError("Unknown job kind")
        remote = self.cloud.remote(str(body.get("remote") or ""))
        if remote["engine"] != "rclone" and kind in ("offload", "sync"):
            raise CloudError(f"{KINDS[kind]} needs an rclone connection: rclone checks each "
                             "file by checksum before anything is deleted")
        job = {"id": str(body.get("id") or f"j-{secrets.token_hex(4)}"), "kind": kind,
               "name": _clean_label(body.get("name"), KINDS[kind]), "remote": remote["id"],
               "remote_path": str(body.get("remote_path") or "").strip().strip("/")[:500],
               "interval_hours": max(0.0, min(float(body.get("interval_hours") or 0), 24 * 30))}
        if any(ord(c) < 32 for c in job["remote_path"]):
            raise CloudError("invalid cloud folder")
        if kind in ("backup", "sync", "offload"):
            job["local_path"] = self._local(str(body.get("local_path") or ""), kind=kind)
        if kind == "offload" and body.get("confirm") is not True:
            raise CloudError("Offload deletes local files after upload; confirm it")
        if kind == "import":
            job["platform"] = str(body.get("platform") or "")[:40]
        if kind == "settings":
            job["keep"] = max(1, min(int(body.get("keep") or 14), 365))
        def upsert(cfg):
            old = next((j for j in cfg["jobs"] if j.get("id") == job["id"]), {})
            for carried in ("last_run", "last_result", "seen"):
                if carried in old:
                    job[carried] = old[carried]
            cfg["jobs"] = [j for j in cfg["jobs"] if j.get("id") != job["id"]] + [job]
        self.cloud.update_cfg(upsert)
        return {"ok": True, "job": job}

    def delete_job(self, job_id: str) -> dict:
        def drop(cfg):
            cfg["jobs"] = [j for j in cfg["jobs"] if j.get("id") != job_id]
        self.cloud.update_cfg(drop)
        return {"ok": True}

    def _job(self, job_id: str) -> dict:
        found = next((j for j in self.cloud.cfg()["jobs"] if j.get("id") == job_id), None)
        if found is None:
            raise CloudError("no such job")
        return found

    def _record(self, job_id: str, **fields) -> None:
        def apply(cfg):
            for job in cfg["jobs"]:
                if job.get("id") == job_id:
                    job.update(fields)
        self.cloud.update_cfg(apply)

    # -- runs ---------------------------------------------------------------------------

    def _new_run(self, label: str, job_id: str = "") -> dict:
        run = {"id": f"r-{secrets.token_hex(4)}", "job": job_id, "label": label,
               "state": "running", "started": _now(), "finished": "", "message": "",
               "files": 0, "bytes": 0, "total_bytes": 0, "errors": 0}
        with self._lock:
            self._runs[run["id"]] = run
            while len(self._runs) > MAX_RUNS:
                self._runs.popitem(last=False)
        return run

    def runs(self) -> list[dict]:
        with self._lock:
            return [dict(r) for r in reversed(self._runs.values())]

    def running(self, job_id: str) -> bool:
        return any(r["job"] == job_id and r["state"] == "running" for r in self.runs())

    def _spawn(self, run: dict, work) -> dict:
        def body():
            try:
                message = work(run) or "done"
                run.update(state="done", message=str(message)[:500])
            except (CloudError, RcloneError, native.NativeError, OSError) as err:
                run.update(state="failed", message=str(err)[:500])
            except Exception as err:   # noqa: BLE001 - a job thread must report, not die
                log.exception("cloud job failed")
                run.update(state="failed", message=f"{err.__class__.__name__}: {err}"[:500])
            run["finished"] = _now()
            if run["job"]:
                self._record(run["job"], last_run=run["finished"],
                             last_result=f"{run['state']}: {run['message']}"[:300])
        threading.Thread(target=body, name=f"romarr-cloud-{run['id']}", daemon=True).start()
        return dict(run)

    def run(self, job_id: str) -> dict:
        job = self._job(job_id)
        with self._lock:
            if self.running(job_id):
                raise CloudError("That job is already running")
            run = self._new_run(job["name"], job_id)
        return self._spawn(run, lambda r: self._work(job, r))

    def _work(self, job: dict, run: dict) -> str:
        remote = self.cloud.remote(job["remote"])
        kind = job["kind"]
        if kind == "settings":
            return self._settings_backup(job, remote, run)
        if kind == "import":
            return self._import_folder(job, remote, run)
        local = self._local(job["local_path"], kind=kind)
        if remote["engine"] == "rclone":
            dst = fs_string(remote["id"], job["remote_path"])
            mode = {"backup": "copy", "sync": "sync", "offload": "move"}[kind]
            self._wait(self.cloud.rclone.copy(local, dst, mode=mode), run)
            return f"{run['files']} file(s), {run['bytes']} bytes"
        if kind != "backup":
            raise CloudError(f"{KINDS[kind]} needs an rclone connection")
        return self._native_upload(job, remote, local, run)

    def _wait(self, jobid: int, run: dict) -> dict:
        delay = FIRST_POLL
        while True:
            status = self.cloud.rclone.job(jobid)
            run.update(bytes=status["bytes"], total_bytes=status["total_bytes"],
                       files=status["transfers"], errors=status["errors"])
            if status["finished"]:
                if not status["success"]:
                    raise CloudError(status["error"] or "rclone reported a failure")
                return status
            time.sleep(delay)
            delay = min(POLL, delay * 2)

    # -- native engine ------------------------------------------------------------------

    def _native_upload(self, job, remote, local: str, run: dict) -> str:
        """Back up a folder with a built-in client: new and changed files go up.

        Only regular files inside the folders ROMarr was given are read: a
        symlink to somewhere else is skipped, so a backup can never carry a
        file from outside that boundary.
        """
        client = self.cloud.native_client(remote)
        roots = self.svc._host_roots()
        listings: dict[str, dict] = {}
        base = job["remote_path"]
        skipped = 0
        for folder, _dirs, files in os.walk(local):
            rel_dir = os.path.relpath(folder, local).replace(os.sep, "/")
            rel_dir = "" if rel_dir == "." else rel_dir
            target_dir = "/".join(p for p in (base, rel_dir) if p)
            for name in files:
                path = Path(folder) / name
                if name.endswith(".partial") or path.is_symlink() or not path.is_file():
                    skipped += path.is_symlink()
                    continue
                try:
                    confined(str(path), roots)
                except ValueError:
                    skipped += 1
                    continue
                if target_dir not in listings:
                    listings[target_dir] = self._sizes(client, target_dir)
                size = path.stat().st_size
                if listings[target_dir].get(name) != size:
                    client.upload(path, f"{target_dir}/{name}".lstrip("/"))
                    run["files"] += 1
                    run["bytes"] += size
        note = f"; skipped {skipped} link(s) outside the allowed folders" if skipped else ""
        return f"{run['files']} file(s) uploaded, {run['bytes']} bytes{note}"

    @staticmethod
    def _sizes(client, folder: str) -> dict:
        try:
            return {e["name"]: e.get("size") for e in client.list(folder) if not e["is_dir"]}
        except native.NativeError:
            return {}

    def _list_files(self, remote: dict, path: str) -> list[dict]:
        """Every file under `path`, with sizes; bounded depth and count."""
        if remote["engine"] == "rclone":   # one recursive call
            return self.cloud.rclone.list(remote["id"], path.strip("/"),
                                          recurse=True)[:MAX_IMPORT_FILES * 4]
        client = self.cloud.native_client(remote)
        out, queue = [], deque([(path.strip("/"), 0)])
        while queue and len(out) < MAX_IMPORT_FILES * 4:
            folder, depth = queue.popleft()
            for row in client.list(folder):
                full = f"{folder}/{row['name']}".strip("/")
                if row["is_dir"]:
                    if depth < MAX_DEPTH:
                        queue.append((full, depth + 1))
                else:
                    out.append({**row, "path": full})
        return out

    # -- imports ------------------------------------------------------------------------

    def _staging(self, platform: str, run_id: str) -> Path:
        target = self.svc.library_for(platform) if platform else self.svc.default_library()
        if target is None:
            raise CloudError("No library is configured to import into")
        root = Path(self.svc.library_root(target[0]))
        staging = root / STAGING / run_id
        staging.mkdir(parents=True, exist_ok=True)
        return staging

    def _fetch(self, remote: dict, path: str, dest: Path, run: dict) -> None:
        if remote["engine"] == "rclone":
            parent, _, name = path.rpartition("/")
            self._wait(self.cloud.rclone.copy_file(fs_string(remote["id"], parent), name,
                                                   str(dest.parent), dest.name), run)
        else:
            run["bytes"] += self.cloud.native_client(remote).download(path, dest)

    def _adopt_one(self, path: Path, platform: str) -> tuple[bool, str]:
        """(in the library now, refusal reason). Already-present counts as in."""
        result = self.svc.adopt(str(path), platform)
        if result.get("ok") or result.get("imported"):
            return True, ""
        first = (result.get("refused") or [{}])[0]
        reason = str(result.get("reason") or first.get("reason") or "refused")
        return "already in the library" in reason, reason

    def _adopt_all(self, staging: Path, platform: str, run: dict) -> tuple[int, list[str]]:
        imported, refused = 0, []
        for path in sorted(p for p in staging.rglob("*") if p.is_file()):
            ok, reason = self._adopt_one(path, platform)
            if ok and not reason:
                imported += 1
            elif reason:
                refused.append(f"{path.name}: {reason}"[:200])
        run["files"] = imported
        return imported, refused

    def _import_folder(self, job: dict, remote: dict, run: dict) -> str:
        seen = set(job.get("seen") or [])
        files = [f for f in self._list_files(remote, job["remote_path"])
                 if f"{f['path']}|{f['size']}" not in seen][:MAX_IMPORT_FILES]
        if not files:
            return "nothing new to import"
        platform = job.get("platform", "")
        staging = self._staging(platform, run["id"])
        imported, refused, failed = 0, [], 0
        try:
            run["total_bytes"] = sum(int(f["size"] or 0) for f in files)
            for f in files:
                rel = f["path"][len(job["remote_path"]):].strip("/") or f["name"]
                dest = _inside(staging, rel)
                if dest is None:
                    refused.append(f"{f['name']}: unsafe name")
                    continue
                dest.parent.mkdir(parents=True, exist_ok=True)
                try:
                    self._fetch(remote, f["path"], dest, run)
                except (CloudError, RcloneError, native.NativeError, OSError) as err:
                    failed += 1
                    refused.append(f"{f['name']}: download failed ({err})"[:200])
                    continue
                ok, reason = self._adopt_one(dest, platform)
                if ok:
                    # Seen only once it is in the library: a failed download or
                    # a refusal is tried again on the next run.
                    seen.add(f"{f['path']}|{f['size']}")
                    imported += 0 if reason else 1
                if reason:
                    refused.append(f"{f['name']}: {reason}"[:200])
                dest.unlink(missing_ok=True)
            run["files"] = imported
        finally:
            _drop(staging)
            self._record(job["id"], seen=sorted(seen)[-MAX_SEEN:])
        return _summary(imported, refused)

    def import_now(self, body: dict) -> dict:
        """One-off import of a cloud path or a Google Drive share link."""
        remote = self.cloud.remote(str(body.get("remote") or ""))
        platform = str(body.get("platform") or "")[:40]
        link = str(body.get("link") or "").strip()
        path = str(body.get("path") or "").strip().strip("/")
        if not link and not path:
            raise CloudError("Give a cloud path or a Google Drive share link")
        if link:
            file_id = drive_id(link)
            is_folder = "/folders/" in link
            if not file_id:
                raise CloudError("That is not a Google Drive share link")
            if remote["type"] not in ("drive", "gdrive"):
                raise CloudError("Share links are copied through a Google Drive connection")
        run = self._new_run(f"Import {link or path}"[:120])
        if link:
            return self._spawn(run, lambda r: self._import_link(remote, file_id, platform, r,
                                                                is_folder=is_folder))

        def work(r):
            parent, _, name = path.rpartition("/")
            entry = next((e for e in self.cloud.list(remote, parent) if e["name"] == name), None)
            if entry is None:
                raise CloudError("That cloud path does not exist")
            if entry["is_dir"]:
                files, parent = self._list_files(remote, path), path
            else:
                files = [{**entry, "path": path}]
            staging = self._staging(platform, r["id"])
            try:
                for f in files[:MAX_IMPORT_FILES]:
                    rel = f["path"][len(parent):].strip("/") or f["name"]
                    dest = _inside(staging, rel)
                    if dest is not None:
                        self._fetch(remote, f["path"], dest, r)
                return _summary(*self._adopt_all(staging, platform, r))
            finally:
                _drop(staging)
        return self._spawn(run, work)

    def _import_link(self, remote: dict, file_id: str, platform: str, run: dict, *,
                     is_folder: bool = False) -> str:
        staging = self._staging(platform, run["id"])
        try:
            if remote["engine"] == "native":
                client = self.cloud.native_client(remote)
                self._native_tree(client, file_id, staging, run, 0)
            elif is_folder:
                # copyid takes files only; a folder is read through a remote rooted at it.
                self._wait(self.cloud.rclone.copy(
                    self.cloud.rclone.folder_fs(remote["id"], file_id), str(staging)), run)
            else:
                temp = f"ROMarr import/{run['id']}"
                self.cloud.rclone.copy_id(remote["id"], file_id, temp)
                try:
                    self._wait(self.cloud.rclone.copy(fs_string(remote["id"], temp), str(staging)), run)
                finally:
                    try:
                        self.cloud.rclone.call("operations/purge", {
                            "fs": fs_string(remote["id"]), "remote": temp})
                    except RcloneError:
                        log.warning("could not remove the temporary Drive copy %s", temp)
            return _summary(*self._adopt_all(staging, platform, run))
        finally:
            _drop(staging)

    def _native_tree(self, client, file_id: str, dest: Path, run: dict, depth: int) -> None:
        info = client.info(file_id)
        name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", info.get("name") or file_id)[:200]
        if name.strip(". ") == "":
            name = "_" + file_id[:12]      # "." and ".." must never become path steps
        if info.get("mimeType") == native.GoogleDrive.FOLDER:
            if depth >= MAX_DEPTH:
                return
            for child in client.children(file_id)[:MAX_IMPORT_FILES]:
                self._native_tree(client, child["id"], dest / name, run, depth + 1)
        elif not str(info.get("mimeType") or "").startswith("application/vnd.google-apps."):
            run["bytes"] += client.download_id(file_id, dest / name)

    # -- settings backup ------------------------------------------------------------------

    def _settings_backup(self, job: dict, remote: dict, run: dict) -> str:
        stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
        name = f"romarr-settings-{stamp}.json"
        from .ops import make_backup
        # The same envelope Settings -> Backup writes and Restore reads, with
        # every credential (API key, password hash, 2FA secret and recovery
        # codes, client passwords) stripped.
        payload = json.dumps(make_backup(dict(self.svc.store.settings)), indent=1, default=str)
        fd, tmp = tempfile.mkstemp(dir=str(self.cloud.data_dir), prefix=".cloud-settings-",
                                   suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as out:
                out.write(payload)
            folder = job["remote_path"]
            if remote["engine"] == "rclone":
                self._wait(self.cloud.rclone.copy_file(os.path.dirname(tmp), os.path.basename(tmp),
                                                       fs_string(remote["id"], folder), name), run)
            else:
                self.cloud.native_client(remote).upload(Path(tmp), f"{folder}/{name}".lstrip("/"))
            run.update(files=1, bytes=len(payload))
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
        pruned = self._prune(remote, job["remote_path"], int(job.get("keep") or 14))
        return f"saved {name}" + (f"; removed {pruned} older" if pruned else "")

    def _prune(self, remote: dict, folder: str, keep: int) -> int:
        client = self.cloud.native_client(remote) if remote["engine"] == "native" else None
        rows = self.cloud.list(remote, folder, client=client)
        old = sorted(r["name"] for r in rows if not r["is_dir"]
                     and re.match(r"^romarr-settings-\d{8}-\d{6}\.json$", r["name"]))[:-keep]
        for name in old:
            self.cloud.delete(remote, f"{folder}/{name}".lstrip("/"), client=client)
        return len(old)

    # -- scheduler ------------------------------------------------------------------------

    def scheduled(self) -> bool:
        return any(float(j.get("interval_hours") or 0) > 0 for j in self.cloud.cfg()["jobs"])

    def tick(self) -> str:
        started = []
        now = time.time()
        for job in self.cloud.cfg()["jobs"]:
            hours = float(job.get("interval_hours") or 0)
            if hours <= 0 or self.running(job["id"]):
                continue
            last = job.get("last_run") or ""
            try:
                last_ts = calendar.timegm(time.strptime(last, "%Y-%m-%dT%H:%M:%SZ"))
            except ValueError:
                last_ts = 0
            if now - last_ts >= hours * 3600:
                try:
                    self.run(job["id"])
                    started.append(job["name"])
                except CloudError as err:
                    log.warning("cloud job %s did not start: %s", job["name"], err)
        return f"started {', '.join(started)}" if started else "nothing due"

    # -- mounts ---------------------------------------------------------------------------

    def _mount_point(self, path: str) -> str:
        real = os.path.realpath(str(path or ""))
        if not os.path.isabs(str(path or "")) or not usable_root(real):
            raise CloudError("Choose an absolute folder that is not a system folder")
        data_dir = os.path.realpath(str(self.cloud.data_dir))
        if real == data_dir or real.startswith(data_dir + os.sep):
            raise CloudError("A mount cannot go inside ROMarr's data folder")
        allowed = (self.svc._host_roots() + ["/mnt", "/media", "/srv"]
                   + env_roots(self.svc._env, "ROMARR_MOUNT_ROOTS"))
        try:
            confined(real, allowed)
        except ValueError:
            raise CloudError("Mount points go under /mnt, /media, /srv, a library or "
                             "download folder, or a folder in ROMARR_MOUNT_ROOTS") from None
        return real

    def save_mount(self, body: dict) -> dict:
        remote = self.cloud.remote(str(body.get("remote") or ""))
        if remote["engine"] != "rclone":
            raise CloudError("Mounting needs an rclone connection")
        cache = str(body.get("cache_mode") or "full")
        if cache not in CACHE_MODES:
            raise CloudError("Cache mode must be off, minimal, writes or full")
        mount = {"id": str(body.get("id") or f"m-{secrets.token_hex(4)}"), "remote": remote["id"],
                 "remote_path": str(body.get("remote_path") or "").strip().strip("/")[:500],
                 "mount_point": self._mount_point(str(body.get("mount_point") or "")),
                 "cache_mode": cache, "auto": bool(body.get("auto", True)),
                 "cache_max_gb": max(0.0, min(float(body.get("cache_max_gb") or 10), 100000)),
                 "allow_other": bool(body.get("allow_other", False))}
        def upsert(cfg):
            clash = [m for m in cfg["mounts"] if m["mount_point"] == mount["mount_point"]
                     and m["id"] != mount["id"]]
            if clash:
                raise CloudError("Another mount already uses that folder")
            cfg["mounts"] = [m for m in cfg["mounts"] if m.get("id") != mount["id"]] + [mount]
        self.cloud.update_cfg(upsert)
        return {"ok": True, "mount": mount}

    def _mount(self, mount_id: str) -> dict:
        found = next((m for m in self.cloud.cfg()["mounts"] if m.get("id") == mount_id), None)
        if found is None:
            raise CloudError("no such mount")
        return found

    def delete_mount(self, mount_id: str) -> dict:
        cfg = self.cloud.cfg()
        found = next((m for m in cfg["mounts"] if m.get("id") == mount_id), None)
        if found and self._is_mounted(found["mount_point"]):
            self.cloud.rclone.unmount(found["mount_point"])
        def drop(cfg):
            cfg["mounts"] = [m for m in cfg["mounts"] if m.get("id") != mount_id]
        self.cloud.update_cfg(drop)
        return {"ok": True}

    def _is_mounted(self, mount_point: str) -> bool:
        return any(m["mount_point"] == mount_point for m in self.cloud.rclone.mounts())

    def mount(self, mount_id: str) -> dict:
        found = self._mount(mount_id)
        ok, reason = fuse_available()
        if not ok:
            raise CloudError(reason)
        point = self._mount_point(found["mount_point"])
        if self._is_mounted(point):
            return {"ok": True, "mounted": True}
        os.makedirs(point, exist_ok=True)
        if os.listdir(point):
            raise CloudError("The mount folder must be empty")
        try:
            self.cloud.rclone.mount(fs_string(found["remote"], found["remote_path"]), point,
                                    cache_mode=found["cache_mode"],
                                    allow_other=found.get("allow_other", False),
                                    cache_max_gb=float(found.get("cache_max_gb") or 10))
        except RcloneError as err:
            raise CloudError(str(err)) from None
        return {"ok": True, "mounted": True}

    def unmount(self, mount_id: str) -> dict:
        found = self._mount(mount_id)
        try:
            self.cloud.rclone.unmount(found["mount_point"])
        except RcloneError as err:
            raise CloudError(str(err)) from None
        return {"ok": True, "mounted": False}

    def mount_states(self) -> dict:
        if not self.cloud.cfg()["mounts"] or not self.cloud.rclone.available:
            return {}
        active = {m["mount_point"] for m in self.cloud.rclone.mounts()}
        return {m["id"]: m["mount_point"] in active for m in self.cloud.cfg()["mounts"]}

    def automount(self) -> None:
        """Mount the auto mounts at startup, quietly, in the background."""
        mounts = [m for m in self.cloud.cfg()["mounts"] if m.get("auto")]
        if not mounts or not fuse_available()[0] or not self.cloud.rclone.available:
            return

        def body():
            for m in mounts:
                try:
                    self.mount(m["id"])
                except (CloudError, RcloneError, OSError) as err:
                    log.warning("cloud mount %s did not start: %s", m["mount_point"], err)
        threading.Thread(target=body, name="romarr-cloud-automount", daemon=True).start()

    def unmount_all(self) -> None:
        for m in self.cloud.rclone.mounts() if self.cloud.rclone.running else []:
            try:
                self.cloud.rclone.unmount(m["mount_point"])
            except RcloneError:
                pass


def _summary(imported: int, refused: list[str]) -> str:
    text = f"imported {imported}"
    if refused:
        text += f"; refused {len(refused)}: " + "; ".join(refused[:3])
    return text


def _drop(staging: Path) -> None:
    """Remove one run's staging folder, and the shared parent once it is empty."""
    shutil.rmtree(staging, ignore_errors=True)
    try:
        staging.parent.rmdir()
    except OSError:
        pass


def _inside(staging: Path, rel: str) -> Path | None:
    """`staging / rel`, or None when `rel` would leave the staging folder."""
    parts = [p for p in str(rel).replace("\\", "/").split("/") if p]
    if not parts or any(p in (".", "..") for p in parts):
        return None
    dest = staging.joinpath(*parts)
    try:
        dest.resolve().relative_to(staging.resolve())
    except ValueError:
        return None
    return dest
