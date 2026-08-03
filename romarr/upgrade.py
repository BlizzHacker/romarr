"""Manual import, tags, and deciding whether a new release is an upgrade.

The last of the *arr surface, and the upgrade rule is the one worth arguing
about.

Radarr upgrades on a quality ladder: 1080p beats 720p, and the ladder is a
matter of taste. ROMs have no such ladder -- a dump is byte-identical to the
published cartridge or it is not -- so "better" here means something stronger
and much less arguable:

    an unverified file is upgraded by a verified one.

That is not a preference, it is a fact about the bytes, and it makes ROMarr's
upgrade the only one in this class of tool that cannot be wrong.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from .dat import BAD_DUMP, UNKNOWN, VERIFIED

log = logging.getLogger(__name__)


# --- upgrades ---------------------------------------------------------------

#: How much better each verification state is than the one below it.
#:
#: A bad dump ranks *below* unknown deliberately. Unknown means "no DAT knows
#: this", which is the normal state of homebrew and translations; bad-dump
#: means "a DAT knows what this should be and this is not it". Ranking them
#: equal would let a corrupt file sit forever next to a perfectly good
#: unrecognised one, with nothing ever replacing it.
VERDICT_RANK = {BAD_DUMP: 0, UNKNOWN: 1, VERIFIED: 2}


@dataclass(frozen=True)
class UpgradeDecision:
    upgrade: bool
    reason: str = ""


def is_upgrade(current_verdict: str, candidate_verdict: str, *,
               current_size: int = 0, candidate_size: int = 0,
               allow_sidegrade: bool = False) -> UpgradeDecision:
    """Whether a candidate should replace what is already in the library.

    Verification first, because it is the only comparison here that is a fact
    rather than a preference. Size is a tie-breaker and only with
    `allow_sidegrade`, since re-downloading a byte-identical game because the
    new copy is 3 KB larger is churn, not an upgrade.
    """
    current = VERDICT_RANK.get(current_verdict, VERDICT_RANK[UNKNOWN])
    candidate = VERDICT_RANK.get(candidate_verdict, VERDICT_RANK[UNKNOWN])

    if candidate > current:
        return UpgradeDecision(
            True, f"{candidate_verdict} beats {current_verdict}")
    if candidate < current:
        return UpgradeDecision(
            False, f"{candidate_verdict} is worse than {current_verdict}")

    if not allow_sidegrade:
        return UpgradeDecision(
            False, f"both are {current_verdict}; no upgrade")
    if candidate_size > current_size:
        return UpgradeDecision(
            True, f"same verdict, larger dump ({candidate_size} > {current_size})")
    return UpgradeDecision(False, "same verdict, not larger")


# --- tags -------------------------------------------------------------------

#: A tag is a label a person types, so it is normalised rather than trusted:
#: "Favourites", "favourites " and "FAVOURITES" are one tag, and treating them
#: as three produces a filter list nobody can use.
_TAG_CLEAN = re.compile(r"[^a-z0-9 _-]+")


def normalise_tag(tag: str) -> str:
    cleaned = _TAG_CLEAN.sub("", str(tag or "").strip().lower())
    return re.sub(r"\s+", " ", cleaned).strip()


def merge_tags(existing, adding=None, removing=None) -> list[str]:
    """The resulting tag set, normalised, deduplicated and ordered.

    Sorted rather than insertion-ordered because tags are rendered as a row of
    chips and a set that reorders itself on every edit is unreadable.
    """
    out = {normalise_tag(t) for t in (existing or []) if normalise_tag(t)}
    out |= {normalise_tag(t) for t in (adding or []) if normalise_tag(t)}
    out -= {normalise_tag(t) for t in (removing or []) if normalise_tag(t)}
    return sorted(out)


def matches_tags(item_tags, required) -> bool:
    """Whether an item carries every required tag."""
    have = {normalise_tag(t) for t in (item_tags or [])}
    return all(normalise_tag(t) in have for t in (required or []))


# --- manual import ----------------------------------------------------------

@dataclass
class Candidate:
    """One file a scan found, and what ROMarr thinks it is."""

    path: str
    filename: str
    size: int = 0
    platform: str = ""
    reason: str = ""
    verdict: str = UNKNOWN
    game: str = ""


@dataclass
class ScanResult:
    candidates: list[Candidate] = field(default_factory=list)
    skipped: int = 0
    error: str = ""


#: Never offered for import. Nothing here is a ROM, and listing them makes an
#: operator read past twenty rows of noise to find the four that matter.
_IGNORE_SUFFIXES = (
    ".nfo", ".txt", ".diz", ".sfv", ".jpg", ".jpeg", ".png", ".gif",
    ".url", ".md5", ".sha1", ".par2", ".exe", ".bat", ".db", ".ini",
    ".xml", ".dat", ".log", ".srm", ".state", ".sav",
)


def scan(directory, platforms, dats=None, *, limit: int = 2000) -> ScanResult:
    """Find importable files under a directory.

    Radarr calls this Manual Import and it exists for the same reason here:
    an operator arrives with a library already on disk, and telling them to
    re-download everything ROMarr could have adopted is absurd.

    The platform is guessed from the extension and the *parent directory
    name*, in that order. Extension alone is not enough -- `.bin` is Mega
    Drive, Atari 2600 and a PlayStation track, and `.iso` is five systems --
    so a file sitting in a folder called `psx` is taken at its word.
    """
    root = Path(directory)
    if not root.is_dir():
        return ScanResult(error=f"{root} is not a directory")

    by_extension: dict[str, list] = {}
    by_slug = {}
    for platform in platforms:
        by_slug[platform.slug.lower()] = platform
        for extension in platform.extensions:
            by_extension.setdefault(extension.lower(), []).append(platform)

    result = ScanResult()
    for path in sorted(root.rglob("*")):
        if len(result.candidates) >= limit:
            break
        if not path.is_file():
            continue
        suffix = path.suffix.lower()
        if suffix in _IGNORE_SUFFIXES:
            result.skipped += 1
            continue

        # The parent directory wins when it names a platform: it is a
        # deliberate statement by whoever laid the library out, and an
        # extension shared by five systems is not.
        parent = by_slug.get(path.parent.name.lower())
        options = by_extension.get(suffix) or []
        if parent is not None and (not options or parent in options):
            platform, reason = parent, f"in a folder named {path.parent.name!r}"
        elif len(options) == 1:
            platform, reason = options[0], f"{suffix} is unique to it"
        elif options:
            platform = options[0]
            reason = (f"{suffix} is used by "
                      f"{', '.join(p.slug for p in options)} -- confirm before "
                      "importing")
        else:
            result.skipped += 1
            continue

        candidate = Candidate(
            path=str(path), filename=path.name,
            size=path.stat().st_size,
            platform=platform.slug, reason=reason,
        )
        if dats is not None:
            from .dat import hash_file

            try:
                match = dats.lookup(**hash_file(path))
                candidate.verdict = match.status
                candidate.game = match.game
                # A DAT match is a stronger statement than either guess above,
                # and it names the actual game rather than the file.
                if match.status == VERIFIED and match.game:
                    candidate.reason = f"verified in a DAT as {match.game}"
            except OSError as exc:
                log.warning("could not hash %s: %s", path, exc)
        result.candidates.append(candidate)

    return result
