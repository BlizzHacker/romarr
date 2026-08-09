"""Choosing which release to grab, and which file inside it is the ROM.

Both decisions are pure functions over metadata, so they are unit-testable
without a network, a download client, or a filesystem. That matters because
they are where this pipeline goes wrong in practice: grab the wrong release and
you download 40GB of a PC port instead of a 512KB cartridge; pick the wrong file
and RomM imports a readme.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from .platforms import DISC, Platform

log = logging.getLogger(__name__)

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
    # Spelled out, because a listing usually writes the machine in full and
    # the short forms never appear. Without these a PlayStation request took
    # "Dynasty Warriors 2 - PlayStation 2" as its own platform: "playstation"
    # is psx's alias, and it is a substring of every later Sony console.
    "playstation 2", "playstation 3", "playstation 4", "playstation 5",
    "playstation portable", "playstation vita",
    "nintendo 64", "super nintendo", "game boy advance", "game boy color",
    "sega saturn", "sega dreamcast", "master system", "game gear",
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


# Scene markers that only ever appear on a PC release. Kept apart from
# FOREIGN_PLATFORM_MARKERS because these say something different: not "names
# another console" but "is a cracked PC game", which is why the
# own-platform exemption must not apply to them -- no retro system is called
# CODEX or TENOKE.
#
# The gap they close, seen live: a PlayStation request for "Dynasty Warriors"
# grabbed "DYNASTY WARRIORS ORIGINS (CRACK FIXED)", a 2025 PC game. It names
# no platform at all, so nothing rejected it, and 42 seeders scored it above
# the correct "Dynasty Warriors - PlayStation" on one seeder. Seeder count
# should never outrank being the right machine.
PC_RELEASE_MARKERS = (
    "crack", "cracked", "crackfix", "denuvo", "goldberg", "steamworks",
    "tenoke", "codex", "plaza", "skidrow", "reloaded", "razor1911", "flt",
    "empress", "rune", "hoodlum", "prophet", "doge", "hi2u", "darksiders",
    "fitgirl", "dodi", "elamigos", "kaos", "sim0n", "0xdeadc0de",
    # A patch is not a game, however well seeded.
    "update v", "patch v", "hotfix", "dlc unlocker", "trainer",
)

# Names for "several games in one box", which a single-game request cannot use.
# Kept apart from _JUNK_MARKERS because these are matched as whole words: the
# substring "set" is inside "Sunset", and "classics" must not catch "Classic".
COMPILATION_MARKERS = (
    "classics", "collection", "compilation", "anthology", "romset",
    "rom set", "complete set", "full set", "no-intro", "nointro", "goodgen",
    "tosec", "redump", "everdrive", "megaset", "mega set", "all games",
)

# Dump-project names that mean "this dump is correct" on the medium the
# project actually covers, and "this is their whole set" everywhere else.
#
# Redump is the disc-preservation project -- what No-Intro is for cartridges.
# On a disc release its name is the single most reliable quality signal
# available, and it was in COMPILATION_MARKERS, which rejects outright. Every
# correctly-labelled disc dump would have been thrown out and the releases
# left standing would have been the unlabelled ones: the scorer would have
# systematically preferred worse dumps, and the reason it gave would have been
# "a compilation, not a single cartridge".
#
# Only the medium's own project is exempted, and only for that medium. A
# Redump-labelled SNES release really is a set, because Redump does not dump
# cartridges. `_is_a_set` still rejects "Redump - Sony PlayStation Collection"
# on the word "collection", which is what actually makes it a set.
_DUMP_PROJECT_FOR_MEDIUM = {DISC: ("redump",)}
_DUMP_PROJECT_BONUS = 40

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
          platform: Platform | None = None, *,
          profile=None, blocklist=None) -> Judgement:
    """Rank a release and record why. Higher is better; negative means no.

    `profile` and `blocklist` are operator policy and are applied before any
    scoring. A blocked release is refused even when a profile scores it
    highly: somebody who preferred a term and then blocked a specific release
    meant the block, and reading it the other way round is the worst available
    interpretation.
    """
    if blocklist is not None and release in blocklist:
        return Judgement(-2000,
                         verdict=f"blocklisted: {blocklist.reason_for(release)}")
    if profile:
        refusal = profile.refusal(release.title.lower())
        if refusal:
            return Judgement(-900, verdict=refusal)
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
        own = {platform.slug.lower(), platform.name.lower(), *platform.aliases,
               *platform.native_markers}
        own_words = {w for entry in own for w in entry.split()}
        # Longest first, so the most specific name in the title decides. Every
        # Sony console contains "playstation", so checking the short alias
        # first exempted "PlayStation 2" from a PlayStation request and let a
        # PS2 disc win a PSX search.
        for marker in sorted(FOREIGN_PLATFORM_MARKERS, key=len, reverse=True):
            if not _mentions(lowered, marker):
                continue
            if marker in own or marker in own_words:
                # This platform's own name. Nothing more specific matched, so
                # the title is talking about us.
                break
            return Judgement(-300,
                             verdict=f"names another platform ({marker})")

    # A cracked PC release, whatever it is named. Rejected rather than
    # penalised for the same reason a compilation is: there is no ROM inside
    # for the importer to file, so the grab would succeed and the import
    # could not.
    for marker in PC_RELEASE_MARKERS:
        if _mentions(lowered, marker):
            return Judgement(-300, verdict=f"a PC release ({marker})")

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
        native_projects = _DUMP_PROJECT_FOR_MEDIUM.get(platform.media, ())
        for marker in COMPILATION_MARKERS:
            if marker in native_projects:
                continue
            if _mentions(lowered, marker):
                return Judgement(
                    -250,
                    verdict=f"a compilation, not a single game ({marker})")
        for project in native_projects:
            if _mentions(lowered, project):
                add(_DUMP_PROJECT_BONUS, f"a {project.title()} dump")
                break

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
        medium = "disc image" if platform.is_disc else "cartridge"
        if release.size > platform.max_size:
            add(-200,
                f"too big for a {platform.name} {medium} "
                f"({release.size // 1048576}MB, ceiling "
                f"{platform.max_size // 1048576}MB)")
        elif release.size < _size_floor(platform):
            # The floor is per medium for the same reason the ceiling is. Four
            # kilobytes is a real floor for an Atari cartridge and no floor at
            # all for a disc: a few hundred KB offered for a PlayStation
            # request is a patch, a cheat file or a lone .cue, and every one of
            # those imports cleanly and boots nothing.
            add(-200, f"too small to be a {platform.name} {medium}")

    if profile:
        for delta, text in profile.adjustments(lowered):
            add(delta, text)

    return Judgement(points, tuple(reasons))


#: The smallest plausible download, per medium. A CD-based game can genuinely
#: be small -- some are a few megabytes of data and a lot of silence -- so this
#: is set to catch a file that is not a disc at all rather than to judge how
#: much of the disc was used.
_DISC_FLOOR = 1024 * 1024
_CARTRIDGE_FLOOR = 4 * 1024


def _size_floor(platform: Platform) -> int:
    return _DISC_FLOOR if platform.is_disc else _CARTRIDGE_FLOOR


def score(release: Release, wanted: str, platform: Platform | None = None) -> int:
    """Rank a release. Higher is better; negative means "do not take this"."""
    return judge(release, wanted, platform).points


def explain(release: Release, wanted: str,
            platform: Platform | None = None) -> list[str]:
    """Why this release scored what it did, worst reason first."""
    return judge(release, wanted, platform).why()


def best_release(releases: list[Release], wanted: str,
                 platform: Platform | None = None, *,
                 profile=None, blocklist=None) -> Release | None:
    """The highest-scoring usable release, or None if nothing qualifies."""
    ranked = [(judge(r, wanted, platform, profile=profile,
                     blocklist=blocklist).points, r) for r in releases]
    ranked = [(s, r) for s, r in ranked if s > 0]
    if not ranked:
        return None
    # Sort by score, then break the tie on size -- in the direction the medium
    # calls for.
    #
    # For a cartridge, smaller wins: a bigger file at the same score is almost
    # always a romset or a bad dump, not a better copy.
    #
    # For a disc that reasoning inverts. Two rips of the same game differ by
    # how much of the disc was kept -- audio tracks, video, the lot -- so the
    # smaller of two PlayStation releases scoring alike is the one with
    # something missing. The ceiling above already excludes anything that is
    # too big to be the game at all, so preferring the larger here can only
    # choose between plausible rips.
    if platform is not None and platform.is_disc:
        ranked.sort(key=lambda pair: (-pair[0], -pair[1].size))
    else:
        ranked.sort(key=lambda pair: (-pair[0], pair[1].size))
    return ranked[0][1]


_EXTRA_SUFFIXES = (
    ".nfo", ".txt", ".diz", ".sfv", ".jpg", ".jpeg", ".png",
    ".url", ".md5", ".sha1", ".par2", ".exe", ".bat",
)


def _is_extra(name: str) -> bool:
    return name.lower().endswith(_EXTRA_SUFFIXES)


@dataclass(frozen=True)
class RomSet:
    """Every file that has to be imported for one game to work.

    A cartridge is a set of one and always was. A disc is not, and treating it
    as one is the quietest way to break a library: a `.cue` is a few hundred
    bytes of text naming tracks, so importing it alone produces an entry with a
    title, a cover, a plausible size and no game. Nothing downstream can tell
    that apart from a working import -- not the scanner, not the player, not
    the operator until they press play.

    `primary` is the file an emulator is pointed at. `members` is everything
    that has to travel with it, `primary` included.
    """

    primary: str
    members: tuple[str, ...]

    @property
    def is_multi_file(self) -> bool:
        return len(self.members) > 1


#: What a sheet names its tracks with. Read out of `SIDECAR_FOR` in
#: `RommStreamServer/archives.py` rather than derived again here: that table
#: is already proven against this library, and two copies of the same
#: knowledge would drift in opposite directions.
_SIDECAR_FOR: dict[str, tuple[str, ...]] = {
    ".cue": (".bin", ".img", ".iso"),
    ".gdi": (".bin", ".raw"),
    ".ccd": (".img", ".sub"),
    ".mds": (".mdf",),
}

#: A playlist binds the discs of one game together. RetroArch and EmulatorJS
#: both take an `.m3u` as the thing you load for a multi-disc game, so when one
#: is present it is the primary regardless of what else the release holds --
#: importing disc one of three is not importing the game.
_PLAYLIST = ".m3u"


def pick_rom_file(filenames: list[str], platform: Platform) -> str | None:
    """Which file in a finished download is the ROM to import.

    Kept for callers that want a single name, and implemented on top of
    `pick_rom_set` so the two can never disagree about which file is the ROM.
    """
    chosen = pick_rom_set(filenames, platform)
    return chosen.primary if chosen else None


def pick_rom_set(filenames: list[str], platform: Platform, *,
                 read=None) -> RomSet | None:
    """Which files in a finished download make up the game.

    Prefers the platform's own extensions in declared order, then falls back to
    any file that is not obviously an extra. Returns None when the download
    holds nothing playable, which is a real outcome worth reporting rather than
    guessing at.

    `read(name) -> bytes | None` gives access to a sheet's contents. The caller
    owns it because only the caller knows whether a name is a path on disk or a
    member of an archive. It is optional: without it the sidecar fallback still
    produces a complete set, just a more generous one.
    """
    if not filenames:
        return None

    playable = [f for f in filenames if not _is_extra(f)]

    # A playlist first, and only for a platform that declares it -- an .m3u
    # beside a SNES rom is somebody else's file.
    if _PLAYLIST in platform.extensions:
        playlists = [f for f in playable if f.lower().endswith(_PLAYLIST)]
        if playlists:
            primary = sorted(playlists, key=lambda f: (-_region_rank(f), len(f)))[0]
            listed = _playlist_members(primary, playable, read)
            return RomSet(primary, (primary, *listed))

    for ext in platform.extensions:
        matches = [f for f in playable if f.lower().endswith(ext)]
        if not matches:
            continue
        # A multi-dump archive: prefer the region the scorer prefers too.
        matches.sort(key=lambda f: (-_region_rank(f), len(f)))
        primary = matches[0]
        sidecars = _sidecars_for(primary, playable, read)
        return RomSet(primary, (primary, *sidecars))
    return None


def pick_all_rom_sets(filenames: list[str], platform: Platform, *,
                      read=None) -> list[RomSet]:
    """Every valid ROM set the archive holds, not just the best one.

    A cartridge zip may bundle several ROMs (a 3-in-1 collection, a bundle of
    homebrew).  pick_rom_set keeps the one that ranks highest; this returns all
    of them so that importing a multi-ROM zip does not silently drop games.
    Disc releases and playlists are unchanged -- a cue with its bins is one
    game regardless.
    """
    if not filenames:
        return []

    playable = [f for f in filenames if not _is_extra(f)]

    if _PLAYLIST in platform.extensions:
        playlists = [f for f in playable if f.lower().endswith(_PLAYLIST)]
        if playlists:
            sets = []
            for primary in sorted(playlists,
                                  key=lambda f: (-_region_rank(f), len(f))):
                listed = _playlist_members(primary, playable, read)
                sets.append(RomSet(primary, (primary, *listed)))
            return sets if sets else []

    # Extension groups in the platform's declared order, exactly as
    # `pick_rom_set` does. Flattening them into one list and sorting by name
    # loses that priority, and a track file then outranks the sheet that owns
    # it: a Dreamcast .gdi with track01.bin beside it produced "track01.bin"
    # as one game and the .gdi as another, because the track's name is
    # shorter. The descriptor has to be considered first so it can claim its
    # own tracks as sidecars before they are mistaken for games.
    consumed: set[str] = set()
    sets: list[RomSet] = []

    for ext in platform.extensions:
        matches = [f for f in playable
                   if f.lower().endswith(ext) and f not in consumed]
        # A multi-dump archive: prefer the region the scorer prefers too.
        matches.sort(key=lambda f: (-_region_rank(f), len(f)))
        for primary in matches:
            if primary in consumed:
                continue
            sidecars = _sidecars_for(primary, playable, read)
            members = (primary, *sidecars)
            consumed.update(members)
            sets.append(RomSet(primary, members))

    return sets


def _sidecars_for(primary: str, filenames: list[str], read) -> tuple[str, ...]:
    """The track files `primary` cannot boot without.

    Read out of the sheet where possible. A sheet states its tracks by name,
    and that is the only source that gets a directory holding two discs right:
    both cues may say `game.bin`, and each means the one beside it.
    """
    ext = _suffix(primary)
    wanted = _SIDECAR_FOR.get(ext)
    if not wanted:
        return ()

    named = _names_in_sheet(primary, read, _cue_and_gdi_names)
    if named is not None:
        found = _resolve_beside(primary, named, filenames)
        if found:
            return found

    # The sheet could not be read, or named nothing this download holds.
    #
    # Deliberately over-inclusive from here. A file too many costs disk; a file
    # too few costs a game that imports, displays and does not boot -- and the
    # operator finds out at the point where they have already committed.
    return _companions(primary, filenames, wanted)


def _playlist_members(primary: str, filenames: list[str], read) -> tuple[str, ...]:
    """The disc images an `.m3u` lists, and their own sidecars.

    A playlist may name `.cue` files rather than whole-disc images, in which
    case each of those drags its own tracks along -- which is why this recurses
    through `_sidecars_for` rather than stopping at the names in the file.
    """
    named = _names_in_sheet(primary, read, _playlist_names)
    listed = _resolve_beside(primary, named or (), filenames)
    if not listed:
        # No readable playlist: take every disc image beside it. A playlist
        # with nothing under it is not a game.
        listed = tuple(f for f in filenames
                       if f != primary and _same_parent(primary, f)
                       and _suffix(f) != _PLAYLIST)
    out: list[str] = []
    for disc in listed:
        for name in (disc, *_sidecars_for(disc, filenames, read)):
            if name not in out:
                out.append(name)
    return tuple(out)


def _names_in_sheet(primary: str, read, parse) -> tuple[str, ...] | None:
    """Filenames a sheet refers to, or None when it could not be read.

    None and () mean different things and both are real: None is "no answer
    available, fall back", () is "the sheet was read and named nothing".
    """
    if read is None:
        return None
    try:
        raw = read(primary)
    except Exception:  # an unreadable member must not end the import
        log.warning("could not read %r to find its tracks", primary)
        return None
    if raw is None:
        return None
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    return parse(raw)


# `FILE "Track 01.bin" BINARY`, and the unquoted form some writers emit.
#
# Every gap is `[ \t]`, never `\s`. `\s` matches a newline, and a sheet is a
# line-oriented format where that is the difference between reading it and
# reading through it: a .gdi opens with a bare track count, and `\s+` let the
# first pattern start on that count, run over the line break, and capture the
# *sector size* of track one as its filename -- silently dropping track one
# from every Dreamcast import.
_CUE_FILE = re.compile(r'^[ \t]*FILE[ \t]+(?:"([^"]+)"|(\S+))',
                       re.IGNORECASE | re.MULTILINE)

# A gdi track line: index, LBA, type, sector size, filename, offset. The
# filename is the first field that is not a bare number.
_GDI_LINE = re.compile(
    r'^[ \t]*\d+[ \t]+\d+[ \t]+\d+[ \t]+\d+[ \t]+(?:"([^"]+)"|(\S+))',
    re.MULTILINE)


def _cue_and_gdi_names(text: str) -> tuple[str, ...]:
    out: list[str] = []
    for pattern in (_CUE_FILE, _GDI_LINE):
        for match in pattern.finditer(text):
            name = match.group(1) or match.group(2)
            if name and name not in out:
                out.append(name)
    return tuple(out)


def _playlist_names(text: str) -> tuple[str, ...]:
    """An m3u is one path per line; `#` is a comment."""
    return tuple(
        line.strip() for line in text.splitlines()
        if line.strip() and not line.strip().startswith("#")
    )


def _resolve_beside(primary: str, named, filenames: list[str]) -> tuple[str, ...]:
    """Match names from a sheet against what the download actually holds.

    A sheet names its tracks relative to itself, so the match is made within
    the sheet's own directory. That is the whole reason two discs in one
    download do not bleed into each other: both sheets may say `game.bin`.
    """
    parent = _parent(primary)
    by_key = {}
    for f in filenames:
        if _parent(f) == parent:
            by_key.setdefault(_basename(f).lower(), f)
    out: list[str] = []
    for name in named:
        got = by_key.get(_basename(name.replace("\\", "/")).lower())
        if got and got != primary and got not in out:
            out.append(got)
    return tuple(out)


def _companions(primary: str, filenames: list[str],
                wanted: tuple[str, ...]) -> tuple[str, ...]:
    """Track files beside `primary`, when the sheet could not name them.

    Two passes, because the two real layouts need different answers. A
    download holding several games names each game's tracks after the game
    ("Game A (Track 01).bin"), so a shared stem is the right filter. A
    download holding one game names them `track01.bin`, sharing nothing with
    the sheet -- so when nothing shares the stem, everything beside it is
    taken.
    """
    stem = _stem(primary).lower()
    beside = [f for f in filenames
              if f != primary and _same_parent(primary, f)
              and _suffix(f) in wanted]
    shared = [f for f in beside if _stem(f).lower().startswith(stem)]
    return tuple(shared or beside)


def _parent(path: str) -> str:
    normalised = path.replace("\\", "/")
    return normalised.rsplit("/", 1)[0] if "/" in normalised else ""


def _basename(path: str) -> str:
    normalised = path.replace("\\", "/")
    return normalised.rsplit("/", 1)[-1]


def _stem(path: str) -> str:
    base = _basename(path)
    return base.rsplit(".", 1)[0] if "." in base else base


def _suffix(path: str) -> str:
    base = _basename(path)
    return ("." + base.rsplit(".", 1)[-1]).lower() if "." in base else ""


def _same_parent(a: str, b: str) -> bool:
    return _parent(a) == _parent(b)


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
