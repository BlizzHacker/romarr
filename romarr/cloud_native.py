"""Google Drive and OneDrive spoken directly, for installs without rclone.

rclone is the engine ROMarr prefers (`cloud_rclone`): it covers every
provider and handles every edge. These two clients exist for the installs
where there is no rclone binary -- the Windows build and the Android app --
and they cover the two services most people already have. They do four
things, which is all the cloud jobs need: list a folder, report the quota,
download a file and upload one (resumable, in chunks).

Sign-in is OAuth 2.0 authorization code with PKCE, against an OAuth app the
operator registers themselves (Google Cloud "Desktop app", Microsoft Entra
"Mobile and desktop applications"). The redirect is either ROMarr's own
callback URL or the paste-back address ``http://127.0.0.1:53682/`` -- the
same one rclone uses -- so a server nobody can reach from outside still
signs in. Borrowing another program's OAuth client is not done here.
"""
from __future__ import annotations

import base64
import hashlib
import logging
import os
import re
import secrets
import time
from pathlib import Path
from urllib.parse import quote, urlencode

import requests

log = logging.getLogger(__name__)

#: 2.5 MiB: a whole number of Google's 256 KiB units *and* OneDrive's 320 KiB
#: units, so every non-final chunk is aligned for both.
CHUNK = 8 * 327680
TIMEOUT = 60

PROVIDERS = {
    "gdrive": {
        "label": "Google Drive",
        "auth_url": "https://accounts.google.com/o/oauth2/v2/auth",
        "token_url": "https://oauth2.googleapis.com/token",
        "scope": "https://www.googleapis.com/auth/drive",
        "extra": {"access_type": "offline", "prompt": "consent"},
    },
    "onedrive": {
        "label": "OneDrive",
        "auth_url": "https://login.microsoftonline.com/common/oauth2/v2.0/authorize",
        "token_url": "https://login.microsoftonline.com/common/oauth2/v2.0/token",
        "scope": "Files.ReadWrite.All offline_access User.Read",
        "extra": {},
    },
}


class NativeError(RuntimeError):
    """A provider refused; the message is safe to show."""


def pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)[:96]
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
    return verifier, challenge.decode().rstrip("=")


def auth_url(provider: str, client_id: str, redirect_uri: str, state: str,
             challenge: str) -> str:
    spec = PROVIDERS[provider]
    query = {"client_id": client_id, "redirect_uri": redirect_uri, "response_type": "code",
             "scope": spec["scope"], "state": state, "code_challenge": challenge,
             "code_challenge_method": "S256", **spec["extra"]}
    return f"{spec['auth_url']}?{urlencode(query)}"


def _token_request(provider: str, data: dict, session: requests.Session) -> dict:
    try:
        response = session.post(PROVIDERS[provider]["token_url"], data=data, timeout=TIMEOUT)
    except requests.RequestException as err:
        raise NativeError(f"could not reach {PROVIDERS[provider]['label']}: "
                          f"{err.__class__.__name__}") from None
    try:
        body = response.json()
    except ValueError:
        body = {}
    if response.status_code >= 400 or "access_token" not in body:
        reason = body.get("error_description") or body.get("error") or f"HTTP {response.status_code}"
        raise NativeError(f"{PROVIDERS[provider]['label']} refused the sign-in: {str(reason)[:200]}")
    token = {k: body[k] for k in ("access_token", "refresh_token", "token_type") if k in body}
    token["expires_at"] = time.time() + float(body.get("expires_in") or 3600) - 60
    return token


def exchange(provider: str, app: dict, code: str, verifier: str, redirect_uri: str, *,
             session: requests.Session | None = None) -> dict:
    data = {"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri,
            "client_id": app["client_id"], "code_verifier": verifier}
    if app.get("client_secret"):
        data["client_secret"] = app["client_secret"]
    return _token_request(provider, data, session or requests.Session())


