"""Operator policy over the scorer.

Two things every *arr has and ROMarr did not: a way to say "prefer this, never
that", and a way to say "never offer me this exact release again".

Both exist elsewhere as flat lists -- a set of words, a set of ids. The
difference here is that ROMarr's scorer already explains itself release by
release, so both of these can be *visible*:

  * a preferred term appears in `Judgement.why()` with the points it added, so
    an operator can see their own policy working rather than infer it from a
    changed outcome;
  * a blocklist entry carries the reason it was blocked and when, so "why will
    it never take this one" has an answer that is not "look in the logs".

"Why did it take that one" and "why will it never take this one" are the two
questions anybody actually asks about an *arr, and a flat list answers
neither.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

#: A term wrapped in slashes is a regular expression, the way Radarr's release
#: profiles work. The cases that need one are real: `(Rev 1)` but not
#: `(Rev 10)` cannot be expressed as a substring.
_REGEX_TERM = re.compile(r"^/(.*)/$", re.DOTALL)


def _matches(term: str, haystack: str) -> bool:
    """Whether `term` -- literal or `/regex/` -- appears in `haystack`."""
    term = str(term or "")
    if not term:
        return False
    pattern = _REGEX_TERM.match(term)
    if pattern:
        try:
            return re.search(pattern.group(1), haystack, re.IGNORECASE) is not None
        except re.error as exc:
            # A bad pattern is an operator typo, and taking every search down
            # with it would turn a one-character mistake into a dead install.
            log.warning("ignoring unparseable release-profile term %r: %s",
                        term, exc)
            return False
    return term.lower() in haystack


@dataclass
class ReleaseProfile:
    """Preferred, excluded and required terms.

    `preferred` carries its own weight per term rather than a single global
    bonus, because "I mildly like this" and "I will take nothing else" are
    different instructions and one number cannot express both.
    """

    #: (term, points). Negative is a penalty, which is how "avoid but do not
    #: refuse" is written.
    preferred: list[tuple[str, int]] = field(default_factory=list)
    #: Any match refuses the release outright.
    excluded: list[str] = field(default_factory=list)
    #: Every term must appear, or the release is refused.
    required: list[str] = field(default_factory=list)

    @classmethod
    def from_settings(cls, settings: dict) -> "ReleaseProfile":
        """Build from stored settings, tolerating whatever shape they are in.

        Preferred terms are stored as a list of `{term, score}` because a UI
        edits them as rows, and a tuple in JSON comes back as a list.
        """
        preferred = []
        for entry in settings.get("preferred_terms") or []:
            if isinstance(entry, dict):
                term = str(entry.get("term") or "")
                try:
                    points = int(entry.get("score") or 0)
                except (TypeError, ValueError):
                    points = 0
            elif isinstance(entry, (list, tuple)) and len(entry) == 2:
                term, points = str(entry[0]), int(entry[1] or 0)
            else:
                term, points = str(entry), 25
            if term:
                preferred.append((term, points))
        return cls(
            preferred=preferred,
            excluded=[str(t) for t in (settings.get("excluded_terms") or []) if t],
            required=[str(t) for t in (settings.get("required_terms") or []) if t],
        )

    def refusal(self, lowered: str) -> str:
        """Why this release is refused outright, or "" if it is not."""
        for term in self.excluded:
            if _matches(term, lowered):
                return f"excluded by release profile ({term!r})"
        for term in self.required:
            if not _matches(term, lowered):
                return f"missing a required term ({term!r})"
        return ""

    def adjustments(self, lowered: str) -> list[tuple[int, str]]:
        """Point changes this profile makes, ready for `Judgement.reasons`."""
        out = []
        for term, points in self.preferred:
            if points and _matches(term, lowered):
                out.append((points, f"release profile prefers {term!r}"))
        return out

    def __bool__(self) -> bool:
        return bool(self.preferred or self.excluded or self.required)


_INFOHASH = re.compile(r"btih:([0-9a-fA-F]{40}|[0-9a-zA-Z]{32})")


def release_id(release) -> str:
    """A stable identity for one release.

    The infohash when there is one, because indexers rewrite titles freely and
    two results for the same torrent have to block as one. Otherwise the
    indexer, title and size together -- enough that a re-run of the same search
    recognises the same entry, and specific enough that a different dump of the
    same game is not caught by mistake.
    """
    found = _INFOHASH.search(str(getattr(release, "download_url", "") or ""))
    if found:
        return f"btih:{found.group(1).lower()}"
    return (f"{getattr(release, 'indexer', '')}|"
            f"{getattr(release, 'title', '')}|"
            f"{getattr(release, 'size', 0)}").lower()


class Blocklist:
    """Releases that must never be taken again, and why."""

    def __init__(self):
        self._entries: dict[str, dict] = {}

    def add(self, release, reason: str = "") -> dict:
        entry = {
            "id": release_id(release),
            "title": getattr(release, "title", ""),
            "indexer": getattr(release, "indexer", ""),
            "size": getattr(release, "size", 0),
            "reason": reason or "blocked by the operator",
            "blocked_at": int(time.time()),
        }
        self._entries[entry["id"]] = entry
        return entry

    def remove(self, entry_id: str) -> bool:
        return self._entries.pop(str(entry_id), None) is not None

    def reason_for(self, release) -> str:
        entry = self._entries.get(release_id(release))
        return entry["reason"] if entry else ""

    def __contains__(self, release) -> bool:
        return release_id(release) in self._entries

    def as_items(self) -> list[dict]:
        return sorted(self._entries.values(),
                      key=lambda e: -e.get("blocked_at", 0))

    @classmethod
    def from_items(cls, items) -> "Blocklist":
        out = cls()
        for item in items or []:
            if isinstance(item, dict) and item.get("id"):
                out._entries[str(item["id"])] = dict(item)
        return out

    def __len__(self) -> int:
        return len(self._entries)
