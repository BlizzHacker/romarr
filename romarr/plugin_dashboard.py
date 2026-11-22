"""One page per installed ROM Hub plugin: what it is, its settings, how to use it, and a way to try it.

Everything here is read from the plugin itself -- its manifest's config schema,
its README, its catalogue entry -- so a plugin nobody wrote a ROMarr card for
still gets a working settings form. The two hand-built cards (Compressatorium,
Mister Download Manager) keep their own validation; this is the generic path.

Secrets are write-only from here on: a `secret`-typed field goes to ROM Hub's
secret store (keyring or encrypted file), never to `state.json`, and the page
only ever learns whether one is set.
"""

from __future__ import annotations

import logging
import re
import threading
from pathlib import Path

from . import hub

log = logging.getLogger(__name__)

_lock = threading.Lock()

#: The README is shown in the page; a runaway one is cut rather than shipped whole.
MAX_README_CHARS = 40_000
#: Rows a "try it" search shows.
TRY_LIMIT = 15
#: Shown when a plugin asks GitHub's API for listings.
_GITHUB_HOSTS = ("api.github.com", "raw.githubusercontent.com", "github.com")

#: What each capability does in ROMarr, and where to use it. Plain words,
#: because this is the paragraph an operator reads right after installing.
CAPABILITY_GUIDE = {
    "search": ("Finds games", [
        "Its results appear in ROMarr's search and are used for automatic grabs from Wanted.",
        "Try a search below; results here come from this plugin only.",
    ]),
    "importer": ("Downloads what it finds", [
        "Grabbing one of its results downloads through ROM Hub, then ROMarr imports it into your library.",
        "Needs a ROM Hub download client. Use \"Set up ROM Hub downloads\" on the plugins page if you have none.",
    ]),
    "metadata": ("Adds details to games already in RomM", [
        "Enrich a RomM game by its id below. This writes to your RomM library.",
        "A RomM game's id is the number in its URL, for example /rom/61640.",
        "No match? Give the exact name or hash the plugin uses (for a zipped ROM, RomM's "
        "hash is of the zip, not the ROM inside it).",
    ]),
    "cores": ("Lists emulator cores and tools", [
        "List what it offers below. Install one from the server with: rom-hub cores install <plugin> <core>.",
    ]),
    "assets": ("Lists emulator add-ons (cheats, overlays, controller profiles)", [
        "List what it offers below. Install one from the server with: rom-hub assets install <plugin> <asset>.",
    ]),
    "firmware": ("Lists BIOS and firmware files it can provide", [
        "List what it offers below. Install one from the server with: rom-hub firmware install <plugin> <id>.",
    ]),
    "stream": ("Plays or opens items in the browser", [
        "Its results can be opened in the browser from ROMarr's search results.",
    ]),
}

_BRAND_BLOCK = re.compile(r"<table><tr><td>.*?</td></tr></table>\s*", re.S)
_CONFIG_ROW = re.compile(r"^\|\s*`([A-Za-z0-9_]+)`\s*\|(.*)\|\s*$", re.M)


def _registry():
    _, registry = hub._import()
    return registry.Registry(hub.HOME)


def _catalogue_entry(slug: str) -> dict:
    try:
        for item in hub.plugins().get("items") or []:
            if item.get("slug") == slug:
                return item
    except Exception:  # noqa: BLE001 - the page still works without it
        log.warning("could not read the catalogue entry for %s", slug)
    return {}


def _readme(path: Path) -> str:
    try:
        text = (Path(path) / "README.md").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    text = _BRAND_BLOCK.sub("", text)
    if len(text) > MAX_README_CHARS:
        text = text[:MAX_README_CHARS] + "\n\n(README shortened; the full text is in the plugin's repository.)"
    return text


def _field_help(readme: str) -> dict:
    """`key -> meaning` from a README config table: | `key` | type | default | meaning |."""
    out = {}
    for key, rest in _CONFIG_ROW.findall(readme or ""):
        cells = [c.strip() for c in rest.split("|")]
        if cells and cells[-1]:
            out[key] = re.sub(r"\*\*|`", "", cells[-1])
    return out


def _secret_store():
    from rom_hub import secrets
    return secrets.open_store(hub.HOME)


def _secret_set(slug: str, key: str, config: dict) -> bool:
    try:
        if _secret_store().get(slug, key):
            return True
    except Exception:  # noqa: BLE001 - an unreadable store reads as "not set"
        log.warning("could not read the secret store for %s", slug)
    return bool(config.get(key))  # an un-migrated plaintext value


