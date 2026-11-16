"""TitleDB integration for Nintendo Switch validation (issue #23).

Switch is the one platform with no No-Intro or Redump DAT, so the hash
verification every other platform rides does not exist for it. What the
preservation scene uses instead is TitleDB -- blawar's catalogue of every
Switch title ID, published as plain JSON on GitHub:

  https://github.com/blawar/titledb

Two files matter here:

  * ``versions.json`` (1.7MB) -- every title ID that has ever received an
    update, mapped to its full version history (version number -> release
    date). Small enough to fetch at startup and cache, and authoritative
    for the question "is this title ID real". This is what loads by
    default.

  * ``US.en.json`` and its regional siblings (65-95MB each) -- the same
    catalogue keyed by title ID with names, descriptions, sizes and
    publishers. Heavy: loading one costs seconds of parse time and several
    hundred MB of RAM, which in a 512MB container is not a default. Opt in
    with ``TITLED_NAMES=1`` (region via ``TITLED_REGION``, default
    ``US.en``); the download is cached on disk under ROMARR_DATA and only
    re-fetched when missing or older than the TTL.

What neither file is needed for is telling a base game from its DLC and
its updates, or finding the latest version of a title -- that is arithmetic
on the title ID and a max() over the version history:

  * ``xxxxxxxxxxxx000`` -- base title (application)
  * base ID + ``0x1001`` through ``0x1fff`` -- DLC / add-on content
  * ``xxxxxxxxxxxx800`` -- update / patch of the base title
  * IDs below ``0100000000002000`` -- system titles (applets, services)

So a dump named ``[01007EF00011E000]`` is verifiable as a real title, its
kind, and its version history with only the small file loaded -- which is
what "only look for the latest version and see base games, DLC and
updates" from the issue actually requires.

Every network call here is best-effort with a disk cache and a short
timeout. TitleDB being down must never stop ROMarr from starting, and an
unreachable network with no cache degrades to an empty index that answers
"not in database" -- the honest verdict -- rather than a crash.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

#: Where the real TitleDB data lives. The website (titledb.com) is a JS
#: front-end over these files; the GitHub raw endpoint is the source of
#: truth and the one that answers a plain GET.
TITLED_RAW = "https://raw.githubusercontent.com/blawar/titledb/master"

#: How long a cached copy stays fresh. The catalogue grows with every
#: eShop release, but nothing here rots: a week-old index still knows
#: every title that existed a week ago.
CACHE_TTL_SECONDS = 7 * 24 * 3600

#: Seconds to wait on a fetch before giving up and using cache-or-nothing.
FETCH_TIMEOUT = 15

#: The boundary under which a base title ID is a system title rather than a
#: game. System applets and services live below it; the first real game
#: (1-2-Switch, 01000320000CC000) lives above.
SYSTEM_TID_BOUNDARY = 0x0100000000002000


@dataclass(frozen=True)
class SwitchTitle:
    """One entry from TitleDB."""

    title_id: str
    name: str = ""
    #: version number -> release date, straight from versions.json.
    versions: dict = field(default_factory=dict)
    #: Which region file this metadata came from ("" when names not loaded).
    region: str = ""
    size: int = 0
    publisher: str = ""
    description: str = ""
    release_date: str = ""

    @property
    def kind(self) -> str:
        """base | dlc | update | system -- arithmetic on the title ID."""
        return classify_tid(self.title_id)

    @property
    def latest_version(self) -> int:
        """The highest version number this title has ever received."""
        try:
            return max((int(v) for v in self.versions), default=0)
        except (TypeError, ValueError):
            return 0

    @property
    def display_name(self) -> str:
        name = self.name or "Unknown title"
        return f"{name} [{self.title_id}]"


def classify_tid(title_id: str) -> str:
    """What kind of Switch title an ID names, from the ID alone.

    Applications reserve a 0x2000-wide family: base at 0, update at
    0x800 and add-on content at 0x1001..0x1fff.
    """
    tid = str(title_id or "").strip().lower().removeprefix("0x")
    if not re.fullmatch(r"[0-9a-f]{16}", tid):
        return "invalid"
    value = int(tid, 16)
    if value < SYSTEM_TID_BOUNDARY:
        return "system"
    low = value & 0x1FFF
    if low == 0:
        return "base"
    if low == 0x800:
        return "update"
    if 0x1001 <= low <= 0x1FFF:
        return "dlc"
    return "unknown"


def base_tid_of(title_id: str) -> str:
    """Return the application ID shared by an update and its add-on content."""
    tid = str(title_id or "").strip().lower().removeprefix("0x")
    if not re.fullmatch(r"[0-9a-f]{16}", tid):
        return ""
    return f"{int(tid, 16) & ~0x1fff:016x}"


def _fetch_json(url: str, timeout: int = FETCH_TIMEOUT):
    """GET a JSON document, or None. Network failure is never an exception
    here: TitleDB being down is an operational fact, not a bug in ROMarr."""
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": "romarr", "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001 - every failure mode is "no data"
        log.warning("TitleDB fetch failed for %s: %s", url, exc)
        return None


def _cache_path(cache_dir: Optional[str | Path], name: str) -> Optional[Path]:
    if not cache_dir:
        return None
    try:
        path = Path(cache_dir)
        path.mkdir(parents=True, exist_ok=True)
        return path / name
    except OSError as exc:
        log.warning("TitleDB cache dir %s unusable: %s", cache_dir, exc)
        return None


def _load_cached_or_fetch(name: str, url: str,
                          cache_dir: Optional[str | Path]):
    """Fresh cache -> cache -> network -> None, in that order of preference.

    The cache-first order is deliberate: startup should not depend on
    GitHub being reachable, and a week-old catalogue is still a catalogue.
    """
    cached = _cache_path(cache_dir, name)
    if cached and cached.exists():
        age = time.time() - cached.stat().st_mtime
        if age < CACHE_TTL_SECONDS:
            try:
                return json.loads(cached.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                log.warning("TitleDB cache %s unreadable: %s", cached, exc)
        else:
            log.info("TitleDB cache %s is %dh old; refreshing", name,
                     int(age // 3600))
    data = _fetch_json(url)
    if data is not None and cached:
        try:
            cached.write_text(json.dumps(data), encoding="utf-8")
        except OSError as exc:
            log.warning("could not write TitleDB cache %s: %s", cached, exc)
    if data is None and cached and cached.exists():
        # Stale cache beats no cache: every title in it was real once, and
        # "not in database" for a title the week-old file knows is a worse
        # answer than a slightly old yes.
        try:
            log.warning("TitleDB unreachable; using stale cache %s", name)
            return json.loads(cached.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
    return data


@dataclass
class TitleDBIndex:
    """The Switch catalogue, queryable offline.

    `by_id` always reflects versions.json (the authoritative ID list).
    Names, sizes and publishers appear only when a region file has been
    loaded -- see `load_names`, and the module docstring for why that is
    not the default.
    """

    by_id: dict[str, SwitchTitle] = field(default_factory=dict)
    #: Set when a region file supplied names, so callers can tell "no such
    #: title" from "no names loaded".
    names_loaded: bool = False
    region: str = ""
    _by_name: dict[str, list[str]] = field(default_factory=dict)

    # Backwards-compatible alias: earlier code (and the status page) read
    # `.titles`; one name should win, but breaking the app.py wiring over a
    # rename is not a trade worth making.
    @property
    def titles(self) -> dict[str, SwitchTitle]:
        return self.by_id

    def __len__(self) -> int:
        return len(self.by_id)

    def lookup_by_id(self, title_id: str) -> Optional[SwitchTitle]:
        """A title by ID, tolerating case and a 0x prefix."""
        tid = str(title_id or "").strip().lower().removeprefix("0x")
        return self.by_id.get(tid)

    def search_by_name(self, query: str) -> list[SwitchTitle]:
        """Partial case-insensitive name search. Empty without `load_names`
        -- versions.json carries IDs and dates, not names."""
        text = str(query or "").strip().lower()
        if not text or not self._by_name:
            return []
        seen: set[str] = set()
        out: list[SwitchTitle] = []
        for name, tids in self._by_name.items():
            if text in name:
                for tid in tids:
                    if tid not in seen and tid in self.by_id:
                        seen.add(tid)
                        out.append(self.by_id[tid])
        return out

    def family_of(self, title_id: str) -> dict[str, list[SwitchTitle]]:
        """Base game, its DLC and its updates, grouped by kind.

        Match the application family across the update and DLC ranges.
        """
        base = base_tid_of(title_id)
        if not base:
            return {}
        groups: dict[str, list[SwitchTitle]] = {
            "base": [], "dlc": [], "update": []}
        for tid, title in self.by_id.items():
            if base_tid_of(tid) == base:
                kind = classify_tid(tid)
                if kind in groups:
                    groups[kind].append(title)
        return groups

    def add(self, title: SwitchTitle) -> None:
        old = self.by_id.get(title.title_id)
        if old and old.name:
            self._by_name[old.name.lower()] = [
                tid for tid in self._by_name.get(old.name.lower(), []) if tid != title.title_id]
        self.by_id[title.title_id] = title
        if title.name:
            key = title.name.lower()
            self._by_name.setdefault(key, []).append(title.title_id)

    def load_names(self, region: Optional[str] = None,
                   cache_dir: Optional[str | Path] = None) -> int:
        """Fetch a region file and merge names into this index.

        Returns how many titles gained a name. The file is large (65-95MB);
        the cache makes it a one-time cost per TTL.
        """
        return load_names_into(self, region=region, cache_dir=cache_dir)


def _default_cache_dir() -> str:
    """Beside ROMarr's own store, so the cache lives and dies with the
    install and survives container restarts on a mounted volume."""
    data = os.environ.get("ROMARR_DATA", "/opt/romarr/romarr.json")
    return str(Path(data).parent / "titledb")


def fetch_versions(cache_dir: Optional[str | Path] = None) -> Optional[dict]:
    """versions.json: {title_id: {version_number: release_date}}."""
    return _load_cached_or_fetch(
        "versions.json", f"{TITLED_RAW}/versions.json",
        cache_dir if cache_dir is not None else _default_cache_dir())


def build_index(cache_dir: Optional[str | Path] = None,
                *, with_names: Optional[bool] = None) -> TitleDBIndex:
    """Build the catalogue from versions.json, names optional.

    `with_names` defaults to the TITLED_NAMES environment variable, which
    defaults to off: a 90MB region file is seconds of parse time and
    several hundred MB of RAM, and validation does not need it. ID
    existence, kind, family and version history all come from the small
    file.
    """
    index = TitleDBIndex()
    versions = fetch_versions(cache_dir)
    if isinstance(versions, dict):
        for tid, history in versions.items():
            tid_norm = str(tid).strip().lower()
            if not re.fullmatch(r"[0-9a-f]{16}", tid_norm):
                continue
            index.add(SwitchTitle(
                title_id=tid_norm,
                versions=history if isinstance(history, dict) else {}))
        for title in list(index.by_id.values()):
            if title.kind != "base":
                continue
            patch_id = f"{int(title.title_id, 16) + 0x800:016x}"
            existing = index.lookup_by_id(patch_id)
            history = dict(existing.versions) if existing else {}
            for value, date in title.versions.items():
                if re.fullmatch(r"[0-9]{1,10}", str(value)) and 0 < int(value) <= 0xFFFFFFFF:
                    history.setdefault(str(value), date)
            if history:
                index.add(SwitchTitle(patch_id, versions=history))
        log.info("TitleDB: indexed %d Switch title IDs", len(index))
    else:
        log.warning("TitleDB: versions.json unavailable; Switch validation "
                    "will answer 'not in database' for every title")

    load_names = with_names
    if load_names is None:
        load_names = os.environ.get("TITLED_NAMES", "").lower() in (
            "1", "true", "yes", "on")
    if load_names:
        index.load_names(cache_dir=cache_dir)
    return index


def load_names_into(index: TitleDBIndex,
                    region: Optional[str] = None,
                    cache_dir: Optional[str | Path] = None) -> int:
    """Fetch a region file and merge names into an existing index.

    Returns how many titles gained a name. The file is large (65-95MB);
    the cache makes it a one-time cost per TTL.
    """
    region = (region or os.environ.get("TITLED_REGION", "US.en")).strip()
    data = _load_cached_or_fetch(
        f"{region}.json", f"{TITLED_RAW}/{region}.json",
        cache_dir if cache_dir is not None else _default_cache_dir())
    if not isinstance(data, dict):
        log.warning("TitleDB: region file %s unavailable", region)
        return 0
    named = 0
    seen = set()
    for tid, row in data.items():
        if not isinstance(row, dict):
            continue
        # Region catalogues are keyed by eShop product ID; the row carries the title ID.
        tid_norm = str(row.get("id") or tid).strip().lower()
        if not re.fullmatch(r"[0-9a-f]{16}", tid_norm) or tid_norm in seen:
            continue
        seen.add(tid_norm)
        name = str(row.get("name") or "").strip()
        try:
            size = max(0, int(row.get("size") or 0))
        except (ValueError, TypeError, OverflowError):
            size = 0
        existing = index.by_id.get(tid_norm)
        title = SwitchTitle(
            title_id=tid_norm,
            name=name,
            versions=existing.versions if existing else {},
            region=region,
            size=size,
            publisher=str(row.get("publisher") or ""),
            description=str(row.get("description") or ""),
            release_date=str(row.get("releaseDate") or ""),
        )
        index.add(title)
        if name:
            named += 1
    index.names_loaded = named > 0
    index.region = region if named else ""
    log.info("TitleDB: %d titles named from %s", named, region)
    return named


def validate_switch_rom(filename: str, title_id: Optional[str] = None,
                        index: Optional[TitleDBIndex] = None) -> dict:
    """Validate a Switch ROM against TitleDB.

    Three identifiers, in order of trust: an explicit title ID, a 16-hex
    ID in the filename (every scene dump carries one in brackets), then a
    name search when the region file is loaded. The verdicts stay honest
    about which was used.

    "not_in_database" is deliberately NOT a failure verdict: a new release
    ahead of the catalogue refresh, or an index built without names, lands
    there, and treating absence as proof of badness is the same mistake
    dat.py's UNKNOWN exists to avoid.
    """
    result = {
        "valid": False,
        "status": "unknown",
        "title": None,
        "kind": "",
        "latest_version": 0,
        "details": "",
    }

    tid = str(title_id or "").strip()
    if not tid:
        # A Switch title ID is exactly 16 hex digits; word-boundary it so a
        # 19-digit serial in a filename cannot masquerade as one.
        matches = re.findall(r"(?<![0-9A-Fa-f])([0-9A-Fa-f]{16})(?![0-9A-Fa-f])",
                             str(filename or ""))
        tid = matches[0] if matches else ""

    if tid and classify_tid(tid) == "invalid":
        result["status"] = "invalid_id"
        result["details"] = f"{tid!r} is not a 16-hex-digit title ID"
        return result

    if tid:
        result["kind"] = classify_tid(tid)
        if index is not None:
            title = index.lookup_by_id(tid)
            if title:
                result["status"] = "verified"
                result["title"] = title
                result["valid"] = True
                result["latest_version"] = title.latest_version
                result["details"] = f"Verified: {title.display_name}"
                return result
            result["status"] = "not_in_database"
            result["details"] = (
                f"Title ID {tid.upper()} ({result['kind']}) is not in the "
                f"loaded TitleDB index"
                + ("" if index.names_loaded else
                   " (names not loaded; ID list only)"))
            return result
        # No index: the ID is still classifiable, which is more than nothing.
        result["status"] = "classified_only"
        result["details"] = (f"{tid.upper()} parses as a {result['kind']} "
                             f"title, but no TitleDB index is loaded")
        return result

    if index is not None and index.names_loaded:
        basename = re.sub(r"\.[^.]+$", "", str(filename or ""))
        matches = index.search_by_name(basename)
        if matches:
            result["status"] = "matched_by_name"
            result["title"] = matches[0]
            result["kind"] = matches[0].kind
            result["latest_version"] = matches[0].latest_version
            result["valid"] = True
            result["details"] = f"Found {len(matches)} matching title(s) by name"
            return result

    result["status"] = "no_identifier"
    result["details"] = ("Could not extract a title ID from the filename"
                         + ("" if (index and index.names_loaded) else
                            " and no names are loaded for a name search"))
    return result
