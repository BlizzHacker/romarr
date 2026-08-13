"""ROMarr's window onto ROM Hub — the plugin layer of the Cartridge ecosystem.

ROMarr acquires games; ROM Hub is where the *sources* it can acquire from live,
as sandboxed, backend-agnostic plugins. This module is the thin bridge: it reads
the Hub's plugin catalogue and installed state through the Hub's own Python API
(the same one its CLI uses), so the ROMarr UI can show every plugin, install one,
and turn it on or off — without ROMarr reimplementing any of the Hub's logic.

Everything here is read-through-then-act: the source of truth stays the Hub's
registry on disk (`$ROM_HUB_HOME`). ROMarr never edits it directly.
"""
from __future__ import annotations

import dataclasses
import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)

# ROM Hub keeps its state (catalog cache, installed plugins) under this home.
# ROMarr gives it a stable directory it owns so state survives restarts.
HOME = Path(os.environ.get("ROM_HUB_HOME", "/opt/romarr/.rom-hub"))
# The Hub refuses to run plugins where it can't sandbox them; in ROMarr's
# container that's fine for *listing/installing*, and the flag is only consulted
# when a plugin subprocess is actually launched.
os.environ.setdefault("ROM_HUB_ALLOW_UNSANDBOXED", "1")
os.environ.setdefault("ROM_HUB_HOME", str(HOME))


def _backend_env() -> dict:
    """Hand ROMarr's own library credentials to the Hub.

    ROMarr already knows which library it files into and how to authenticate to
    it; the Hub needs the same thing to run `import` and `enrich`. Left to
    itself the Hub reads its own variables and finds nothing, so a plugin
    installed from the Hub tab could be listed but never used. The names differ
    by one word -- ROMarr reads ROMM_USERNAME, the Hub reads ROMM_USER -- so the
    mapping is explicit rather than assumed.
    """
    env = {}
    url = os.environ.get("LIBRARY_URL") or os.environ.get("ROMM_URL", "")
    user = os.environ.get("LIBRARY_USERNAME") or os.environ.get("ROMM_USERNAME", "")
    pw = os.environ.get("LIBRARY_PASSWORD") or os.environ.get("ROMM_PASSWORD", "")
    kind = (os.environ.get("LIBRARY_KIND") or "romm").strip().lower()
    if url:
        env["ROMM_URL"] = url
        env["ROM_HUB_BACKEND"] = kind
    if user:
        env["ROMM_USER"] = user
    if pw:
        env["ROMM_PASSWORD"] = pw
    return env


def _import():
    """Import the Hub lazily so ROMarr still starts if it isn't installed."""
    from rom_hub import catalog_sources, registry  # noqa: WPS433
    return catalog_sources, registry


def available() -> bool:
    try:
        _import()
        return True
    except Exception:
        return False


def _installed_slugs(registry) -> dict:
    """slug -> {enabled, version, commit} for what's installed, best-effort."""
    out = {}
    try:
        reg = registry.Registry(HOME)
        for p in reg.installed():
            # InstalledPlugin: slug, path, manifest, enabled, config, commit.
            # The installed version lives in the plugin's own manifest.
            slug = getattr(p, "slug", None)
            manifest = getattr(p, "manifest", {}) or {}
            if slug:
                out[slug] = {
                    "enabled": bool(getattr(p, "enabled", True)),
                    "version": manifest.get("version", "") if isinstance(manifest, dict) else "",
                    "commit": (getattr(p, "commit", "") or "")[:8],
                }
    except Exception:
        pass
    return out


