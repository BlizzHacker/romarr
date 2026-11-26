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
import re
import sys
import threading
from pathlib import Path
from .platforms import resolve, same_machine

log = logging.getLogger(__name__)

_FLARESOLVERR_LOCK = threading.Lock()
_FLARESOLVERR_URL = ""


def configure_flaresolverr(url: str | None) -> None:
    """Set the operator-configured FlareSolverr URL for host-side Hub HTTP."""
    global _FLARESOLVERR_URL
    with _FLARESOLVERR_LOCK:
        _FLARESOLVERR_URL = str(url or "").strip().rstrip("/")


_GITHUB_TOKEN_LOCK = threading.Lock()
_GITHUB_TOKEN = ""


def configure_github_token(token: str | None) -> None:
    """Set the GitHub token ROM Hub's host fetcher sends to api.github.com.

    ROM Hub reads ROM_HUB_GITHUB_TOKEN, and only ever sends it on GET
    requests to api.github.com. Without one, every plugin that lists GitHub
    releases or files shares the anonymous 60-requests-an-hour limit of the
    whole network's public address. Applied to ROMarr's own environment for
    the in-process search, and to the Hub CLI's through `_host_env`; plugin
    subprocesses never see it.
    """
    global _GITHUB_TOKEN
    value = str(token or "").strip()
    with _GITHUB_TOKEN_LOCK:
        _GITHUB_TOKEN = value
        if value:
            os.environ["ROM_HUB_GITHUB_TOKEN"] = value
        else:
            os.environ.pop("ROM_HUB_GITHUB_TOKEN", None)


def github_token_set() -> bool:
    with _GITHUB_TOKEN_LOCK:
        return bool(_GITHUB_TOKEN or os.environ.get("ROM_HUB_GITHUB_TOKEN", "").strip())


def flaresolverr_url() -> str:
    """Return the active FlareSolverr URL from settings or environment."""
    with _FLARESOLVERR_LOCK:
        configured = _FLARESOLVERR_URL
    return configured or os.environ.get("ROM_HUB_FLARESOLVERR_URL", "").strip().rstrip("/")


def _build_fetcher(HttpxFetcher, *, timeout: float | None = None, transport=None):
    """Construct HttpxFetcher, passing flaresolverr_url when supported."""
    kwargs = {"transport": transport}
    if timeout is not None:
        kwargs["timeout"] = timeout
    solver = flaresolverr_url()
    if solver:
        try:
            return HttpxFetcher(**kwargs, flaresolverr_url=solver)
        except TypeError:
            pass
    return HttpxFetcher(**kwargs)


def _wrap_download_transport(base_transport):
    """Wrap an httpx download transport with FlareSolverr when configured.

    For binary downloads, solving a Cloudflare challenge must retry the
    original request against the inner transport using the cached clearance
    cookie rather than returning the solver's HTML wrapper page as the file.
    """
    solver = flaresolverr_url()
    if not solver:
        return base_transport
    try:
        import httpx
        from rom_hub.flaresolverr import (
            FlareSolverrTransport,
            is_cloudflare_challenge,
            normalize_flaresolverr_url,
        )
        normalized = normalize_flaresolverr_url(solver)
        if not normalized:
            return base_transport

        class _BinaryDownloadSolverTransport(FlareSolverrTransport):
            def handle_request(self, request: httpx.Request) -> httpx.Response:
                if request.url.scheme != "https" or request.extensions.get("rom_hub_bypass_solver"):
                    return self._inner.handle_request(request)
                req = self._apply_cached(request)
                response = self._inner.handle_request(req)
                if request.method.upper() != "GET" or response.status_code not in (403, 503):
                    return response
                body = response.read()
                if not is_cloudflare_challenge(response.status_code, response.headers, body):
                    return httpx.Response(
                        status_code=response.status_code,
                        headers=response.headers,
                        content=body,
                        request=req,
                    )
                response.close()
                self._solve(str(request.url))
                return self._inner.handle_request(self._apply_cached(request))

        return _BinaryDownloadSolverTransport(normalized, inner=base_transport)
    except Exception:
        return base_transport

# ROM Hub keeps its state (catalog cache, installed plugins) under this home.
# ROMarr gives it a stable directory it owns so state survives restarts.
HOME = Path(os.environ.get("ROM_HUB_HOME", "/opt/romarr/.rom-hub"))
os.environ.setdefault("ROM_HUB_HOME", str(HOME))
# ROM_HUB_ALLOW_UNSANDBOXED is deliberately NOT set here. It used to be pinned
# to "1" at import time, before the Hub grew a seccomp sandbox -- which meant
# any in-process Hub call saw the flag and ran plugins with no confinement at
# all. `_plugin_env()` below is the only place that may opt out, and only when
# the sandbox genuinely cannot be installed. Asked for, never assumed.


def _backend_env() -> dict:
    """Share backend connection details; credentials require an explicit grant.

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
    if os.environ.get("ROMARR_PLUGIN_BACKEND_CREDENTIALS") == "1":
        if user:
            env["ROMM_USER"] = user
        if pw:
            env["ROMM_PASSWORD"] = pw
    elif url:
        # Embedded URL credentials are the same grant as a separate password.
        from urllib.parse import urlsplit, urlunsplit
        try:
            parts = urlsplit(url)
        except ValueError:
            env.pop("ROMM_URL", None)
        else:
            env["ROMM_URL"] = urlunsplit((parts.scheme, parts.netloc.rsplit("@", 1)[-1],
                                          parts.path, "", ""))
    return env


def _import():
    """Import the Hub lazily so ROMarr still starts if it isn't installed."""
    # Python caches each site-packages directory listing, so without this a
    # `pip install rom-hub` made while ROMarr is running stays invisible until
    # a restart -- the Hub tab just keeps saying it is not installed.
    import importlib
    importlib.invalidate_caches()
    from rom_hub import catalog_sources, registry  # noqa: WPS433
    return catalog_sources, registry


def available() -> bool:
    """Report whether this runtime can use the separately installed plugin host."""
    if os.environ.get("ROMARR_ANDROID") == "1":
        return False
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


def _uncatalogued(registry, installed: dict, seen: set) -> list[dict]:
    """Installed plugins no loaded catalogue lists, described by their manifests.

    A plugin ROMarr preinstalls, or one installed from a URL, can be newer
    than the catalogue a pinned ROM Hub ships. Without this it is installed,
    enabled and invisible: no card to configure or disable it from.
    """
    extra = []
    try:
        plugins = registry.Registry(HOME).installed()
    except Exception:
        return extra
    for plugin in plugins:
        slug = getattr(plugin, "slug", None)
        if not slug or slug in seen or slug not in installed:
            continue
        manifest = getattr(plugin, "manifest", None)
        extra.append({
            "slug": slug,
            "name": getattr(manifest, "name", "") or slug,
            "author": "",
            "version": getattr(manifest, "version", "") or "",
            "repository": "",
            "capabilities": sorted(getattr(manifest, "capabilities", None) or {}),
            "network": list(getattr(manifest, "network", None) or []),
            "description": "Installed; not listed in the plugin catalogue.",
            "key_required": False,
            "platforms": [],
            "installed": True,
            "enabled": installed[slug]["enabled"],
        })
    return extra


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
    seen = {item["slug"] for item in items}
    items.extend(_uncatalogued(registry, installed, seen))
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


def _host_env() -> dict:
    """Build the sanitized environment for trusted host helpers."""
    env = {name: os.environ[name] for name in _ENV_PASSTHROUGH
           if name in os.environ}
    env["ROM_HUB_HOME"] = str(HOME)
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env.update(_backend_env())
    token = os.environ.get("ROM_HUB_GITHUB_TOKEN", "").strip()
    if token:
        env["ROM_HUB_GITHUB_TOKEN"] = token
    return env


def _confinement_possible() -> bool:
    """Seccomp exists only on Linux; elsewhere there is nothing to install."""
    return sys.platform.startswith("linux")


def _sandbox_policy() -> bool:
    """Require confinement wherever it can exist, unless the operator opts out.

    On Linux a missing filter is a fixable deployment gap, so plugins are
    refused until it is fixed or explicitly waived. Windows and macOS have no
    seccomp at all; refusing there would only remove the plugin feature the
    desktop apps ship, so plugins run unconfined with a warning, as before.
    """
    ok, why = sandbox_state()
    if ok:
        return False
    if not _confinement_possible():
        log.warning("running plugins WITHOUT confinement: %s; seccomp is Linux-only, "
                    "so plugins on this system have no network or exec boundary", why)
        return True
    if os.environ.get("ROMARR_ALLOW_UNSANDBOXED_PLUGINS") != "1":
        raise RuntimeError(
            "ROM Hub plugin confinement is unavailable: " + why
            + ". Install Linux pyseccomp or explicitly set "
            "ROMARR_ALLOW_UNSANDBOXED_PLUGINS=1 to allow no confinement.")
    log.warning("running plugins WITHOUT confinement by explicit operator choice: %s", why)
    return True


