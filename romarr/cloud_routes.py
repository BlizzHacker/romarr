"""HTTP routes for cloud storage, kept out of app.py's 12,000 lines.

Every route sits behind app.py's authentication gate, like every other
`/api/v1` route. Bodies are capped at 64 KiB by the caller. Each handler
returns ``(status, json)``; a `CloudError` is the operator's mistake (400),
anything rclone or a provider says is passed on as a readable sentence, and
no response ever contains a token, password, API key or client secret.
"""
from __future__ import annotations

import logging

from .cloud_jobs import KINDS
from .cloud_rclone import RcloneError
from .cloud_storage import CloudError

log = logging.getLogger(__name__)

def _str(body: dict, key: str) -> str:
    value = body.get(key, "")
    return value if isinstance(value, str) else ""


def _status(service, query):
    out = service.cloud.status()
    out["runs"] = service.cloud_jobs.runs()
    out["mount_states"] = service.cloud_jobs.mount_states() if out["rclone"]["available"] else {}
    out["kinds"] = KINDS
    return out


#: path -> handler(service, query). The OAuth callback is served by app.py
#: itself (it answers with a redirect, not JSON) and is listed for OpenAPI.
GET_ROUTES = {
    "/api/v1/cloud": _status,
    "/api/v1/cloud/providers": lambda s, q: s.cloud.providers(),
    "/api/v1/cloud/browse": lambda s, q: s.cloud.browse(q.get("remote", ""), q.get("path", "")),
    "/api/v1/cloud/about": lambda s, q: s.cloud.about(q.get("remote", "")),
    "/api/v1/cloud/runs": lambda s, q: {"runs": s.cloud_jobs.runs()},
}

#: path -> handler(service, body)
POST_ROUTES = {
    "/api/v1/cloud/remotes": lambda s, b: s.cloud.add_remote(b),
    "/api/v1/cloud/remotes/delete": lambda s, b: s.cloud.delete_remote(_str(b, "id")),
    "/api/v1/cloud/signin/start": lambda s, b: s.cloud.signin_start(b),
    "/api/v1/cloud/signin/finish": lambda s, b: s.cloud.signin_finish(_str(b, "address")),
    "/api/v1/cloud/signin/cancel": lambda s, b: s.cloud.signin_cancel(),
    "/api/v1/cloud/apps": lambda s, b: s.cloud.set_app(_str(b, "provider"), _str(b, "client_id"),
                                                       _str(b, "client_secret")),
    "/api/v1/cloud/rclone": lambda s, b: s.cloud.set_rclone_path(_str(b, "path")),
    "/api/v1/cloud/link": lambda s, b: s.cloud.share_link(_str(b, "remote"), _str(b, "path")),
    "/api/v1/cloud/jobs": lambda s, b: s.cloud_jobs.save_job(b),
    "/api/v1/cloud/jobs/delete": lambda s, b: s.cloud_jobs.delete_job(_str(b, "id")),
    "/api/v1/cloud/jobs/run": lambda s, b: s.cloud_jobs.run(_str(b, "id")),
    "/api/v1/cloud/import": lambda s, b: s.cloud_jobs.import_now(b),
    "/api/v1/cloud/mounts": lambda s, b: s.cloud_jobs.save_mount(b),
    "/api/v1/cloud/mounts/delete": lambda s, b: s.cloud_jobs.delete_mount(_str(b, "id")),
    "/api/v1/cloud/mounts/mount": lambda s, b: s.cloud_jobs.mount(_str(b, "id")),
    "/api/v1/cloud/mounts/unmount": lambda s, b: s.cloud_jobs.unmount(_str(b, "id")),
    "/api/v1/cloud/hosts": lambda s, b: s.cloud.set_host_account(_str(b, "kind"), b),
    "/api/v1/cloud/hosts/delete": lambda s, b: s.cloud.delete_host_account(_str(b, "kind")),
}

#: Every path served here, for the OpenAPI drift test (`openapi.served_routes`).
GET_PATHS = tuple(GET_ROUTES) + ("/api/v1/cloud/oauth/callback",)
POST_PATHS = tuple(POST_ROUTES)


def _call(table: dict, service, path: str, arg: dict) -> tuple[int, dict]:
    handler = table.get(path)
    if handler is None:
        return 404, {"error": "not found"}
    try:
        return 200, handler(service, arg)
    except (CloudError, RcloneError) as err:
        return 400, {"error": str(err)}
    except (TypeError, ValueError) as err:
        return 400, {"error": f"invalid request: {err.__class__.__name__}"}


def get(service, path: str, query: dict) -> tuple[int, dict]:
    return _call(GET_ROUTES, service, path, query)


def post(service, path: str, body: dict) -> tuple[int, dict]:
    return _call(POST_ROUTES, service, path, body)


def oauth_callback(service, query: dict) -> tuple[bool, str]:
    """Where a provider sends the browser back when ROMarr's URL is the redirect."""
    try:
        service.cloud.native_callback(query)
        return True, ""
    except CloudError as err:
        return False, str(err)
