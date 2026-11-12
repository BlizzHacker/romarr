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
import unicodedata
from dataclasses import dataclass

from .platforms import DISC, PLATFORMS, Platform, all_extensions, by_slug, resolve, same_machine

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
    "nintendo switch 2", "switch 2", "switch2", "nintendo switch", "switch", "nsp", "xci",
    "playstation", "ps1", "ps2", "ps3", "ps4", "ps5", "psp", "psx", "vita",
    "psx2psp", "ps12psp",
    # Spelled out, because a listing usually writes the machine in full and
    # the short forms never appear. Without these a PlayStation request took
    # "Dynasty Warriors 2 - PlayStation 2" as its own platform: "playstation"
    # is psx's alias, and it is a substring of every later Sony console.
    "playstation 2", "playstation 3", "playstation 4", "playstation 5",
    "playstation portable", "playstation vita", "ps vita",
    "nintendo 64", "n64", "super nintendo", "super nes", "snes",
    "nintendo entertainment system", "nes",
    "game boy advance", "gameboy advance", "gba",
    "game boy color", "gameboy color", "gbc",
    "game boy", "gameboy",
    "sega genesis", "mega drive", "megadrive", "sega cd", "mega cd", "sega 32x",
    "sega saturn", "sega dreamcast", "master system", "game gear",
    # "xbox" is a substring of both later machines, so the spelled-out forms
    # are needed for the same reason "playstation 2" is above: markers are
    # tried longest-first, and without these an Xbox request treats an "Xbox
    # 360" title as naming itself and imports a disc no Xbox can read.
    "xbox", "xbox 360", "xbox one", "xbox live arcade", "xbox series",
    "xbox series x", "xbox series s", "x360", "xbla",
    "nintendo gamecube", "gamecube", "gcn",
    "nintendo wii u", "nintendo wii", "wii", "wiiu", "wii u",
    "new nintendo 3ds", "new 3ds", "nintendo 3ds", "n3ds", "3ds",
    "nintendo dsi", "nintendo ds", "nds", "dsi", "ds",
    "pc engine supergrafx", "pc engine cd", "pc engine",
    "turbografx 16", "turbografx cd", "turbografx",
    "nec pc fx", "pc fx",
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
    # Scene groups tag Switch releases "NSW-<group>" and Vita releases
    # "PSV-<group>", and often never spell the console out. Without these a
    # GameCube request for Paper Mario accepted the Switch remake's update patch,
    # and a PSP request for Lumines accepted a Vita release.
    "nsw", "psv",
}

# Generic brand alias ("nintendo") only exists for resolve("nintendo") -> nes;
# never treat it as positive platform evidence or infer NES from it.
_NON_SCORING_ALIASES = frozenset({"nintendo"})


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
    "x64", "x86", "win64", "win32",
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

#: The same idea for PC, where "repack" is FitGirl's and DODI's normal
#: shipping form rather than a defect, and "patched" describes half the
#: catalogue. What still marks junk on PC: betas, demos, trainers, bare
#: updates and DLC-only uploads.
_JUNK_MARKERS_DIGITAL = (
    "beta", "demo", "sample", "trainer", "update only", "dlc only",
)

#: Repack groups with long, consistent track records. The bonus exists so a
#: FitGirl repack outranks an anonymous upload of the same game -- the same
#: judgement a person makes on the search page.
_TRUSTED_REPACKERS = ("fitgirl", "dodi", "elamigos", "kaoskrew", "masquerade",
                      "gog-rip", "razor1911")

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
_TRANSLATION_TAG = re.compile(
    r"([\[(]\s*t[-+]\s*[a-z]{2,4}[^\])]*[\])]|\[\s*tr\s+[a-z]{2,4}[^\]]*\])",
    re.IGNORECASE,
)

_TAG_RE = re.compile(r"[\(\[]([^\)\]]+)[\)\]]")
# Language codes used in release tags. A two-letter tag alone is not evidence
# of a language: NP is the Nintendo Power release format, for example.
_LANGUAGE_CODES = frozenset("""
af ar bg ca cs cy da de el en es et eu fa fi fr ga gl he hi hr hu id is it
ja ko lt lv mk ms nl no pl pt ro ru sk sl sr sv th tr uk vi zh
""".split())


def is_foreign_language_tag(title: str) -> bool:
    regions = detect_regions(title)
    allowed = {"en"}
    if "japan" in regions:
        allowed.add("ja")
    if not regions.intersection({"usa", "japan"}):
        return False
    languages = set()
    for tag in _TAG_RE.findall(title.lower()):
        codes = set(re.split(r"[,/ ]+", tag.strip()))
        if codes and codes <= _LANGUAGE_CODES:
            languages.update(codes)
    return bool(languages and languages.isdisjoint(allowed))