def _plugin_secrets(plugin) -> dict:
    """The plugin's saved `secret` settings, resolved the way the Hub CLI does.

    ROM Hub keeps secrets out of `state.json` and hands them to a plugin
    only in its start-up frame. The CLI does that through `prepare_secrets`;
    ROMarr's own in-process starts (search, torrent resolve, download plan)
    did not, so a plugin whose key was saved -- Demozoo, CurseForge, Nexus
    Mods, GitHub Games, Mister Download Manager -- ran as if it had none.
    A store that cannot be read means no secrets, never a failed search.
    """
    try:
        from rom_hub.cli import prepare_secrets
    except Exception:  # noqa: BLE001 - an older Hub without secret support
        return {}
    try:
        return prepare_secrets(plugin, HOME)
    except Exception as exc:  # noqa: BLE001 - logged; the plugin runs keyless
        log.warning("could not read saved secrets for %s: %s",
                    getattr(plugin, "slug", "?"), type(exc).__name__)
        return {}


def _plugin_env() -> dict:
    """Apply the same fail-closed policy to the Hub CLI and worker bridge."""
    env = _host_env()
    if _sandbox_policy():
        env["ROM_HUB_ALLOW_UNSANDBOXED"] = "1"
    return env


def _run_cli(*args, timeout=180):
    """Run the Hub CLI in ROMarr's own interpreter and capture the result.

    Install/enable/disable are one-shot operations the Hub already implements;
    shelling out to its entry point is the honest way to reuse them without
    copying the registry-mutation code here.

    `_plugin_env()` refuses automatic fallback when confinement is unavailable.
    This CLI manages installed plugin state; search and fetch workers use the
    Hub's seccomp boundary and ROMarr's trusted interpreter startup adapter.
    Backend credentials are shared only after an explicit operator grant.
    """
    import subprocess
    import sys
    # The Hub ships a `rom-hub` console script next to the interpreter; it has no
    # `python -m rom_hub` entry point. Prefer the script; fall back to the CLI
    # module's main() if the script isn't on PATH.
    if os.environ.get("ROMARR_ANDROID") == "1" or sys.platform == "android":
        return {"ok": False, "out": "", "err": "ROM Hub subprocess plugins are not bundled with Android. Connect to a ROMarr server for plugin sources.", "code": -1}
    script = Path(sys.executable).with_name("rom-hub")
    if script.exists():
        cmd = [str(script), *args]
    else:
        cmd = [sys.executable, "-c",
               "import sys; from rom_hub.cli import main; sys.exit(main())", *args]
    try:
        env = _plugin_env()
    except RuntimeError as exc:
        return {"ok": False, "out": "", "err": str(exc), "code": -1}
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


def plugin_sources() -> dict:
    """slug -> {source, ref, commit} for every installed plugin.

    The registry's state.json records what each plugin was installed FROM
    (git URL or catalog slug) and the commit it resolved to, which is exactly
    what an update needs: re-run that install, and the Hub's own copy-first,
    swap-second staging makes it atomic.
    """
    out: dict[str, dict] = {}
    try:
        state = _json.loads((HOME / "state.json").read_text(encoding="utf-8"))
    except Exception:
        return out
    if not isinstance(state, dict):
        return out
    for slug, entry in state.items():
        if isinstance(entry, dict):
            out[slug] = {
                "source": str(entry.get("source") or ""),
                "ref": str(entry.get("ref") or "") or None,
                "commit": str(entry.get("commit") or ""),
            }
    return out


def update_plugins() -> dict:
    """Re-install every plugin from its recorded source, pulling the latest.

    A plugin pinned to a ref updates within that ref (a branch moves, a tag
    does not); a plugin installed from a catalog slug updates when its
    catalog does. Uses the Hub's own install path, so staging, state.json
    and the manifest check are the Hub's -- nothing here reaches around them.
    Returns per-plugin results; one plugin failing never stops the rest.
    """
    sources = plugin_sources()
    if not sources:
        return {"ok": True, "updated": [], "failed": {},
                "message": "no installed plugins"}
    updated, failed = [], {}
    for slug, info in sorted(sources.items()):
        if not info["source"]:
            failed[slug] = "no recorded source -- installed by hand?"
            continue
        args = ["plugin", "install", info["source"]]
        if info["ref"]:
            args += ["--ref", info["ref"]]
        result = _run_cli(*args)
        if result["ok"]:
            updated.append(slug)
        else:
            failed[slug] = (result["err"] or result["out"] or
                            "install failed")[:200]
    return {"ok": not failed, "updated": updated, "failed": failed,
            "message": f"updated {len(updated)}, failed {len(failed)}"}


# ------------------------------------------------------------- search/fetch --
#
# The two in-process bridges the acquisition pipeline needs: `search` asks
# every enabled plugin that declares the capability, and `fetch` downloads one
# chosen result into a staging directory through the plugin's own gated plan.
# Both go through the Hub's own machinery -- PluginProcess, FetchPlan
# allowlisting, HttpDownloader -- so the confinement and network policy a
# plugin runs under are the Hub's, not a copy of them here.

import base64
import json as _json

HUB_SCHEME = "hub://"


def encode_result(row: dict) -> str:
    """A search result as a grabbable URL.

    The download pipeline speaks URLs: `Release.download_url` is what
    `hand_off` gives a client. A hub result is not a URL -- it is a
    (plugin, source_id) pair the plugin can turn back into a FetchPlan -- so
    the whole row rides along, base64-encoded. Opaque by design: nothing
    downstream parses it except `fetch`, and a credential-free encoding keeps
    it safe to store in the ledger and the queue.
    """
    payload = base64.urlsafe_b64encode(
        _json.dumps(row, sort_keys=True).encode("utf-8")).decode("ascii")
    return HUB_SCHEME + payload


def decode_result(url: str) -> dict:
    """The search result inside a `hub://` URL. Raises ValueError if it is
    not one or does not decode -- a corrupt ledger row must fail loudly, not
    silently fetch nothing."""
    if not str(url or "").startswith(HUB_SCHEME):
        raise ValueError("not a hub:// URL")
    try:
        raw = base64.urlsafe_b64decode(
            url[len(HUB_SCHEME):].encode("ascii"))
        row = _json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise ValueError(f"undecodable hub URL: {exc}") from exc
    if not isinstance(row, dict) or not row.get("source_id"):
        raise ValueError("hub URL carries no source_id")
    return row


def _unsandboxed() -> bool:
    """Apply explicit policy and trusted startup before the Hub launches workers."""
    allow = _sandbox_policy()
    from .plugin_safety import install_trusted_startup
    install_trusted_startup()
    return allow


def archive_org_auth_status() -> dict:
    """Expose configuration status, never credentials, to ROMarr's UI."""
    try:
        from .ia_auth import status
    except ImportError:
        return {"configured": False, "external": False,
                "error": "Install the pinned ROM Hub version for Archive.org authentication"}
    return status(HOME)


def archive_org_auth_configure(email="", password="", clear=False) -> dict:
    """UI operation: store in ROM Hub's secret store, NOT in plugin config."""
    if not clear:
        try:
            from rom_hub.registry import Registry
            Registry(HOME).get("archive-org")
        except (ImportError, LookupError, OSError, ValueError):
            return {"ok": False, "error": "Install the Archive.org plugin first"}
    try:
        from .ia_auth import configure
    except ImportError:
        return {"ok": False,
                "error": "Install the pinned ROM Hub version for Archive.org authentication"}
    return configure(HOME, email=email, password=password, clear=clear)


#: Plugins whose search results are not game files: guides, video
#: tutorials, manuals, mods, artwork, cheats, save tools, BIOS and emulator
#: listings, and source repositories. Their rows are useful on their own
#: pages but are never a release for a game request -- a "Super Metroid
#: review" video must not be grabbed and imported as Super Metroid.
NON_GAME_PLUGINS = frozenset({
    "youtube-tutorials", "strategywiki", "gamefaqs", "game-manuals",
    "moddb", "gamebanana", "nexusmods", "modrinth", "curseforge", "thunderstore",
    "github-games", "ludusavi", "retroarch-autoconfig", "libretro-cheats",
    "libretro-thumbnails", "libretro-overlays", "libretro-database", "openvgdb",
    "hasheous", "open-bios", "emulators", "libretro-cores", "retroachievements",
    "compressatorium", "mdm",
})

