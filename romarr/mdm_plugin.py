"""The Mister Download Manager ROM Hub plugin: preinstalled, and where MDM's settings live.

ROMarr's two standalone download clients, `mdm_http` (direct HTTP) and
`mdm_torrent` (BitTorrent), drive Mister Download Manager
(github.com/anefzaoui/misterdownloadmanager, by anefzaoui, GPL-3.0-or-later)
through its CLI or its Remote HTTPS API (`mdm`). The connection is published as
a ROM Hub plugin, `rom-hub-mdm`, shown as "Mister Download Manager (direct
downloads)", so it sits in the plugin catalogue with its credit, keeps the API
token in ROM Hub's secret store, and offers MDM's own release archives.

Once the plugin's `mode` is set, it is where both clients get their mode,
remote URL, API token, category and (for direct HTTP) connections: `apply`
lays those over a client's stored row whenever ROMarr builds the client.
Saving an MDM client in ROMarr writes the same fields back to the plugin
(`sync_from_client`), so an edit in either place shows in the other. With
`mode` blank, each client keeps its own settings and the plugin is left alone.

The downloading stays in ROMarr. A ROM Hub plugin runs in a sandbox that may
only make HTTPS requests to the hosts its manifest names; it cannot run MDM
or reach one on the LAN. So the plugin carries settings and credit, and ROMarr
reads them here.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping
from urllib.parse import urlsplit

from . import hub, preinstalled_plugins
from .mdm import MDM_CATEGORIES, _is_loopback_host

log = logging.getLogger(__name__)

SLUG = "mdm"
LABEL = "Mister Download Manager (direct downloads)"
PLUGIN_REPO = "https://github.com/BlizzHacker/rom-hub-mdm"
#: The release ROMarr preinstalls when it has no bundled copy to install from.
PLUGIN_REF = "v0.1.0"
UPSTREAM_URL = "https://github.com/anefzaoui/misterdownloadmanager"
CREDIT = ("Mister Download Manager is by anefzaoui "
          "(https://github.com/anefzaoui/misterdownloadmanager), licensed "
          "GPL-3.0-or-later. ROMarr runs it as a separate program or talks to "
          "its Remote HTTPS API, and includes none of its code.")

#: ROMarr's download-client types this plugin configures.
CLIENT_TYPES = ("mdm_torrent", "mdm_http")
MODES = ("cli", "remote")
DEFAULT_CONNECTIONS = 8
MIN_CONNECTIONS = 1
MAX_CONNECTIONS = 128
#: What the download-client form sends for a secret it did not change.
SECRET_PLACEHOLDER = "********"

#: Guards the plugin's config and token together, so a reader never sees a
#: field from before a save beside one from after it. Separate from the
#: preinstall lock, which is held across git work.
_config_lock = threading.Lock()


def preinstall(*, home: Path | None = None,
               env: Mapping[str, str] | None = None) -> str:
    """Install the plugin once, if ROM Hub is here and it never was.

    See `preinstalled_plugins.preinstall` for the outcomes it returns.
    """
    return preinstalled_plugins.preinstall(SLUG, PLUGIN_REPO, PLUGIN_REF,
                                           home=home, env=env)


def preinstall_in_background() -> None:
    """`preinstall` on a daemon thread, so a slow git never delays serving."""
    preinstalled_plugins.preinstall_in_background(SLUG, PLUGIN_REPO, PLUGIN_REF)


@dataclass(frozen=True)
class Settings:
    """The plugin's MDM settings, once its mode is set."""

    mode: str
    remote_url: str
    api_token: str
    connections: int
    category: str


def _connections(value: object) -> int:
    """A stored `connections` value, clamped into MDM's range."""
    try:
        number = int(value) if not isinstance(value, bool) else DEFAULT_CONNECTIONS
    except (TypeError, ValueError):
        number = DEFAULT_CONNECTIONS
    return max(MIN_CONNECTIONS, min(MAX_CONNECTIONS, number))


def _config(home: Path) -> dict | None:
    plugin = preinstalled_plugins.enabled_plugin(SLUG, home)
    if plugin is None:
        return None
    return dict(getattr(plugin, "config", None) or {})


def settings(*, home: Path | None = None) -> Settings | None:
    """The enabled plugin's settings, or None when it is absent, off or unset."""
    if not hub.available():
        return None
    home = Path(home or hub.HOME)
    with _config_lock:
        config = _config(home)
        if config is None:
            return None
        mode = str(config.get("mode") or "").strip().lower()
        if mode not in MODES:
            return None
        return Settings(
            mode=mode,
            remote_url=str(config.get("remote_url") or "").strip(),
            api_token=preinstalled_plugins.secret(SLUG, home, "api_token"),
            connections=_connections(config.get("connections", DEFAULT_CONNECTIONS)),
            category=str(config.get("category") or "").strip().lower(),
        )


