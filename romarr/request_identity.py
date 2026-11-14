"""Conservative, pure policy core for ROMarr Part 2 (NOT wired to live imports).

No filesystem, network, DAT parsing, Docker or application writes occur here.
Integration MUST pass trusted platform provenance and bind the decision to a
fresh, unchanged queue/request and source identity before publication.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Callable, Iterable

_RELEASE_TAG = re.compile(
    r"(?:USA|Europe|Japan|World|Rev(?:ision)?[ .-]*(?:\d+[a-z]?|[a-z])|En|Fr|De)",
    re.IGNORECASE,
)

REASON_CODES = frozenset({
    "MATCH_CONFIRMED", "REQUEST_MISSING", "SOURCE_INCOMPLETE",
    "SOURCE_UNSAFE", "UNSUPPORTED_FORMAT", "SAFETY_LIMIT",
    "IMPORT_FAILED", "DAT_UNKNOWN", "DAT_FAILED", "DAT_IDENTITY_MISSING",
    "DAT_PLATFORM_UNCONFIRMED", "DAT_PLATFORM_MISMATCH", "REQUEST_TITLE_MISMATCH",
    "AMBIGUOUS_MATCH", "REVIEW_REQUIRED",
})


@dataclass(frozen=True)
class Candidate:
    """One independently verified ROM set, not one filename or archive member.

    verified_game and dat_catalog are results from actual bytes/DAT lookup,
    not from the release filename. `verified_platform` MUST be from trusted
    catalog-to-platform mapping, not a guessed DAT display-name substring.
    """
    status: str  # verified | unknown | bad-dump
    verified_game: str = ""
    dat_catalog: str = ""
    verified_platform: str = ""
    complete: bool = True


@dataclass(frozen=True)
class Decision:
    """Pre-publication decision for exactly one matched request/import job."""
    action: str  # import | hold | reject
    code: str
    message: str

    def __post_init__(self) -> None:
        if self.action not in ("import", "hold", "reject"):
            raise ValueError("invalid decision action")
        if self.code not in REASON_CODES:
            raise ValueError("invalid stable reason")


def _strict_title_key(value: str) -> str:
    """Fold harmless punctuation and known release tags; never fuzzy-match games.

    Deliberately excludes DAT region/REV tags but preserves sequel numbers,
    subtitles and content words. A false negative holds for review; a false
    positive could publish the wrong game, so prefer strict evidence.
    """
    value = unicodedata.normalize("NFKC", str(value or ""))
    # Fold Latin accents without erasing meaningful combining marks in other
    # scripts (e.g. Japanese kana); never drop non-ASCII title letters.
    value = "".join(
        "".join(part for part in unicodedata.normalize("NFKD", char)
                if not unicodedata.combining(part))
        if "LATIN" in unicodedata.name(char, "") else char
        for char in value
    )
    # Revision identifiers are digits or one letter, not words like Revenge.
    # Preserve content words; discard only groups consisting entirely of
    # recognized comma-separated region, language or revision tags.
    value = re.sub(
        r"\(([^()]*)\)",
        lambda match: " " if all(_RELEASE_TAG.fullmatch(token.strip())
                                  for token in match.group(1).split(","))
                              else match.group(0),
        value,
    )
    # Combining marks carry title identity in many non-Latin scripts. NFKC
    # leaves Devanagari vowel signs separate from their base consonants.
    return " ".join("".join(
        char if (char.isalnum() or unicodedata.category(char).startswith("M"))
        else " " for char in value.casefold()
    ).split())


def decide_request(
    requested_game: str,
    requested_platform: str,
    candidates: Iterable[Candidate],
    *,
    source_complete: bool = True,
    source_safe: bool = True,
    safety_limit_hit: bool = False,
    source_supported: bool = True,
    reviewed: bool = False,
    title_equivalent: Callable[[str, str], bool] | None = None,
) -> Decision:
    """Require one unambiguous DAT-confirmed title+platform before auto-import.

    `reviewed` does not bypass a proven wrong-game or bad-DAT result. Existing
    bundle/attachment approval remains a separate ROMarr interface; this core
    does not claim to implement its durable review token or route checks.
    """
    def d(action: str, code: str, message: str) -> Decision:
        return Decision(action, code, message)

    if not _strict_title_key(requested_game) or not str(requested_platform or "").strip():
        return d("hold", "REQUEST_MISSING", "Request identity is incomplete; inspect the Queue entry.")
    if not source_complete:
        return d("hold", "SOURCE_INCOMPLETE", "Download selection is incomplete; inspect selected files.")
    if not source_safe:
        return d("reject", "SOURCE_UNSAFE", "Source inspection rejected unsafe content; nothing imported.")
    if safety_limit_hit:
        return d("reject", "SAFETY_LIMIT", "Archive safety limit was reached; nothing imported.")
    if not source_supported:
        return d("hold", "UNSUPPORTED_FORMAT", "Source format requires manual review.")
    choices = tuple(candidates)
    if not choices:
        return d("hold", "DAT_UNKNOWN", "No eligible DAT-identified game was found; review the download.")
    if any(not isinstance(c, Candidate) for c in choices):
        raise TypeError("candidates must be Candidate records")
    if any(not c.complete for c in choices):
        return d("hold", "SOURCE_INCOMPLETE", "An eligible ROM set is incomplete; review the download.")
    if any(c.status not in ("verified", "unknown", "bad-dump") for c in choices):
        return d("hold", "REVIEW_REQUIRED", "Verification result was not recognized; review required.")
    if any(c.status == "bad-dump" for c in choices):
        return d("reject", "DAT_FAILED", "DAT verification failed for one or more ROM sets; nothing imported.")
    if any(c.status == "unknown" for c in choices):
        return d("hold", "DAT_UNKNOWN", "DAT identity is unknown, not a confirmed bad dump; review required.")
    if any(not c.verified_game.strip() or not c.dat_catalog.strip() for c in choices):
        return d("hold", "DAT_IDENTITY_MISSING", "DAT verified bytes but did not supply a reliable game identity.")
    if any(not c.verified_platform.strip() for c in choices):
        return d("hold", "DAT_PLATFORM_UNCONFIRMED", "DAT platform cannot be established safely; review required.")
    if any(c.verified_platform.casefold() != requested_platform.casefold() for c in choices):
        return d("hold", "DAT_PLATFORM_MISMATCH", "Verified ROM platform differs from the request; review required.")
    if len(choices) != 1:
        return d("hold", "AMBIGUOUS_MATCH", "Multiple verified ROM sets require explicit review before import.")
    checker = title_equivalent or (lambda a, b: _strict_title_key(a) == _strict_title_key(b))
    if not checker(choices[0].verified_game, requested_game):
        return d("hold", "REQUEST_TITLE_MISMATCH", "DAT game title differs from the requested game; nothing imported.")
    return d("import", "MATCH_CONFIRMED", "One DAT-verified title and platform match the request.")