#: Plugins whose catalogue only covers specific platforms. When an operator
#: searches for a specific system outside a plugin's scope (e.g. searching for
#: a Switch or SNES game while `demozoo`, `aminet` or `worldofspectrum` is
#: enabled), skipping incompatible plugins avoids waiting on slow remote APIs
#: whose results could never match the requested platform.
PLATFORM_SCOPED_PLUGINS: dict[str, frozenset[str]] = {
    "demozoo": frozenset({
        "dos", "amiga", "c64", "zxs", "zx81", "sinclair-zx81", "atari-st",
        "cpc", "msx", "msx2", "vic-20", "plus4", "atari800", "atari2600",
        "apple2", "appleii", "win", "linux",
    }),
    "aminet": frozenset({"amiga", "cd32"}),
    "whdload": frozenset({"amiga", "cd32"}),
    "lemon64": frozenset({"c64"}),
    "csdb": frozenset({"c64", "vic-20", "plus4"}),
    "worldofspectrum": frozenset({"zxs", "zx81", "sinclair-zx81"}),
    "zxart": frozenset({"zxs", "zx81", "sinclair-zx81"}),
    "famicom-world": frozenset({"nes", "fds"}),
    "snes-central": frozenset({"snes"}),
    "smwcentral": frozenset({"snes", "n64"}),
    "exodos": frozenset({"dos", "win"}),
    "repacks": frozenset({"win", "pc", "windows", "dos", "linux"}),
    "scummvm-freeware": frozenset({"dos", "win", "amiga", "atari-st", "mac"}),
}


def _plugin_supports_platform(plugin, platform: str | None) -> bool:
    """Whether `plugin` can return game releases for `platform` when `only` is None."""
    slug = str(getattr(plugin, "slug", "") or "")
    if slug in NON_GAME_PLUGINS:
        return False
    if not platform:
        return True
    resolved = resolve(platform)
    candidates = {str(platform).lower(), str(_search_platform(platform) or "").lower()}
    if resolved is not None:
        candidates.add(resolved.slug.lower())
    candidates.discard("")
    scoped = PLATFORM_SCOPED_PLUGINS.get(slug)
    if scoped is not None and not (candidates & scoped):
        return False
    manifest_plats = getattr(getattr(plugin, "manifest", None), "platforms", None) or ()
    if manifest_plats:
        declared = {str(p).strip().lower() for p in manifest_plats if str(p).strip()}
        if declared and not (declared & {"*", "all", "any"}) and not (candidates & declared):
            return False
    return True


def searchable_plugins() -> list[str]:
    """Slugs of installed, enabled plugins that declare `search`."""
    try:
        _, registry = _import()
        reg = registry.Registry(HOME)
        return sorted(p.slug for p in reg.installed()
                      if getattr(p, "enabled", True)
                      and "search" in (getattr(p.manifest, "capabilities", ())
                                       or ()))
    except Exception:
        return []


def _search_platform(platform: str | None) -> str | None:
    """Translate ROMarr IDs/labels to the shared ROM Hub source vocabulary.

    Archive.org, No-Intro and other Hub sources use RomM identifiers for
    these machines. Keep this conversion at the plugin boundary: persisted
    ROMarr IDs and collection-path filtering must retain their own identity.
    Unknown plugin-specific IDs pass through without broadening the search.
    """
    resolved = resolve(platform) if platform else None
    slug = resolved.slug if resolved else platform
    return {
        "genesis-slash-megadrive": "genesis",
        "turbografx16--1": "tg16",
        "odyssey-2-slash-videopac-g7000": "odyssey-2",
        "sinclair-zx81": "zx81",
        "apple2": "appleii",
    }.get(slug, slug)


def search(query: str, platform: str | None = None, limit: int = 25,
           timeout: float = 90.0, only: str | tuple[str, ...] | None = None) -> tuple[list[dict], str]:
    """Search every enabled ROM Hub plugin, merged.

    Returns `(rows, error)`. Rows are plain dicts with the Hub's SearchResult
    fields (source_id, title, platform, size_bytes, url, plugin). `only`
    limits the fan-out to those plugin slugs (one plugin's own search box).
    `error` is
    a string when the Hub itself could not be used at all; a single plugin
    failing is not an error -- the Hub's fan-out already isolates those, and
    "two of three sources answered" is a normal search, not a fault.
    """
    try:
        from rom_hub import registry as _registry
        from rom_hub.broker.fetcher import HttpxFetcher
        from rom_hub.dispatcher import search_all
    except Exception as exc:
        return [], f"ROM Hub is not installed: {exc}"
    try:
        plugins = [p for p in _registry.Registry(HOME).installed()
                   if getattr(p, "enabled", True)
                   and (only is None or (p.slug == only if isinstance(only, str) else p.slug in only))
                   and (only is not None or _plugin_supports_platform(p, platform))
                   and "search" in (getattr(p.manifest, "capabilities", ())
                                    or ())]
    except Exception as exc:
        return [], f"could not read the plugin registry: {exc}"
    if not plugins:
        return [], ""
    # The fetcher gets the same budget as the call. Its own default (30s) is
    # shorter than some sources need -- MiNERVA measured 21-25s per query
    # live, and a ReadTimeout inside the plugin surfaced as a plugin failure
    # rather than as a slow source. One timeout to reason about, not two.
    try:
        from .ia_session import archive_transport
        from .ia_auth import status as ia_status
    except ImportError:
        ia = {"configured": False, "external": False, "error": ""}
        archive_transport = lambda *a, **k: None
    else:
        ia = ia_status(HOME)
    # ROM Hub shares a fetcher across all plugins in a search fanout.
    # Split only when IA auth is configured, so another plugin cannot use
    # the Archive.org cookies through an otherwise allowed archive.org URL.
    auth_error = ""
    if ia["error"]:
        # Never silently downgrade a configured but invalid account to
        # anonymous access. Other plugins should still return their results.
        auth_error = "Archive.org authentication: " + ia["error"]
        plugins = [p for p in plugins if p.slug != "archive-org"]
    if ia["configured"]:
        batches = [
            ([p for p in plugins if p.slug != "archive-org"], None),
            ([p for p in plugins if p.slug == "archive-org"], "archive-org"),
        ]
    else:
        batches = [(plugins, None)]
    call_limit = 100 if platform else limit
    rows = []
    for group, auth_slug in batches:
        if not group:
            continue
        try:
            transport = archive_transport(auth_slug, HOME) if auth_slug else None
            fetcher = _build_fetcher(HttpxFetcher, timeout=max(60.0, timeout), transport=transport)
        except Exception as exc:
            auth_error = f"Archive.org authentication setup failed: {type(exc).__name__}: {exc}"
            continue
        try:
            outcome = search_all(group, fetcher=fetcher, query=query,
                                 secrets_for=_plugin_secrets,
                                 platform=_search_platform(platform), limit=call_limit, timeout=timeout,
                                 allow_unsandboxed=_unsandboxed())
        except Exception as exc:
            auth_error = f"ROM Hub search failed: {type(exc).__name__}: {exc}"
            continue
        finally:
            fetcher.close()
        for source in getattr(outcome, "statuses", []):
            if source.slug == "archive-org" and not source.ok:
                auth_error = f"Archive.org search failed: {source.error}"
            elif only is not None and source.slug == only and not source.ok:
                # Asked about one plugin, its failure is the answer -- not
                # an empty result list that reads as "the site has nothing".
                auth_error = f"{only} search failed: {source.error}"
        for r in outcome.results:
            try:
                rows.append(r.model_dump())
            except Exception:  # pragma: no cover -- pydantic model, always dumps
                rows.append(dict(r))
    resolved_plat = resolve(platform) if platform else None

    # Group by plugin to cap at limit and filter collection paths (#118)
    by_plugin: dict[str, list[dict]] = {}
    for row in rows:
        plugin_slug = str(row.get("plugin") or "")
        extra = row.get("extra") if isinstance(row.get("extra"), dict) else {}
        coll_path = extra.get("path")
        if resolved_plat and coll_path:
            parts = [p.strip() for p in re.split(r"[\/]+", str(coll_path)) if p.strip() and p != "."]
            folder_parts = parts[:-1] if len(parts) > 1 else parts
            found_machine = None
            for folder in reversed(folder_parts):
                if folder.lower() == "nintendo":
                    continue
                p = resolve(folder)
                if p is not None:
                    found_machine = p
                    break
            if found_machine is not None:
                if not same_machine(found_machine.slug, resolved_plat.slug):
                    continue  # names another machine -> drop
            else:
                title = str(row.get("title") or "").lower()
                has_ext = any(title.endswith(ext) for ext in resolved_plat.extensions)
                if not has_ext:
                    continue  # names no machine and title has no platform extension -> drop

        by_plugin.setdefault(plugin_slug, []).append(row)

    rows = []
    for plugin_slug, p_rows in by_plugin.items():
        rows.extend(p_rows[:limit])
    return rows, auth_error


