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
os.environ.setdefault("ROM_HUB_HOME", str(HOME))
# ROM_HUB_ALLOW_UNSANDBOXED is deliberately NOT set here. It used to be pinned
# to "1" at import time, before the Hub grew a seccomp sandbox -- which meant
# any in-process Hub call saw the flag and ran plugins with no confinement at
# all. `_plugin_env()` below is the only place that may opt out, and only when
# the sandbox genuinely cannot be installed. Asked for, never assumed.


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
    # Python caches each site-packages directory listing, so without this a
    # `pip install rom-hub` made while ROMarr is running stays invisible until
    # a restart -- the Hub tab just keeps saying it is not installed.
    import importlib
    importlib.invalidate_caches()
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
    # CLI diagnostics and plugin names must round-trip on Windows too.
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
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

    The subprocess runs with `_plugin_env()`, which confines it via the Hub's
    seccomp sandbox when pyseccomp is available and only sets
    ROM_HUB_ALLOW_UNSANDBOXED (with a warning) when the sandbox cannot be
    installed. Installing a plugin still runs that plugin's code with ROMarr's
    own privileges inside the confined environment; `_plugin_env` limits what
    it inherits.
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
    """Whether Hub calls from this process may run plugins unconfined.

    Only when the sandbox genuinely cannot run here -- the same rule
    `_plugin_env` applies to subprocesses. Asked for, never assumed.
    """
    ok, why = sandbox_state()
    if not ok:
        log.warning("ROM Hub calls run WITHOUT confinement: %s", why)
    return not ok


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


def search(query: str, platform: str | None = None, limit: int = 25,
           timeout: float = 90.0) -> tuple[list[dict], str]:
    """Search every enabled ROM Hub plugin, merged.

    Returns `(rows, error)`. Rows are plain dicts with the Hub's SearchResult
    fields (source_id, title, platform, size_bytes, url, plugin). `error` is
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
    fetcher = HttpxFetcher(timeout=max(60.0, timeout))
    try:
        outcome = search_all(plugins, fetcher=fetcher, query=query,
                             platform=platform, limit=limit, timeout=timeout,
                             allow_unsandboxed=_unsandboxed())
    except Exception as exc:
        return [], f"ROM Hub search failed: {type(exc).__name__}: {exc}"
    finally:
        fetcher.close()
    rows = []
    for r in outcome.results:
        try:
            rows.append(r.model_dump())
        except Exception:  # pragma: no cover -- pydantic model, always dumps
            rows.append(dict(r))
    return rows, ""


def resolve_torrents(rows: list[dict]) -> list[dict]:
    """Give every magnet-bearing row its plugin-resolved magnet, in place.

    Some sources distribute by torrent only -- MiNERVA Archive is one -- and
    their magnets arrive bare: an info-hash with no trackers, which is a
    DHT-only download that never starts on networks that block it. The Hub's
    `torrent` capability exists for exactly this: the plugin describes the
    torrent (`TorrentSource`), the host validates it against the plugin's own
    manifest allowlist (`check_magnet`, which gates every `tr=` by hostname)
    and hands it over.

    This is the general half of that handoff on ROMarr's side: nothing here
    knows which plugin produced the row or what site it came from. Rows are
    grouped by plugin so one sandboxed subprocess resolves all of a plugin's
    rows -- spawning one per result would be absurd -- and every failure
    degrades to the row's own raw magnet rather than losing the result: a
    DHT-only download that starts slowly beats no download at all.

    A resolved magnet rides in the row's `magnet` key. `_hub_releases` reads
    that key and builds a `protocol="torrent"` Release, so the grab goes to
    the operator's torrent client exactly like an indexer magnet; rows
    without it keep riding the `hub://` FetchPlan lane.
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

    by_plugin: dict[str, list[dict]] = {}
    for row in rows:
        if str(row.get("source_id") or "").startswith("magnet:"):
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
                        allow_unsandboxed=_unsandboxed()) as proc:
                    for row in plugin_rows:
                        raw = str(row.get("source_id") or "")
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
                                "%s could not resolve its torrent for %r: %s "
                                "-- falling back to the bare magnet",
                                slug, row.get("title", "")[:60], exc)
                            row.setdefault("magnet", raw)
            except Exception as exc:
                log.warning("torrent plugin %r failed to run: %s", slug, exc)
                for row in plugin_rows:
                    row.setdefault("magnet",
                                   str(row.get("source_id") or ""))
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
    fetcher = HttpxFetcher()
    try:
        with PluginProcess(plugin_dir=plugin.path, manifest=plugin.manifest,
                           config=getattr(plugin, "config", {}) or {},
                           fetcher=fetcher,
                           allow_unsandboxed=_unsandboxed()) as proc:
            plan = proc.plan(result)
            with HttpDownloader(
                    allowlist=list(getattr(plugin.manifest, "network", [])
                                   or [])) as downloader:
                files = []
                for f in plan.files:
                    saved = downloader.download(f.url,
                                                dest / f.relative_path(),
                                                f.size_bytes)
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
