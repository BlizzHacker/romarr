"""The Compressatorium ROM Hub plugin: preinstalled, and where its settings live.

ROMarr's batch decompression talks to a Compressatorium container
(github.com/pacnpal/compressatorium, by pacnpal). The connection is published
as a ROM Hub plugin, `rom-hub-compressatorium`, so it sits in the plugin
catalogue with its credit, keeps the API key in ROM Hub's secret store, and
can be removed like any other plugin.

The decompression itself stays in ROMarr (`decompress`). A ROM Hub plugin runs
in a sandbox that may only make HTTPS requests to the hosts its manifest
names; it can reach neither a container on the LAN nor the library it would
have to write to. So the plugin carries settings and credit, and ROMarr reads
them here.

Preinstalled, not forced: ROMarr installs the plugin once, the first time it
starts with ROM Hub available, and records that it did. An operator who
disables the plugin (ROM Hub has no uninstall) or deletes it keeps it that way.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from typing import Mapping
from urllib.parse import urlsplit

from . import hub

log = logging.getLogger(__name__)

SLUG = "compressatorium"
PLUGIN_REPO = "https://github.com/BlizzHacker/rom-hub-compressatorium"
#: The release ROMarr preinstalls when it has no bundled copy to install from.
PLUGIN_REF = "v0.1.0"
UPSTREAM_URL = "https://github.com/pacnpal/compressatorium"
CREDIT = ("Compressatorium is by pacnpal (https://github.com/pacnpal/compressatorium). "
          "ROMarr talks to it over its HTTP API and includes none of its code.")

#: Where the Docker image and desktop packages put plugins they ship with.
DEFAULT_BUNDLED_DIR = Path("/opt/romarr/bundled-plugins")

#: Inside ROM_HUB_HOME: the plugins ROMarr has preinstalled there, ever.
MARKER_NAME = "romarr-preinstalled.json"

_lock = threading.Lock()


def bundled_dir(env: Mapping[str, str] | None = None) -> Path:
    env = os.environ if env is None else env
    return Path(env.get("ROMARR_BUNDLED_PLUGINS") or DEFAULT_BUNDLED_DIR)


def _marker(home: Path) -> Path:
    return home / MARKER_NAME


def _read_marker(home: Path) -> dict:
    try:
        data = json.loads(_marker(home).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_marker(home: Path, data: dict) -> None:
    path = _marker(home)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def preinstall(*, home: Path | None = None,
               env: Mapping[str, str] | None = None) -> str:
    """Install the plugin once, if ROM Hub is here and it never was.

    Returns what happened, for the log and for tests: `no-hub`, `offered`
    (installed before; left as the operator has it), `present` (already
    installed by hand), `installed`, or `failed: <reason>`. A failure is not
    recorded, so the next start tries again.
    """
    if not hub.available():
        return "no-hub"
    home = Path(home or hub.HOME)
    with _lock:
        marker = _read_marker(home)
        if SLUG in marker:
            return "offered"
        try:
            _, registry = hub._import()
            reg = registry.Registry(home)
            if any(p.slug == SLUG for p in reg.installed()):
                outcome = "present"
            else:
                bundled = bundled_dir(env) / SLUG
                if (bundled / "manifest.toml").is_file():
                    reg.install(str(bundled))
                else:
                    reg.install(PLUGIN_REPO, ref=PLUGIN_REF)
                outcome = "installed"
            marker[SLUG] = PLUGIN_REF
            _write_marker(home, marker)
        except Exception as exc:  # the host is optional; never block startup
            log.warning("could not preinstall the %s plugin: %s", SLUG, exc)
            return f"failed: {exc}"
    log.info("compressatorium plugin: %s", outcome)
    return outcome


def preinstall_in_background() -> None:
    """`preinstall` on a daemon thread, so a slow git never delays serving."""
    threading.Thread(target=preinstall, name="preinstall-plugins",
                     daemon=True).start()


def _plugin(home: Path):
    """The installed, enabled plugin, or None."""
    try:
        _, registry = hub._import()
        plugin = registry.Registry(home).get(SLUG)
    except Exception:
        return None
    return plugin if getattr(plugin, "enabled", True) else None


def _secret(home: Path, key: str) -> str:
    try:
        from rom_hub import secrets
        value = secrets.open_store(home).get(SLUG, key)
    except Exception as exc:
        log.warning("cannot read the %s plugin's %s: %s", SLUG, key, exc)
        return ""
    return str(value or "")


def connection(env: Mapping[str, str], *, home: Path | None = None) -> tuple[str, str, str]:
    """(url, api_key, source) for reaching Compressatorium.

    The environment wins outright when it names a URL: a container
    configured that way keeps working exactly as before. Otherwise the
    enabled plugin's `url`, with its key from ROM Hub's secret store.
    `source` is `env`, `plugin` or "" (not configured).
    """
    url = str(env.get("COMPRESSATORIUM_URL") or "").strip()
    if url:
        return url, str(env.get("COMPRESSATORIUM_API_KEY") or ""), "env"
    home = Path(home or hub.HOME)
    plugin = _plugin(home)
    if plugin is None:
        return "", "", ""
    url = str((getattr(plugin, "config", None) or {}).get("url") or "").strip()
    if not url:
        return "", "", ""
    return url, _secret(home, "api_key"), "plugin"


def _valid_url(url: str) -> str:
    """"" when `url` can address a Compressatorium, else why not."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return "url must be an http:// or https:// address, such as http://compressatorium:8080"
    if parts.username or parts.password:
        return "put the API key in its own field, not in the url"
    if parts.query or parts.fragment:
        return "url must be the service's base address, with no query or fragment"
    return ""


def configure(*, url: str | None = None, api_key: str | None = None,
              clear_api_key: bool = False, home: Path | None = None) -> dict:
    """Save the plugin's `url` and/or secret `api_key` from the Hub page.

    `None` leaves a field as it is, so the key can be replaced without
    retyping the address and the other way round. The key goes to ROM Hub's
    secret store, never into the plugin config that `state.json` holds.
    """
    home = Path(home or hub.HOME)
    try:
        _, registry = hub._import()
        reg = registry.Registry(home)
        plugin = reg.get(SLUG)
    except Exception:
        return {"ok": False, "error": "Install the Compressatorium plugin first"}
    if url is not None:
        url = str(url).strip()
        problem = _valid_url(url) if url else ""
        if problem:
            return {"ok": False, "error": problem}
        config = dict(getattr(plugin, "config", None) or {})
        config["url"] = url.rstrip("/")
        reg.set_config(SLUG, config)
    if clear_api_key or api_key:
        try:
            from rom_hub import secrets
            store = secrets.open_store(home)
            if clear_api_key:
                store.delete(SLUG, "api_key")
            else:
                store.set(SLUG, "api_key", str(api_key))
        except Exception as exc:
            return {"ok": False, "error": f"could not save the API key: {exc}"}
    return {"ok": True}


def status(env: Mapping[str, str], *, home: Path | None = None) -> dict:
    """What the Hub page shows about the integration. Never the key."""
    home = Path(home or hub.HOME)
    plugin = _plugin(home) if hub.available() else None
    url, key, source = connection(env, home=home)
    return {
        "plugin_slug": SLUG,
        "plugin_repo": PLUGIN_REPO,
        "plugin_enabled": plugin is not None,
        "configured": bool(url),
        "url": url,
        "api_key_set": bool(key),
        "source": source,
        "upstream_url": UPSTREAM_URL,
        "credit": CREDIT,
    }