def resolve_torrents(rows: list[dict]) -> list[dict]:
    """Resolve plugin torrent rows through the plugin's validated capability.

    Rows are grouped by plugin so one sandboxed subprocess handles a batch.
    Legacy plugins whose source id is itself a magnet keep their DHT-only
    fallback if resolution fails. Collection rows keep the magnet in extra
    metadata and fail closed when the plugin cannot validate it, so an
    unapproved magnet never reaches qBittorrent.
    """
    if not rows:
        return rows
    try:
        from rom_hub import registry as _registry
        from rom_hub.broker.fetcher import HttpxFetcher
        from rom_hub.broker.host import PluginProcess
        from rom_hub.types import SearchResult
    except Exception as exc:
        log.warning("torrent resolution unavailable: %s", exc)
        return rows

    def raw_magnet(row: dict) -> str:
        source_id = str(row.get("source_id") or "")
        if source_id.startswith("magnet:"):
            return source_id
        extra = row.get("extra")
        return (str(extra.get("torrent_magnet") or "")
                if isinstance(extra, dict) else "")

    by_plugin: dict[str, list[dict]] = {}
    for row in rows:
        if raw_magnet(row).startswith("magnet:"):
            by_plugin.setdefault(str(row.get("plugin") or ""), []).append(row)
    if not by_plugin:
        return rows

    fetcher = HttpxFetcher(timeout=90.0)
    try:
        for slug, plugin_rows in by_plugin.items():
            try:
                plugin = _registry.Registry(HOME).get(slug)
            except Exception as exc:
                log.warning("cannot resolve torrents for %r: %s", slug, exc)
                continue
            if not getattr(plugin, "enabled", True):
                continue
            manifest = getattr(plugin, "manifest", None)
            if "torrent" not in (getattr(manifest, "capabilities", None) or {}):
                # No torrent capability: these rows stay on the hub:// lane.
                continue
            try:
                with PluginProcess(
                        plugin_dir=plugin.path, manifest=manifest,
                        config=getattr(plugin, "config", {}) or {},
                        fetcher=fetcher, timeout=90.0,
                        secrets=_plugin_secrets(plugin),
                        allow_unsandboxed=_unsandboxed()) as proc:
                    for row in plugin_rows:
                        raw = raw_magnet(row)
                        try:
                            result = SearchResult(**{
                                k: v for k, v in row.items() if k != "magnet"})
                            source = proc.resolve_torrent(result)
                            magnet = str(getattr(source, "source", "") or "")
                            # Only a magnet kind belongs here; a torrent_url
                            # would need the fetch lane, not the client.
                            if (magnet.startswith("magnet:")
                                    and getattr(source, "kind", "") == "magnet"):
                                row["magnet"] = magnet
                        except Exception as exc:
                            log.warning(
                                "%s could not resolve its torrent for %r: %s",
                                slug, row.get("title", "")[:60], exc)
                            # Old plugins use the source_id itself as a bare
                            # magnet. New collection rows keep the magnet in
                            # extra and must not bypass the host's allowlist.
                            if str(row.get("source_id") or "").startswith("magnet:"):
                                row.setdefault("magnet", raw)
            except Exception as exc:
                log.warning("torrent plugin %r failed to run: %s", slug, exc)
                for row in plugin_rows:
                    source_id = str(row.get("source_id") or "")
                    if source_id.startswith("magnet:"):
                        row.setdefault("magnet", source_id)
    finally:
        fetcher.close()
    return rows


def fetch(row: dict, dest_dir) -> dict:
    """Download one search result into `dest_dir` through its plugin.

    The plugin is asked for a FetchPlan -- the Hub re-validates the shape and
    allowlists every URL against the manifest's declared hosts -- and the
    Hub's own downloader streams the files. This is the download half of the
    Hub's `import`; the upload half deliberately does not run here, because
    ROMarr's import pipeline (DAT verification, naming, history, blocklist)
    owns what happens to a finished file.
    """
    from pathlib import Path
    try:
        from rom_hub import registry as _registry
        from rom_hub.broker.fetcher import HttpxFetcher
        from rom_hub.broker.host import PluginProcess
        from rom_hub.importer import HttpDownloader
        from rom_hub.types import SearchResult
    except Exception as exc:
        return {"ok": False, "error": f"ROM Hub is not installed: {exc}",
                "files": []}
    slug = str(row.get("plugin") or "")
    try:
        result = SearchResult(**row)
    except Exception as exc:
        return {"ok": False, "error": f"invalid hub result: {exc}", "files": []}
    try:
        plugin = _registry.Registry(HOME).get(slug)
    except Exception as exc:
        return {"ok": False,
                "error": f"plugin {slug!r} is not installed: {exc}",
                "files": []}
    if not getattr(plugin, "enabled", True):
        return {"ok": False,
                "error": f"plugin {slug!r} is disabled", "files": []}
    dest = Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)
    try:
        from .ia_session import archive_transport
    except ImportError:
        archive_transport = lambda *a, **k: None
    try:
        fetcher = _build_fetcher(HttpxFetcher, transport=archive_transport(slug, HOME))
    except Exception as exc:
        return {"ok": False,
                "error": f"Archive.org authentication setup failed: {type(exc).__name__}: {exc}",
                "files": []}
    try:
        dl_transport = _wrap_download_transport(archive_transport(slug, HOME))
        with PluginProcess(plugin_dir=plugin.path, manifest=plugin.manifest,
                           config=getattr(plugin, "config", {}) or {},
                           fetcher=fetcher,
                           secrets=_plugin_secrets(plugin),
                           allow_unsandboxed=_unsandboxed()) as proc:
            plan = proc.plan(result)
            allowlist = getattr(proc, "network_policy",
                                list(getattr(plugin.manifest, "network", []) or []))
            with HttpDownloader(allowlist=allowlist, transport=dl_transport) as downloader:
                files = []
                for f in plan.files:
                    dl_kwargs = {"headers": f.headers} if getattr(f, "headers", None) else {}
                    saved = downloader.download(f.url,
                                                dest / f.relative_path(),
                                                f.size_bytes,
                                                **dl_kwargs)
                    files.append(str(saved))
    except Exception as exc:
        return {"ok": False,
                "error": f"{type(exc).__name__}: {exc}", "files": []}
    finally:
        fetcher.close()
    return {"ok": True, "error": "", "files": files}


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


# --------------------------------------------------------- Igir v5.8.0 1G1R --

IGIR_VERSION = "5.8.0"
IGIR_RELEASE_URL = "https://github.com/emmercm/igir/releases/tag/v5.8.0"

DEFAULT_IGIR_PROFILE = {
    "commands": ["copy", "extract", "test", "clean", "report"],
    "input_dirs": ["/downloads/rom-hub"],
    "output_dir": "/library/roms/{romm}",
    "dat_dirs": ["/config/dats"],
    "single_1g1r": True,
    "prefer_verified": True,
    "prefer_good": True,
    "prefer_regions": ["USA", "WORLD", "EUR", "JPN"],
    "prefer_languages": ["EN", "JA"],
    "no_demos": True,
    "no_prototypes": True,
    "no_betas": True,
    "dry_run": True,
}


def igir_status() -> dict:
    """Detect whether the igir binary (v5.8.0+) is on PATH and return its status."""
    import shutil
    import subprocess
    binary = shutil.which("igir")
    cmd_preview = " ".join(build_igir_command(DEFAULT_IGIR_PROFILE))
    if not binary:
        return {
            "available": False,
            "version": "",
            "target_version": IGIR_VERSION,
            "release_url": IGIR_RELEASE_URL,
            "install_cmd": f"npm install -g igir@{IGIR_VERSION}",
            "default_profile": DEFAULT_IGIR_PROFILE,
            "command_preview": cmd_preview,
        }
    try:
        proc = subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=10)
        ver = (proc.stdout or proc.stderr or "").strip()
    except Exception:
        ver = "installed"
    return {
        "available": True,
        "binary": binary,
        "version": ver,
        "target_version": IGIR_VERSION,
        "release_url": IGIR_RELEASE_URL,
        "install_cmd": f"npm install -g igir@{IGIR_VERSION}",
        "default_profile": DEFAULT_IGIR_PROFILE,
        "command_preview": cmd_preview,
    }


def build_igir_command(cfg: dict | None = None) -> list[str]:
    """Build an Igir v5.8.0 1G1R command line using ROMarr's region/verification basis."""
    merged = dict(DEFAULT_IGIR_PROFILE)
    if isinstance(cfg, dict):
        merged.update({k: v for k, v in cfg.items() if v is not None})

    allowed_cmds = ("copy", "move", "link", "extract", "zip", "test", "dir2dat", "fixdat", "clean", "report")
    cmds = [c for c in (merged.get("commands") or ["copy", "test", "report"]) if c in allowed_cmds]
    if not cmds:
        cmds = ["test", "report"]
    if merged.get("dry_run"):
        cmds = [c for c in cmds if c in ("test", "report")] or ["test", "report"]

    args = ["igir", *cmds]
    for d in merged.get("dat_dirs") or []:
        if str(d).strip():
            args.extend(["--dat", str(d).strip()])
    for i in merged.get("input_dirs") or []:
        if str(i).strip():
            args.extend(["--input", str(i).strip()])

    out_dir = str(merged.get("output_dir") or "/library/roms/{romm}").strip()
    args.extend(["--output", out_dir])

    if merged.get("single_1g1r", True):
        args.append("--single")
    if merged.get("prefer_verified", True):
        args.append("--prefer-verified")
    if merged.get("prefer_good", True):
        args.append("--prefer-good")

    regions = [str(r).strip().upper() for r in (merged.get("prefer_regions") or []) if str(r).strip()]
    if regions:
        args.extend(["--prefer-region", ",".join(regions)])

    langs = [str(l).strip().upper() for l in (merged.get("prefer_languages") or []) if str(l).strip()]
    if langs:
        args.extend(["--prefer-language", ",".join(langs)])

    if merged.get("no_demos", True):
        args.append("--no-demos")
    if merged.get("no_prototypes", True):
        args.append("--no-prototypes")
    if merged.get("no_betas", True):
        args.append("--no-betas")

    return args


