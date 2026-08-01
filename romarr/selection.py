"""Choosing which release to grab, and which file inside it is the ROM.

Both decisions are pure functions over metadata, so they are unit-testable
without a network, a download client, or a filesystem. That matters because
they are where this pipeline goes wrong in practice: grab the wrong release and
you download 40GB of a PC port instead of a 512KB cartridge; pick the wrong file
and RomM imports a readme.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .platforms import Platform

# Newznab/Torznab categories. 1000-1999 is Console, 4000-4999 is PC; only the
# console range and the PC-games sub-range are of interest here.
CONSOLE_CATEGORIES = range(1000, 2000)
PC_GAME_CATEGORIES = range(4050, 4070)

# Platform names that, when they appear in a title, mean the release is for a
# DIFFERENT system than the one asked for. A search for a SNES game happily
# returns "[Nintendo Switch] Super Mario World (NSP)" -- the title matches and
# the size is plausible, so nothing else in the scorer rejects it, and you end
# up importing a Switch package into your SNES folder.
FOREIGN_PLATFORM_MARKERS = {
    "nintendo switch", "switch", "nsp", "xci",
    "playstation", "ps1", "ps2", "ps3", "ps4", "ps5", "psp", "psx", "vita",
    "xbox", "x360", "xbla",
    "gamecube", "wii", "wiiu", "wii u", "3ds", "nds", "ds",
    "android", "apk", "ios",
    "pc", "windows", "steam", "gog", "repack",
    "dreamcast", "saturn", "psvr",
    # Repackaged for a console that is not this one. A Wii Virtual Console WAD
    # of a Genesis game is not a Genesis cartridge: it cannot be played as one,
    # and it cannot even be imported, since no Genesis extension appears among
    # its files. Reported live -- a Genesis request grabbed
    # "Phantasy.Star.IV.USA.SMD.Virtual.Console", 14MB of .wad and .par2.
    #
    # "wii" alone does not catch it, because the title says Virtual Console and
    # never says Wii.
    "virtual console", "wiiware", "wad", "eshop",
}

# Names for "several games in one box", which a single-game request cannot use.
# Kept apart from _JUNK_MARKERS because these are matched as whole words: the
# substring "set" is inside "Sunset", and "classics" must not catch "Classic".
COMPILATION_MARKERS = (
    "classics", "collection", "compilation", "anthology", "romset",
    "rom set", "complete set", "full set", "no-intro", "nointro", "goodgen",
    "tosec", "redump", "everdrive", "megaset", "mega set", "all games",
)

# Words that mean "this is not a plain cartridge dump". Matched as substrings,
# which is why the stem "translat" is listed rather than "translation": it also
# catches "translated", "retranslated" and "retranslation", and RetroWithin
# publishes "Chrono Trigger (Retranslated)" -- a hack the exact word missed.
_JUNK_MARKERS = (
    "beta", "proto", "prototype", "demo", "sample", "hack", "patched",
    "translat", "trainer", "repack", "update only", "dlc",
)

# Language markers, and what they mean here.
#
# A release title says what was DONE to a ROM at least as often as it says
# which ROM it is, and none of that was being read. That is how a request for
# Chrono Trigger was answered with "[SNES] Chrono Trigger [RUS] [jRPG] [by
# Chief-NET] [1995]" -- a Russian fan translation. It is a genuine SNES
# cartridge dump of the right game, so every check above passes; it was simply
# the best-seeded of thirteen results and nothing in its title says "hack" or
# "translation" in so many words.
#
# Japanese is deliberately absent. "(J)" on a cartridge is a region, not a
# translation -- the game as published -- and the region ladder already ranks
# it below USA, World and Europe. Penalising it here would push a legitimate
# Japanese dump below a Russian fan patch, which is the same bug facing the
# other way.
#
# Two-letter codes are absent too. "(E)" is Europe and "it"/"es"/"de" appear in
# the No-Intro language field of releases that are perfectly fine; the gain is
# not worth the collision.
_NON_ENGLISH_MARKERS = (
    "rus", "russian", "ger", "german", "deu",
    "fra", "fre", "french", "esp", "spa", "spanish",
    "ita", "italian", "por", "portuguese", "pt br", "ptbr",
    "kor", "korean", "chs", "cht", "chinese",
    "pol", "polish", "dut", "ned", "dutch",
    "swe", "swedish", "tur", "turkish", "ukr", "ukrainian",
)

# GoodTools translation marks. "(T-Eng)" is a superseded translation patch,
# "(T+Rus)" the current one; either way the ROM has been altered after it left
# the cartridge. Included even when the target language is English, because a
# fan patch is still not the published game.
_TRANSLATION_TAG = re.compile(r"[\[(]\s*t[-+]\s*[a-z]{2,4}[^\])]*[\])]")

# "[MULTI5]", "[MULTi8-ENG]", "MULTi10-PLAZA". In every result seen this marks
# a localised PC or emulator release, never a cartridge dump. `\d*` keeps it
# off "multiplayer".
_MULTI_LANGUAGE = re.compile(r"\bmulti-?\d*\b")

# "[by Chief-NET]", "by progameroms", "by SMW Central", "[By Destrap]". On a
# cartridge ROM a credit means somebody MADE this -- a hack, a translation, a
# repackage -- rather than dumped it. A published cartridge has a publisher in
# its title, not an author.
_CREDITED_TO_A_GROUP = re.compile(r"\bby\s+\S")

# Weights. The translation penalty is deliberately larger than the junk-marker
# one: a hacked or prototype dump of the right game in the right language is
# still closer to what was asked for than a fluent translation into a language
# the requester cannot read. Both are penalties rather than rejections, so a
# release can still be taken when it is genuinely the only thing that exists --
# though at -150 it will usually fall below the zero floor best_release applies.
_TRANSLATION_PENALTY = 150
_CREDIT_PENALTY = 80
_GOOD_DUMP_BONUS = 50

# GoodTools "verified good dump": the strongest quality signal a ROM title
# carries, and worth more than any region preference.
_GOOD_DUMP_MARKER = "[!]"

# Region preference: most emulator users want USA, then World, then Europe.
_REGION_SCORE = (
    (("usa", "(u)", "[u]", "ntsc-u"), 40),
    (("world", "(w)", "global"), 30),
    (("europe", "(e)", "pal"), 20),
    (("japan", "(j)", "ntsc-j"), 10),
)

# The same preference as compact GoodTools region codes, which is how much of a
# real result set is labelled. The ladder above reads only the spelled-out
# forms and the single-letter ones, so "(UE)" and "(JU)" scored nothing at all
# for region -- "Super Metroid (JU) [!].smc", an ideal result, was ranked as if
# it had no region at all. A combined code is worth its best constituent: a
# (JU) dump runs on a US console, so it is worth what (U) is worth.
_REGION_CODES = {
    "u": 40, "us": 40, "ue": 40, "uj": 40, "ju": 40,
    "jue": 40, "uej": 40, "jeu": 40,
    "w": 30,
    "e": 20, "eu": 20, "eur": 20,
    "j": 10, "jp": 10, "jpn": 10,
}


@dataclass(frozen=True)
class Release:
    """One search result from an indexer."""

    title: str
    size: int                 # bytes
    seeders: int              # 0 for usenet
    categories: tuple[int, ...]
    download_url: str
    protocol: str             # "torrent" | "usenet"
    indexer: str = ""
    # Whether the indexer it came from is a private tracker. This changes two
    # decisions that are wrong by default for private trackers: what a seeder
    # count of zero means (see score) and whether a magnet may be rebuilt from
    # a bare infohash (see indexers._download_link).
    private: bool = False


def is_game_release(release: Release) -> bool:
    """Whether an indexer result is a game at all, by category."""
    return any(
        c in CONSOLE_CATEGORIES or c in PC_GAME_CATEGORIES
        for c in release.categories
    )


def _normalise(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def title_matches(release_title: str, wanted: str) -> bool:
    """Whether a release plausibly IS the requested game.

    Every significant word of the request must appear. Short words are ignored
    because "of", "the" and roman numerals produce false negatives more often
    than they prevent false positives.
    """
    haystack = _normalise(release_title)
    words = [w for w in _normalise(wanted).split() if len(w) > 2]
    if not words:
        return False
    return all(w in haystack for w in words)


@dataclass(frozen=True)
class Judgement:
    """Why a release scored what it did.

    The scorer used to return a bare integer. That is enough to rank releases
    and useless for the only question anybody actually asks: why did it take
    *that* one. Every rejection in this file is a decision somebody may
    disagree with, and disagreeing requires seeing it.

    One implementation produces both views. A separate "explain" function that
    mirrored the scoring would drift from it, and a scorer that disagrees with
    its own explanation is worse than no explanation at all.
    """

    points: int
    reasons: tuple[tuple[int, str], ...] = ()
    # Set when a single rule disqualified the release outright, in which case
    # points is that rule's value and anything accumulated before it is void.
    verdict: str = ""

    @property
    def accepted(self) -> bool:
        return self.points > 0

    def why(self) -> list[str]:
        """Readable lines, worst first, for a UI or a log."""
        if self.verdict:
            return [self.verdict]
        return [f"{d:+d} {text}" for d, text in
                sorted(self.reasons, key=lambda pair: pair[0])]


def judge(release: Release, wanted: str,
          platform: Platform | None = None) -> Judgement:
    """Rank a release and record why. Higher is better; negative means no."""
    if not is_game_release(release):
        return Judgement(-1000, verdict="not a game release (wrong category)")
    if not title_matches(release.title, wanted):
        return Judgement(-500, verdict=f"title does not match {wanted!r}")

    points = 0
    reasons: list[tuple[int, str]] = []
    lowered = release.title.lower()

    def add(delta: int, text: str) -> None:
        nonlocal points
        points += delta
        if delta:
            reasons.append((delta, text))

    # Availability. Usenet has no seeders, so it is not penalised for having none.
    #
    # What a zero means depends on where the result came from. On a public
    # tracker it means the torrent is dead and nothing will ever come of it. On
    # a private one it is routine and temporary: the rare retro content these
    # trackers exist for often sits at zero seeders until somebody idle
    # reconnects, and rejecting it outright means the whole catalogue of a
    # tracker like RetroWithin is unreachable.
    #
    # The bonus is capped low on purpose. At `min(seeders, 50) * 4` it reached
    # 200 -- more than every quality signal here added together -- so a
    # well-seeded public romset outranked an exact, correctly-labelled
    # cartridge dump, and a private tracker with a handful of seeders per
    # torrent could never win a ranking no matter how right its release was.
    # Capped at 40 it ranks alongside region preference, which is the weight
    # availability actually deserves: a tie-breaker, not the deciding vote.
    if release.protocol == "torrent":
        if release.seeders <= 0:
            if not release.private:
                return Judgement(-400, verdict="no seeders on a public tracker")
            add(-20, "no seeders (private tracker, often temporary)")
        else:
            add(min(release.seeders, 20) * 2, f"{release.seeders} seeders")

    region = 0
    label = ""
    for markers, value in _REGION_SCORE:
        if any(m in lowered for m in markers):
            region, label = value, markers[0]
            break
    for code, value in _REGION_CODES.items():
        if value > region and _mentions(lowered, code):
            region, label = value, code
    add(region, f"region {label}" if label else "region")

    if _GOOD_DUMP_MARKER in lowered:
        add(_GOOD_DUMP_BONUS, "verified good dump [!]")

    hit = next((m for m in _JUNK_MARKERS if m in lowered), "")
    if hit:
        add(-120, f"looks like a hack, beta or repack ({hit!r})")

    # What language the release is in, and whether it is the game as published.
    #
    # Every marker is matched as a whole word, and that is not a nicety: "ger"
    # is inside "Trigger", "ita" inside "digital", "spa" inside "space" and
    # "por" inside "port". A substring test would penalise a large part of
    # every result set -- starting with the game that prompted this.
    translated = (
        any(_mentions(lowered, marker) for marker in _NON_ENGLISH_MARKERS)
        or _TRANSLATION_TAG.search(lowered) is not None
        or _MULTI_LANGUAGE.search(lowered) is not None
    )
    if translated:
        add(-_TRANSLATION_PENALTY, "not English, or a fan translation")

    if _CREDITED_TO_A_GROUP.search(lowered):
        add(-_CREDIT_PENALTY, "credited to a group, which usually means a hack")

    # Reject a release that names a system other than the one requested. Its own
    # platform's aliases are removed from the check first, so asking for a Wii
    # game does not disqualify a title that says "Wii".
    if platform is not None:
        own = {platform.slug.lower(), platform.name.lower(), *platform.aliases}
        own_words = {w for entry in own for w in entry.split()}
        for marker in FOREIGN_PLATFORM_MARKERS:
            if marker in own or marker in own_words:
                continue
            if _mentions(lowered, marker):
                return Judgement(-300,
                                 verdict=f"names another platform ({marker})")

    # A compilation is not a cartridge dump, and cannot be imported as one.
    #
    # Same class of thing as a romset: it holds the requested game, so the title
    # matches, and it frequently names the platform, so it collects the platform
    # bonus too. Live evidence -- one Genesis search for Phantasy Star IV ranked
    # "SEGA Genesis Classics Phantasy Star IV" (17.5MB, the Steam package) above
    # "Phantasy Star IV - Mega Drive - Genesis" (2.3MB, the actual dump). The
    # size ceiling could not catch it, because 17.5MB sits inside the headroom
    # deliberately left for a zipped dump carrying box art.
    #
    # Rejected rather than penalised: a PC installer or a multi-game set holds no
    # single cartridge for the importer to choose, so the grab would succeed and
    # the import could not. Whole-word matched, so "Collector" and "Classic
    # Edition" as part of a real game name are untouched.
    if platform is not None:
        for marker in COMPILATION_MARKERS:
            if _mentions(lowered, marker):
                return Judgement(
                    -250,
                    verdict=f"a compilation, not a single cartridge ({marker})")

    # Positive evidence that this really is the requested system. It matters
    # more now that the search casts a wider net: a bare title search returns
    # the same game for several consoles, and most such releases name no
    # platform at all. A ROM extension is the strongest signal available --
    # ".smc" says Super Nintendo far more reliably than any title text.
    if platform is not None:
        if _has_extension(lowered, platform.extensions):
            add(60, f"carries a {platform.name} ROM extension")
        elif _mentions(lowered, platform.slug.lower()) or any(
                _mentions(lowered, alias) for alias in platform.aliases):
            add(30, f"names {platform.name}")

    # A cartridge ROM is small. An oversized "release" for a cartridge platform
    # is a romset, a PC port, or a disc image -- none of which this pipeline can
    # hand to a browser emulator.
    #
    # The ceiling is per platform (Platform.max_size) because one number cannot
    # describe both a 32KB Atari cartridge and a 64MB N64 one. The single 512MB
    # limit this replaces was larger than every cartridge ever made, so a 452MB
    # PC build of Final Fantasy III passed it and, being the best-seeded result,
    # was picked for a SNES request.
    if platform is not None:
        if release.size > platform.max_size:
            add(-200,
                f"too big for a {platform.name} cartridge "
                f"({release.size // 1048576}MB, ceiling "
                f"{platform.max_size // 1048576}MB)")
        elif release.size < 4 * 1024:
            add(-200, "too small to be a ROM")

    return Judgement(points, tuple(reasons))


def score(release: Release, wanted: str, platform: Platform | None = None) -> int:
    """Rank a release. Higher is better; negative means "do not take this"."""
    return judge(release, wanted, platform).points


def explain(release: Release, wanted: str,
            platform: Platform | None = None) -> list[str]:
    """Why this release scored what it did, worst reason first."""
    return judge(release, wanted, platform).why()


def best_release(releases: list[Release], wanted: str,
                 platform: Platform | None = None) -> Release | None:
    """The highest-scoring usable release, or None if nothing qualifies."""
    ranked = [(score(r, wanted, platform), r) for r in releases]
    ranked = [(s, r) for s, r in ranked if s > 0]
    if not ranked:
        return None
    # Sort by score, then prefer the smaller file: for cartridge ROMs a bigger
    # file is almost always a romset or a bad dump, not a better copy.
    ranked.sort(key=lambda pair: (-pair[0], pair[1].size))
    return ranked[0][1]


def pick_rom_file(filenames: list[str], platform: Platform) -> str | None:
    """Which file in a finished download is the ROM to import.

    Prefers the platform's own extensions in declared order, then falls back to
    any file that is not obviously an extra. Returns None when the download
    holds nothing playable, which is a real outcome worth reporting rather than
    guessing at.
    """
    if not filenames:
        return None

    def is_extra(name: str) -> bool:
        lowered = name.lower()
        return lowered.endswith(
            (".nfo", ".txt", ".diz", ".sfv", ".jpg", ".jpeg", ".png",
             ".url", ".md5", ".sha1", ".par2", ".exe", ".bat")
        )

    for ext in platform.extensions:
        matches = [f for f in filenames if f.lower().endswith(ext) and not is_extra(f)]
        if matches:
            # A multi-dump archive: prefer the region the scorer prefers too.
            matches.sort(key=lambda f: (-_region_rank(f), len(f)))
            return matches[0]
    return None


def _region_rank(name: str) -> int:
    lowered = name.lower()
    for markers, value in _REGION_SCORE:
        if any(m in lowered for m in markers):
            return value
    return 0


# What can follow an extension and still leave it an extension: nothing, or a
# delimiter. A dot cannot -- ".smd." is followed by more name, which makes it a
# token in a scene release rather than the end of a filename.
_AFTER_EXTENSION = ("", " ", ")", "]", "}", ",", ";", '"', "'", "	")


def _has_extension(lowered: str, extensions: tuple[str, ...]) -> bool:
    """Whether a real ROM extension appears, not merely those characters.

    The bonus this feeds is the strongest platform signal the scorer has, on the
    grounds that ".smc" identifies a Super Nintendo cartridge better than any
    words in a title. That is only true of an actual extension. It used to be a
    plain substring test, and scene releases are dot-separated, so
    "Phantasy.Star.IV.USA.SMD.Virtual.Console" contained ".smd" and earned the
    full bonus for being a Wii package.

    It also matched a shorter extension inside a longer one -- Genesis declares
    both .md and .smd, and ".md" is inside ".smd" -- so a genuine .smd release
    matched twice and a dotted name containing "SMD" matched at all.
    """
    for ext in extensions:
        at = lowered.find(ext)
        while at != -1:
            end = at + len(ext)
            if lowered[end:end + 1] in _AFTER_EXTENSION:
                return True
            at = lowered.find(ext, at + 1)
    return False


def _mentions(haystack: str, marker: str) -> bool:
    """Whether `marker` appears in `haystack` as a whole word or phrase.

    Substring matching is wrong here: "ds" is inside "worlds", and "pc" is
    inside "pcb", so a naive check disqualifies half of every result set.
    """
    padded = f" {re.sub(r'[^a-z0-9 ]+', ' ', haystack)} "
    return f" {marker} " in re.sub(r"\s+", " ", padded)