_UNSET = object()


def apply(cfg: dict, current: Settings | None | object = _UNSET) -> dict:
    """`cfg` with the plugin's settings laid over it, for an MDM client.

    Returns a new dict; `cfg` is not changed. Any other client type, or an
    absent or unset plugin, gets `cfg` back as it is. `current` lets a caller
    that walks many rows read the plugin once.
    """
    kind = str(cfg.get("type") or "").strip().lower()
    if kind not in CLIENT_TYPES:
        return cfg
    if current is _UNSET:
        current = settings()
    if current is None:
        return cfg
    out = dict(cfg)
    out["mode"] = current.mode
    out["remote_url"] = current.remote_url
    out["api_token"] = current.api_token
    out["category"] = current.category
    if kind == "mdm_http":
        out["connections"] = current.connections
    return out


def _url_problem(url: str) -> str:
    """"" when `url` can address an MDM Remote API, else why not.

    The same rule `mdm.MdmRemoteTransport` applies, checked at save time so
    the operator hears it then: https, or plain http only to this machine,
    because the bearer token must not cross a network in cleartext.
    """
    parts = urlsplit(url)
    host = parts.hostname or ""
    scheme = parts.scheme.lower()
    if not host or scheme not in ("http", "https"):
        return "remote_url must be an https:// address, such as https://127.0.0.1:8463"
    if scheme == "http" and not _is_loopback_host(host):
        return "remote_url must use https:// (plain http:// is allowed only for localhost)"
    if parts.username or parts.password:
        return "put the API token in its own field, not in remote_url"
    if parts.query or parts.fragment:
        return "remote_url must be the service's base address, with no query or fragment"
    return ""


def _validated(config: dict) -> tuple[dict, str]:
    """The plugin config to store, normalised, and "" or why it is refused."""
    mode = str(config.get("mode") or "").strip().lower()
    if mode and mode not in MODES:
        return config, "mode must be cli, remote, or blank"
    remote_url = str(config.get("remote_url") or "").strip().rstrip("/")
    if remote_url:
        problem = _url_problem(remote_url)
        if problem:
            return config, problem
    elif mode == "remote":
        return config, "Remote mode needs remote_url, such as https://127.0.0.1:8463"
    raw_conns = config.get("connections", DEFAULT_CONNECTIONS)
    if raw_conns in (None, ""):
        raw_conns = DEFAULT_CONNECTIONS
    if isinstance(raw_conns, bool):
        return config, "connections must be a whole number from 1 to 128"
    try:
        connections = int(raw_conns)
    except (TypeError, ValueError):
        return config, "connections must be a whole number from 1 to 128"
    if not MIN_CONNECTIONS <= connections <= MAX_CONNECTIONS:
        return config, "connections must be a whole number from 1 to 128"
    category = str(config.get("category") or "").strip().lower()
    if category and category not in MDM_CATEGORIES:
        return config, ("category must be blank or one of "
                        + ", ".join(sorted(MDM_CATEGORIES)))
    return {"mode": mode, "remote_url": remote_url, "connections": connections,
            "category": category}, ""


def configure(*, mode: str | None = None, remote_url: str | None = None,
              api_token: str | None = None, clear_api_token: bool = False,
              connections: int | str | None = None, category: str | None = None,
              home: Path | None = None) -> dict:
    """Save the plugin's settings from the Hub page or a download-client save.

    `None` leaves a field as it is. The token goes to ROM Hub's secret store,
    never into the plugin config that `state.json` holds. The whole result is
    validated before anything is written, so a refused save changes nothing.
    """
    home = Path(home or hub.HOME)
    try:
        _, registry = hub._import()
        reg = registry.Registry(home)
    except Exception:
        return {"ok": False, "error": f"Install the {LABEL} plugin first"}
    with _config_lock:
        # Read the plugin under the lock: Registry.get returns a snapshot, and
        # two overlapping saves merging into an older one could pair one
        # save's token with the other's address.
        try:
            plugin = reg.get(SLUG)
        except Exception:
            return {"ok": False, "error": f"Install the {LABEL} plugin first"}
        config = dict(getattr(plugin, "config", None) or {})
        for key, value in (("mode", mode), ("remote_url", remote_url),
                           ("connections", connections), ("category", category)):
            if value is not None:
                config[key] = value
        clean, problem = _validated(config)
        if problem:
            return {"ok": False, "error": problem}
        # The token is saved first and the config only once it is, so a
        # failure never leaves a new address paired with the old token --
        # that pairing would send the old token to a service it was never
        # meant for.
        store = previous = None
        if clear_api_token or api_token:
            try:
                from rom_hub import secrets
                store = secrets.open_store(home)
                previous = store.get(SLUG, "api_token")
                if clear_api_token:
                    store.delete(SLUG, "api_token")
                else:
                    store.set(SLUG, "api_token", str(api_token))
            except Exception as exc:
                return {"ok": False, "error": f"could not save the API token: {exc}"}
        try:
            reg.set_config(SLUG, {**config, **clean})
        except Exception as exc:
            if store is not None:
                try:  # put the old token back with the old address
                    if previous:
                        store.set(SLUG, "api_token", str(previous))
                    else:
                        store.delete(SLUG, "api_token")
                except Exception:
                    log.warning("could not restore the %s plugin's API token", SLUG)
            return {"ok": False, "error": f"could not save the settings: {exc}"}
    return {"ok": True}