def detail(slug: str) -> dict:
    """Everything the dashboard shows for one installed plugin."""
    try:
        plugin = _registry().get(slug)
    except Exception as exc:  # noqa: BLE001 - registry raises its own error types
        return {"ok": False, "error": f"{slug} is not installed: {exc}"}
    manifest = plugin.manifest
    config = dict(getattr(plugin, "config", None) or {})
    schema = dict(getattr(manifest, "config_schema", None) or {})
    readme = _readme(plugin.path)
    help_text = _field_help(readme)
    fields = []
    for key, spec in schema.items():
        spec = spec if isinstance(spec, dict) else {}
        kind = str(spec.get("type") or "str")
        field = {"key": key, "type": kind, "default": spec.get("default"),
                 "help": help_text.get(key, "")}
        if kind == "secret":
            field["set"] = _secret_set(slug, key, config)
        else:
            field["value"] = config.get(key, spec.get("default"))
        fields.append(field)
    capabilities = sorted(getattr(manifest, "capabilities", None) or {})
    network = list(getattr(manifest, "network", None) or [])
    entry = _catalogue_entry(slug)
    needs = [f"Needs your {f['key'].replace('_', ' ').replace('api', 'API')}: add it under "
             f"Settings below. The README says where to get one."
             for f in fields if f["type"] == "secret" and not f["set"] and entry.get("key_required")]
    uses_github = any(h in network for h in _GITHUB_HOSTS)
    if uses_github and not hub.github_token_set():
        needs.append("Reads GitHub's API, which allows 60 requests an hour without a token. "
                     "Add a GitHub token in Settings -> General to lift that.")
    return {
        "ok": True,
        "slug": slug,
        "name": getattr(manifest, "name", "") or slug,
        "version": getattr(manifest, "version", "") or "",
        "enabled": bool(getattr(plugin, "enabled", True)),
        "description": entry.get("description") or "",
        "repository": entry.get("repository") or "",
        "terms": entry.get("terms") or "",
        "platforms": entry.get("platforms") or [],
        "capabilities": capabilities,
        "network": network,
        "guide": [{"capability": c, "title": CAPABILITY_GUIDE[c][0], "steps": CAPABILITY_GUIDE[c][1]}
                  for c in capabilities if c in CAPABILITY_GUIDE],
        "fields": fields,
        "needs": needs,
        "uses_github": uses_github,
        "readme": readme,
    }


def _coerce(kind: str, key: str, value):
    if kind == "int":
        try:
            return int(str(value).strip())
        except ValueError:
            raise ValueError(f"{key} must be a whole number") from None
    if kind == "bool":
        if isinstance(value, bool):
            return value
        text = str(value).strip().lower()
        if text in ("true", "1", "yes", "on"):
            return True
        if text in ("false", "0", "no", "off", ""):
            return False
        raise ValueError(f"{key} must be on or off")
    if kind == "list[str]":
        items = value if isinstance(value, list) else re.split(r"[,\n]", str(value or ""))
        return [str(i).strip() for i in items if str(i).strip()]
    return str(value if value is not None else "").strip()


def save(slug: str, values: dict | None = None, secrets: dict | None = None,
         clear: list | None = None) -> dict:
    """Save settings. Every value is checked before anything is written."""
    values, secrets, clear = values or {}, secrets or {}, list(clear or [])
    with _lock:
        try:
            registry = _registry()
            plugin = registry.get(slug)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"{slug} is not installed: {exc}"}
        schema = dict(getattr(plugin.manifest, "config_schema", None) or {})
        kinds = {k: str((s or {}).get("type") or "str") for k, s in schema.items()}
        config = dict(getattr(plugin, "config", None) or {})
        try:
            for key, value in values.items():
                if key not in kinds:
                    raise ValueError(f"{slug} has no setting called {key}")
                if kinds[key] == "secret":
                    raise ValueError(f"{key} is a secret; send it as a secret")
                config[key] = _coerce(kinds[key], key, value)
            for key in list(secrets) + clear:
                if kinds.get(key) != "secret":
                    raise ValueError(f"{slug} has no secret called {key}")
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        try:
            store = _secret_store() if (secrets or clear) else None
            for key, value in secrets.items():
                if str(value or "").strip():
                    store.set(slug, key, str(value))
            for key in clear:
                store.delete(slug, key)
                config.pop(key, None)
            registry.set_config(slug, config)
        except Exception as exc:  # noqa: BLE001 - surfaced to the operator
            return {"ok": False, "error": f"could not save {slug}'s settings: {exc}"}
    return {"ok": True}


def _lines(result: dict, limit: int = 60) -> list[str]:
    text = (result.get("out") or "") + ("\n" + result["err"] if result.get("err") else "")
    return [line for line in text.splitlines() if line.strip()][:limit]


def run(slug: str, action: str, *, query: str = "", platform: str = "",
        rom_id: str = "", source_id: str = "") -> dict:
    """Use the plugin from its page: search, enrich, or list what it offers."""
    try:
        plugin = _registry().get(slug)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{slug} is not installed: {exc}"}
    caps = set(getattr(plugin.manifest, "capabilities", None) or {})
    if action == "search":
        if "search" not in caps:
            return {"ok": False, "error": f"{slug} does not search"}
        if not query.strip():
            return {"ok": False, "error": "Type something to search for"}
        rows, error = hub.search(query.strip(), platform.strip() or None, TRY_LIMIT, only=slug)
        return {"ok": not error, "error": error,
                "rows": [{"title": r.get("title"), "platform": r.get("platform"),
                          "size_bytes": r.get("size_bytes"), "url": r.get("url")}
                         for r in rows[:TRY_LIMIT]]}
    if action == "enrich":
        if "metadata" not in caps:
            return {"ok": False, "error": f"{slug} does not add metadata"}
        if not re.fullmatch(r"\d{1,12}", rom_id.strip()):
            return {"ok": False, "error": "Enter the RomM game id (a number)"}
        args = ["enrich", slug, rom_id.strip()]
        source_id = source_id.strip()
        if source_id:
            # The plugin's own override: an exact filename, title or hash for
            # when the library's name or a zipped file's hash cannot match.
            if len(source_id) > 300 or any(c in source_id for c in "\r\n\x00"):
                return {"ok": False, "error": "The exact name or hash is too long or has line breaks"}
            args += ["--source-id", source_id]
        result = hub._run_cli(*args, timeout=300)
        return {"ok": bool(result.get("ok")), "lines": _lines(result)}
    if action in ("cores", "assets", "firmware"):
        if action not in caps:
            return {"ok": False, "error": f"{slug} does not list {action}"}
        result = hub._run_cli(action, "list", slug, timeout=300)
        return {"ok": bool(result.get("ok")), "lines": _lines(result, 400)}
    return {"ok": False, "error": f"unknown action {action!r}"}