def plugins() -> dict:
    """The whole plugin catalogue, annotated with install/enabled state.

    Shape mirrors ROMarr's other list endpoints: {"items": [...], "error": ...}.
    Each item is safe to render directly in the Hub tab.
    """
    try:
        catalog_sources, registry = _import()
    except Exception as exc:
        return {"items": [], "error": f"ROM Hub is not installed: {exc}"}

    try:
        merged = catalog_sources.load_all(HOME)
    except Exception as exc:
        return {"items": [], "error": f"could not read the plugin catalog: {exc}"}

    installed = _installed_slugs(registry)
    items = []
    for sourced in merged.entries:
        e = getattr(sourced, "entry", sourced)
        d = dataclasses.asdict(e) if dataclasses.is_dataclass(e) else dict(vars(e))
        slug = d.get("slug")
        inst = installed.get(slug)
        items.append({
            "slug": slug,
            "name": d.get("name") or slug,
            "author": d.get("author") or "",
            "version": d.get("version") or "",
            "repository": d.get("repository") or "",
            "capabilities": list(d.get("capabilities") or []),
            "network": list(d.get("network") or []),
            "description": d.get("description") or "",
            "key_required": bool(d.get("key_required")),
            "platforms": list(d.get("platforms") or []),
            "installed": inst is not None,
            "enabled": inst["enabled"] if inst else False,
        })
    items.sort(key=lambda x: (not x["installed"], x["slug"]))
    return {
        "items": items,
        "installed_count": sum(1 for i in items if i["installed"]),
        "total": len(items),
        "error": None,
    }


#: Environment a plugin subprocess is allowed to inherit.
#:
#: It used to get `dict(os.environ, ...)` -- ROMarr's entire environment, which
#: is where ROMARR_API_KEY, ROMARR_PASSWORD, PROWLARR_API_KEY, QBITTORRENT_PASS
#: and LIBRARY_PASSWORD all live. A plugin needs a library to import into; it
#: has no business holding the key to ROMarr itself or the password to the
#: torrent client, and "it is probably fine" is not a trust boundary.
#:
#: Only what a process needs to run at all is passed through, plus the library
#: credentials `_backend_env` hands over deliberately. Anything else a plugin
#: wants has to be configured on the plugin.
_ENV_PASSTHROUGH = ("PATH", "HOME", "LANG", "LC_ALL", "TZ", "TMPDIR",
                    "SYSTEMROOT", "TEMP", "TMP",
                    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY",
                    "http_proxy", "https_proxy", "no_proxy")




def sandbox_state() -> tuple[bool, str]:
    """Whether ROM Hub can confine a plugin here, and why not if it cannot.

    The Hub runs each plugin as a subprocess whose only route outward is an RPC
    back to the host, checked against the hosts that plugin declared. That
    boundary is enforced by a seccomp filter, and the filter needs Linux and
    `pyseccomp`. Both are ordinary to have; neither was installed, so ROMarr
    had been opting out of the whole mechanism.

    Reported rather than assumed, so the Plugins page can say which of the two
    states an install is actually in.
    """
    try:
        from rom_hub.sandbox import probe
    except Exception as err:  # noqa: BLE001 - the Hub may not be installed
        return False, f"ROM Hub is not available ({err.__class__.__name__})"
    try:
        return probe()
    except Exception as err:  # noqa: BLE001
        detail = str(err).strip()
        suffix = f": {detail}" if detail else ""
        return False, f"sandbox probe failed: {err.__class__.__name__}{suffix}"


def _plugin_env() -> dict:
    """The environment a plugin subprocess runs with.

    Built by allowlist rather than by subtraction: a denylist silently stops
    covering anything added later, and the thing added later is exactly the
    credential nobody thought about.
    """
    env = {name: os.environ[name] for name in _ENV_PASSTHROUGH
           if name in os.environ}
    env["ROM_HUB_HOME"] = str(HOME)
    ok, why = sandbox_state()
    if not ok:
        # Asked for, never assumed. ROMarr used to set this unconditionally on
        # the belief that its container could not confine a plugin. That was
        # wrong: the Hub's seccomp filter needs `pyseccomp` and nothing else,
        # so the only thing standing between plugins and a real boundary was a
        # missing dependency -- and setting the flag turned off the network and
        # exec confinement the Hub exists to provide.
        #
        # The Hub's own words for this flag are "no confinement at all ... a
        # development convenience, never a deployment setting". So it is set
        # only when the sandbox genuinely cannot be installed, and loudly.
        fix = ("Install pyseccomp to restore it." if "pyseccomp" in why
               else "Plugins will run with no network or exec confinement.")
        log.warning("running plugins WITHOUT confinement: %s. %s", why, fix)
        env["ROM_HUB_ALLOW_UNSANDBOXED"] = "1"
    env.update(_backend_env())
    return env