_TOKEN_TO_REGIONS = {
    "usa": {"usa"}, "us": {"usa"}, "united states": {"usa"}, "america": {"usa"},
    "europe": {"europe"}, "eu": {"europe"}, "eur": {"europe"},
    "world": {"world"}, "global": {"world"},
    "japan": {"japan"}, "jp": {"japan"}, "jpn": {"japan"},
    "australia": {"australia"}, "au": {"australia"}, "aus": {"australia"},
    "germany": {"germany"}, "de": {"germany"}, "deu": {"germany"}, "ger": {"germany"}, "gr": {"germany"},
    "france": {"france"}, "fr": {"france"}, "fra": {"france"}, "fre": {"france"},
    "spain": {"spain"}, "es": {"spain"}, "esp": {"spain"}, "spa": {"spain"},
    "italy": {"italy"}, "it": {"italy"}, "ita": {"italy"},
    "netherlands": {"netherlands"}, "nl": {"netherlands"}, "holland": {"netherlands"}, "dut": {"netherlands"}, "ned": {"netherlands"},
    "sweden": {"sweden"}, "se": {"sweden"}, "swe": {"sweden"},
    "norway": {"norway"}, "no": {"norway"}, "nor": {"norway"},
    "denmark": {"denmark"}, "dk": {"denmark"}, "dan": {"denmark"},
    "finland": {"finland"}, "fi": {"finland"}, "fin": {"finland"},
    "brazil": {"brazil"}, "br": {"brazil"}, "pt-br": {"brazil"}, "ptbr": {"brazil"},
    "portugal": {"portugal"}, "pt": {"portugal", "brazil"},
    "korea": {"korea"}, "kr": {"korea"}, "kor": {"korea"},
    "china": {"china"}, "cn": {"china"}, "chn": {"china"}, "chs": {"china"}, "cht": {"china"}, "zh": {"china"},
    "russia": {"russia"}, "ru": {"russia"}, "rus": {"russia"},
    "poland": {"poland"}, "pl": {"poland"}, "pol": {"poland"},
    "canada": {"canada"}, "ca": {"canada"},
    "u": {"usa"}, "e": {"europe"}, "j": {"japan"}, "w": {"world"}, "a": {"australia"},
    "g": {"germany"}, "f": {"france"}, "s": {"spain"}, "i": {"italy"}, "k": {"korea"},
    "c": {"china"}, "b": {"brazil"},
    "ue": {"usa", "europe"}, "uj": {"usa", "japan"}, "ju": {"japan", "usa"},
    "jue": {"japan", "usa", "europe"}, "uej": {"usa", "europe", "japan"}, "jeu": {"japan", "europe", "usa"},
}

def detect_regions(title: str) -> set[str]:
    lowered = title.lower()
    detected = set()
    ambiguous = set()
    for tag_content in _TAG_RE.findall(lowered):
        tokens = [t for t in re.split(r"[,/ ]+", tag_content.strip()) if t]
        if not tokens:
            continue
        if all(token in _LANGUAGE_CODES for token in tokens):
            # A standalone compact country code still works as a region when
            # there is no stronger evidence. A list like En,Fr is language.
            if len(tokens) == 1:
                ambiguous.update(_TOKEN_TO_REGIONS.get(tokens[0], ()))
        elif all(token in _TOKEN_TO_REGIONS for token in tokens):
            for token in tokens:
                detected.update(_TOKEN_TO_REGIONS[token])
    if not detected:
        if re.search(r"\bpal\b", lowered):
            detected.add("europe")
        if re.search(r"\bntsc-u\b", lowered) or re.search(r"\bntsc-us\b", lowered):
            detected.add("usa")
        elif re.search(r"\bntsc-j\b", lowered):
            detected.add("japan")
        elif re.search(r"\bntsc\b", lowered):
            detected.update({"usa", "japan"})
    if not detected:
        detected = ambiguous
    return detected

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
    # The release's page on its indexer, when the indexer names one. Safe for
    # a browser -- unlike download_url it never carries a credential, and the
    # Search page links it so "what is this release" has a one-click answer.
    info_url: str = ""
    # A collection-backed result can name one exact file inside its torrent.
    # Keep that path server-side with the release; it is never a download URL.
    torrent_file_path: str = ""
    file_selection_required: bool = False
    content_kind: str = ""


def collection_content_kind(path: str) -> str:
    """Classify known collection media hierarchies, never words in a title.

    The final component is a filename, not a content branch. A Games folder
    nested under Manuals does not turn a document into a playable release.
    """
    parts = [re.sub(r"[\s_-]+", "-", unicodedata.normalize("NFKC", part).strip().casefold())
             for part in re.split(r"[\\/]+", str(path or "")) if part.strip() not in ("", ".")]
    directories = set(parts[:-1])
    if directories & {"manual", "manuals"}:
        return "manual"
    if directories & {"commercial", "commercials"}:
        return "commercial"
    if "tosec-pix" in directories:
        return "associated-media"
    return ""


def collection_content_refusal(release) -> str:
    kind = collection_content_kind(getattr(release, "torrent_file_path", ""))
    kind = kind or getattr(release, "content_kind", "")
    if kind in {"manual", "commercial", "associated-media"}:
        label = "TOSEC-PIX associated media" if kind == "associated-media" else kind
        return f"non-game collection content ({label})"
    return ""