class _Base:
    """Token handling shared by both providers."""

    label = ""
    provider = ""

    def __init__(self, app: dict, token: dict, *, on_token=None,
                 session: requests.Session | None = None):
        self._app = app
        self._token = dict(token)
        self._on_token = on_token
        self._session = session or requests.Session()

    def _access(self) -> str:
        if time.time() >= float(self._token.get("expires_at") or 0):
            refresh = self._token.get("refresh_token")
            if not refresh:
                raise NativeError(f"{self.label} sign-in expired; sign in again")
            data = {"grant_type": "refresh_token", "refresh_token": refresh,
                    "client_id": self._app["client_id"]}
            if self._app.get("client_secret"):
                data["client_secret"] = self._app["client_secret"]
            fresh = _token_request(self.provider, data, self._session)
            fresh.setdefault("refresh_token", refresh)
            self._token = fresh
            if self._on_token:
                self._on_token(dict(fresh))
        return self._token["access_token"]

    def _req(self, method: str, url: str, *, ok=(200, 201), **kwargs):
        headers = dict(kwargs.pop("headers", {}) or {})
        headers["Authorization"] = f"Bearer {self._access()}"
        try:
            response = self._session.request(method, url, headers=headers,
                                             timeout=kwargs.pop("timeout", TIMEOUT), **kwargs)
        except requests.RequestException as err:
            raise NativeError(f"{self.label}: {err.__class__.__name__}") from None
        if response.status_code not in ok:
            try:
                detail = response.json().get("error")
                detail = detail.get("message") if isinstance(detail, dict) else detail
            except ValueError:
                detail = ""
            raise NativeError(f"{self.label} answered {response.status_code}: {str(detail or '')[:200]}")
        return response


def _parts(path: str) -> list[str]:
    parts = [p for p in str(path or "").replace("\\", "/").split("/") if p]
    if any(p in (".", "..") or any(ord(c) < 32 for c in p) for p in parts):
        raise NativeError("invalid path")
    return parts


class GoogleDrive(_Base):
    label = "Google Drive"
    provider = "gdrive"
    API = "https://www.googleapis.com/drive/v3"
    UPLOAD = "https://www.googleapis.com/upload/drive/v3/files"
    FOLDER = "application/vnd.google-apps.folder"

    def _child(self, parent: str, name: str) -> dict | None:
        safe = name.replace("\\", "\\\\").replace("'", "\\'")
        rows = self._req("GET", f"{self.API}/files", params={
            "q": f"'{parent}' in parents and name = '{safe}' and trashed = false",
            "fields": "files(id,name,mimeType,size)", "pageSize": 2,
            "supportsAllDrives": "true", "includeItemsFromAllDrives": "true"}).json().get("files") or []
        if len(rows) > 1:
            # Drive allows two items with one name in a folder; a path cannot
            # say which, and acting on the wrong one is worse than refusing.
            raise NativeError(f"two items are named {name!r} in that Google Drive folder; "
                              "rename one of them")
        return rows[0] if rows else None

    def _resolve(self, path: str, *, create: bool = False) -> dict | None:
        node = {"id": "root", "mimeType": self.FOLDER, "name": ""}
        for part in _parts(path):
            child = self._child(node["id"], part)
            if child is None:
                if not create:
                    return None
                child = self._req("POST", f"{self.API}/files", json={
                    "name": part, "mimeType": self.FOLDER, "parents": [node["id"]]},
                    params={"fields": "id,name,mimeType", "supportsAllDrives": "true"}).json()
            node = child
        return node

    def list(self, path: str = "") -> list[dict]:
        node = self._resolve(path)
        if node is None:
            raise NativeError("no such folder")
        out, page = [], None
        base = "/".join(_parts(path))
        while True:
            params = {"q": f"'{node['id']}' in parents and trashed = false", "pageSize": 1000,
                      "fields": "nextPageToken,files(id,name,mimeType,size,modifiedTime)",
                      "supportsAllDrives": "true", "includeItemsFromAllDrives": "true"}
            if page:
                params["pageToken"] = page
            body = self._req("GET", f"{self.API}/files", params=params).json()
            for f in body.get("files") or []:
                is_dir = f.get("mimeType") == self.FOLDER
                out.append({"name": f["name"], "path": f"{base}/{f['name']}".lstrip("/"),
                            "size": None if is_dir else int(f.get("size") or 0),
                            "is_dir": is_dir, "modified": f.get("modifiedTime", "")})
            page = body.get("nextPageToken")
            if not page:
                return out

    def about(self) -> dict:
        quota = self._req("GET", f"{self.API}/about", params={"fields": "storageQuota"}).json()
        quota = quota.get("storageQuota") or {}
        total = int(quota["limit"]) if quota.get("limit") else None
        used = int(quota.get("usage") or 0)
        return {"total": total, "used": used, "free": (total - used) if total else None}

    def download(self, path: str, dest: Path) -> int:
        node = self._resolve(path)
        if node is None or node.get("mimeType") == self.FOLDER:
            raise NativeError("no such file")
        return self.download_id(node["id"], dest)

    def download_id(self, file_id: str, dest: Path) -> int:
        response = self._req("GET", f"{self.API}/files/{quote(file_id)}",
                             params={"alt": "media", "supportsAllDrives": "true",
                                     "acknowledgeAbuse": "true"},
                             stream=True, timeout=300)
        return _stream(response, dest)

    def info(self, file_id: str) -> dict:
        return self._req("GET", f"{self.API}/files/{quote(file_id)}", params={
            "fields": "id,name,mimeType,size", "supportsAllDrives": "true"}).json()

    def children(self, folder_id: str) -> list[dict]:
        out, page = [], None
        while True:
            params = {"q": f"'{folder_id}' in parents and trashed = false", "pageSize": 1000,
                      "fields": "nextPageToken,files(id,name,mimeType,size)",
                      "supportsAllDrives": "true", "includeItemsFromAllDrives": "true"}
            if page:
                params["pageToken"] = page
            body = self._req("GET", f"{self.API}/files", params=params).json()
            out.extend(body.get("files") or [])
            page = body.get("nextPageToken")
            if not page:
                return out

    def upload(self, local: Path, path: str) -> None:
        parts = _parts(path)
        if not parts:
            raise NativeError("give the file a name")
        parent = self._resolve("/".join(parts[:-1]), create=True)
        existing = self._child(parent["id"], parts[-1])
        size = Path(local).stat().st_size
        if existing:
            start = self._req("PATCH", f"{self.UPLOAD}/{existing['id']}",
                              params={"uploadType": "resumable", "supportsAllDrives": "true"},
                              json={}, headers={"X-Upload-Content-Length": str(size)})
        else:
            start = self._req("POST", self.UPLOAD,
                              params={"uploadType": "resumable", "supportsAllDrives": "true"},
                              json={"name": parts[-1], "parents": [parent["id"]]},
                              headers={"X-Upload-Content-Length": str(size)})
        _chunked_put(self._session, start.headers["Location"], Path(local), size,
                     auth=lambda: {"Authorization": f"Bearer {self._access()}"},
                     provider="gdrive")

    def delete(self, path: str) -> None:
        node = self._resolve(path)
        if node is not None:
            self._req("DELETE", f"{self.API}/files/{node['id']}", ok=(200, 204),
                      params={"supportsAllDrives": "true"})


