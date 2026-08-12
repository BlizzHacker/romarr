"""Disc-based platforms are supported, and the table that says so is honest.

The README used to claim disc platforms were excluded because "browser
emulators cannot stream" them. RomM 4.9.2's own core map runs nine optical
systems -- psx, psp, saturn, segacd, 3do, philips-cd-i, pc-fx, turbografx-cd
and amiga-cd32 -- and the stream server runs the rest, so the exclusion
described ROMarr's importer rather than any limitation. These tests pin the
platform table that replaces it.
"""

from __future__ import annotations

import pytest

from romarr import platforms
from romarr.platforms import CARTRIDGE, COMPUTER, DISC, MB, GB, Platform, resolve


DISC_SLUGS = (
    "psx", "ps2", "ps3", "psp", "saturn", "segacd", "dc", "ngc", "wii",
    "wiiu", "xbox", "xbox360", "3do",
    "philips-cd-i", "pc-fx", "turbografx-16-slash-pc-engine-cd",
    "amiga-cd32", "neo-geo-cd", "atari-jaguar-cd",
)


def test_every_disc_platform_is_present():
    """The exclusion is gone: each optical system can be named and requested."""
    missing = [s for s in DISC_SLUGS if platforms.by_slug(s) is None]
    assert not missing, f"disc platforms still unreachable: {missing}"


def test_disc_platforms_declare_disc_media():
    for slug in DISC_SLUGS:
        assert platforms.by_slug(slug).media == DISC, slug


def test_cartridge_platforms_kept_their_medium():
    """Adding discs must not reclassify what already worked."""
    for slug in ("nes", "snes", "gba", "n64", "genesis-slash-megadrive"):
        assert platforms.by_slug(slug).media == CARTRIDGE, slug


def test_cartridge_ceilings_are_unchanged():
    """The per-platform ceilings were tuned against real result sets.

    A disc release passing them is the bug this whole change exists to fix;
    a cartridge ceiling drifting upward while nobody looked is how the
    452MB PC build of Final Fantasy III got picked for a SNES request.
    """
    assert platforms.by_slug("nes").max_size == 8 * MB
    assert platforms.by_slug("snes").max_size == 24 * MB
    assert platforms.by_slug("gba").max_size == 128 * MB
    assert platforms.by_slug("n64").max_size == 256 * MB


@pytest.mark.parametrize("slug,floor,ceiling", [
    # A single CD is 700MB; the ceiling has to clear an uncompressed
    # bin/cue rip with audio tracks without admitting a PC repack.
    ("psx", 1 * GB, 4 * GB),
    ("saturn", 1 * GB, 4 * GB),
    ("segacd", 1 * GB, 4 * GB),
    ("3do", 1 * GB, 4 * GB),
    # DVD-based. PS2 dual layer is 8.5GB.
    ("ps2", 8 * GB, 16 * GB),
    ("wii", 8 * GB, 16 * GB),
    # miniDVD is 1.5GB; UMD is 1.8GB.
    ("ngc", 2 * GB, 8 * GB),
    ("psp", 2 * GB, 8 * GB),
    # Sixth-generation DVD, same shape as PS2. An Xbox disc is DVD-9 at
    # 8.5GB and XGD3 on the 360 is 8.7GB; the largest measured title in the
    # Vimm's Lair capture is 8.06GB.
    ("xbox", 8 * GB, 16 * GB),
    ("xbox360", 8 * GB, 16 * GB),
    # Blu-ray. PS3 dual layer is 50GB (largest measured 44.8GB); a Wii U
    # disc is 25GB (largest measured 22.4GB).
    ("ps3", 48 * GB, 96 * GB),
    ("wiiu", 24 * GB, 48 * GB),
])
def test_disc_ceilings_clear_one_disc_without_admitting_a_repack(slug, floor, ceiling):
    platform = platforms.by_slug(slug)
    assert floor <= platform.max_size <= ceiling, (
        f"{slug} ceiling {platform.max_size // (1024 ** 3)}GB is outside the "
        f"plausible range for one {platform.name} disc")


def test_disc_platforms_prefer_a_single_file_image_first():
    """Extension order is preference order, and a whole-disc image wins.

    A `.chd` or `.rvz` is one file that cannot be separated from its
    tracks. A `.cue` can, and routinely is -- which is the failure this
    ordering exists to make unlikely in the first place.
    """
    whole_disc = {".chd", ".rvz", ".iso", ".cso", ".pbp", ".wbfs", ".gdi"}
    for slug in DISC_SLUGS:
        platform = platforms.by_slug(slug)
        assert platform.extensions[0] in whole_disc, (
            f"{slug} prefers {platform.extensions[0]!r} over a whole-disc image")