def is_game_release(release: Release) -> bool:
    """Whether an indexer result is a game at all, by category."""
    return any(
        c in CONSOLE_CATEGORIES or c in PC_GAME_CATEGORIES
        for c in release.categories
    )


def _normalise(text: str) -> str:
    """Fold Unicode accents and punctuation into lowercase ASCII words."""
    folded = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", " ", folded.lower()).strip()


def _normalise_marker(text: str) -> str:
    """Normalise a platform label or marker into space-separated lowercase tokens."""
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", (text or "").lower())).strip()


_DASH_CHARS = "\\-\u2013\u2014"
_EXT_TOKEN_RE = re.compile(
    r"(?<=\S)\.([a-z0-9]{2,8})\s*$",
    re.IGNORECASE,
)
_BRACKET_GROUP_RE = re.compile(
    r"\((?!(?:19|20)\d{2}\))[^()]*\)|\[(?!(?:19|20)\d{2}\])[^\[\]]*\]|\{[^{}]*\}"
)
_INVERTED_ARTICLE_RE = re.compile(rf",\s*(?:the|an?)(?=\s*$|\s*[:{_DASH_CHARS}])", re.IGNORECASE)
_LEADING_ARTICLE_RE = re.compile(r"^\s*(?:the|an?)\s+", re.IGNORECASE)
_PLUS_SEP_RE = re.compile(r"\s*\+\s*")
_PLATFORM_SEP_RE = re.compile(rf"\s+[{_DASH_CHARS}]\s+|:\s+")
_TRAILING_SUBTITLE_SEP_RE = re.compile(rf"^\s*(?::|[{_DASH_CHARS}]+(?=\s))")
_TRAILING_CHUNK_SPLIT_RE = re.compile(rf"\s*:\s*|\s+[{_DASH_CHARS}]+\s+")
_ALPHA_NUM_TOKEN_RE = re.compile(r"[a-z0-9]+")

# Single-word console acronyms safe to strip as unbracketed prefixes before a
# game title even without a dash separator. Ambiguous English words or shared
# brand names ("nintendo", "switch", "genesis", "saturn", "dreamcast", "pc",
# "arcade", etc.) are excluded so they cannot strip real title prefixes.
_UNAMBIGUOUS_SINGLE_PLATFORMS = frozenset({
    "nes", "snes", "n64", "gba", "gbc", "gb", "nds", "3ds", "n3ds",
    "ps1", "ps2", "ps3", "ps4", "ps5", "psp", "psx", "psvita",
    "wii", "wiiu", "xbox", "xbox360", "x360",
    "sfc", "fds", "sfam", "megadrive", "segacd",
    "atari2600", "atari5200", "atari7800", "c64", "c128",
    "msx", "msx2", "pcfx", "pcecd", "cd32", "sg1000", "famicom",
})

# Multi-word platform aliases that are storefront/descriptive terms rather than
# system names and must never excuse an unbracketed title prefix.
_AMBIGUOUS_MULTI_PLATFORM_PREFIXES = frozenset({
    ("games", "on", "demand"),
    ("disk", "system"),
})

_ROMAN_TO_ARABIC = {
    "ii": "2",
    "iii": "3",
    "iv": "4",
    "vi": "6",
    "vii": "7",
    "viii": "8",
    "ix": "9",
    "xi": "11",
    "xii": "12",
    "xiii": "13",
    "xiv": "14",
    "xv": "15",
    "xvi": "16",
    "xvii": "17",
    "xviii": "18",
    "xix": "19",
}

_SEQUEL_NEXT_TOKENS = frozenset({"0", "64", "x", *(str(n) for n in range(2, 20))})
_NON_EXT_PREFIXES = frozenset({"mr", "mrs", "ms", "dr", "st", "vs", "jr", "sr"})
_TRAILING_METADATA_TOKENS = frozenset({
    *_LANGUAGE_CODES,
    *(tok for key in _TOKEN_TO_REGIONS for tok in _normalise_marker(key).split()),
    "ntsc", "pal", "u", "j", "e", "w", "ue", "ju", "uj", "jue", "uej", "jeu",
    "rev", "v", "ver", "version", "disc", "disk", "cd", "dvd", "side", "part",
    "redump", "nointro", "no", "intro", "tosec", "unl", "unlicensed",
    "repack", "rip", "iso", "rom", "nsp", "xci", "nsz", "xcz", "pbp", "chd", "cso",
    "wad", "cia", "3ds", "eshop", "virtual", "console", "wiiware", "psn", "xbla",
    "pc", "windows", "win", "steam", "gog", "nintendo", "sony", "sega", "microsoft",
    "reloaded", "fitgirl", "dodi", "elamigos", "kaos", "kaoskrew", "masquerade",
    "codex", "plaza", "skidrow", "razor1911", "tenoke", "prophet", "hoodlum",
    "empress", "rune", "goldberg", "flt", "hi2u", "darksiders", "doge",
    "multi", "eng", "rus", "ger", "deu", "fra", "fre", "esp", "spa", "ita",
    "por", "kor", "chs", "cht", "pol", "dut", "ned", "swe", "tur", "ukr",
})