def run_igir(cfg: dict | None = None, timeout: int = 600) -> dict:
    """Execute Igir v5.8.0 or return the planned 1G1R command line."""
    import shlex
    import subprocess
    cmd = build_igir_command(cfg)
    cmd_str = " ".join(shlex.quote(part) for part in cmd)
    if isinstance(cfg, dict) and cfg.get("preview_only"):
        return {"ok": True, "command": cmd_str, "out": f"Planned 1G1R command: {cmd_str}", "err": ""}
    status = igir_status()
    if not status["available"]:
        return {
            "ok": False,
            "command": cmd_str,
            "error": f"igir binary not found on PATH. Install with: {status['install_cmd']}",
            "out": "",
        }
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=_host_env())
        return {
            "ok": proc.returncode == 0,
            "command": cmd_str,
            "out": (proc.stdout or "")[-4000:],
            "err": (proc.stderr or "")[-2000:],
            "code": proc.returncode,
        }
    except subprocess.TimeoutExpired:
        return {"ok": False, "command": cmd_str, "error": "igir timed out", "out": ""}
    except Exception as exc:
        return {"ok": False, "command": cmd_str, "error": str(exc), "out": ""}


# --------------------------------------------------- Devices (fleet) API --
#
# Two different kinds of device, and the page treats them differently:
#
#   kind "files"  a device whose frontend reads a ROM folder (ES-DE, RetroPie,
#                 Batocera, EmuDeck, Android handhelds). ROMarr plans an Igir
#                 copy of verified games into that folder, laid out with the
#                 frontend's own platform folder names (the Igir token), and,
#                 when the device is remote, the rsync/rclone step to get there.
#   kind "app"    a device running an app that talks to your library server
#                 (Argosy, Grout, DeckRommSync, RetroArch cloud sync, console
#                 feeds). Nothing is copied; the plan is the setup steps.
#
# Plans are non-destructive unless the caller asks for a mirror: no Igir
# `clean`, no `rsync --delete`, `rclone copy` rather than `rclone sync`. A
# device folder usually holds games and saves that are not in the library.

DEVICE_PROFILES: list[dict] = [
    {
        "id": "es-de",
        "kind": "files",
        "name": "ES-DE (EmulationStation Desktop Edition)",
        "category": "Desktop and handheld frontend",
        "protocol": "Folder, SMB/NFS share or SSH",
        "default_path": "~/ROMs",
        "default_ssh_host": "user@device.local",
        "gamelist_format": "esde",
        "igir_token": "{es}",
        "description": "ES-DE on Windows, macOS, Linux or Android. Games are copied into its ROMs folder with ES-DE's platform folder names.",
    },
    {
        "id": "retropie-arcadepunks",
        "kind": "files",
        "name": "RetroPie (Raspberry Pi, including ArcadePunks RetroPie images)",
        "category": "Raspberry Pi and arcade cabinets",
        "protocol": "SSH (rsync) or the \\\\retropie\\roms share",
        "default_path": "/home/pi/RetroPie/roms",
        "default_ssh_host": "pi@retropie.local",
        "gamelist_format": "esde",
        "igir_token": "{es}",
        "description": "RetroPie uses EmulationStation folder names under /home/pi/RetroPie/roms.",
    },
    {
        "id": "rpi-batocera-arcadepunks",
        "kind": "files",
        "name": "Batocera (including Arcade Punks Batocera images)",
        "category": "Raspberry Pi, PC and arcade cabinets",
        "protocol": "SSH (rsync) or the \\\\batocera\\share\\roms share",
        "default_path": "/userdata/roms",
        "default_ssh_host": "root@batocera.local",
        "gamelist_format": "esde",
        "igir_token": "{batocera}",
        "description": "Batocera 40+ and Arcade Punks Batocera packs keep ROMs under /userdata/roms with Batocera's folder names.",
    },
    {
        "id": "steamos-deck",
        "kind": "files",
        "name": "Steam Deck / SteamOS (EmuDeck or RetroDECK)",
        "category": "Handheld PC",
        "protocol": "Folder, SD card or SSH",
        "default_path": "~/Emulation/roms",
        "default_ssh_host": "deck@steamdeck.local",
        "gamelist_format": "esde",
        "igir_token": "{es}",
        "description": "EmuDeck keeps ROMs in ~/Emulation/roms (or the same path on the SD card) with ES-DE folder names.",
    },
    {
        "id": "adreno-android-handhelds",
        "kind": "files",
        "name": "Android handhelds — Snapdragon / Adreno (Odin 2, Retroid Pocket, AYN, Ayaneo)",
        "category": "Android handheld",
        "protocol": "microSD card, MTP or SSH (Termux)",
        "default_path": "/storage/emulated/0/ROMs",
        "default_ssh_host": "user@android.local",
        "gamelist_format": "esde",
        "igir_token": "{romm}",
        "description": "For ES-DE Android, Daijisho or Beacon reading a ROMs folder on internal storage or the SD card.",
    },
    {
        "id": "argosy",
        "kind": "app",
        "name": "Argosy Launcher (Android)",
        "category": "App that connects to RomM",
        "protocol": "RomM API",
        "default_path": "",
        "gamelist_format": "romm-api",
        "igir_token": "{romm}",
        "description": "Android launcher that browses and launches your RomM library.",
        "setup": [
            "Install Argosy from its GitHub releases on the Android device.",
            "Add a server and enter your RomM address: {library_url}.",
            "Sign in with a RomM account (a dedicated one is best).",
        ],
    },
    {
        "id": "grout",
        "kind": "app",
        "name": "Grout (muOS / NextUI handhelds)",
        "category": "App that connects to RomM",
        "protocol": "RomM API",
        "default_path": "",
        "gamelist_format": "romm-api",
        "igir_token": "{romm}",
        "description": "Companion for Anbernic, Miyoo and TrimUI handhelds running muOS or NextUI; downloads games from RomM on the device.",
        "setup": [
            "Install Grout on the handheld following its README.",
            "Point it at your RomM address: {library_url}.",
            "Pick platforms on the device; Grout downloads into its own ROM folders.",
        ],
    },
    {
        "id": "deckrommsync",
        "kind": "app",
        "name": "DeckRommSync (Steam Deck)",
        "category": "App that connects to RomM",
        "protocol": "RomM API",
        "default_path": "",
        "gamelist_format": "romm-api",
        "igir_token": "{romm}",
        "description": "Keeps a Steam Deck's EmuDeck library and saves in step with RomM.",
        "setup": [
            "Install DeckRommSync on the Steam Deck (desktop mode).",
            "Enter your RomM address: {library_url}.",
            "Choose which platforms to keep on the Deck.",
        ],
    },
    {
        "id": "retroarch-webdav",
        "kind": "app",
        "name": "RetroArch cloud sync (WebDAV)",
        "category": "Saves and states",
        "protocol": "WebDAV",
        "default_path": "",
        "gamelist_format": "webdav",
        "igir_token": "{romm}",
        "description": "RetroArch's built-in cloud sync keeps saves and states the same on every device.",
        "setup": [
            "In RetroArch: Settings > Saving > Cloud Sync, turn it on and choose WebDAV.",
            "Enter your library server's WebDAV address: {library_url}/webdav/.",
            "Use the same account on every device.",
        ],
    },
    {
        "id": "console-cfw-feeds",
        "kind": "app",
        "name": "Console homebrew feeds (Tinfoil, pkgj, pkgi, Kekatsu)",
        "category": "Console installers",
        "protocol": "HTTP feeds",
        "default_path": "",
        "gamelist_format": "feed",
        "igir_token": "{romm}",
        "description": "Install straight onto a modded Switch, PS Vita, PSP, PS3 or DS from your RomM feeds.",
        "setup": [
            "On the console's installer (Tinfoil, pkgj, pkgi, Kekatsu), add a custom source.",
            "Use your RomM feed address: {library_url}/api/feeds/.",
        ],
    },
]

TRANSPORTS = ("local", "ssh", "rclone")
STAGE_ROOT = "/tmp/romarr-stage"


def _device(device_id: str) -> dict | None:
    return next((p for p in DEVICE_PROFILES if p["id"] == device_id), None)