class OneDrive(_Base):
    label = "OneDrive"
    provider = "onedrive"
    API = "https://graph.microsoft.com/v1.0/me/drive"

    def _item(self, path: str) -> str:
        parts = _parts(path)
        if not parts:
            return f"{self.API}/root"
        return f"{self.API}/root:/{quote('/'.join(parts))}:"

    def list(self, path: str = "") -> list[dict]:
        base = "/".join(_parts(path))
        url = f"{self._item(path)}/children" if base else f"{self.API}/root/children"
        out = []
        params = {"$top": 1000, "$select": "name,size,folder,file,lastModifiedDateTime"}
        while url:
            body = self._req("GET", url, params=params).json()
            params = None
            for f in body.get("value") or []:
                is_dir = "folder" in f
                out.append({"name": f["name"], "path": f"{base}/{f['name']}".lstrip("/"),
                            "size": None if is_dir else int(f.get("size") or 0),
                            "is_dir": is_dir, "modified": f.get("lastModifiedDateTime", "")})
            url = body.get("@odata.nextLink")
        return out

    def about(self) -> dict:
        quota = self._req("GET", self.API, params={"$select": "quota"}).json().get("quota") or {}
        return {"total": quota.get("total"), "used": quota.get("used"),
                "free": quota.get("remaining")}

    def download(self, path: str, dest: Path) -> int:
        if not _parts(path):
            raise NativeError("no such file")
        response = self._req("GET", f"{self._item(path)}/content", stream=True, timeout=300)
        return _stream(response, dest)

    def upload(self, local: Path, path: str) -> None:
        if not _parts(path):
            raise NativeError("give the file a name")
        size = Path(local).stat().st_size
        self._ensure_folders(_parts(path)[:-1])
        session = self._req("POST", f"{self._item(path)}/createUploadSession", json={
            "item": {"@microsoft.graph.conflictBehavior": "replace"}}).json()
        # The upload URL is pre-authorised; sending a bearer token to it is refused.
        _chunked_put(self._session, session["uploadUrl"], Path(local), size, auth=dict,
                     provider="onedrive")

    def _ensure_folders(self, parts: list[str]) -> None:
        """Create the missing folders of `parts`, reusing any that exist.

        Graph's upload session needs an existing parent; a backup's first file
        in a new subfolder otherwise fails.
        """
        for i in range(len(parts)):
            here = "/".join(parts[:i + 1])
            if self._req("GET", self._item(here), ok=(200, 404)).status_code == 200:
                continue
            parent = "/".join(parts[:i])
            url = f"{self._item(parent)}/children" if parent else f"{self.API}/root/children"
            self._req("POST", url, ok=(200, 201, 409), json={
                "name": parts[i], "folder": {}, "@microsoft.graph.conflictBehavior": "fail"})

    def delete(self, path: str) -> None:
        if _parts(path):
            self._req("DELETE", self._item(path), ok=(204, 404))