def _build_strippable_extensions() -> frozenset[str]:
    """Build the set of parsed ROM, archive, and NZB extensions for title cleaning."""
    extra = {".nzb", ".torrent", ".par2", ".rar", ".zip", ".7z", ".gz", ".tar"}
    exts = {ext.lower() for ext in (*all_extensions(), *extra)}
    return frozenset(ext for ext in exts if len(ext) >= 3 and not ext[1:].isdigit())


_STRIPPABLE_EXTENSIONS = _build_strippable_extensions()


def _build_platform_prefix_tuples() -> frozenset[tuple[str, ...]]:
    """Build normalised token tuples allowed as unbracketed platform prefixes."""
    prefixes: set[tuple[str, ...]] = {(tok,) for tok in _UNAMBIGUOUS_SINGLE_PLATFORMS}
    for plat in PLATFORMS:
        for label in (plat.name, plat.slug, *plat.aliases):
            norm = tuple(_normalise(label).split())
            if len(norm) >= 2 and norm not in _AMBIGUOUS_MULTI_PLATFORM_PREFIXES:
                prefixes.add(norm)
    return frozenset(prefixes)


_PLATFORM_PREFIX_TUPLES = _build_platform_prefix_tuples()


def _strip_extensions(text: str, *, min_len: int = 3) -> tuple[str, bool]:
    """Iteratively remove trailing known extensions of length >= `min_len` (dot included)."""
    current = (text or "").rstrip()
    removed = False
    while True:
        match = _EXT_TOKEN_RE.search(current)
        if not match:
            return current, removed
        ext = f".{match.group(1).lower()}"
        if len(ext) < min_len or ext not in _STRIPPABLE_EXTENSIONS:
            return current, removed
        if len(ext) <= 3:
            before = re.search(r"([a-z0-9]+)$", current[:match.start()], re.IGNORECASE)
            if before and before.group(1).lower() in _NON_EXT_PREFIXES:
                return current, removed
        current = current[:match.start()].rstrip()
        removed = True


def _strip_brackets(text: str) -> str:
    """Strip parenthetical and bracketed metadata unless doing so empties the text."""
    stripped = text
    while True:
        updated = _BRACKET_GROUP_RE.sub(" ", stripped)
        if updated == stripped:
            break
        stripped = updated
    return stripped if _normalise(stripped) else text


def _strip_articles(text: str) -> str:
    """Normalise catalog comma-inverted and leading English articles."""
    cleaned = _INVERTED_ARTICLE_RE.sub("", text)
    if _normalise(cleaned):
        candidate = _LEADING_ARTICLE_RE.sub("", cleaned)
        if _normalise(candidate):
            return candidate
        return cleaned
    return text


def _canonical_tokens(raw_tokens: list[str]) -> list[str]:
    """Canonicalise unambiguous Roman numerals in a token sequence.

    Leaves ``"v"`` untouched at index 0 (so ``V-Rally`` never becomes
    ``5-Rally``) or when immediately followed by ``"0"`` / ``"1"`` (version
    tags like ``v 1.0``), and never folds ``"i"`` or ``"x"``.
    """
    out: list[str] = []
    total = len(raw_tokens)
    for idx, tok in enumerate(raw_tokens):
        mapped = _ROMAN_TO_ARABIC.get(tok)
        if mapped is not None:
            out.append(mapped)
        elif tok == "v" and idx > 0 and (idx + 1 >= total or raw_tokens[idx + 1] not in ("0", "1")):
            out.append("5")
        else:
            out.append(tok)
    return out


def _prepare_core(text: str) -> str:
    """Return title core after extension, bracket, and article normalisation."""
    without_ext, removed = _strip_extensions(text or "")
    stripped = _strip_brackets(without_ext)
    if not removed:
        stripped, _ = _strip_extensions(stripped, min_len=4)
    return _strip_articles(stripped)


def _prefix_is_platform(core: str, r_tokens: list[str], start: int) -> bool:
    """Whether tokens before `start` represent a platform prefix rather than game text."""
    if start == 0:
        return True
    prefix = tuple(r_tokens[:start])
    if prefix in _PLATFORM_PREFIX_TUPLES:
        return True
    # Also accept an explicit separator (`Platform - Game` or `Platform: Game`)
    # when the left side resolves to a non-generic platform name.
    for match in _PLATFORM_SEP_RE.finditer(core):
        left = core[:match.start()].strip()
        if not left:
            continue
        left_norm = _normalise(left)
        if left_norm in ("nintendo", "arcade"):
            continue
        if resolve(left) is None:
            continue
        left_tokens = tuple(_canonical_tokens(left_norm.split()))
        if left_tokens == prefix:
            return True
    return False


def _within_single_crossover_part(core: str, start: int, end: int) -> bool:
    """Whether `[start, end)` sits wholly inside one `+`-delimited compilation part."""
    parts = [part for part in _PLUS_SEP_RE.split(core) if _normalise(part)]
    if len(parts) <= 1:
        return False
    pos = 0
    for part in parts:
        count = len(_normalise(part).split())
        seg_end = pos + count
        if pos <= start and end <= seg_end:
            return True
        pos = seg_end
    return False


