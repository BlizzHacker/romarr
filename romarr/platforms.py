"""Platform knowledge: names, RomM folder slugs, and ROM file extensions.

Three separate things have to agree before a downloaded file can become a
playable entry in RomM:

  1. what a user (or GG Requestz) calls the platform      -- "SNES", "Super Nintendo"
  2. what RomM calls the library folder                    -- `snes`
  3. which file inside the download is actually the ROM    -- `.smc`, not `.nfo`

Getting (3) wrong is the most common failure: game torrents routinely ship
readmes, box art, and sometimes several regional dumps in one archive.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


#: What kind of medium a game shipped on. This is not decoration: it decides
#: the magnitude of the size ceiling, the wording when a release is rejected
#: for size, and -- the part that actually mattered -- whether one file is a
#: complete game or the first of several.
#:
#: A cartridge dump is one file. A disc rip is a `.cue` naming `.bin` tracks,
#: or a `.gdi` naming five of them, and importing the sheet alone produces a
#: library entry that looks correct and boots nothing. `selection.pick_rom_set`
#: exists because of this field.
CARTRIDGE = "cartridge"
DISC = "disc"
COMPUTER = "computer"


@dataclass(frozen=True)
class Platform:
    """A console RomM can hold and an emulator can run."""

    slug: str                      # RomM's fs_slug -- the library folder name
    name: str                      # human label
    extensions: tuple[str, ...]    # ROM extensions, most-preferred first
    aliases: tuple[str, ...] = field(default=())
    # The largest plausible download for ONE game on this system. See MB below.
    max_size: int = 32 * 1024 * 1024
    media: str = CARTRIDGE
    # Words in FOREIGN_PLATFORM_MARKERS that are native here, and so must not
    # disqualify a release.
    #
    # The marker list is shared, and a marker that names another system for
    # fifteen platforms can name *this* one for the sixteenth. "wad" is the
    # case that forced this: it is the marker that catches a Wii Virtual
    # Console repackage of a Genesis game, and it is also the extension every
    # legitimate WiiWare title ships as. Without an exemption, adding Wii as a
    # platform would have made most of its catalogue unrequestable.
    native_markers: tuple[str, ...] = field(default=())
    # Whether an archive IS the ROM here rather than something to look inside.
    #
    # For MAME and FBNeo the `.zip` is the romset: the core opens it itself and
    # expects its internal layout of chip dumps. Extracting one produces a
    # directory of `.u1`/`.c1`/`.v1` files that no core can load, so the import
    # would "succeed" and leave an unplayable entry. `RommStreamServer` has the
    # same rule in `archives.py` under the same reasoning -- extraction is
    # opt-in per platform, never "expand anything compressed".
    archive_is_the_rom: bool = False

    @property
    def is_disc(self) -> bool:
        return self.media == DISC


MB = 1024 * 1024
GB = 1024 * MB

# How big a release for each system can plausibly be.
#
# Scoring used a single 512MB ceiling for every platform, which is roughly
# eighty times the largest SNES cartridge ever made. A 452MB PC build of Final
# Fantasy III sailed under it, ranked top on seeders and was picked for a SNES
# request -- the same failure as a translation hack, arrived at through size.
#
# Each number is the biggest cartridge the system shipped, rounded up hard for
# headroom, because a release is not always a bare ROM: it may be zipped, carry
# box art and a readme, or hold several regional dumps. The headroom is what
# makes this safe to tighten -- the job is to exclude PC ports and romsets, not
# to second-guess how a dump was packaged.
#
#   system   biggest cartridge          ceiling here
#   NES      1MB   (mapper-heavy carts)  8MB
#   SNES     6MB   (Tales of Phantasia)  24MB
#   GBA      32MB  (full 256Mbit carts)  128MB
#   N64      64MB  (Resident Evil 2)     256MB
#   Genesis  8MB   (Pier Solar)          32MB
#
# Disc ceilings follow the same rule one medium up: the capacity of the disc,
# rounded for a rip that may carry uncompressed audio tracks. They still do
# real work -- a 12GB PS2 ceiling is what keeps a 60GB PC repack out of a PS2
# request, which is the same job the 24MB SNES ceiling does against a 452MB PC
# build.
#
#   system   disc capacity              ceiling here
#   PSX      700MB (CD)                  2GB
#   Saturn   700MB (CD)                  2GB
#   Dreamcast 1.2GB (GD-ROM)             3GB
#   GameCube 1.5GB (miniDVD)             4GB
#   PSP      1.8GB (UMD)                 4GB
#   PS2      8.5GB (dual-layer DVD)      12GB
#   Wii      8.5GB (dual-layer DVD)      12GB
PLATFORMS: tuple[Platform, ...] = (
    # "famicom" and "super famicom" were aliases here until those became
    # platforms in their own right. RomM keeps separate folders for them and
    # the live library fills both -- 106 famicom, 968 fds, 127 sfam -- so a
    # request naming the Japanese machine now reaches the Japanese folder
    # instead of being filed under its western twin.
    Platform("nes", "Nintendo Entertainment System", (".nes", ".fds", ".unf"),
             ("nintendo", "nintendo entertainment system"),
             max_size=8 * MB),
    # Aliases carry the machine's full formal name from here down. `resolve`
    # matches exactly, so a name that is not written here is one it will not
    # answer -- and RomM publishes the long forms, not the short ones. Each of
    # these is a display name read off the live library, not an invention:
    # 1,848 rows arrive as "Super Nintendo Entertainment System".
    Platform("snes", "Super Nintendo", (".smc", ".sfc", ".swc", ".fig"),
             ("super nintendo", "sfc", "super nes",
              "super nintendo entertainment system"),
             max_size=24 * MB),
    Platform("gb", "Game Boy", (".gb",), ("gameboy", "game boy"),
             max_size=8 * MB),
    Platform("gbc", "Game Boy Color", (".gbc",), ("gameboy color", "game boy color"),
             max_size=16 * MB),
    Platform("gba", "Game Boy Advance", (".gba",), ("gameboy advance", "game boy advance"),
             max_size=128 * MB),
    Platform("n64", "Nintendo 64", (".z64", ".n64", ".v64"),
             ("nintendo 64", "n 64"), max_size=256 * MB),
    Platform("genesis-slash-megadrive", "Sega Genesis / Mega Drive",
             (".md", ".gen", ".smd", ".bin"),
             ("genesis", "mega drive", "megadrive", "sega genesis",
              "sega mega drive/genesis"),
             max_size=32 * MB),
    Platform("sms", "Sega Master System", (".sms",),
             ("master system", "sega master system/mark iii"),
             max_size=8 * MB),
    Platform("gamegear", "Game Gear", (".gg",), ("game gear", "sega game gear"),
             max_size=8 * MB),
    Platform("atari2600", "Atari 2600", (".a26", ".bin"), ("2600", "vcs"),
             max_size=8 * MB),
    Platform("atari7800", "Atari 7800", (".a78",), ("7800",), max_size=8 * MB),
    Platform("lynx", "Atari Lynx", (".lnx",), (), max_size=8 * MB),
    Platform("turbografx16--1", "TurboGrafx-16", (".pce",), ("pc engine", "turbografx"),
             max_size=16 * MB),
    Platform("wonderswan", "WonderSwan", (".ws", ".wsc"), (), max_size=16 * MB),
    Platform("neo-geo-pocket", "Neo Geo Pocket", (".ngp", ".ngc"), (), max_size=8 * MB),
    Platform("virtualboy", "Virtual Boy", (".vb",), ("virtual boy",), max_size=8 * MB),

    # -- large cartridges ---------------------------------------------------
    #
    # Excluded before for the same reason discs were, and just as wrongly: a
    # DS cartridge is 512MB at the very most and EmulatorJS runs `melonds` for
    # it out of the box.
    # The mid-generation revisions are folded in deliberately, not by accident
    # of matching: `.dsi` is already in the extension list above and melonDS
    # runs those carts, so RomM's separate `nintendo-dsi` and `new-nintendo-3ds`
    # folders belong to these two platforms rather than to nothing. Declared
    # because `resolve` matches exactly -- an undeclared revision is a name it
    # will not answer.
    Platform("nds", "Nintendo DS", (".nds", ".dsi", ".ids"),
             ("nintendo ds", "ds", "nintendo dsi", "dsi"), max_size=512 * MB),
    Platform("3ds", "Nintendo 3DS", (".3ds", ".cci", ".cxi", ".cia"),
             ("nintendo 3ds", "3ds", "new nintendo 3ds"), max_size=8 * GB),

    # -- optical media ------------------------------------------------------
    #
    # Extension order is preference order, and for a disc that ordering is a
    # correctness rule rather than a taste: a `.chd` or `.rvz` is ONE file that
    # cannot be separated from its tracks, while a `.cue` is a text file that
    # is worthless without the `.bin` beside it. Preferring the whole-disc
    # image makes the fragile case the fallback rather than the default.
    #
    # `.cue` and `.bin` are always declared together. A platform that named
    # the sheet without the tracks would import a pointer to nothing --
    # `test_a_cue_never_appears_without_its_bin` fails if that is ever
    # narrowed.
    Platform("psx", "Sony PlayStation",
             (".chd", ".pbp", ".cue", ".bin", ".img", ".ccd", ".iso", ".m3u"),
             ("playstation", "sony playstation", "ps1", "psone", "psx"),
             max_size=2 * GB, media=DISC),
    Platform("ps2", "Sony PlayStation 2",
             (".chd", ".iso", ".cso", ".bin", ".gz"),
             ("playstation 2", "sony playstation 2", "ps2"),
             max_size=12 * GB, media=DISC),
    Platform("psp", "Sony PlayStation Portable",
             (".cso", ".iso", ".chd", ".pbp"),
             ("playstation portable", "psp"),
             max_size=4 * GB, media=DISC),
    Platform("saturn", "Sega Saturn",
             (".chd", ".cue", ".bin", ".iso", ".ccd", ".mds"),
             ("sega saturn",), max_size=2 * GB, media=DISC),
    Platform("segacd", "Sega CD / Mega-CD",
             (".chd", ".cue", ".bin", ".iso"),
             ("sega cd", "mega cd", "megacd", "mega-cd", "sega mega-cd"),
             max_size=2 * GB, media=DISC),
    Platform("dc", "Sega Dreamcast",
             (".chd", ".gdi", ".cdi", ".cue", ".bin"),
             ("dreamcast", "sega dreamcast"), max_size=3 * GB, media=DISC),
    Platform("ngc", "Nintendo GameCube",
             (".rvz", ".iso", ".gcm", ".ciso", ".gcz"),
             ("gamecube", "nintendo gamecube", "game cube", "gcn"),
             max_size=4 * GB, media=DISC),
    Platform("wii", "Nintendo Wii",
             (".rvz", ".wbfs", ".iso", ".wad", ".ciso", ".gcz"),
             ("nintendo wii",), max_size=12 * GB, media=DISC,
             # See Platform.native_markers. Every Wii release that is a
             # WiiWare or Virtual Console title says so, and those are the
             # markers that exist to catch such a title being offered for a
             # *cartridge* platform. Here they are the catalogue.
             native_markers=("wad", "wiiware", "virtual console", "eshop")),
    Platform("3do", "3DO Interactive Multiplayer",
             (".chd", ".cue", ".bin", ".iso"),
             ("3do interactive", "panasonic 3do"), max_size=2 * GB, media=DISC),
    Platform("philips-cd-i", "Philips CD-i",
             (".chd", ".cue", ".bin", ".iso"),
             ("cd-i", "cdi", "philips cdi"), max_size=2 * GB, media=DISC),
    Platform("pc-fx", "NEC PC-FX",
             (".chd", ".cue", ".bin", ".iso"),
             ("pcfx", "nec pc-fx"), max_size=2 * GB, media=DISC),
    # RomM has two slugs for this machine. This is the one the live library
    # materialises and the one `RommStreamServer/tiers.py` routes, so it is the
    # one that can actually be filed into and played.
    Platform("turbografx-16-slash-pc-engine-cd", "TurboGrafx-CD / PC Engine CD",
             (".chd", ".cue", ".bin", ".iso"),
             ("turbografx cd", "turbografx-cd", "pc engine cd", "pcecd",
              "super cd-rom",
              # RomM's display name, and it names the CD machine -- which is
              # what the old earliest-alias ranking got backwards, handing it
              # to the cartridge TurboGrafx because "turbografx" came first in
              # the string. The name is claimed here and only here.
              #
              # It is also not enough on its own: the live library has THREE
              # folders under this one display name (`turbografx16--1`,
              # `pcenginecd` and this slug, 1,262 rows between them). No
              # resolution of the name can separate them, which is the whole
              # argument for callers passing RomM's `platform_slug`.
              "turbografx-16/pc engine cd"),
             max_size=2 * GB, media=DISC),
    Platform("amiga-cd32", "Amiga CD32",
             (".chd", ".cue", ".bin", ".iso"),
             ("cd32", "amiga cd32"), max_size=2 * GB, media=DISC),
    Platform("neo-geo-cd", "Neo Geo CD",
             (".chd", ".cue", ".bin", ".iso"),
             ("neogeo cd", "neo geo cd"), max_size=2 * GB, media=DISC),
    Platform("atari-jaguar-cd", "Atari Jaguar CD",
             (".chd", ".cue", ".bin", ".iso"),
             ("jaguar cd",), max_size=2 * GB, media=DISC),

    # -- everything else with a player --------------------------------------
    #
    # Extensions and ceilings below are read off the live library rather than
    # recalled: the counts came from sampling each platform's folder, so
    # `.a52` is here because 320 Atari 5200 files end in it and `.col` because
    # 172 ColecoVision ones do.
    #
    # The bar for inclusion is a real play route -- a core in RomM's base
    # EmulatorJS map, or one installed on the stream server. Every platform
    # here has one, and together they were 17,000-odd games in the library
    # that ROMarr had no way to request.
    Platform("jaguar", "Atari Jaguar",
             (".jag", ".j64", ".rom", ".abs", ".cof", ".prg"),
             ("atari jaguar",), max_size=32 * MB),
    # For MAME and FBNeo the zip is the romset, not a container -- see
    # Platform.archive_is_the_rom.
    Platform("arcade", "Arcade", (".zip", ".7z", ".chd"),
             ("mame", "coin-op"), max_size=256 * MB,
             archive_is_the_rom=True),
    Platform("neogeoaes", "Neo Geo AES", (".zip", ".7z"),
             ("neo geo aes", "neogeo aes"), max_size=256 * MB,
             archive_is_the_rom=True),
    Platform("neogeomvs", "Neo Geo MVS", (".zip", ".7z"),
             ("neo geo mvs", "neogeo mvs"), max_size=256 * MB,
             archive_is_the_rom=True),
    Platform("atari5200", "Atari 5200", (".a52", ".bin"), ("5200",),
             max_size=8 * MB),
    Platform("colecovision", "ColecoVision", (".col", ".rom", ".bin", ".zip"),
             ("coleco",), max_size=8 * MB),
    Platform("sega32", "Sega 32X", (".32x", ".bin", ".zip"),
             ("32x", "sega 32x", "mega 32x"), max_size=32 * MB),
    Platform("supergrafx", "PC Engine SuperGrafx", (".sgx", ".pce", ".zip"),
             ("super grafx",), max_size=16 * MB),
    Platform("vectrex", "Vectrex", (".vec", ".bin", ".zip"), (),
             max_size=8 * MB),
    Platform("intellivision", "Intellivision", (".int", ".rom", ".bin", ".zip"),
             ("intv",), max_size=8 * MB),
    Platform("wonderswan-color", "WonderSwan Color", (".wsc", ".zip"),
             ("wonderswan colour",), max_size=16 * MB),
    Platform("neo-geo-pocket-color", "Neo Geo Pocket Color", (".ngc", ".ngp"),
             ("neo geo pocket colour", "ngpc"), max_size=16 * MB),
    # Regional twins of platforms already here. They are separate RomM folders
    # with their own catalogues -- 968 fds, 127 sfam, 106 famicom -- so a
    # request for one must not be filed under the other.
    # "Family Computer" is the name RomM publishes for both of these -- the
    # literal translation, not the transliteration ROMarr names them by.
    Platform("fds", "Famicom Disk System", (".fds", ".zip"),
             ("famicom disk system", "disk system",
              "family computer disk system"), max_size=8 * MB),
    Platform("famicom", "Famicom", (".nes", ".zip"), ("family computer",),
             max_size=8 * MB),
    Platform("sfam", "Super Famicom", (".sfc", ".smc", ".zip"), (),
             max_size=24 * MB),

    # -- home computers -----------------------------------------------------
    #
    # Disk images, not cartridges, and often several per title -- which is why
    # `.m3u` is declared where the library actually uses it (99 of the Sharp
    # X68000 entries).
    Platform("c64", "Commodore 64",
             (".d64", ".t64", ".d81", ".tap", ".prg", ".crt", ".zip"),
             # The last of these is RomM's display name for the 688-row C64
             # folder. It resolved to nothing at all before, because no alias
             # here spells the machine the way the catalogue does.
             ("commodore 64", "c 64", "commodore c64/128/max"),
             max_size=16 * MB, media=COMPUTER),
    Platform("c128", "Commodore 128", (".d64", ".d81", ".t64", ".prg"),
             ("commodore 128",), max_size=16 * MB, media=COMPUTER),
    Platform("vic-20", "Commodore VIC-20", (".prg", ".d64", ".t64", ".crt"),
             ("vic 20", "vic20"), max_size=16 * MB, media=COMPUTER),
    Platform("amiga", "Commodore Amiga",
             (".adf", ".hdf", ".ipf", ".lha", ".zip"),
             ("commodore amiga",), max_size=512 * MB, media=COMPUTER),
    Platform("acpc", "Amstrad CPC", (".dsk", ".sna", ".cdt", ".zip"),
             ("amstrad cpc", "cpc"), max_size=32 * MB, media=COMPUTER),
    Platform("zxs", "Sinclair ZX Spectrum",
             (".tzx", ".tap", ".z80", ".sna", ".zip"),
             ("zx spectrum", "spectrum", "speccy"),
             max_size=16 * MB, media=COMPUTER),
    Platform("msx", "MSX", (".rom", ".cas", ".dsk", ".mx1", ".zip"), (),
             max_size=32 * MB, media=COMPUTER),
    Platform("msx2", "MSX2", (".rom", ".dsk", ".mx2", ".zip"), (),
             max_size=32 * MB, media=COMPUTER),
    Platform("sharp-x68000", "Sharp X68000",
             (".m3u", ".dim", ".xdf", ".d88", ".zip"),
             ("x68000", "sharp x 68000"), max_size=256 * MB, media=COMPUTER),
    Platform("dos", "MS-DOS", (".dosz", ".zip", ".dos", ".img", ".chd"),
             ("ms-dos", "ms dos", "pc dos"), max_size=2 * GB, media=COMPUTER,
             archive_is_the_rom=True,
             # `dos` and `pc` are in FOREIGN_PLATFORM_MARKERS because they mean
             # "this is a PC port" for every console. Here they are the
             # platform.
             native_markers=("dos", "pc", "windows")),
    # Modern PC, which is Questarr's home turf and now also ours. `win` is
    # the slug RomM's own filesystem layout uses for Windows. media="digital"
    # is what flips the scorer's rules: on every other platform a FitGirl or
    # DODI repack is a wrong grab, on this one it is the normal shipping
    # form -- and the whole download is one installer set, so import keeps
    # it together instead of cherry-picking a setup.exe.
    Platform("win", "PC (Windows)", (".exe", ".msi", ".iso"),
             ("pc", "windows", "microsoft windows", "pc (windows)",
              "pc windows"),
             max_size=250 * GB, media="digital",
             native_markers=("pc", "windows", "repack", "steam", "gog",
                             "codex", "tenoke", "rune", "empress", "skidrow",
                             "plaza", "flt", "razor1911", "cracked",
                             "crackfix", "goldberg", "online-fix")),
)

_BY_SLUG = {p.slug: p for p in PLATFORMS}


# Punctuation is noise in a platform name and every source spells it
# differently: RomM publishes "Turbografx-16/PC Engine CD", a request form
# sends "TurboGrafx 16 / PC Engine CD", a slug writes it out with "slash".
# Folding punctuation to spaces lets one declared alias answer all three
# without any of them having to guess at the others' spelling.
_PUNCTUATION = re.compile(r"[^a-z0-9]+")


def _normalise(text: str) -> str:
    return _PUNCTUATION.sub(" ", text.strip().lower()).strip()


#: Every name a platform answers to, normalised, mapped to that platform.
#:
#: First declaration wins, which only matters if two platforms claim one name
#: -- and that is a silent misfiling, so
#: `test_no_two_platforms_answer_to_the_same_name` fails the suite instead of
#: letting declaration order decide it.
_BY_LABEL: dict[str, Platform] = {}
for _platform in PLATFORMS:
    for _label in (_platform.slug, _platform.name, *_platform.aliases):
        _BY_LABEL.setdefault(_normalise(_label), _platform)


def all_extensions() -> set[str]:
    """Every extension any supported platform recognises."""
    return {ext for p in PLATFORMS for ext in p.extensions}


def by_slug(slug: str) -> Platform | None:
    return _BY_SLUG.get(slug.strip().lower())


def resolve(text: str) -> Platform | None:
    """Find the platform a name refers to, or nothing.

    Matches the slug, the display name, or a declared alias -- exactly, once
    case and punctuation are folded away. Nothing else. A name this does not
    know resolves to nothing, and that is the feature rather than the gap.

    This used to hunt for any alias *inside* the text and rank the hits by
    where they started. Both halves were wrong, and measured against the live
    library they answered 4,173 rows about the wrong machine:

      * A bare substring has no word boundaries. "ds" sits inside "Edsac" and
        "pc" inside "Amstrad PCW", so those resolved to the Nintendo DS and to
        Windows -- 753 rows on the Amstrad alone. `selection._mentions` has
        guarded against precisely this since the scorer was written, for
        precisely these two words. `resolve` never got the same treatment, and
        `resolve` is the one that decides which directory a file lands in.

      * A prefix match with words left over is not a match. "Nintendo Switch"
        begins with "nintendo", which is an NES alias, so 2,874 Switch rows
        were answered about a Famicom; "PlayStation Vita" begins with
        "playstation", so 34 more were answered about a PS1. The old
        `_leaves_a_model_number` caught the digit form of this ("PlayStation
        5") and could never catch the word form, because no rule separates
        "Vita" from "Entertainment System" -- one names a different machine,
        the other finishes the name of this one. Only a declaration can tell
        those apart, which is why the compound names are spelled out in the
        table above.

    Ranking by position then made the last case worse instead of better.
    "Turbografx-16/PC Engine CD" has "CD" in it, and earliest-alias-wins
    picked the cartridge machine because "turbografx" comes before "pc engine
    cd" in the string.

    Refusing to answer is the safe failure here, and callers are built for it:
    `Service.request` returns "unknown platform: ...", and the webhook path
    records a failed event somebody can go and fix. A wrong answer instead
    files a download into another console's folder and says nothing at all.
    """
    if not text:
        return None
    # Before normalisation, because a slug is the authoritative form and
    # `genesis-slash-megadrive` must not have to survive a round trip through
    # punctuation folding to be recognised as itself.
    needle = text.strip().lower()
    if needle in _BY_SLUG:
        return _BY_SLUG[needle]
    return _BY_LABEL.get(_normalise(needle))


def platform_for_file(filename: str) -> Platform | None:
    """Which platform a ROM file belongs to, judged by its extension.

    Ambiguous extensions (`.bin` is both Atari 2600 and Mega Drive) resolve to
    the first platform that claims them, so callers that already know the
    platform should not rely on this.
    """
    lowered = filename.lower()
    for platform in PLATFORMS:
        for ext in platform.extensions:
            if lowered.endswith(ext):
                return platform
    return None
