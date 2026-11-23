"""Cloud storage for ROMarr: connections, sign-in and file-host accounts.

A *connection* is one cloud account ROMarr may use. It is backed by either
engine:

* ``rclone`` (`cloud_rclone`): every provider rclone knows -- Google Drive,
  OneDrive, Dropbox, MEGA, Box, pCloud, S3/B2/Wasabi/R2, WebDAV, SFTP and the
  rest -- configured from rclone's own option list, so a provider rclone
  adds tomorrow is offered here without a ROMarr release.
* ``native`` (`cloud_native`): Google Drive and OneDrive spoken directly, for
  installs with no rclone binary.

What a connection is *for* lives in `cloud_jobs`: backup and offload of
library folders, importing from a cloud folder or a Google Drive share link,
a settings backup, and mounting a cloud folder as a library root.

Secrets -- OAuth tokens, OAuth app secrets, file-host passwords and keys --
are kept out of ``romarr.json``: rclone's in its own ``rclone.conf``, the
rest in ``cloud-secrets.json``, both mode 0600 beside the settings. So the
settings API, settings backups and support bundles never carry them, and no
route ever returns one.
"""
from __future__ import annotations

import json
import logging
import os
import re
import secrets
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

import requests

from . import cloud_native as native
from .cloud_rclone import AUTH_PORT, REMOTE_NAME, Authorizer, Rclone, RcloneError, fuse_available
from .downloaders import SECRET_PLACEHOLDER
from .premium_hosts import HostAccountError, OneFichier, Rapidgator

log = logging.getLogger(__name__)

SETTINGS_KEY = "cloud_storage"
SECRETS_FILE = "cloud-secrets.json"
PASTE_REDIRECT = f"http://127.0.0.1:{AUTH_PORT}/"
#: Microsoft only accepts "localhost" for desktop redirects; rclone uses the same.
PASTE_REDIRECTS = {"gdrive": PASTE_REDIRECT, "onedrive": f"http://localhost:{AUTH_PORT}/"}
PENDING_SECONDS = 900
NATIVE_TYPES = {"gdrive": "Google Drive (built in)", "onedrive": "OneDrive (built in)"}
HOST_KINDS = {"rapidgator": "Rapidgator", "1fichier": "1fichier"}
_LABEL = re.compile(r"^[^\x00-\x1f]{1,80}$")
DEFAULTS = {"remotes": [], "jobs": [], "mounts": [], "rclone_path": ""}
NO_BROWSER_KEYS = ("service_account_file", "service_account_credentials", "box_config_file")


class CloudError(ValueError):
    """A request ROMarr refuses; the message is safe to show."""