def _stream(response, dest: Path) -> int:
    dest = Path(dest)
    partial = dest.with_name(dest.name + ".partial")
    written = 0
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        with open(partial, "wb") as out:
            for block in response.iter_content(1024 * 1024):
                if block:
                    out.write(block)
                    written += len(block)
        os.replace(partial, dest)
    finally:
        response.close()
        if partial.exists():
            partial.unlink()
    return written


def _ranges_offset(body) -> int | None:
    """The first missing byte from Graph's ``nextExpectedRanges``, or None."""
    ranges = (body or {}).get("nextExpectedRanges") if isinstance(body, dict) else None
    if not ranges:
        return None
    try:
        return int(str(ranges[0]).split("-")[0])
    except ValueError:
        return None


def _acknowledged(response, provider: str) -> int | None:
    """The next byte a non-final chunk response asks for; None when it does not say."""
    if provider == "gdrive" and response.status_code == 308:
        match = re.match(r"^bytes=0-(\d+)$", response.headers.get("Range", "") or "")
        return int(match.group(1)) + 1 if match else 0
    if provider == "onedrive" and response.status_code == 202:
        try:
            return _ranges_offset(response.json())
        except ValueError:
            return None
    return None


def _ask_offset(session, url: str, size: int, provider: str, auth) -> int | None:
    """After a lost or refused response, ask the upload session where it is.

    Returns `size` when the provider says the upload is already complete.
    """
    try:
        if provider == "gdrive":
            r = session.put(url, data=b"", timeout=TIMEOUT,
                            headers={**auth(), "Content-Length": "0",
                                     "Content-Range": f"bytes */{size}"})
            if r.status_code in (200, 201):
                return size
            return _acknowledged(r, provider) if r.status_code == 308 else None
        r = session.get(url, timeout=TIMEOUT)
        return _ranges_offset(r.json()) if r.status_code == 200 else None
    except (requests.RequestException, ValueError):
        return None


def _chunked_put(session: requests.Session, url: str, local: Path, size: int, *, auth,
                 provider: str = "gdrive") -> None:
    """Upload `local` through a resumable session; return only once it is committed.

    The next chunk always starts where the provider says it got to (Drive's
    ``Range``, Graph's ``nextExpectedRanges``), not where this side assumed.
    After a lost response or a 416, the session is asked for its offset and
    the upload resumes from there. Success is only the provider's final
    200/201; reaching the end of the file without one is an error.
    """
    if size == 0:
        response = session.put(url, data=b"", headers={**auth(), "Content-Length": "0"},
                               timeout=TIMEOUT)
        if response.status_code not in (200, 201):
            raise NativeError(f"upload refused: HTTP {response.status_code}")
        return
    with open(local, "rb") as src:
        offset = 0
        while True:
            src.seek(offset)
            block = src.read(CHUNK)
            if not block:
                raise NativeError("the provider did not confirm the upload; the file may have "
                                  "changed while it was being sent")
            end = offset + len(block) - 1
            headers = {**auth(), "Content-Length": str(len(block)),
                       "Content-Range": f"bytes {offset}-{end}/{size}"}
            moved = False
            for attempt in range(4):
                try:
                    response = session.put(url, data=block, headers=headers, timeout=300)
                except requests.RequestException:
                    response = None
                if response is not None and response.status_code in (200, 201):
                    return
                if response is not None and response.status_code in (202, 308):
                    told = _acknowledged(response, provider)
                    offset = end + 1 if told is None else told
                    moved = True
                    break
                told = _ask_offset(session, url, size, provider, auth)
                if told is not None and told >= size:
                    return
                if told is not None and told != offset:
                    offset, moved = told, True
                    break
                if attempt == 3:
                    code = getattr(response, "status_code", "no answer")
                    raise NativeError(f"upload failed at byte {offset}: {code}")
                time.sleep(2 ** attempt)
            if not moved:
                raise NativeError(f"upload stalled at byte {offset}")


def client(provider: str, app: dict, token: dict, *, on_token=None,
           session: requests.Session | None = None):
    cls = {"gdrive": GoogleDrive, "onedrive": OneDrive}.get(provider)
    if cls is None:
        raise NativeError("unknown provider")
    return cls(app, token, on_token=on_token, session=session)