def _device_root(path: str) -> str:
    """The device's ROM folder without a per-platform placeholder.

    Earlier plans asked for `.../roms/{platform}`, which Igir does not expand
    (it would create a folder literally named `{platform}`). The platform
    folder is the profile's Igir token, appended by the plan.
    """
    root = str(path or "").strip().rstrip("/")
    for placeholder in ("{platform}", "{romm}", "{es}", "{batocera}"):
        if root.endswith("/" + placeholder):
            root = root[: -len(placeholder) - 1]
    return root


def fleet_profiles() -> dict:
    """Every supported device, grouped by kind, and whether Igir is installed."""
    return {
        "profiles": DEVICE_PROFILES,
        "kinds": {
            "files": "Copy games onto the device's ROM folder",
            "app": "The device runs an app that connects to your library server",
        },
        "igir": igir_status(),
    }


def build_fleet_sync_plan(spec: dict | None = None) -> dict:
    """Plan getting games onto one device: commands for folders, steps for apps.

    Nothing runs here. The plan is shown to the user, who previews it and runs
    it on the machine that holds the library.
    """
    import shlex
    spec = spec if isinstance(spec, dict) else {}
    device_id = str(spec.get("device_id") or "es-de").strip()
    profile = _device(device_id)
    if profile is None:
        return {"ok": False, "error": f"unknown device {device_id!r}"}

    if profile["kind"] == "app":
        library_url = str(spec.get("library_url") or "").strip().rstrip("/") or "your RomM address"
        return {
            "ok": True,
            "kind": "app",
            "device": profile,
            "setup": [step.replace("{library_url}", library_url) for step in profile["setup"]],
            "steps": [],
            "igir_command": "",
            "network_sync_command": "",
            "gamelist_export_url": "",
        }

    transport = str(spec.get("transport") or "local").strip().lower()
    if transport not in TRANSPORTS:
        transport = "local"
    mirror = bool(spec.get("mirror", False))
    target_root = _device_root(spec.get("target_path") or profile["default_path"])
    source = (str(spec.get("source_path") or "").strip()
              or os.environ.get("LIBRARY_PATH") or "/roms")
    regions = [str(r).strip().upper()
               for r in (spec.get("prefer_regions") or ["USA", "WORLD", "EUR", "JPN"])
               if str(r).strip()]
    ssh_host = str(spec.get("ssh_host") or profile.get("default_ssh_host") or "user@device.local").strip()
    token = profile["igir_token"]
    stage = f"{STAGE_ROOT}/{device_id}"
    output = f"{target_root}/{token}" if transport == "local" else f"{stage}/{token}"

    commands = ["copy", "extract", "test", "report"]
    if mirror and transport == "local":
        commands.insert(3, "clean")
    igir_cfg = {
        "input_dirs": [source],
        "output_dir": output,
        "dat_dirs": [str(spec.get("dat_dir") or "/config/dats")],
        "single_1g1r": bool(spec.get("single_1g1r", True)),
        "prefer_verified": True,
        "prefer_good": True,
        "prefer_regions": regions,
        "dry_run": bool(spec.get("dry_run", False)),
    }

    def shell_arg(arg: str) -> str:
        # Keep a leading ~/ outside the quotes, or the shell never expands it
        # and Igir writes into a folder literally named "~".
        if arg.startswith("~/"):
            return "~/" + shlex.quote(arg[2:])
        return shlex.quote(arg)

    def quoted(cmd: list[str]) -> str:
        return " ".join(shell_arg(x) for x in cmd)

    preview = quoted(build_igir_command({**igir_cfg, "commands": ["test", "report"], "dry_run": True}))
    copy = quoted(build_igir_command({**igir_cfg, "commands": commands}))

    transfer = ""
    if transport == "ssh":
        delete = " --delete-after" if mirror else ""
        # rsync resolves a relative remote path from the login's home folder.
        remote = target_root[2:] if target_root.startswith("~/") else target_root
        transfer = (f"rsync -avz --progress{delete} {shlex.quote(stage + '/')} "
                    f"{shlex.quote(ssh_host + ':' + remote + '/')}")
    elif transport == "rclone":
        verb = "sync" if mirror else "copy"
        transfer = f"rclone {verb} {shlex.quote(stage + '/')} {shlex.quote(target_root + '/')} --progress"

    gamelist = (f"/api/v1/frontend/export?format={profile['gamelist_format']}"
                if profile["gamelist_format"] in ("esde", "launchbox", "playnite") else "")

    steps = [
        {"title": "Preview: check what would be copied (changes nothing)", "command": preview},
        {"title": ("Copy verified games into the device folder" if transport == "local"
                   else "Copy verified games into a staging folder"), "command": copy},
    ]
    if transfer:
        steps.append({"title": "Send them to the device", "command": transfer})
    if gamelist:
        steps.append({"title": "Download the frontend's game list", "url": gamelist})

    warnings = []
    if mirror:
        warnings.append("Mirror is on: files on the device that are not in this set will be deleted.")

    return {
        "ok": True,
        "kind": "files",
        "device": profile,
        "transport": transport,
        "mirror": mirror,
        "target_path": target_root,
        "output_path": output,
        "steps": steps,
        "warnings": warnings,
        "preview_command": preview,
        "igir_command": copy,
        "network_sync_command": transfer,
        "gamelist_export_url": gamelist,
    }


def mcp_manifest(base_url: str = "<YOUR_ROMARR_URL>") -> dict:
    """Describe the real MCP endpoint with placeholder-only owner connection examples."""
    from . import mcp
    endpoint = base_url.rstrip("/") + mcp.PATH
    return {"name": "romarr-mcp", "version": "1.0.0", "base_url": base_url,
            "endpoint": endpoint, "transport": "streamable-http", "stateless": True,
            "protocol_versions": list(mcp.VERSIONS), "tools": mcp.tools(),
            "writes": "Disabled unless ROMARR_MCP_WRITE=1; never enabled on Android",
            "mcp_client_config": {"mcpServers": {"romarr": {
                "type": "http", "url": endpoint,
                "headers": {"X-Api-Key": "<YOUR_ROMARR_API_KEY>"}}}},
            "stdio_config": {"mcpServers": {"romarr": {
                "command": "python", "args": ["-m", "romarr.mcp"],
                "env": {"ROMARR_URL": base_url, "ROMARR_API_KEY": "<YOUR_ROMARR_API_KEY>"}}}},
            "optional_desktop_control": {"name": "Open-Eyes", "required": False,
                "repository": "https://github.com/BlizzHacker/open-eyes"}}


# ---------------------------------------------- eXo2romm (eXoDOS -> RomM) --

EXO2ROMM_VERSION = "0.1.0"
EXO2ROMM_AUTHOR = "derkommutator"
EXO2ROMM_UPSTREAM_URL = "https://github.com/derkommutator/eXo2romm"
EXO2ROMM_PLUGIN_REPO = "https://github.com/BlizzHacker/rom-hub-exo2romm"
EXO2ROMM_CREDIT = (
    "Original creator: derkommutator (https://github.com/derkommutator/eXo2romm, "
    "The Unlicense). This plugin was inspired to assist with more users having this."
)
EXO2ROMM_INSPIRED_NOTE = (
    "This plugin was inspired to assist with more users having standalone eXoDOS "
    "game zips in RomM and ROMarr, building on derkommutator's eXo2romm."
)

DEFAULT_EXO2ROMM_PROFILE: dict[str, object] = {
    "dos_dir": "/data/eXoDOS/Content/eXo/eXoDOS/!dos",
    "output_dir": "/var/lib/romm/library/roms/dos",
    "error_log": "",
    "input_paths": [],
    "dry_run": True,
    "overwrite": False,
}

_EXO_NEEDLE = ".\\exodos\\"
_EXO_MOUNT_KEYWORDS = frozenset({"mount", "imgmount", "boot"})
_EXO_BOUNDARY_CHARS = ("", " ", "\t", '"', "\r")


def exo2romm_status() -> dict:
    """Return status, defaults, and upstream attribution for the eXo2romm plugin."""
    installed = False
    enabled = False
    try:
        _, registry = _import()
        info = _installed_slugs(registry).get("exo2romm")
        if info is not None:
            installed = True
            enabled = bool(info.get("enabled", False))
    except Exception:
        pass
    return {
        "available": True,
        "engine": "pure-python",
        "version": EXO2ROMM_VERSION,
        "plugin_slug": "exo2romm",
        "plugin_installed": installed,
        "plugin_enabled": enabled,
        "plugin_repo": EXO2ROMM_PLUGIN_REPO,
        "upstream_author": EXO2ROMM_AUTHOR,
        "upstream_url": EXO2ROMM_UPSTREAM_URL,
        "credit": EXO2ROMM_CREDIT,
        "inspired_note": EXO2ROMM_INSPIRED_NOTE,
        "default_profile": dict(DEFAULT_EXO2ROMM_PROFILE),
    }