def token_update(submitted: dict) -> tuple[str | None, bool]:
    """(api_token, clear) for `configure`, from what a client form sent.

    The form sends the placeholder for a token it did not change, which must
    leave the plugin's token alone; an emptied field clears it.
    """
    if "api_token" not in submitted:
        return None, False
    token = submitted.get("api_token")
    if token == SECRET_PLACEHOLDER:
        return None, False
    if token in ("", None):
        return None, True
    return str(token), False


def sync_from_client(submitted: dict, *, home: Path | None = None) -> dict:
    """Write an MDM client's settings from ROMarr's form back to the plugin.

    Only while the plugin's mode is set: that is when the plugin is where the
    clients' settings come from. Otherwise the client keeps its own settings
    and this does nothing. Returns `configure`'s answer, or `{"ok": True,
    "synced": False}` when there was nothing to do.
    """
    kind = str(submitted.get("type") or "").strip().lower()
    if kind not in CLIENT_TYPES or settings(home=home) is None:
        return {"ok": True, "synced": False}
    token, clear = token_update(submitted)
    category = str(submitted.get("category") or "").strip().lower()
    result = configure(
        mode=str(submitted.get("mode") or "cli").strip().lower(),
        remote_url=str(submitted.get("remote_url") or ""),
        api_token=token, clear_api_token=clear,
        # A torrent row has no connections field; leave the plugin's as it is.
        connections=submitted.get("connections") if kind == "mdm_http" else None,
        # ROMarr's torrent rows default to the label "romarr", which MDM has
        # no category for and ignores; store that as "no category".
        category=category if category in MDM_CATEGORIES else "",
        home=home,
    )
    return {**result, "synced": bool(result.get("ok"))}


def for_test(submitted: dict, merged: dict) -> dict:
    """The row a client form's Test button should try, as a new dict.

    Test tries what the form shows. While the plugin is set, a token the form
    left as the placeholder is the plugin's token, not the one stored on the
    row, which the plugin's may have replaced.
    """
    out = dict(merged)
    kind = str(merged.get("type") or "").strip().lower()
    if kind in CLIENT_TYPES and "api_token" in submitted:
        current = settings()
        if current is not None:
            if submitted.get("api_token") == SECRET_PLACEHOLDER:
                out["api_token"] = current.api_token
            elif submitted.get("api_token") == "":
                # An emptied field tests as no token, exactly as Save would
                # clear it -- not as the row's old token merge_secrets restored.
                out["api_token"] = ""
    return out


def status(*, home: Path | None = None) -> dict:
    """What the Hub card and the clients page show. Never the token."""
    home = Path(home or hub.HOME)
    plugin_config = None
    token_set = False
    if hub.available():
        with _config_lock:
            plugin_config = _config(home)
            if plugin_config is not None:
                token_set = bool(preinstalled_plugins.secret(SLUG, home, "api_token"))
    config = plugin_config or {}
    mode = str(config.get("mode") or "").strip().lower()
    return {
        "plugin_slug": SLUG,
        "label": LABEL,
        "plugin_repo": PLUGIN_REPO,
        "plugin_enabled": plugin_config is not None,
        # The same test `settings` makes: enabled, with a mode set.
        "configured": plugin_config is not None and mode in MODES,
        "mode": mode,
        "remote_url": str(config.get("remote_url") or ""),
        "api_token_set": token_set,
        "connections": _connections(config.get("connections", DEFAULT_CONNECTIONS)),
        "category": str(config.get("category") or ""),
        "modes": list(MODES),
        "categories": sorted(MDM_CATEGORIES),
        "client_types": list(CLIENT_TYPES),
        "upstream_url": UPSTREAM_URL,
        "credit": CREDIT,
    }
