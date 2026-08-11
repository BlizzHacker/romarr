"""What ROMarr knows about the bytes on disk.

The audit already walks the library and hashes every file to decide whether
it matches a DAT. It then threw the hashes away and kept only the counts,
which meant nothing in ROMarr could answer the one question netplay depends
on: *what is the SHA1 of my copy of this game?*

Without that, a netplay offer carries an empty hash, and the other side can
only ever answer "missing". The handshake looks like it works and settles
nothing.

This index is the missing half. It is deliberately its own file rather than
another table inside ``romarr.json``: a large library is tens of thousands of
entries, and the main data file is rewritten on every recorded event. Keeping
them apart means an audit does not make every subsequent write heavier.

Two lookups, because netplay needs both directions:

  * ``by_sha1``   -- somebody offered me a dump; do I have exactly it?
  * ``for_game``  -- I want to offer this shelf entry; what is its hash?
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from dataclasses import asdict, dataclass
from pathlib import Path

log = logging.getLogger(__name__)

#: Titles arrive from two unrelated places -- a filename on disk and a
#: library server's metadata -- and they rarely agree on punctuation, region
#: tags or spacing. Matching on a normalised form is what lets an offer built
#: from the shelf find the file the audit hashed.
_TAGS = re.compile(r"[\(\[][^\)\]]*[\)\]]")
_NOISE = re.compile(r"[^a-z0-9]+")


def normalise(title: str) -> str:
    """A title reduced to what two sources are likely to agree on."""
    text = _TAGS.sub(" ", str(title or "").lower())
    return _NOISE.sub(" ", text).strip()


@dataclass
class Entry:
    """One file, as netplay needs to see it."""

    sha1: str
    name: str
    platform: str = ""
    verified: bool = False
    path: str = ""


class HashIndex:
    """Every hash the audit has computed, and the two ways to ask about it."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._lock = threading.RLock()
        self._by_sha1: dict[str, Entry] = {}
        #: (platform, normalised title) -> sha1. A game can legitimately have
        #: several dumps; the first verified one wins, because that is the one
        #: worth offering a stranger.
        self._by_game: dict[tuple[str, str], str] = {}

    # -- building ------------------------------------------------------------

    def add(self, sha1: str, name: str, platform: str = "",
            verified: bool = False, path: str = "") -> Entry | None:
        sha1 = str(sha1 or "").lower()
        if not sha1:
            return None
        entry = Entry(sha1=sha1, name=name, platform=platform,
                      verified=bool(verified), path=path)
        with self._lock:
            self._by_sha1[sha1] = entry
            key = (str(platform or "").lower(), normalise(name))
            if key[1]:
                existing = self._by_game.get(key)
                # Prefer a verified dump when one turns up later: an offer
                # should carry the copy this install stands behind.
                if existing is None or (
                        verified and not self._by_sha1[existing].verified):
                    self._by_game[key] = sha1
        return entry

    def clear_platform(self, platform: str) -> int:
        """Forget one platform, so re-auditing it replaces rather than adds."""
        slug = str(platform or "").lower()
        with self._lock:
            gone = [s for s, e in self._by_sha1.items()
                    if e.platform.lower() == slug]
            for sha1 in gone:
                del self._by_sha1[sha1]
            self._by_game = {k: v for k, v in self._by_game.items()
                             if k[0] != slug}
            return len(gone)

    # -- asking --------------------------------------------------------------

    def by_sha1(self, sha1: str) -> Entry | None:
        with self._lock:
            return self._by_sha1.get(str(sha1 or "").lower())

    def for_game(self, name: str, platform: str = "") -> Entry | None:
        """The dump to offer for a shelf entry, matched on a normalised title.

        Platform-qualified first, because the same title exists on six
        machines and offering the Mega Drive dump to somebody holding the
        SNES one is precisely the mismatch this whole mechanism exists to
        prevent.
        """
        key = normalise(name)
        if not key:
            return None
        with self._lock:
            sha1 = self._by_game.get((str(platform or "").lower(), key))
            if sha1 is None and not platform:
                for (_plat, title), candidate in self._by_game.items():
                    if title == key:
                        sha1 = candidate
                        break
            return self._by_sha1.get(sha1) if sha1 else None

    def entries(self) -> list[Entry]:
        with self._lock:
            return list(self._by_sha1.values())

    def __len__(self) -> int:
        with self._lock:
            return len(self._by_sha1)

    def platforms(self) -> dict[str, int]:
        with self._lock:
            counts: dict[str, int] = {}
            for entry in self._by_sha1.values():
                counts[entry.platform] = counts.get(entry.platform, 0) + 1
            return counts

    # -- persistence ---------------------------------------------------------

    def load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as err:
            log.warning("hash index at %s is unreadable (%s); starting empty",
                        self.path, err)
            return
        with self._lock:
            self._by_sha1.clear()
            self._by_game.clear()
        for item in raw.get("entries", []):
            if isinstance(item, dict):
                self.add(item.get("sha1", ""), item.get("name", ""),
                         item.get("platform", ""), item.get("verified", False),
                         item.get("path", ""))

    def save(self) -> None:
        with self._lock:
            payload = {"entries": [asdict(e) for e in self._by_sha1.values()]}
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(payload), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError as err:
            log.warning("could not write the hash index to %s: %s",
                        self.path, err)