def test_a_cue_never_appears_without_its_bin():
    """Declaring .cue but not .bin would import a sheet pointing at nothing."""
    for slug in DISC_SLUGS:
        exts = platforms.by_slug(slug).extensions
        if ".cue" in exts:
            assert ".bin" in exts, f"{slug} declares .cue with no .bin"


def test_playstation_resolves_from_the_names_people_use():
    for text in ("psx", "ps1", "playstation", "PlayStation", "sony playstation"):
        assert resolve(text) is not None and resolve(text).slug == "psx", text


def test_playstation_2_is_not_resolved_as_playstation_1():
    """The qualifier trap that already caught SNES/NES, one console family over.

    "playstation 2" contains "playstation". Position-ranked matching has to
    keep picking the specific one, or every PS2 request silently becomes a
    PS1 request.
    """
    for text in ("ps2", "playstation 2", "PlayStation 2", "sony playstation 2"):
        assert resolve(text).slug == "ps2", text


def test_gamecube_and_wii_do_not_collide():
    assert resolve("gamecube").slug == "ngc"
    assert resolve("nintendo gamecube").slug == "ngc"
    assert resolve("wii").slug == "wii"


def test_dreamcast_resolves():
    for text in ("dreamcast", "sega dreamcast", "dc"):
        assert resolve(text).slug == "dc", text


def test_every_slug_is_unique():
    slugs = [p.slug for p in platforms.PLATFORMS]
    assert len(slugs) == len(set(slugs))


def test_every_alias_resolves_to_the_platform_that_declares_it():
    """An alias claimed by two platforms is a silent misfiling waiting to happen."""
    for platform in platforms.PLATFORMS:
        for alias in platform.aliases:
            got = resolve(alias)
            assert got is not None, f"{platform.slug} alias {alias!r} resolves to nothing"
            assert got.slug == platform.slug, (
                f"{platform.slug} alias {alias!r} resolves to {got.slug}")


def test_no_two_platforms_answer_to_the_same_name():
    """The same guard as above, one level down at the normalised form.

    `resolve` folds case and punctuation before matching, so two labels that
    look distinct in the table can collide once folded -- "pc (windows)" and
    "pc windows" already do, harmlessly, because one platform declares both.
    A collision across *two* platforms is not harmless: `_BY_LABEL` keeps the
    first declaration, which hands every request for that name to whichever
    machine happens to sit higher up the table, silently.
    """
    owner: dict[str, str] = {}
    for platform in platforms.PLATFORMS:
        for label in (platform.slug, platform.name, *platform.aliases):
            key = platforms._normalise(label)
            claimed = owner.setdefault(key, platform.slug)
            assert claimed == platform.slug, (
                f"{key!r} is claimed by both {claimed} and {platform.slug}")


def test_media_is_one_of_the_four_known_values():
    # "digital" is the fourth medium: modern PC, where there is no physical
    # dump and a 100GB installer is a normal size.
    for platform in platforms.PLATFORMS:
        assert platform.media in (CARTRIDGE, DISC, COMPUTER,
                                  "digital"), platform.slug


def test_all_extensions_still_answers_for_every_platform():
    every = platforms.all_extensions()
    assert ".smc" in every and ".chd" in every and ".rvz" in every


def test_disc_platforms_are_reported_as_such():
    """A caller can ask which platforms need multi-file handling."""
    disc = {p.slug for p in platforms.PLATFORMS if p.media == DISC}
    assert set(DISC_SLUGS) <= disc
    assert "snes" not in disc


# --- the documentation has to agree with the code --------------------------

def _readme() -> str:
    from pathlib import Path
    return (Path(__file__).resolve().parents[1] / "README.md").read_text(
        encoding="utf-8")


def test_the_readme_no_longer_excludes_disc_platforms():
    """The claim that started this, pinned so it cannot come back.

    "Disc-based platforms are excluded -- multi-gigabyte images that browser
    emulators cannot stream." Both halves were false: RomM 4.9.2's own core
    map runs nine optical systems in a browser, and the stream server runs the
    rest server-side.
    """
    readme = _readme().lower()
    assert "disc-based platforms are excluded" not in readme
    assert "cannot stream" not in readme


def test_the_readme_lists_the_disc_platforms_the_code_supports():
    """A README that names a platform the code cannot request, or omits one it
    can, is the same defect facing either way."""
    readme = _readme()
    for name in ("PlayStation 2", "Dreamcast", "GameCube", "Saturn", "PSP",
                 "Neo Geo CD", "Amiga CD32"):
        assert name in readme, name


def test_the_readme_documents_every_play_route():
    readme = _readme()
    for route in ("EmulatorJS", "Archive.org", "Download", "STREAM_SERVER_URL"):
        assert route in readme, route