class SecretStore:
    """A small JSON file, mode 0600, that settings never see."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.RLock()

    def load(self) -> dict:
        with self._lock:
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return {}
            return data if isinstance(data, dict) else {}

    def update(self, section: str, key: str, value) -> None:
        with self._lock:
            data = self.load()
            bucket = dict(data.get(section) or {})
            if value is None:
                bucket.pop(key, None)
            else:
                bucket[key] = value
            data[section] = bucket
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), prefix=".cloud-secrets-")
            try:
                os.chmod(tmp, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as out:
                    json.dump(data, out)
                os.replace(tmp, self.path)
            finally:
                if os.path.exists(tmp):
                    os.unlink(tmp)

    def get(self, section: str, key: str, default=None):
        return (self.load().get(section) or {}).get(key, default)


def _clean_label(value, fallback: str) -> str:
    text = str(value or "").strip() or fallback
    if not _LABEL.match(text):
        raise CloudError("Labels are 1-80 printable characters")
    return text


class CloudStorage:
    """Connections, sign-in sessions and host accounts for one ROMarr."""

    def __init__(self, service, *, rclone: Rclone | None = None, session=None):
        self.svc = service
        data_dir = Path(service.store.path).parent
        self.data_dir = data_dir
        self.secrets = SecretStore(data_dir / SECRETS_FILE)
        # Mount caches can be large; ROMARR_RCLONE_CACHE puts them on a big disk
        # rather than beside romarr.json, which is often a small root filesystem.
        cache_dir = (getattr(service, "_env", {}) or {}).get("ROMARR_RCLONE_CACHE") or data_dir / "rclone-cache"
        self.rclone = rclone or Rclone(data_dir / "rclone.conf",
                                       binary=str(self.cfg().get("rclone_path") or ""),
                                       cache_dir=cache_dir)
        self.authorizer = Authorizer(self.rclone)
        # One session for every provider and host call: connection reuse, and
        # host accounts are built once rather than on every grab.
        self._session = session or requests.Session()
        self._lock = threading.RLock()
        self._pending: dict[str, dict] = {}
        self._accounts: list | None = None
        #: The outcome of the latest rclone sign-in, for the page to notice a
        #: sign-in that finished without a paste. Never holds a token.
        self.last_signin: dict = {}
        from .hosters import register_accounts
        register_accounts(self.host_accounts)

    # -- settings ----------------------------------------------------------------

    def cfg(self) -> dict:
        raw = self.svc.store.settings.get(SETTINGS_KEY) or {}
        out = {**DEFAULTS, **(raw if isinstance(raw, dict) else {})}
        for key in ("remotes", "jobs", "mounts"):
            out[key] = [dict(x) for x in out.get(key) or [] if isinstance(x, dict)]
        return out

    def save_cfg(self, cfg: dict) -> None:
        with self._lock:
            self.svc.store.settings[SETTINGS_KEY] = {k: cfg.get(k, DEFAULTS[k]) for k in DEFAULTS}
            self.svc.store.save()

    def remote(self, remote_id: str) -> dict:
        found = next((r for r in self.cfg()["remotes"] if r.get("id") == remote_id), None)
        if found is None:
            raise CloudError("no such connection")
        return found

    # -- what the page shows -----------------------------------------------------

    def status(self) -> dict:
        cfg = self.cfg()
        fuse_ok, fuse_reason = fuse_available()
        stored = self.secrets.load()
        apps = stored.get("apps") or {}
        hosts = stored.get("hosts") or {}
        return {
            "rclone": {"available": self.rclone.available, "path": self.rclone.binary(),
                       "version": self.rclone.version() if self.rclone.available else "",
                       "configured_path": cfg.get("rclone_path", "")},
            "fuse": {"available": fuse_ok, "reason": fuse_reason},
            "remotes": cfg["remotes"], "jobs": cfg["jobs"], "mounts": cfg["mounts"],
            "native_types": NATIVE_TYPES,
            "apps": {p: {"client_id": (apps.get(p) or {}).get("client_id", ""),
                         "secret_set": bool((apps.get(p) or {}).get("client_secret"))}
                     for p in NATIVE_TYPES},
            "hosts": {k: {"label": v, "set": bool(hosts.get(k)),
                          "login": (hosts.get(k) or {}).get("login", "") if k == "rapidgator" else ""}
                      for k, v in HOST_KINDS.items()},
            "paste_redirect": PASTE_REDIRECT,
            "paste_redirects": PASTE_REDIRECTS,
            "signin_active": self.authorizer.active,
            "last_signin": self.last_signin,
        }

    def providers(self) -> dict:
        try:
            listed = self.rclone.providers() if self.rclone.available else []
        except RcloneError as err:
            return {"rclone": [], "native": NATIVE_TYPES, "error": str(err)}
        return {"rclone": listed, "native": NATIVE_TYPES}

    def set_rclone_path(self, path: str) -> dict:
        path = str(path or "").strip()
        if path and not (os.path.isfile(path) and os.access(path, os.X_OK)
                         and os.path.basename(path).lower().startswith("rclone")):
            raise CloudError("That is not an rclone executable")
        cfg = self.cfg()
        cfg["rclone_path"] = path
        self.save_cfg(cfg)
        self.rclone.set_binary(path)
        return {"ok": True, "available": self.rclone.available}

    # -- OAuth apps (own client ids) ------------------------------------------------

    def set_app(self, provider: str, client_id: str, client_secret: str) -> dict:
        if provider not in NATIVE_TYPES:
            raise CloudError("Apps are for gdrive or onedrive")
        client_id = str(client_id or "").strip()
        if client_id and not re.match(r"^[A-Za-z0-9._-]{8,200}$", client_id):
            raise CloudError("That client id does not look right")
        old = self.secrets.get("apps", provider) or {}
        secret = str(client_secret or "").strip()
        if secret in ("", SECRET_PLACEHOLDER):
            secret = old.get("client_secret", "") if client_id == old.get("client_id") else ""
        self.secrets.update("apps", provider, {"client_id": client_id, "client_secret": secret}
                            if client_id else None)
        return {"ok": True}

    # -- sign-in ---------------------------------------------------------------------

    def signin_start(self, body: dict) -> dict:
        engine = body.get("engine") or "rclone"
        name = str(body.get("name") or "").strip()
        label = _clean_label(body.get("label"), name or "Cloud")
        self._prune_pending()
        if engine == "rclone":
            rtype = str(body.get("type") or "")
            provider = self.rclone.provider(rtype)
            if provider is None or not provider["oauth"]:
                raise CloudError("That provider does not sign in through a browser")
            self._check_new_name(name)
            client_id = str(body.get("client_id") or "").strip()
            client_secret = str(body.get("client_secret") or "").strip()
            if body.get("use_own_app") and rtype in ("drive", "onedrive"):
                app = self.secrets.get("apps", "gdrive" if rtype == "drive" else "onedrive") or {}
                client_id, client_secret = app.get("client_id", ""), app.get("client_secret", "")
                if not client_id:
                    raise CloudError("Save your own app's client id first")
            params = _clean_params(body.get("params"))
            if client_id:
                params.update({"client_id": client_id, "client_secret": client_secret})
            self._pending["rclone"] = {"engine": "rclone", "type": rtype, "name": name,
                                       "label": label, "params": params,
                                       "shared": not client_id, "at": time.monotonic()}
            self.last_signin = {}
            try:
                url = self.authorizer.start(rtype, client_id=client_id, client_secret=client_secret,
                                            on_token=self._complete_rclone)
            except RcloneError as err:
                self._pending.pop("rclone", None)
                raise CloudError(str(err)) from None
            return {"url": url, "mode": "paste", "redirect": PASTE_REDIRECT}
        if engine == "native":
            rtype = str(body.get("type") or "")
            if rtype not in NATIVE_TYPES:
                raise CloudError("Built-in sign-in is for Google Drive and OneDrive")
            app = self.secrets.get("apps", rtype) or {}
            if not app.get("client_id"):
                raise CloudError("Save your own app's client id first (see the guide)")
            redirect = PASTE_REDIRECTS[rtype]
            if body.get("redirect") == "callback":
                base = str(body.get("base_url") or "").rstrip("/")
                if not re.match(r"^https?://[^\s/]+$", base):
                    raise CloudError("base_url must be this ROMarr's address")
                redirect = f"{base}/api/v1/cloud/oauth/callback"
            state = secrets.token_urlsafe(24)
            verifier, challenge = native.pkce_pair()
            self._pending[state] = {"engine": "native", "type": rtype, "label": label,
                                    "verifier": verifier, "redirect": redirect,
                                    "at": time.monotonic()}
            url = native.auth_url(rtype, app["client_id"], redirect, state, challenge)
            return {"url": url, "mode": "paste" if redirect == PASTE_REDIRECTS[rtype] else "callback",
                    "redirect": redirect}
        raise CloudError("engine must be rclone or native")

    def signin_finish(self, address: str) -> dict:
        """The pasted ``http://127.0.0.1:53682/?…`` address completes a sign-in."""
        query = dict(parse_qsl(urlsplit(str(address or "").strip()).query))
        state = query.get("state", "")
        if state and state in self._pending:
            return self.native_callback(query)
        if self._pending.get("rclone") is None:
            if self.last_signin.get("remote"):
                return {"ok": True, "remote": self.last_signin["remote"]}
            raise CloudError("No sign-in is waiting; start again")
        try:
            self.authorizer.finish(address)   # the connection is made by _complete_rclone
        except RcloneError as err:
            raise CloudError(str(err)) from None
        if self.last_signin.get("error"):
            raise CloudError(self.last_signin["error"])
        return {"ok": True, "remote": self.last_signin.get("remote")}

    def _complete_rclone(self, token: str) -> None:
        """rclone has a token, by paste or directly from the browser: save the connection."""
        pending = self._pending.pop("rclone", None)
        if pending is None:
            return
        try:
            self.rclone.create_remote(pending["name"], pending["type"], pending["params"],
                                      token=token, shared_client=pending["shared"])
        except RcloneError as err:
            self.last_signin = {"error": f"Signed in, but rclone could not save it: {err}"}
            raise
        record = self._add_record({"id": pending["name"], "label": pending["label"],
                                   "engine": "rclone", "type": pending["type"]})["remote"]
        self.last_signin = {"remote": record}

    def native_callback(self, query: dict) -> dict:
        state = str(query.get("state") or "")
        with self._lock:
            pending = self._pending.pop(state, None)
        if pending is None or pending.get("engine") != "native":
            raise CloudError("That sign-in is unknown or expired; start again")
        if query.get("error"):
            raise CloudError(f"The provider refused: {str(query['error'])[:100]}")
        if not query.get("code"):
            raise CloudError("The address has no sign-in code in it")
        app = self.secrets.get("apps", pending["type"]) or {}
        try:
            token = native.exchange(pending["type"], app, query["code"], pending["verifier"],
                                    pending["redirect"], session=self._session)
        except native.NativeError as err:
            raise CloudError(str(err)) from None
        remote_id = f"n-{secrets.token_hex(4)}"
        self.secrets.update("native", remote_id, {"token": token})
        return self._add_record({"id": remote_id, "label": pending["label"],
                                 "engine": "native", "type": pending["type"]})

    def signin_cancel(self) -> dict:
        self.authorizer.cancel()
        self._pending.clear()
        return {"ok": True}

    def _prune_pending(self) -> None:
        now = time.monotonic()
        for key in [k for k, v in self._pending.items() if now - v["at"] > PENDING_SECONDS]:
            self._pending.pop(key, None)

    # -- connections ---------------------------------------------------------------------

    def _check_new_name(self, name: str) -> None:
        if not REMOTE_NAME.match(name or ""):
            raise CloudError("Name: letters, digits, - and _ (40 at most)")
        if name.startswith("n-"):
            raise CloudError("Names starting n- are reserved")
        if any(r.get("id") == name for r in self.cfg()["remotes"]):
            raise CloudError("A connection with that name already exists")

    def _add_record(self, record: dict) -> dict:
        cfg = self.cfg()
        cfg["remotes"] = [r for r in cfg["remotes"] if r.get("id") != record["id"]]
        record["created"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        cfg["remotes"].append(record)
        self.save_cfg(cfg)
        return {"ok": True, "remote": record}

    def add_remote(self, body: dict) -> dict:
        """A connection that needs no browser: keys, passwords, an address."""
        name = str(body.get("name") or "").strip()
        rtype = str(body.get("type") or "")
        self._check_new_name(name)
        provider = self.rclone.provider(rtype)
        if provider is None:
            raise CloudError("rclone has no such provider")
        params = _clean_params(body.get("params"))
        # A service account (Drive) or a JWT config (Box) is sign-in without a browser.
        if provider["oauth"] and not any(k in params for k in NO_BROWSER_KEYS):
            raise CloudError("This provider signs in through a browser: use Sign in")
        try:
            self.rclone.create_remote(name, rtype, params)
        except RcloneError as err:
            raise CloudError(str(err)) from None
        record = self._add_record({"id": name, "engine": "rclone", "type": rtype,
                                   "label": _clean_label(body.get("label"), name)})
        try:
            self.rclone.list(name, "")
        except RcloneError as err:
            record["warning"] = f"Saved, but listing it failed: {err}"
        return record

    def delete_remote(self, remote_id: str) -> dict:
        cfg = self.cfg()
        self.remote(remote_id)
        used = [j["name"] for j in cfg["jobs"] if j.get("remote") == remote_id]
        used += [m["mount_point"] for m in cfg["mounts"] if m.get("remote") == remote_id]
        if used:
            raise CloudError(f"Still used by: {', '.join(used)[:200]}. Remove those first.")
        record = self.remote(remote_id)
        if record["engine"] == "rclone":
            try:
                self.rclone.delete_remote(remote_id)
            except RcloneError as err:
                log.warning("rclone could not forget %s: %s", remote_id, err)
        else:
            self.secrets.update("native", remote_id, None)
        cfg["remotes"] = [r for r in cfg["remotes"] if r.get("id") != remote_id]
        self.save_cfg(cfg)
        return {"ok": True}

    def native_client(self, record: dict):
        stored = self.secrets.get("native", record["id"]) or {}
        app = self.secrets.get("apps", record["type"]) or {}
        if not stored.get("token") or not app.get("client_id"):
            raise CloudError("This connection needs signing in again")

        def keep(token, rid=record["id"]):
            self.secrets.update("native", rid, {"token": token})
        return native.client(record["type"], app, stored["token"], on_token=keep,
                             session=self._session)

    def list(self, record: dict, path: str = "", *, client=None) -> list[dict]:
        """One folder of a connection, whichever engine backs it."""
        if record["engine"] == "rclone":
            return self.rclone.list(record["id"], path)
        return (client or self.native_client(record)).list(path)

    def delete(self, record: dict, path: str, *, client=None) -> None:
        if record["engine"] == "rclone":
            self.rclone.delete_file(record["id"], path)
        else:
            (client or self.native_client(record)).delete(path)

    def browse(self, remote_id: str, path: str = "") -> dict:
        record = self.remote(remote_id)
        try:
            rows = self.list(record, path)
        except (RcloneError, native.NativeError) as err:
            raise CloudError(str(err)) from None
        rows.sort(key=lambda r: (not r["is_dir"], r["name"].lower()))
        return {"path": str(path or "").strip("/"), "entries": rows[:5000],
                "truncated": len(rows) > 5000}

    def about(self, remote_id: str) -> dict:
        record = self.remote(remote_id)
        try:
            if record["engine"] == "rclone":
                return self.rclone.about(remote_id)
            return self.native_client(record).about()
        except (RcloneError, native.NativeError) as err:
            raise CloudError(str(err)) from None

    def share_link(self, remote_id: str, path: str) -> dict:
        record = self.remote(remote_id)
        if record["engine"] != "rclone":
            raise CloudError("Share links need an rclone connection")
        try:
            return {"url": self.rclone.public_link(remote_id, path)}
        except RcloneError as err:
            raise CloudError(str(err)) from None

    # -- file-host accounts --------------------------------------------------------------

    def host_accounts(self) -> list:
        """The premium host accounts, built once and rebuilt when one changes."""
        if self._accounts is None:
            self._accounts = self._build_accounts()
        return self._accounts

    def _build_accounts(self) -> list:
        hosts = self.secrets.load().get("hosts") or {}
        out = []
        rg = hosts.get("rapidgator") or {}
        if rg.get("login"):
            out.append(Rapidgator(rg["login"], rg.get("password", ""), token=rg.get("token", ""),
                                  on_token=self._keep_rg_token, session=self._session))
        fichier = hosts.get("1fichier") or {}
        if fichier.get("api_key"):
            out.append(OneFichier(fichier["api_key"], session=self._session))
        return out

    def _keep_rg_token(self, token: str) -> None:
        current = self.secrets.get("hosts", "rapidgator") or {}
        if current:
            self.secrets.update("hosts", "rapidgator", {**current, "token": token})

    def set_host_account(self, kind: str, body: dict) -> dict:
        if kind not in HOST_KINDS:
            raise CloudError("Unknown file host")
        old = self.secrets.get("hosts", kind) or {}
        if kind == "rapidgator":
            login = str(body.get("login") or "").strip()
            password = str(body.get("password") or "")
            if password in ("", SECRET_PLACEHOLDER) and login == old.get("login"):
                password = old.get("password", "")
            if not login or not password:
                raise CloudError("Rapidgator needs the account e-mail and password")
            account = Rapidgator(login, password, session=self._session)
            try:
                summary = account.sign_in(str(body.get("code") or "").strip())
            except HostAccountError as err:
                raise CloudError(str(err)) from None
            self.secrets.update("hosts", kind, {"login": login, "password": password,
                                                "token": account._token})
            self._accounts = None
            return {"ok": True, **summary}
        key = str(body.get("api_key") or "").strip()
        if key in ("", SECRET_PLACEHOLDER):
            key = old.get("api_key", "")
        if not key:
            raise CloudError("1fichier needs the account's API key")
        try:
            summary = OneFichier(key, session=self._session).sign_in()
        except HostAccountError as err:
            raise CloudError(str(err)) from None
        self.secrets.update("hosts", kind, {"api_key": key})
        self._accounts = None
        return {"ok": True, **summary}

    def delete_host_account(self, kind: str) -> dict:
        if kind not in HOST_KINDS:
            raise CloudError("Unknown file host")
        self.secrets.update("hosts", kind, None)
        self._accounts = None
        return {"ok": True}

    def close(self) -> None:
        self.authorizer.cancel()
        self.rclone.stop()


def _clean_params(params) -> dict:
    """rclone option values from a form: known-shaped keys, string values."""
    out = {}
    if not isinstance(params, dict):
        return out
    for key, value in params.items():
        key = str(key)
        if not re.match(r"^[a-z0-9_]{1,64}$", key) or key in ("token", "config_token"):
            continue
        if isinstance(value, bool):
            value = "true" if value else "false"
        value = str(value if value is not None else "")
        if value and len(value) <= 4096 and not any(ord(c) < 32 for c in value):
            out[key] = value
    return out