def _trailing_subtitle_is_allowed(folded_core: str, spans: list[re.Match[str]], end: int) -> bool:
    """Reject unmatched `: <subtitle>` or ` - <subtitle>` extensions unless platform/metadata."""
    if not spans or end <= 0 or end > len(spans):
        return True
    tail = folded_core[spans[end - 1].end():]
    sep = _TRAILING_SUBTITLE_SEP_RE.match(tail)
    if not sep:
        return True
    after_sep = tail[sep.end():].strip()
    if not after_sep:
        return True
    chunks = [c.strip() for c in _TRAILING_CHUNK_SPLIT_RE.split(after_sep) if _normalise(c)]
    for chunk in chunks:
        norm = _normalise(chunk)
        if norm not in ("nintendo", "arcade") and resolve(chunk) is not None:
            continue
        tokens = norm.split()
        if all(
            tok in _TRAILING_METADATA_TOKENS or tok.isdigit() or re.fullmatch(r"v\d+", tok)
            for tok in tokens
        ):
            continue
        return False
    return True


def title_matches(release_title: str, wanted: str) -> bool:
    """Whether a release plausibly IS the requested game.

    Compares contiguous token sequences after stripping known file extensions,
    bracketed release metadata (preserving year qualifiers like ``(2016)``),
    and leading/comma-inverted articles, while canonicalising unambiguous
    Roman numerals. Rejects differing sequel numbers, non-platform title
    prefixes (``New ...``, ``Dr. ...``, ``Paper ...``), unmatched colon/dash
    subtitles, and single halves of ``A + B`` compilations/crossovers.
    """
    w_core = _prepare_core(wanted)
    w_tokens = _canonical_tokens(_normalise(w_core).split())
    if not w_tokens:
        return False

    r_core = _prepare_core(release_title)
    r_folded = unicodedata.normalize("NFKD", r_core).encode("ascii", "ignore").decode("ascii")
    r_spans = list(_ALPHA_NUM_TOKEN_RE.finditer(r_folded.lower()))
    r_tokens = _canonical_tokens([m.group(0) for m in r_spans])
    w_len = len(w_tokens)
    if len(r_tokens) < w_len:
        return False

    want_has_plus = "+" in w_core
    for start in range(len(r_tokens) - w_len + 1):
        end = start + w_len
        if r_tokens[start:end] != w_tokens:
            continue
        if not _prefix_is_platform(r_core, r_tokens, start):
            continue
        if end < len(r_tokens) and r_tokens[end] in _SEQUEL_NEXT_TOKENS:
            continue
        if not want_has_plus and _within_single_crossover_part(r_core, start, end):
            continue
        if not _trailing_subtitle_is_allowed(r_folded, r_spans, end):
            continue
        return True
    return False


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
        lines = [f"{d:+d} {text}" for d, text in
                 sorted(self.reasons, key=lambda pair: pair[0])]
        if lines:
            return lines
        if self.points > 0:
            return [f"{self.points:+d} matched release"]
        return [f"{self.points:+d} no positive platform, region, or seeder score"]


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
    content_refusal = collection_content_refusal(release)
    if content_refusal:
        return Judgement(-1200, verdict=content_refusal)
    if blocklist is not None and release in blocklist:
        return Judgement(-2000,
                         verdict=f"blocklisted: {blocklist.reason_for(release)}")
    if profile:
        refusal = profile.refusal(release.title.lower(), getattr(release, "size", 0),
                                  content_path=release.torrent_file_path)
        if refusal:
            return Judgement(-900, verdict=refusal)
    if not is_game_release(release):
        return Judgement(-1000, verdict="not a game release (wrong category)")
    if platform is not None and platform.media != "digital" and release.categories:
        if all(4000 <= c < 5000 for c in release.categories):
            return Judgement(-300, verdict="a PC release (PC categories)")
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
    min_seeders = getattr(profile, "min_seeders", None) if profile else None
    if min_seeders is None or min_seeders < 1:
        min_seeders = 1
    if release.protocol == "torrent":
        if release.seeders < min_seeders:
            if not release.private:
                if min_seeders <= 1:
                    return Judgement(-400, verdict="no seeders on a public tracker")
                return Judgement(-400, verdict=f"less than {min_seeders} seeders on a public tracker ({release.seeders} seeders)")
            add(-20, "no seeders (private tracker, often temporary)")
        else:
            add(min(release.seeders, 20) * 2, f"{release.seeders} seeders")

    detected_regions = detect_regions(release.title)
    pref_list = getattr(profile, "preferred_regions", None) or ["usa", "world", "europe", "japan"]
    if profile and getattr(profile, "region_mode", "prefer") == "strict":
        if detected_regions and not any(r in pref_list for r in detected_regions):
            return Judgement(-300, verdict=f"region outside allowed list ({', '.join(sorted(detected_regions))})")

    region_handled = False
    if profile and hasattr(profile, "region_scores") and profile.region_scores:
        best_score = None
        best_lbl = ""
        for r in detected_regions:
            if r in profile.region_scores:
                sc = profile.region_scores[r]
                if best_score is None or sc > best_score:
                    best_score = sc
                    best_lbl = r
        if best_score is not None:
            add(best_score, f"region {best_lbl}")
            region_handled = True

    if not region_handled:
        n = len(pref_list)
        matched_ranks = [(pref_list.index(r), r) for r in detected_regions if r in pref_list]
        if matched_ranks:
            matched_ranks.sort()
            best_rank, best_region = matched_ranks[0]
            pts = int(round(40 * (n - best_rank) / n))
            add(pts, f"region {best_region}")
        elif not detected_regions:
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

    _digital = platform is not None and platform.media == "digital"
    junk = _JUNK_MARKERS_DIGITAL if _digital else _JUNK_MARKERS
    hit = next((m for m in junk if m in lowered), "")
    if hit:
        add(-120, f"looks like a hack, beta or repack ({hit!r})")

    # What language the release is in, and whether it is the game as published.
    #
    # Every marker is matched as a whole word, and that is not a nicety: "ger"
    # is inside "Trigger", "ita" inside "digital", "spa" inside "space" and
    # "por" inside "port". A substring test would penalise a large part of
    # every result set -- starting with the game that prompted this.
    is_tag_or_explicit_trans = (
        _TRANSLATION_TAG.search(lowered) is not None
        or "translated" in lowered
    )
    if profile and getattr(profile, "translation_policy", "exclude") == "exclude":
        if is_tag_or_explicit_trans:
            return Judgement(-300, verdict="translation excluded by policy")

    foreign_lang_tag = is_foreign_language_tag(release.title)
    translated = (
        any(_mentions(lowered, marker) for marker in _NON_ENGLISH_MARKERS)
        or is_tag_or_explicit_trans
        or foreign_lang_tag
        or (_MULTI_LANGUAGE.search(lowered) is not None and not _digital)
    )
    if translated:
        add(-_TRANSLATION_PENALTY, "not English, or a fan translation")

    if _CREDITED_TO_A_GROUP.search(lowered) and not _digital:
        # On a cartridge a group credit usually means a hack; on PC it is
        # how every release is signed.
        add(-_CREDIT_PENALTY, "credited to a group, which usually means a hack")

    # Reject a release that names a system other than the one requested.
    # Evaluate own-platform phrases alongside foreign markers longest-first so
    # compound own names (e.g. "nec pc-fx", "pc engine", "xbox 360") strip only
    # their own tokens while longer foreign names (e.g. "nintendo switch 2"
    # during a Switch search, or "nintendo 64" during an NES search) still trip
    # before shorter own aliases.
    if platform is not None:
        own_raw = {platform.slug, platform.name, *platform.aliases,
                   *platform.native_markers}
        own_norms = {
            norm for entry in own_raw
            if (norm := _normalise_marker(entry))
            and (platform.slug, norm) != ("switch-2", "switch")
        }
        foreign_by_norm: dict[str, str] = {}
        for marker in FOREIGN_PLATFORM_MARKERS:
            norm = _normalise_marker(marker)
            if norm and norm not in foreign_by_norm:
                foreign_by_norm[norm] = marker

        remaining = f" {_normalise_marker(lowered)} "
        combined = sorted(
            own_norms | set(foreign_by_norm),
            key=lambda norm: (len(norm), norm in own_norms),
            reverse=True,
        )
        for norm in combined:
            if f" {norm} " not in remaining:
                continue
            if norm in own_norms:
                remaining = re.sub(rf" {re.escape(norm)}(?= )", "", remaining)
                continue
            return Judgement(-300,
                             verdict=f"names another platform ({foreign_by_norm[norm]})")

    # A cracked PC release, whatever it is named. Rejected rather than
    # penalised for the same reason a compilation is: there is no ROM inside
    # for the importer to file, so the grab would succeed and the import
    # could not. Unless PC is the platform being asked for -- then a scene
    # or repack name is the normal shipping form, not a wrong turn.
    digital = platform is not None and platform.media == "digital"
    if not digital:
        for marker in PC_RELEASE_MARKERS:
            if _mentions(lowered, marker):
                return Judgement(-300, verdict=f"a PC release ({marker})")
    else:
        repacker = next((g for g in _TRUSTED_REPACKERS if g in lowered), "")
        if repacker:
            add(40, f"trusted repacker ({repacker})")

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
    path_confirms_platform = False
    if getattr(release, "torrent_file_path", "") and platform is not None:
        parts = [p.strip() for p in re.split(r"[\/]+", release.torrent_file_path) if p.strip() and p != "."]
        folder_parts = parts[:-1] if len(parts) > 1 else parts
        found_plat = None
        for folder in reversed(folder_parts):
            if folder.lower() == "nintendo":
                continue
            p = resolve(folder)
            if p is not None:
                found_plat = p
                break
        if found_plat is not None:
            if same_machine(found_plat.slug, platform.slug):
                add(60, f"collection path names {platform.name}")
                path_confirms_platform = True
            else:
                return Judgement(-300, verdict=f"collection path names another platform ({found_plat.slug})")
        else:
            has_ext = any(lowered.endswith(ext) for ext in platform.extensions)
            if not has_ext:
                return Judgement(-300, verdict="collection path has no platform evidence")

    has_ext = False
    names_plat = False
    names_explicit_name = False
    has_native_marker = False
    if platform is not None:
        scoring_aliases = [a for a in platform.aliases
                           if a.lower() not in _NON_SCORING_ALIASES]
        has_ext = _has_extension(lowered, platform.extensions)
        names_plat = _mentions(lowered, platform.slug.lower()) or any(
            _mentions(lowered, alias) for alias in scoring_aliases
        )
        names_explicit_name = _mentions(lowered, platform.name.lower())
        has_native_marker = any(_mentions(lowered, m) for m in platform.native_markers)
        if has_ext:
            add(60, f"carries a {platform.name} ROM extension")
        elif names_plat:
            add(30, f"names {platform.name}")
        if platform.is_disc and release.protocol != "hub":
            if not (has_ext or names_plat or path_confirms_platform):
                add(-40, f"no extension, platform name or path for {platform.name}")

    # Award the title-match tie-breaker when no platform was constrained, when
    # the release came from a platform-scoped ROM Hub, or when positive platform
    # evidence exists — so an unmarked indexer upload cannot qualify on title alone.
    has_platform_evidence = (
        platform is None
        or release.protocol == "hub"
        or path_confirms_platform
        or has_ext
        or names_plat
        or names_explicit_name
        or has_native_marker
    )
    if has_platform_evidence:
        add(10, "title matches")
    if release.protocol == "hub":
        add(5, "ROM Hub source")

    # A cartridge ROM is small. An oversized "release" for a cartridge platform
    # is a romset, a PC port, or a disc image -- none of which this pipeline can
    # hand to a browser emulator.
    #
    # The ceiling is per platform (Platform.max_size) because one number cannot
    # describe both a 32KB Atari cartridge and a 64MB N64 one. The single 512MB
    # limit this replaces was larger than every cartridge ever made, so a 452MB
    # PC build of Final Fantasy III passed it and, being the best-seeded result,
    # was picked for a SNES request.
    ceiling = 8192 * 1024 * 1024
    if platform is not None:
        medium = "disc image" if platform.is_disc else "cartridge"
        prof_max = getattr(profile, "max_size_bytes", 0) if profile else 0
        if prof_max > 0:
            if getattr(profile, "_max_size_is_global", False):
                ceiling = min(platform.max_size, prof_max)
            else:
                ceiling = prof_max
        else:
            ceiling = platform.max_size
        if release.size > ceiling:
            return Judgement(
                -400,
                verdict=f"too big for a {platform.name} {medium} "
                        f"({release.size // 1048576}MB, ceiling "
                        f"{ceiling // 1048576}MB)")
        if 0 < release.size < _size_floor(platform):
            # The floor is per medium for the same reason the ceiling is. Four
            # kilobytes is a real floor for an Atari cartridge and no floor at
            # all for a disc: a few hundred KB offered for a PlayStation
            # request is a patch, a cheat file or a lone .cue, and every one of
            # those imports cleanly and boots nothing.
            #
            # `0 <` because zero means UNKNOWN, not tiny. Sources that publish
            # no size -- an RSS feed, MiNERVA's search API -- reported 0 and
            # every one of their releases was rejected as "too small", however
            # good the title match was. Absent evidence is not evidence of
            # absence; the title and extension checks still decide those.
            return Judgement(
                -400,
                verdict=f"too small to be a {platform.name} {medium}")

    if profile and getattr(profile, "size_weight", 0) > 0 and getattr(release, "size", 0) > 0:
        if platform is not None:
            calc_ceiling = ceiling
        else:
            prof_max = getattr(profile, "max_size_bytes", 0)
            calc_ceiling = prof_max if prof_max > 0 else ceiling
        size_adj = profile.size_score_for(release.size, calc_ceiling)
        if size_adj:
            add(*size_adj)

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