def _run_cli(*args, timeout=180):
    """Run the Hub CLI in ROMarr's own interpreter and capture the result.

    Install/enable/disable are one-shot operations the Hub already implements;
    shelling out to its entry point is the honest way to reuse them without
    copying the registry-mutation code here.

    Not sandboxed. ROM_HUB_ALLOW_UNSANDBOXED is set because the Hub cannot
    isolate a subprocess inside ROMarr's container, so installing a plugin runs
    that plugin's code with ROMarr's own privileges. `_plugin_env` limits what
    it inherits; it cannot limit what it does.
    """
    import subprocess
    import sys
    # The Hub ships a `rom-hub` console script next to the interpreter; it has no
    # `python -m rom_hub` entry point. Prefer the script; fall back to the CLI
    # module's main() if the script isn't on PATH.
    script = Path(sys.executable).with_name("rom-hub")
    if script.exists():
        cmd = [str(script), *args]
    else:
        cmd = [sys.executable, "-c",
               "import sys; from rom_hub.cli import main; sys.exit(main())", *args]
    env = _plugin_env()
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)
        ok = p.returncode == 0
        return {"ok": ok, "out": (p.stdout or "").strip(),
                "err": (p.stderr or "").strip(), "code": p.returncode}
    except subprocess.TimeoutExpired:
        return {"ok": False, "out": "", "err": "timed out", "code": -1}
    except Exception as exc:  # pragma: no cover
        return {"ok": False, "out": "", "err": str(exc), "code": -1}


def install(slug: str) -> dict:
    """Install one catalog plugin.

    `plugin install` is non-interactive and takes no confirmation flag; the
    trust warning a human needs is shown in the UI before the button is pressed.
    """
    return _run_cli("plugin", "install", slug)


def _set_enabled(slug: str, on: bool) -> dict:
    """Toggle enable/disable through the registry directly — no subprocess."""
    try:
        _, registry = _import()
        reg = registry.Registry(HOME)
        reg.set_enabled(slug, on)
        return {"ok": True, "out": f"{'enabled' if on else 'disabled'} {slug}",
                "err": "", "code": 0}
    except Exception as exc:
        # fall back to the CLI verb
        return _run_cli("plugin", "enable" if on else "disable", slug)


def enable(slug: str) -> dict:
    return _set_enabled(slug, True)


def disable(slug: str) -> dict:
    return _set_enabled(slug, False)


def uninstall(slug: str) -> dict:
    """ROM Hub has no uninstall, and saying so beats a usage dump.

    Neither the CLI nor `registry.Registry` implements one -- `plugin` offers
    install, browse, list, enable, disable, assets, config and secret, and the
    registry has install/get/installed/set_enabled/set_config and nothing that
    removes. Shelling out to `plugin uninstall` therefore printed argparse's
    list of valid choices, which reads like a bug in ROMarr rather than a
    capability the Hub does not have.

    Disabling is the supported way to stop a plugin being used: it drops out of
    the fan-out and any command aimed at it refuses. Removing the directory by
    hand would leave the registry's state.json still listing it, which is why
    that is not done from here either.
    """
    return {
        "ok": False,
        "out": "",
        "err": (
            f"ROM Hub cannot uninstall a plugin: neither its CLI nor its "
            f"registry implements one. Disable {slug!r} instead -- it then "
            f"drops out of search and every command aimed at it refuses. To "
            f"remove it from disk entirely, delete "
            f"$ROM_HUB_HOME/plugins/{slug} and its entry in "
            f"$ROM_HUB_HOME/state.json together; removing only one of the two "
            f"leaves the registry inconsistent."
        ),
        "code": -1,
    }


# Applied at import so in-process Hub calls see the same backend the CLI does.
for _k, _v in _backend_env().items():
    os.environ.setdefault(_k, _v)