def rewrite_exo_dosbox_conf_bytes(raw: bytes, top: str) -> tuple[bytes, int]:
    """Rewrite pack-root `.\\eXoDOS\\` mounts in `dosbox.conf` bytes while preserving EOLs.

    Mirrors `derkommutator/eXo2romm`: only lines starting cleanly (after optional
    leading whitespace and optional `@`) with `mount`, `imgmount`, or `boot` are
    edited via literal case-insensitive matching; comments and foreign paths stay untouched.
    """
    ltop = top.lower()
    nlen = len(_EXO_NEEDLE)
    out_chunks: list[bytes] = []
    rewritten = 0

    for raw_line in raw.splitlines(keepends=True):
        if raw_line.endswith(b"\r\n"):
            body, eol = raw_line[:-2], b"\r\n"
        elif raw_line.endswith(b"\n"):
            body, eol = raw_line[:-1], b"\n"
        elif raw_line.endswith(b"\r"):
            body, eol = raw_line[:-1], b"\r"
        else:
            body, eol = raw_line, b""

        line_str = body.decode("utf-8", errors="surrogateescape")
        probe = line_str.lstrip(" \t")
        if probe.startswith("@"):
            probe = probe[1:].lstrip(" \t")
        first_word = re.split(r"[ \t]+", probe, maxsplit=1)[0].lower() if probe else ""
        if first_word not in _EXO_MOUNT_KEYWORDS:
            out_chunks.append(raw_line)
            continue

        built: list[str] = []
        rest = line_str
        lc = rest.lower()
        while (pos := lc.find(_EXO_NEEDLE)) != -1:
            built.append(rest[:pos])
            after = rest[pos + nlen:]
            cafter = lc[pos + nlen:]

            cut = -1
            if cafter.startswith(ltop):
                nxt = cafter[len(ltop):len(ltop) + 1]
                if nxt == "\\":
                    cut = len(ltop) + 1
                elif nxt in _EXO_BOUNDARY_CHARS:
                    cut = len(ltop)

            if cut >= 0:
                built.append(".\\")
                rest = after[cut:]
                lc = rest.lower()
            else:
                nxt2 = cafter[:1]
                if nxt2 in _EXO_BOUNDARY_CHARS:
                    built.append(".\\")
                    rest = after
                    lc = rest.lower()
                else:
                    built.append(rest[pos:pos + nlen])
                    rest = after
                    lc = cafter

        built.append(rest)
        new_str = "".join(built)
        if new_str != line_str:
            rewritten += 1
        out_chunks.append(new_str.encode("utf-8", errors="surrogateescape") + eol)

    return b"".join(out_chunks), rewritten


def inspect_exo_zip(zip_path: Path) -> str:
    """Validate an eXoDOS `.zip` archive and return its single safe top-level directory."""
    import zipfile

    if not zip_path.is_file():
        raise ValueError(f"file not found: {zip_path}")
    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            infos = zf.infolist()
    except (zipfile.BadZipFile, OSError) as exc:
        raise ValueError("unreadable zip") from exc

    if not infos:
        raise ValueError("empty zip")

    has_slash = False
    tops: set[str] = set()
    root_non_dirs: set[str] = set()
    for info in infos:
        name = info.filename
        raw_name = getattr(info, "orig_filename", name) or name
        if not name and not raw_name:
            continue
        if "\\" in raw_name or "\\" in name:
            raise ValueError(f"unsafe backslash in zip member path: {raw_name!r}")
        if name.startswith("/") or (len(name) >= 2 and name[1] == ":"):
            raise ValueError(f"unsafe absolute zip member path: {name!r}")
        parts = [seg for seg in name.split("/") if seg]
        if any(seg == ".." for seg in parts):
            raise ValueError(f"unsafe traversal zip member path: {name!r}")
        if "/" in name:
            has_slash = True
        elif not info.is_dir():
            root_non_dirs.add(name)
        head = name.split("/", 1)[0]
        if head:
            tops.add(head)

    if len(tops) != 1:
        raise ValueError("zip does not unzip into a single top-level directory")
    if not has_slash:
        raise ValueError("files sit at the zip root (no subdirectory)")

    top = next(iter(tops))
    if top in root_non_dirs:
        raise ValueError(f"root file conflicts with top-level directory: {top!r}")
    if top in ("", ".", "..") or "\\" in top or "/" in top or ":" in top:
        raise ValueError(f"unsafe top-level directory name: {top!r}")
    return top


def _exo_append_error_log(error_log: Path, message: str) -> None:
    import datetime as _dt

    try:
        error_log.parent.mkdir(parents=True, exist_ok=True)
        stamp = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with error_log.open("a", encoding="utf-8") as fh:
            fh.write(f"{stamp}  {message}\n")
    except OSError:
        pass


def repackage_exo_zip(
    zip_path: Path,
    dos_dir: Path,
    output_dir: Path,
    *,
    dry_run: bool = False,
    overwrite: bool = False,
    error_log: Path | None = None,
) -> dict:
    """Clone one eXoDOS zip into `output_dir` with rewritten `<top>/dosbox.conf`."""
    import datetime as _dt
    import shutil
    import tempfile
    import zipfile
    from pathlib import PurePosixPath

    zip_path = Path(zip_path)
    dos_dir = Path(dos_dir)
    output_dir = Path(output_dir)
    tag = zip_path.name

    try:
        top = inspect_exo_zip(zip_path)
        dos_base = dos_dir.resolve()
        src_conf = (dos_base / PurePosixPath(top).name / "dosbox.conf").resolve()
        if not src_conf.is_relative_to(dos_base):
            raise ValueError(f"template path escapes DOS_DIR: {top!r}")
        if not src_conf.is_file():
            raise ValueError(f'no dosbox.conf found in "{dos_dir / top}"')
    except ValueError as exc:
        msg = f"SKIP [{tag}]: {exc}"
        if error_log and not dry_run:
            _exo_append_error_log(error_log, msg)
        return {"zip_path": str(zip_path), "status": "skipped_error", "top_dir": "",
                "output_zip": "", "rewritten_lines": 0, "detail": msg, "preview_conf": ""}

    out_base = output_dir.resolve()
    if not tag or tag in (".", "..") or "/" in tag or "\\" in tag:
        msg = f"SKIP [{tag}]: unsafe output archive name"
        if error_log and not dry_run:
            _exo_append_error_log(error_log, msg)
        return {"zip_path": str(zip_path), "status": "skipped_error", "top_dir": top,
                "output_zip": "", "rewritten_lines": 0, "detail": msg, "preview_conf": ""}

    out_zip = out_base / tag
    if out_zip.parent != out_base or out_zip.is_symlink():
        msg = f"SKIP [{tag}]: unsafe output archive target"
        if error_log and not dry_run:
            _exo_append_error_log(error_log, msg)
        return {"zip_path": str(zip_path), "status": "skipped_error", "top_dir": top,
                "output_zip": "", "rewritten_lines": 0, "detail": msg, "preview_conf": ""}

    if out_zip.resolve() == zip_path.resolve():
        msg = f"SKIP [{tag}]: output archive path cannot overwrite input archive"
        if error_log and not dry_run:
            _exo_append_error_log(error_log, msg)
        return {"zip_path": str(zip_path), "status": "skipped_error", "top_dir": top,
                "output_zip": "", "rewritten_lines": 0, "detail": msg, "preview_conf": ""}

    if not dry_run and out_zip.exists() and not overwrite:
        return {
            "zip_path": str(zip_path),
            "status": "skipped_exists",
            "top_dir": top,
            "output_zip": str(out_zip),
            "rewritten_lines": 0,
            "detail": f"SKIP [{tag}]: output already exists: {out_zip} (left untouched)",
            "preview_conf": "",
        }

    try:
        raw_conf = src_conf.read_bytes()
        edited_conf, rewritten_lines = rewrite_exo_dosbox_conf_bytes(raw_conf, top)
    except OSError as exc:
        msg = f"SKIP [{tag}]: could not prepare edited dosbox.conf: {exc}"
        if error_log and not dry_run:
            _exo_append_error_log(error_log, msg)
        return {"zip_path": str(zip_path), "status": "skipped_error", "top_dir": top,
                "output_zip": "", "rewritten_lines": 0, "detail": msg, "preview_conf": ""}

    if dry_run:
        return {
            "zip_path": str(zip_path),
            "status": "dry_run",
            "top_dir": top,
            "output_zip": str(out_zip),
            "rewritten_lines": rewritten_lines,
            "detail": f"DRY-RUN [{tag}] -> {out_zip} ({rewritten_lines} mount line(s) rewritten from {top}/dosbox.conf)",
            "preview_conf": edited_conf.decode("utf-8", errors="replace")[:2000],
        }

    target_entry = f"{top}/dosbox.conf".casefold()
    tmp_path: Path | None = None
    try:
        out_base.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            dir=out_base,
            prefix=f".{zip_path.stem}.",
            suffix=".tmp",
            delete=False,
        ) as tf:
            tmp_path = Path(tf.name)

        with zipfile.ZipFile(zip_path, "r") as src_zf, zipfile.ZipFile(tmp_path, "w") as dst_zf:
            for info in src_zf.infolist():
                if info.filename.rstrip("/").casefold() == target_entry:
                    continue
                new_info = zipfile.ZipInfo(info.filename, date_time=info.date_time)
                new_info.compress_type = info.compress_type
                new_info.external_attr = info.external_attr
                new_info.create_system = info.create_system
                if info.is_dir():
                    dst_zf.writestr(new_info, b"")
                else:
                    with src_zf.open(info, "r") as src_member, dst_zf.open(new_info, "w") as dst_member:
                        shutil.copyfileobj(src_member, dst_member, length=65536)

            conf_info = zipfile.ZipInfo(
                f"{top}/dosbox.conf",
                date_time=_dt.datetime.now().timetuple()[:6],
            )
            conf_info.compress_type = zipfile.ZIP_DEFLATED
            dst_zf.writestr(conf_info, edited_conf)

        if overwrite:
            os.replace(tmp_path, out_zip)
            tmp_path = None
        else:
            try:
                os.link(tmp_path, out_zip)
            except OSError as link_exc:
                if isinstance(link_exc, FileExistsError):
                    raise
                lock_path = out_base / f".{out_zip.name}.lock"
                lock_fd = os.open(lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
                os.close(lock_fd)
                try:
                    if out_zip.exists() or out_zip.is_symlink():
                        raise FileExistsError(f"output appeared concurrently: {out_zip}")
                    os.replace(tmp_path, out_zip)
                    tmp_path = None
                finally:
                    lock_path.unlink(missing_ok=True)
    except Exception as exc:
        msg = f"SKIP [{tag}]: failed to write output zip: {exc}"
        if error_log:
            _exo_append_error_log(error_log, msg)
        return {"zip_path": str(zip_path), "status": "skipped_error", "top_dir": top,
                "output_zip": "", "rewritten_lines": 0, "detail": msg, "preview_conf": ""}
    finally:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)

    return {
        "zip_path": str(zip_path),
        "status": "processed",
        "top_dir": top,
        "output_zip": str(out_zip),
        "rewritten_lines": rewritten_lines,
        "detail": f"OK [{tag}] -> {out_zip} (conf from {top}/, {rewritten_lines} mount line(s) rewritten)",
        "preview_conf": "",
    }


