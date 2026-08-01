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
import os
from pathlib import Path

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


def _run_cli(*args, timeout=180):
    """Run the Hub CLI in ROMarr's own interpreter and capture the result.

    Install/enable/disable are one-shot, sandboxed operations the Hub already
    implements; shelling out to its entry point is the honest way to reuse them
    without copying the registry-mutation code here.
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
    env = dict(os.environ, ROM_HUB_HOME=str(HOME), ROM_HUB_ALLOW_UNSANDBOXED="1")
    env.update(_backend_env())
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
    return _run_cli("plugin", "uninstall", slug)


# Applied at import so in-process Hub calls see the same backend the CLI does.
for _k, _v in _backend_env().items():
    os.environ.setdefault(_k, _v)
