"""Read-only request matching of a selected import source against loaded DATs.

This adapter does not import files, change queue state, or write to a library.
Catalog-to-platform associations must be explicitly supplied; a DAT display
name alone is insufficient provenance. A caller must revalidate source/queue
identity and enforce the decision inside its publication transaction.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .request_identity import Candidate, Decision, decide_request


@dataclass(frozen=True)
class RequestEvidence:
    """One aggregate decision and the matched DAT record when unambiguous."""

    decision: Decision
    verification: Any = None


def evaluate_source_request(
    source: Any,
    selected_sets: Any,
    dats: Any,
    requested_game: str,
    requested_platform: str,
    *,
    catalog_platforms: Mapping[str, str] | None = None,
    reviewed: bool = False,
) -> RequestEvidence:
    """Hash selected members once; fail closed on uncertain DAT provenance.

    Only one requested game/set is automatically eligible. Every member of a
    multi-file set must verify to the same game and same catalog; a different
    track game, missing hash, cross-catalog collision, or unconfigured catalog
    returns a hold/reject decision, never a publish authorization. The source
    is already inspected by the caller and is never modified here.
    """
    def hold(code: str, message: str) -> RequestEvidence:
        return RequestEvidence(Decision("hold", code, message))

    groups = tuple(selected_sets)
    if len(groups) != 1:
        return hold("AMBIGUOUS_MATCH", "A single requested game needs one selected ROM set; review the selection.")
    members = tuple(groups[0].members)
    if not members:
        return hold("SOURCE_INCOMPLETE", "No ROM members were selected; review the source.")
    if dats is None or not callable(getattr(dats, "verified_candidates", None)):
        return hold("DAT_UNKNOWN", "No usable DAT candidate index is loaded; review the source.")
    if catalog_platforms is not None and not isinstance(catalog_platforms, Mapping):
        return hold("DAT_PLATFORM_UNCONFIRMED", "DAT platform mapping is invalid; review configuration.")

    common: dict[tuple[str, str], Any] | None = None
    for member in members:
        suffix = Path(str(member).replace("\\", "/")).suffix
        try:
            hashes = source.hash(member, suffix)
        except Exception:
            return hold("SOURCE_INCOMPLETE", "A selected ROM member could not be read; review the source.")
        if not isinstance(hashes, dict) or not hashes:
            return hold("SOURCE_INCOMPLETE", "A selected ROM member has no usable checksum; review the source.")
        matches = tuple(dats.verified_candidates(**hashes))
        if not matches:
            legacy = dats.lookup(**hashes)
            status = getattr(legacy, "status", "unknown")
            candidate = Candidate("bad-dump" if status == "bad-dump" else "unknown")
            return RequestEvidence(decide_request(requested_game, requested_platform, [candidate]))
        by_identity = {(m.dat, m.game): m for m in matches
                       if m.status == "verified" and m.dat and m.game}
        if len(by_identity) != len(matches):
            return hold("AMBIGUOUS_MATCH", "DAT identities are incomplete or duplicated; review the result.")
        if common is None:
            common = by_identity
        else:
            common = {key: match for key, match in common.items() if key in by_identity}
        if not common:
            return hold("AMBIGUOUS_MATCH", "Members do not share a single verified DAT identity; review the set.")

    candidates = [Candidate("verified", game, catalog,
                            str((catalog_platforms or {}).get(catalog) or ""))
                  for catalog, game in common]
    decision = decide_request(requested_game, requested_platform, candidates, reviewed=reviewed)
    if decision.action != "import":
        return RequestEvidence(decision)

    # A verified track does not establish a complete DAT game. In particular,
    # a lone BIN may verify even though its DAT game declares more tracks.
    # Match exposes only one ROM; use the catalog's authoritative game record.
    only_match = next(iter(common.values()))
    member_count = getattr(dats, "game_rom_count", None)
    if not callable(member_count):
        return hold("DAT_IDENTITY_MISSING", "DAT game membership is unavailable; review required.")
    try:
        required = member_count(only_match)
    except (KeyError, ValueError, TypeError, AttributeError):
        return hold("DAT_IDENTITY_MISSING", "DAT game membership could not be established; review required.")
    if type(required) is not int or required < 1:
        return hold("DAT_IDENTITY_MISSING", "DAT game membership is ambiguous; review required.")
    if required > len(members):
        return hold("SOURCE_INCOMPLETE", "The selected ROM is missing required DAT game members; nothing imported.")
    if required != 1 or len(members) != 1:
        return hold("REVIEW_REQUIRED", "Multi-file DAT games need atomic publication; review required.")
    return RequestEvidence(decision, only_match)