def default_exo2romm_roots() -> list[str]:
    """The shipped eXoDOS/RomM defaults plus the operator's EXO2ROMM_ROOTS."""
    from .path_guard import env_roots
    return ["/data/eXoDOS", "/var/lib/romm/library"] + env_roots(os.environ, "EXO2ROMM_ROOTS")


def run_exo2romm(cfg: dict | None = None, *, allowed_roots=None) -> dict:
    """Preview or run eXo2romm repackaging over one or more eXoDOS zips/folders.

    Every path (eXoDOS folder, inputs, output, error log) must resolve inside
    `allowed_roots` -- the HTTP API passes the configured library, download,
    data and eXoDOS folders -- or, when none are given, the shipped defaults
    plus EXO2ROMM_ROOTS. An API client cannot read or write elsewhere.
    """
    from .path_guard import confined
    roots = list(allowed_roots) if allowed_roots is not None else default_exo2romm_roots()
    cfg = cfg if isinstance(cfg, dict) else {}
    dos_dir = Path(str(cfg.get("dos_dir") or DEFAULT_EXO2ROMM_PROFILE["dos_dir"]).strip()
                   or str(DEFAULT_EXO2ROMM_PROFILE["dos_dir"]))
    output_dir = Path(str(cfg.get("output_dir") or DEFAULT_EXO2ROMM_PROFILE["output_dir"]).strip()
                      or str(DEFAULT_EXO2ROMM_PROFILE["output_dir"]))
    err_raw = str(cfg.get("error_log") or "").strip()
    error_log = Path(err_raw) if err_raw else (output_dir / "eXo2romm-errors.log")
    raw_dry = cfg.get("dry_run")
    if raw_dry is None:
        dry_run = bool(DEFAULT_EXO2ROMM_PROFILE["dry_run"])
    elif isinstance(raw_dry, bool):
        dry_run = raw_dry
    else:
        return {
            "ok": False,
            "error": "dry_run must be a JSON boolean.",
            "credit": EXO2ROMM_CREDIT,
            "upstream_url": EXO2ROMM_UPSTREAM_URL,
        }

    raw_over = cfg.get("overwrite")
    if raw_over is None:
        overwrite = bool(DEFAULT_EXO2ROMM_PROFILE["overwrite"])
    elif isinstance(raw_over, bool):
        overwrite = raw_over
    else:
        return {
            "ok": False,
            "error": "overwrite must be a JSON boolean.",
            "credit": EXO2ROMM_CREDIT,
            "upstream_url": EXO2ROMM_UPSTREAM_URL,
        }

    raw_inputs = cfg.get("input_paths")
    if raw_inputs is None and cfg.get("input_path"):
        raw_inputs = [cfg.get("input_path")]
    if isinstance(raw_inputs, str):
        items = [part.strip() for part in re.split(r"[\r\n]+", raw_inputs) if part.strip()]
    elif isinstance(raw_inputs, list):
        items = [str(part or "").strip() for part in raw_inputs if str(part or "").strip()]
    else:
        items = []

    checked = {"dos_dir": str(dos_dir), "output_dir": str(output_dir), "error_log": str(error_log)}
    checked.update({f"input_paths[{i}]": item for i, item in enumerate(items)})
    safe: dict[str, str] = {}
    for label, value in checked.items():
        try:
            safe[label] = confined(value, roots)
        except ValueError:
            return {
                "ok": False,
                "error": (f"{label} must be inside a configured library, download or eXoDOS folder. "
                          "Add the folder to EXO2ROMM_ROOTS in the ROMarr service or container environment."),
                "credit": EXO2ROMM_CREDIT,
                "upstream_url": EXO2ROMM_UPSTREAM_URL,
            }
    dos_dir, output_dir, error_log = Path(safe["dos_dir"]), Path(safe["output_dir"]), Path(safe["error_log"])
    items = [safe[f"input_paths[{i}]"] for i in range(len(items))]

    if not items:
        return {
            "ok": False,
            "error": "input_paths is required (one or more .zip files or a folder containing eXoDOS .zip archives).",
            "credit": EXO2ROMM_CREDIT,
            "upstream_url": EXO2ROMM_UPSTREAM_URL,
        }

    zips: list[Path] = []
    dir_results: list[dict] = []
    for text in items:
        p = Path(text)
        if p.is_dir():
            try:
                found = sorted(f for f in p.iterdir() if f.is_file() and f.suffix.lower() == ".zip")
                kept = []
                for f in found:
                    try:
                        kept.append(Path(confined(str(f), roots)))
                    except ValueError:
                        dir_results.append({
                            "zip_path": str(f), "status": "skipped_error", "top_dir": "",
                            "output_zip": "", "rewritten_lines": 0, "preview_conf": "",
                            "detail": f"SKIP [{f.name}]: links outside the allowed folders",
                        })
                found = kept
            except OSError as exc:
                msg = f"SKIP [{p.name or p}]: cannot list directory: {exc}"
                if error_log and not dry_run:
                    _exo_append_error_log(error_log, msg)
                dir_results.append({
                    "zip_path": str(p),
                    "status": "skipped_error",
                    "top_dir": "",
                    "output_zip": "",
                    "rewritten_lines": 0,
                    "detail": msg,
                    "preview_conf": "",
                })
                continue
            zips.extend(found)
        else:
            zips.append(p)

    if not zips and not dir_results:
        return {
            "ok": False,
            "error": "No .zip archives found in the provided input_paths.",
            "credit": EXO2ROMM_CREDIT,
            "upstream_url": EXO2ROMM_UPSTREAM_URL,
        }

    results = dir_results + [
        repackage_exo_zip(
            zp,
            dos_dir,
            output_dir,
            dry_run=dry_run,
            overwrite=overwrite,
            error_log=error_log,
        )
        for zp in zips
    ]
    processed = sum(1 for r in results if r["status"] in ("processed", "dry_run"))
    skipped = len(results) - processed
    clean = not any(r["status"] == "skipped_error" for r in results)
    batch_ok = any(r["status"] != "skipped_error" for r in results)
    payload: dict = {
        "ok": batch_ok,
        "clean": clean,
        "dry_run": dry_run,
        "processed": processed,
        "skipped": skipped,
        "summary": f"{processed} processed, {skipped} skipped",
        "dos_dir": str(dos_dir),
        "output_dir": str(output_dir),
        "error_log": str(error_log),
        "credit": EXO2ROMM_CREDIT,
        "inspired_note": EXO2ROMM_INSPIRED_NOTE,
        "upstream_url": EXO2ROMM_UPSTREAM_URL,
        "results": results,
    }
    if not batch_ok:
        payload["error"] = results[0]["detail"] if results else "No archives could be processed."
    return payload