def infer_platform(release: Release) -> Platform | None:
    """Infer the platform of a release when Interactive Search ran with Any platform.

    Checks in order of specificity:
      1. Unique physical ROM extensions in `release.title` or
         `release.torrent_file_path` (e.g. `.sfc` -> `snes`,
         `.z64` -> `n64`, `.gba` -> `gba`).
      2. Explicit platform markers/aliases in `release.title` (longest alias
         matched first so `"game boy advance"` resolves to `gba` before
         `"game boy"` resolves to `gb`).
      3. Shared Switch extensions fall back to the original Switch unless
         the title or torrent path explicitly identifies Switch 2.
      4. PC scene/repack markers (`fitgirl`, `dodi`, `elamigos`, `gog`, etc.)
         or Newznab/Torznab PC category IDs (`4050` PC/Games) -> `win`.
    """
    from .platforms import PLATFORMS, resolve

    text = f"{release.title or ''} {release.torrent_file_path or ''}".lower()
    # 1. Unique ROM extension
    ext_owners: dict[str, list[Platform]] = {}
    for p in PLATFORMS:
        for ext in p.extensions:
            if ext in (".zip", ".7z", ".rar", ".iso", ".bin", ".cue", ".chd",
                       ".exe", ".msi", ".pkg", ".rom"):
                continue
            ext_owners.setdefault(ext.lower(), []).append(p)
    for ext, owners in sorted(ext_owners.items(), key=lambda kv: -len(kv[0])):
        if len(owners) == 1 and _has_extension(text, (ext,)):
            return owners[0]

    # The Switch 2 collection folder can identify packages whose own names
    # omit the console. Check it before the generic Switch package fallback.
    if any(_mentions(text, name) for name in ("switch 2", "switch2")):
        return resolve("switch-2")

    # 2. Explicit platform name / alias in title (longest first)
    lowered = (release.title or "").lower()
    candidates: list[tuple[int, str, Platform]] = []
    for p in PLATFORMS:
        tokens = {p.slug.lower(), p.name.lower(), *(a.lower() for a in p.aliases)} - _NON_SCORING_ALIASES
        for tok in tokens:
            # Skip generic 2-letter tokens like "pc" or "dc" only if we handle them carefully;
            # _mentions enforces word boundaries.
            if len(tok) < 2:
                continue
            if _mentions(lowered, tok):
                candidates.append((len(tok), tok, p))
    if candidates:
        candidates.sort(key=lambda item: (-item[0], item[1]))
        return candidates[0][2]

    # Preserve inference for unlabeled Switch packages from older indexers.
    if any(_has_extension(text, (ext,)) for ext, owners in ext_owners.items()
           if {p.slug for p in owners} == {"switch", "switch-2"}):
        return resolve("switch")

    # 4. PC repack / scene markers or PC Torznab category (4050)
    if any(_mentions(lowered, marker) for marker in PC_RELEASE_MARKERS):
        return resolve("win")
    cats = set(release.categories or ())
    if any(4000 <= c < 4100 for c in cats):
        return resolve("win")
    return None



