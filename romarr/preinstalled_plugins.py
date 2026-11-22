"""ROM Hub plugins ROMarr ships preinstalled: installed once, then left alone.

ROMarr publishes some integrations as ROM Hub plugins (Compressatorium for
batch decompression, Mister Download Manager for standalone downloads), so
they sit in the plugin catalogue with their credit, keep secrets in ROM Hub's
secret store, and can be disabled like any other plugin. Each module that owns
one (`compressatorium_plugin`, `mdm_plugin`) calls `preinstall` here with its
slug, repository and pinned release.

Preinstalled, not forced: ROMarr installs a plugin once, the first time it
starts with ROM Hub available, and records that it did. An operator who
disables the plugin (ROM Hub has no uninstall) or deletes it keeps it that way.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
import threading
from pathlib import Path
from typing import Mapping

from . import hub

log = logging.getLogger(__name__)

#: Where the Docker image and desktop packages put plugins they ship with.
DEFAULT_BUNDLED_DIR = Path("/opt/romarr/bundled-plugins")

#: Inside ROM_HUB_HOME: the plugins ROMarr has preinstalled there, ever.
MARKER_NAME = "romarr-preinstalled.json"

#: Inside ROM_HUB_HOME: the runtime-owned copies bundled plugins install from.
SOURCES_DIR = "bundled-sources"

#: One lock for every preinstalled plugin: they share the marker file, and a
#: read-modify-write of it from two threads would drop one plugin's record.
_lock = threading.Lock()


def bundled_dir(env: Mapping[str, str] | None = None) -> Path:
    env = os.environ if env is None else env
    return Path(env.get("ROMARR_BUNDLED_PLUGINS") or DEFAULT_BUNDLED_DIR)


def _marker(home: Path) -> Path:
    return home / MARKER_NAME


def read_marker(home: Path) -> dict:
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


def preinstall(slug: str, repo: str, ref: str, *, home: Path | None = None,
               env: Mapping[str, str] | None = None) -> str:
    """Install plugin `slug` once, if ROM Hub is here and it never was.

    Returns what happened, for the log and for tests: `no-hub`, `offered`
    (installed before; left as the operator has it), `present` (already
    installed by hand), `installed`, or `failed: <reason>`. A failure is not
    recorded, so the next start tries again.
    """
    if not hub.available():
        return "no-hub"
    home = Path(home or hub.HOME)
    with _lock:
        marker = read_marker(home)
        if slug in marker:
            return "offered"
        try:
            _, registry = hub._import()
            reg = registry.Registry(home)
            if any(p.slug == slug for p in reg.installed()):
                outcome = "present"
            else:
                bundled = bundled_dir(env) / slug
                if (bundled / "manifest.toml").is_file():
                    _install_bundled(reg, bundled, home, slug)
                else:
                    reg.install(repo, ref=ref)
                outcome = "installed"
            marker[slug] = ref
            _write_marker(home, marker)
        except Exception as exc:  # the host is optional; never block startup
            log.warning("could not preinstall the %s plugin: %s", slug, exc)
            return f"failed: {exc}"
    log.info("%s plugin: %s", slug, outcome)
    return outcome


def _install_bundled(reg, bundled: Path, home: Path, slug: str) -> None:
    """Install the bundled checkout from a copy the runtime user owns.

    The image builds the checkout as root, while ROMarr runs as PUID. Git
    refuses a local clone from a repository another user owns, so installing
    straight from the bundled folder failed on every start. A copy under
    ROM_HUB_HOME avoids that without loosening git's ownership check, and it
    is kept: ROM Hub records it as the plugin's source, and a later plugin
    update clones from it again.
    """
    sources = home / SOURCES_DIR
    sources.mkdir(parents=True, exist_ok=True)
    target = sources / slug
    with tempfile.TemporaryDirectory(prefix=".copy-", dir=sources) as stage:
        staged = Path(stage) / slug
        shutil.copytree(bundled, staged, symlinks=True)
        if target.exists():
            shutil.rmtree(target)
        os.replace(staged, target)
    reg.install(str(target))


#: Source plugins every install gets out of the box, as (slug, repository,
#: pinned release). Each was proven against its live source before it was
#: listed (a real search returning rows), and each offers material its
#: publisher distributes freely: Archive.org's software library, homebrew
#: from its authors, libretro's own content downloads, ScummVM's freeware
#: releases and Universal-DB's homebrew index. Like the other preinstalled
#: plugins they are installed once and can be disabled.
DEFAULT_PLUGINS: tuple[tuple[str, str, str], ...] = (
    ("archive-org", "https://github.com/BlizzHacker/rom-hub-archive-org", "v0.4.1"),
    ("homebrew", "https://github.com/BlizzHacker/rom-hub-homebrew", "v0.3.0"),
    ("libretro-content", "https://github.com/BlizzHacker/rom-hub-libretro-content", "v0.2.0"),
    ("scummvm-freeware", "https://github.com/BlizzHacker/rom-hub-scummvm-freeware", "v0.2.0"),
    ("universal-db", "https://github.com/BlizzHacker/rom-hub-universal-db", "v0.2.0"),
)


def preinstall_defaults(*, home: Path | None = None,
                        env: Mapping[str, str] | None = None) -> dict[str, str]:
    """Preinstall every default source plugin; {slug: outcome}.

    One failure never stops the rest: each is tried, and a failed one is
    retried on the next start because `preinstall` records only successes.
    """
    return {slug: preinstall(slug, repo, ref, home=home, env=env)
            for slug, repo, ref in DEFAULT_PLUGINS}


def preinstall_defaults_in_background() -> None:
    """`preinstall_defaults` on a daemon thread, after the integrations."""
    threading.Thread(target=preinstall_defaults, name="preinstall-defaults",
                     daemon=True).start()


def preinstall_in_background(slug: str, repo: str, ref: str) -> None:
    """`preinstall` on a daemon thread, so a slow git never delays serving."""
    threading.Thread(target=preinstall, args=(slug, repo, ref),
                     name=f"preinstall-{slug}", daemon=True).start()


def enabled_plugin(slug: str, home: Path):
    """The installed, enabled plugin `slug`, or None."""
    try:
        _, registry = hub._import()
        plugin = registry.Registry(home).get(slug)
    except Exception:
        return None
    return plugin if getattr(plugin, "enabled", True) else None


def secret(slug: str, home: Path, key: str) -> str:
    """One of plugin `slug`'s secrets from ROM Hub's store, or "" if unreadable."""
    try:
        from rom_hub import secrets
        value = secrets.open_store(home).get(slug, key)
    except Exception as exc:
        log.warning("cannot read the %s plugin's %s: %s", slug, key, exc)
        return ""
    return str(value or "")
