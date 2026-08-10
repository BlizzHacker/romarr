"""Import lists: hand ROMarr a list, let the clock do the asking.

Radarr calls these Import Lists -- an IMDb watchlist, a Trakt list -- and the
idea translates to ROMs better than it does to films, because ROM lists
already exist everywhere: "top 100 SNES games" articles, homebrew catalogue
dumps, a friend's spreadsheet, the text file beside a 1G1R set. Questarr's
one list is a Steam wishlist; this is the general case. (Full sets and 1G1R
stay with `collections.py`, which plans them from DATs -- a curated list and
a complete set are different promises.)

A list is stored configuration, like a download client: a name, a default
platform, and either pasted text or a URL fetched on the scheduler. Syncing
a list adds its titles to Wanted; the missing-search backoff does the rest.

**Each title is added once, ever.** A list is an instruction to acquire, not
a state to enforce: once a title has been added -- and later imported and
therefore removed from Wanted -- a re-sync must not resurrect it, or every
list becomes a slow loop re-downloading its own history. The ledger of what
each list already added is part of the list's stored state.

Parsing favours the shape lists actually arrive in. "Top 100" articles
number their entries; people paste them numbered. Comments and blank lines
are what text files contain. A tab or " - " splits title from platform for
mixed-platform lists, and a line's platform beats the list's default.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ListEntry:
    game: str
    platform: str = ""   # empty means "use the list's default"

    @property
    def key(self) -> str:
        return f"{self.platform}/{self.game.strip().lower()}"


#: "1. Super Metroid", "42) Chrono Trigger", "#3 Earthbound", "10 - F-Zero".
_NUMBERED = re.compile(r"^\s*#?\d{1,4}\s*[.)\-:]?\s+")

#: A comment is a `#` that is not a rank: a whole line starting `# ...`, or
#: an inline ` # ...`. `#3 Earthbound` is a rank and stays.
_COMMENT = re.compile(r"(?:^#(?!\d)|\s#).*$")


def parse_list(text: str) -> list[ListEntry]:
    """Titles out of the text people actually paste.

    One title per line. `#` opens a comment. A tab, ` | `, or ` - ` after
    the title names that line's platform. Leading ranking numbers are
    stripped, because "top 100" lists arrive numbered and nobody should have
    to clean 100 lines by hand to use one.
    """
    entries: list[ListEntry] = []
    seen: set[str] = set()
    for raw in (text or "").splitlines():
        line = _COMMENT.sub("", raw).strip()
        if not line:
            continue
        line = _NUMBERED.sub("", line).strip()
        if not line:
            continue
        platform = ""
        for sep in ("\t", " | ", " - "):
            if sep in line:
                candidate, rest = line.rsplit(sep, 1)
                # Only take the split when the right side looks like a
                # platform name, not a subtitle: "Ecco - The Tides of Time"
                # must stay one title. Platform names are short and carry no
                # digits-with-colon shapes; three words is the ceiling
                # ("Super Nintendo Entertainment System" arrives as SNES).
                rest = rest.strip()
                if rest and len(rest.split()) <= 3 and not rest[0].isdigit():
                    line, platform = candidate.strip(), rest
                break
        if not line:
            continue
        entry = ListEntry(game=line, platform=platform)
        if entry.key in seen:
            continue
        seen.add(entry.key)
        entries.append(entry)
    return entries


def fetch_entries(cfg: dict, *, session=None) -> list[ListEntry]:
    """The entries a stored list currently names.

    A `paste` list reads its stored content; a `url` list fetches. A fetch
    failure raises so the caller can report WHICH list failed -- swallowing
    it here would make an expired URL look like an empty list, and an empty
    list looks like success.
    """
    kind = str(cfg.get("type") or "paste").lower()
    if kind == "paste":
        return parse_list(cfg.get("content") or "")
    if kind == "url":
        import requests
        url = str(cfg.get("url") or "").strip()
        if not url:
            return []
        response = (session or requests).get(url, timeout=30)
        response.raise_for_status()
        return parse_list(response.text)
    raise ValueError(f"unknown list type {kind!r}")


# The Settings page renders list forms from this, the same way download
# clients and indexers describe themselves.
LIST_TYPES = {
    "paste": {
        "label": "Pasted list",
        "help": "One title per line. '# comments', ranking numbers and "
                "'Title<TAB>Platform' lines are understood.",
        "fields": ["name", "enable", "platform", "content"],
    },
    "url": {
        "label": "List at a URL",
        "help": "A plain-text list fetched on the List Sync schedule, so a "
                "list somebody else maintains keeps feeding Wanted.",
        "fields": ["name", "enable", "platform", "url"],
    },
}