def _size_floor(platform: Platform) -> int:
    if platform.slug in ("ps2", "ngc", "xbox"):
        return 8 * 1024 * 1024
    return _DISC_FLOOR if platform.is_disc else _CARTRIDGE_FLOOR


def score(release: Release, wanted: str, platform: Platform | None = None,
          *, profile=None, blocklist=None) -> int:
    """Rank a release. Higher is better; negative means "do not take this"."""
    return judge(release, wanted, platform, profile=profile, blocklist=blocklist).points


def explain(release: Release, wanted: str,
            platform: Platform | None = None,
            *, profile=None, blocklist=None) -> list[str]:
    """Why this release scored what it did, worst reason first."""
    return judge(release, wanted, platform, profile=profile, blocklist=blocklist).why()


def best_release(releases: list[Release], wanted: str,
                 platform: Platform | None = None, *,
                 profile=None, blocklist=None) -> Release | None:
    """The highest-scoring usable release, or None if nothing qualifies."""
    ranked = [(judge(r, wanted, platform, profile=profile,
                     blocklist=blocklist).points, r) for r in releases]
    ranked = [(s, r) for s, r in ranked if s > 0]
    if not ranked:
        return None
    # Sort by score, then break the tie on size -- according to profile.size_preference
    # or the medium's default.
    size_pref = (getattr(profile, "size_preference", "") or "auto").lower() if profile else "auto"
    if size_pref == "smaller":
        ranked.sort(key=lambda pair: (-pair[0], pair[1].size))
    elif size_pref == "larger":
        ranked.sort(key=lambda pair: (-pair[0], -pair[1].size))
    elif size_pref == "none":
        ranked.sort(key=lambda pair: (-pair[0],))
    else:
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


