"""Whole sets, rather than one title at a time.

Two questions people actually ask that ROMarr could not answer:

  "Can I tell it I want a full set?"        -- and it had no notion of a set.
  "Why did it pick that dump?"              -- and 1G1R ran with no way to look.

Both are already answerable from what is loaded: a DAT lists every title a
system ever had, and `Dat.one_game_one_rom` already decides which dump of a
title wins. What was missing is the layer that compares that list against what
is on the shelf and says what to do about the difference.

Nothing here re-implements 1G1R. `Dat.one_game_one_rom` stays the only
selector, so a set built for cleaning and a grab made while acquiring cannot
disagree about which region wins -- they read the same ladder out of
`store.settings["preferred_regions"]`. `explain` exists to *show* that
decision, not to make a second one.

The plan is a report, not an action. Acquiring is a separate step that works
through it in batches and can be stopped and resumed, because a 3,000-title
set is not something to start as one all-or-nothing operation.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .dat import BAD_DUMP, UNKNOWN, VERIFIED, Dat, Game

#: A title present on disk, and how much we trust it.
PRESENT_VERIFIED = "verified"
PRESENT_UNKNOWN = "unknown"
PRESENT_BAD = "bad"
MISSING = "missing"

#: Categories a DAT marks in the name. Kept as words rather than a regex per
#: category so the reason shown to the operator is the word that matched.
#:
#: `translation` is its own category, split out from `hack` on purpose: a
#: fan translation of a Japan-only game is the thing that MAKES that game
#: playable, not a novelty to exclude. The default set still leaves it out
#: (a translation is not the published dump), but the operator can now say
#: "fill the gaps with translations" without also un-excluding trainers and
#: pirate carts.
_FLAGS = {
    "proto": ("proto", "prototype"),
    "beta": ("beta",),
    "demo": ("demo", "sample", "kiosk"),
    "hack": ("hack", "trainer"),
    "translation": ("translation", "translated"),
    "unlicensed": ("unl", "unlicensed", "pirate", "aftermarket"),
}

_PAREN = re.compile(r"[\(\[]([^\)\]]*)[\)\]]")

#: The scene's translation markers, which are not plain words: `[T-En]`,
#: `[T+Eng]`, `[T-En by Foo]`. Matched on the bracket group as a whole
#: because the `T-` / `T+` prefix is the signal, not a standalone token.
_TRANSLATION_MARK = re.compile(r"\bt[+-]\s*(?:en|eng|english)\b", re.IGNORECASE)


def is_translation(name: str) -> bool:
    """Whether a DAT/release name is an English fan translation.

    Both conventions: the No-Intro `(Translation)` word and the scene's
    `[T-En]` / `[T+Eng]` marker.
    """
    if _TRANSLATION_MARK.search(name or ""):
        return True
    return "translation" in flags_in(name)


def flags_in(name: str) -> set[str]:
    """Which categories a DAT name declares.

    Read from the bracketed groups only. A game genuinely called "Beta Blocker"
    is not a beta, and matching the bare word anywhere in the name would drop
    it from every set the operator asked to exclude betas from.
    """
    words = set()
    for group in _PAREN.findall(name or ""):
        for part in re.split(r"[,\s]+", group.lower()):
            words.add(part.strip())
    flags = {flag for flag, terms in _FLAGS.items() if words & set(terms)}
    if _TRANSLATION_MARK.search(name or ""):
        flags.add("translation")
    return flags


#: What to do with an English fan translation in a 1G1R set. The whole point
#: of the split from `hack`: a Japan-only game's translation is often the
#: only way to actually play it, so "exclude everything that is not the
#: published dump" is the wrong default for exactly this case.
#:
#:   exclude   -- translations are not in the set (the safe default)
#:   fill      -- when a title has NO dump in your preferred regions, use a
#:                translation instead of leaving it Japanese-only
#:   prefer    -- use a translation over the region winner whenever one exists
#:   keep_both -- like fill, but keep the original dump too; the translation
#:                is filed in the platform's Translations subfolder so RomM
#:                shows it as a variant rather than a second game
TRANSLATION_POLICIES = ("exclude", "fill", "prefer", "keep_both")


@dataclass(frozen=True)
class Policy:
    """What the operator asked for."""

    regions: tuple[str, ...] = ("usa", "world", "europe", "japan")
    one_game_one_rom: bool = True
    #: Categories to leave out. Everything not listed is included.
    #: `translation` is here by default and lifted when the translation
    #: policy asks for it, so turning on "fill" does not also re-admit
    #: trainers and pirate carts.
    exclude: frozenset[str] = frozenset({"proto", "beta", "demo", "hack",
                                         "translation", "unlicensed"})
    #: Only count a file as present when a DAT verified it.
    verified_only: bool = False
    #: How translations are treated. See TRANSLATION_POLICIES.
    translation_policy: str = "exclude"

    @property
    def uses_translations(self) -> bool:
        return self.translation_policy in ("fill", "prefer", "keep_both")

    def wants(self, name: str) -> tuple[bool, str]:
        """Whether this title belongs in the set, and why not if it does not."""
        got = flags_in(name) & self.exclude
        # A translation the policy actively uses is not "excluded" -- the
        # planner decides per group whether to take it, below.
        if self.uses_translations:
            got = got - {"translation"}
        if got:
            return False, "excluded: " + ", ".join(sorted(got))
        return True, ""


@dataclass
class Title:
    """One row of the plan."""

    name: str
    parent: str
    status: str = MISSING
    #: Why this dump won its group, in the operator's words.
    chosen_because: str = ""
    #: (name, why it lost) for every other dump of the same game.
    discarded: list[tuple[str, str]] = field(default_factory=list)
    #: Present but outside the preferred regions -- worth surfacing, because
    #: "missing" and "only available in Japanese" are different problems.
    outside_preference: bool = False
    #: This row is an English fan translation. Acquisition files it in the
    #: platform's Translations subfolder under the keep_both policy.
    is_translation: bool = False


@dataclass
class Plan:
    """What a set would take, before anything is acquired."""

    dat: str = ""
    dat_version: str = ""
    policy: Policy = field(default_factory=Policy)
    titles: list[Title] = field(default_factory=list)
    #: Titles the policy excluded, kept as a count so the total still adds up.
    excluded: int = 0

    def counts(self) -> dict[str, int]:
        out = {PRESENT_VERIFIED: 0, PRESENT_UNKNOWN: 0, PRESENT_BAD: 0,
               MISSING: 0}
        for title in self.titles:
            out[title.status] = out.get(title.status, 0) + 1
        out["expected"] = len(self.titles)
        out["excluded"] = self.excluded
        out["have"] = out["expected"] - out[MISSING]
        return out

    def missing(self) -> list[Title]:
        return [t for t in self.titles if t.status == MISSING]


def _region_rank(name: str, order: tuple[str, ...]) -> int:
    from .dat import _regions_in
    found = _regions_in(name)
    return min((order.index(r) for r in found if r in order), default=len(order))


def _best_translation(members: list[Game], order: tuple[str, ...]) -> "Game | None":
    """The English translation in a group most worth taking, or None.

    Prefers a translation of a preferred-region base over one of an obscure
    region, then the parent over a clone, then name -- the same shape the
    region ranking uses, so the choice is explainable the same way.
    """
    translations = [g for g in members if is_translation(g.name)]
    if not translations:
        return None
    return min(translations,
               key=lambda g: (_region_rank(g.name, order),
                              0 if not g.cloneof else 1, g.name))


def explain(dat: Dat, winner: Game, group: list[Game],
            regions: tuple[str, ...]) -> tuple[str, list[tuple[str, str]]]:
    """Why `winner` won, and why each of the others did not.

    Phrased from the same inputs `one_game_one_rom` ranks on -- region
    position, then parent-over-clone, then name -- so the explanation cannot
    drift from the decision. If it ever disagrees, one of the two is wrong and
    a test says which.
    """
    order = tuple(r.lower() for r in regions)
    rank = _region_rank(winner.name, order)
    if rank < len(order):
        because = f"region {order[rank].upper()} is {rank + 1} of " \
                  f"{len(order)} in your preference"
    else:
        because = "no dump matched your preferred regions, so the set keeps " \
                  "this one rather than losing the game"
    if not winner.cloneof:
        because += "; it is the parent dump"

    lost = []
    for other in group:
        if other.name == winner.name:
            continue
        other_rank = _region_rank(other.name, order)
        if other_rank > rank:
            why = (f"region ranks lower ({order[other_rank].upper()})"
                   if other_rank < len(order)
                   else "region not in your preference")
        elif other_rank == rank and not winner.cloneof and other.cloneof:
            why = "clone of the chosen dump, same region rank"
        else:
            why = "same region rank; the chosen name sorts first"
        lost.append((other.name, why))
    return because, lost


def build_plan(dat: Dat, present: dict[str, str], policy: Policy) -> Plan:
    """Compare a DAT against what is on the shelf.

    `present` maps a DAT game name to one of the PRESENT_* verdicts. The caller
    owns that mapping because only it knows whether a library is a folder it
    can hash or a server it can only ask.
    """
    plan = Plan(dat=dat.name, dat_version=dat.version, policy=policy)

    groups: dict[str, list[Game]] = {}
    for game in dat.games.values():
        groups.setdefault(dat.group_key(game.name), []).append(game)

    chosen: dict[str, Game] = (dat.one_game_one_rom(list(policy.regions))
                               if policy.one_game_one_rom else {})
    order = tuple(r.lower() for r in policy.regions)

    for parent, members in sorted(groups.items()):
        if policy.one_game_one_rom:
            winner = next((g for g in members if g.name in chosen), None)
            if winner is None:
                continue
            wanted, _ = policy.wants(winner.name)
            if not wanted:
                # The chosen dump is excluded, but a sibling may not be -- a
                # set that excludes prototypes should still contain the game
                # when a real release exists.
                alternatives = [g for g in members if policy.wants(g.name)[0]]
                if not alternatives:
                    plan.excluded += 1
                    continue
                winner = min(alternatives,
                             key=lambda g: (_region_rank(g.name, order),
                                            0 if not g.cloneof else 1, g.name))

            # Translation policy. A group may hold an English fan translation
            # of a title whose only real dump is Japanese; whether to reach
            # for it -- and whether to keep the original alongside -- is the
            # operator's call, not a guess.
            winner_outside = _region_rank(winner.name, order) >= len(order)
            translation = _best_translation(members, order)
            if policy.uses_translations and translation is not None:
                take_it = (policy.translation_policy == "prefer"
                           or winner_outside)  # fill / keep_both: gap only
                if take_it and policy.translation_policy == "keep_both":
                    # Both: the original winner stays, and the translation is
                    # a second row filed under Translations/.
                    because, lost = explain(dat, winner, members, policy.regions)
                    plan.titles.append(Title(
                        name=winner.name, parent=parent,
                        status=present.get(winner.name, MISSING),
                        chosen_because=because, discarded=lost,
                        outside_preference=winner_outside))
                    plan.titles.append(Title(
                        name=translation.name, parent=parent,
                        status=present.get(translation.name, MISSING),
                        chosen_because="English translation, kept as a variant",
                        outside_preference=False, is_translation=True))
                    continue
                if take_it:
                    # fill / prefer: the translation replaces the winner.
                    plan.titles.append(Title(
                        name=translation.name, parent=parent,
                        status=present.get(translation.name, MISSING),
                        chosen_because=(
                            "English translation used because no dump exists "
                            "in your preferred regions" if winner_outside
                            else "English translation preferred over "
                                 f"{winner.name}"),
                        outside_preference=False, is_translation=True))
                    continue

            because, lost = explain(dat, winner, members, policy.regions)
            plan.titles.append(Title(
                name=winner.name, parent=parent,
                status=present.get(winner.name, MISSING),
                chosen_because=because, discarded=lost,
                outside_preference=winner_outside,
            ))
        else:
            for game in sorted(members, key=lambda g: g.name):
                wanted, why = policy.wants(game.name)
                if not wanted:
                    plan.excluded += 1
                    continue
                plan.titles.append(Title(
                    name=game.name, parent=parent,
                    status=present.get(game.name, MISSING),
                    chosen_because="every dump kept (1G1R off)",
                    outside_preference=_region_rank(game.name, order) >= len(order),
                ))
    return plan


# ------------------------------------------------------------- acquisition --
#
# A 3,000-title set is not one operation. It is 3,000 of them, most of which
# will succeed, some of which will not, and all of which have to survive the
# service being restarted halfway through. So the batch is state in the store
# rather than a loop holding everything in memory: what was asked for, what has
# been done, and what failed and why.

BATCH_PENDING = "pending"
BATCH_RUNNING = "running"
BATCH_PAUSED = "paused"
BATCH_DONE = "done"


@dataclass
class Batch:
    """A set acquisition, resumable."""

    id: str = ""
    platform: str = ""
    dat: str = ""
    status: str = BATCH_PENDING
    #: Titles still to request, in order.
    queue: list[str] = field(default_factory=list)
    done: list[str] = field(default_factory=list)
    #: name -> why it failed. Kept so a retry can target only these.
    failed: dict[str, str] = field(default_factory=dict)
    #: Requests to make per pass. Indexers and download clients are shared with
    #: whatever else the operator runs, and a set is not more important than
    #: their other traffic.
    per_pass: int = 5

    def progress(self) -> dict:
        total = len(self.queue) + len(self.done) + len(self.failed)
        return {
            "id": self.id, "platform": self.platform, "dat": self.dat,
            "status": self.status, "total": total,
            "done": len(self.done), "failed": len(self.failed),
            "remaining": len(self.queue),
            "percent": round(100 * (len(self.done) + len(self.failed))
                             / total, 1) if total else 0.0,
        }

    def take(self, count: int | None = None) -> list[str]:
        """The next slice to work on, removed from the queue."""
        size = count if count is not None else self.per_pass
        slice_, self.queue = self.queue[:size], self.queue[size:]
        return slice_

    def record(self, name: str, ok: bool, reason: str = "") -> None:
        if ok:
            self.done.append(name)
            self.failed.pop(name, None)
        else:
            self.failed[name] = reason or "no release found"
        if not self.queue:
            self.status = BATCH_DONE

    def retry_failed(self) -> int:
        """Put the failures back in the queue. Returns how many."""
        names = list(self.failed)
        self.queue.extend(names)
        self.failed.clear()
        if names:
            self.status = BATCH_PENDING
        return len(names)

    def to_dict(self) -> dict:
        return {"id": self.id, "platform": self.platform, "dat": self.dat,
                "status": self.status, "queue": list(self.queue),
                "done": list(self.done), "failed": dict(self.failed),
                "per_pass": self.per_pass}

    @classmethod
    def from_dict(cls, raw: dict) -> "Batch":
        return cls(
            id=str(raw.get("id") or ""),
            platform=str(raw.get("platform") or ""),
            dat=str(raw.get("dat") or ""),
            status=str(raw.get("status") or BATCH_PENDING),
            queue=list(raw.get("queue") or []),
            done=list(raw.get("done") or []),
            failed=dict(raw.get("failed") or {}),
            per_pass=int(raw.get("per_pass") or 5),
        )