def _digital_set(filenames: list[str]) -> RomSet | None:
    """A PC release as one set: the installer and everything it needs.

    A FitGirl or DODI download is setup.exe plus its .bin archives plus
    whatever else the repacker shipped, and cherry-picking the .exe out of
    that is how you import an installer that cannot install. Everything is
    kept -- including the files other platforms treat as extras -- and the
    primary is the setup executable when one exists, because that is the
    file a person runs.
    """
    if not filenames:
        return None
    exes = [f for f in filenames if _basename(f).lower().endswith(".exe")]
    # Prefer the shallowest exact setup.exe over nested redistributable copies.
    # Fall back to a delimited installer label (setup-1.exe) or a
    # architecture-suffixed setup name (setup64/setupx64), not AssetUpdater.exe.
    exact = [f for f in exes if _basename(f).casefold() == "setup.exe"]
    primary = min(exact, key=lambda f: (
        f.replace("\\", "/").count("/"), f.casefold(), f)) if exact else None
    if primary is None:
        primary = next(
            (f for f in exes if re.search(
                r"(?<![a-z0-9])setup(?:[0-9]+|x(?:86|64))?(?![a-z0-9])", _stem(f).casefold())),
            exes[0] if exes else filenames[0])
    return RomSet(primary, tuple(filenames))


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

    if platform.media == "digital":
        return _digital_set(filenames)

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

    if platform.media == "digital":
        one = _digital_set(filenames)
        return [one] if one else []

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
    norm_marker = _normalise_marker(marker)
    if not norm_marker:
        return False
    return f" {norm_marker} " in f" {_normalise_marker(haystack)} "
