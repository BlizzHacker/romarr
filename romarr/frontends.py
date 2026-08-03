"""Handing the library to the launchers people actually sit in front of.

LaunchBox, Playnite and ES-DE are where a lot of people play. None of them
speaks a common protocol, and two of the three are Windows .NET applications
whose plugin model is a compiled DLL -- so the durable integration is not a
plugin at all, it is emitting the formats they already read.

That distinction matters. A plugin breaks when the host application changes
its API; a `gamelist.xml` has been read the same way for fifteen years. The
plugins in `contrib/` are a convenience on top of this module, not a
replacement for it -- and if one of them ever stops working, the export is
still there.

Everything here is a pure function from library rows to text, so all of it is
tested. The plugins are not, because .NET cannot be compiled in this
environment, and that difference is stated in `contrib/README.md` rather than
glossed over.
"""

from __future__ import annotations

import json
import logging
import xml.etree.ElementTree as ET
from xml.dom import minidom

log = logging.getLogger(__name__)

#: ROMarr slug -> the platform name each launcher expects.
#:
#: These are not decoration. LaunchBox and Playnite match their metadata by
#: platform *name*, so a game filed under a name they do not recognise imports
#: with no box art, no description and no association with the rest of that
#: system's library -- it looks like the export half-worked, which is harder to
#: diagnose than a clean failure.
LAUNCHBOX_PLATFORMS = {
    "nes": "Nintendo Entertainment System",
    "famicom": "Nintendo Famicom",
    "fds": "Nintendo Famicom Disk System",
    "snes": "Super Nintendo Entertainment System",
    "sfam": "Super Famicom",
    "n64": "Nintendo 64",
    "gb": "Nintendo Game Boy",
    "gbc": "Nintendo Game Boy Color",
    "gba": "Nintendo Game Boy Advance",
    "nds": "Nintendo DS",
    "3ds": "Nintendo 3DS",
    "ngc": "Nintendo GameCube",
    "wii": "Nintendo Wii",
    "genesis-slash-megadrive": "Sega Genesis",
    "sms": "Sega Master System",
    "gamegear": "Sega Game Gear",
    "sega32": "Sega 32X",
    "segacd": "Sega CD",
    "saturn": "Sega Saturn",
    "dc": "Sega Dreamcast",
    "psx": "Sony Playstation",
    "ps2": "Sony Playstation 2",
    "psp": "Sony PSP",
    "atari2600": "Atari 2600",
    "atari5200": "Atari 5200",
    "atari7800": "Atari 7800",
    "lynx": "Atari Lynx",
    "jaguar": "Atari Jaguar",
    "atari-jaguar-cd": "Atari Jaguar CD",
    "3do": "3DO Interactive Multiplayer",
    "colecovision": "ColecoVision",
    "intellivision": "Mattel Intellivision",
    "vectrex": "GCE Vectrex",
    "turbografx16--1": "NEC TurboGrafx-16",
    "turbografx-16-slash-pc-engine-cd": "NEC TurboGrafx-CD",
    "supergrafx": "NEC SuperGrafx",
    "pc-fx": "NEC PC-FX",
    "wonderswan": "WonderSwan",
    "wonderswan-color": "WonderSwan Color",
    "neo-geo-pocket": "SNK Neo Geo Pocket",
    "neo-geo-pocket-color": "SNK Neo Geo Pocket Color",
    "neogeoaes": "SNK Neo Geo AES",
    "neogeomvs": "SNK Neo Geo MVS",
    "neo-geo-cd": "SNK Neo Geo CD",
    "virtualboy": "Nintendo Virtual Boy",
    "amiga": "Commodore Amiga",
    "amiga-cd32": "Commodore Amiga CD32",
    "c64": "Commodore 64",
    "c128": "Commodore 128",
    "vic-20": "Commodore VIC-20",
    "acpc": "Amstrad CPC",
    "zxs": "Sinclair ZX Spectrum",
    "msx": "Microsoft MSX",
    "msx2": "Microsoft MSX2",
    "sharp-x68000": "Sharp X68000",
    "dos": "MS-DOS",
    "arcade": "Arcade",
    "philips-cd-i": "Philips CD-i",
}

#: ES-DE reads a directory per system, named by its own short slug.
ESDE_FOLDERS = {
    "genesis-slash-megadrive": "megadrive",
    "turbografx16--1": "tg16",
    "turbografx-16-slash-pc-engine-cd": "pcenginecd",
    "neo-geo-pocket": "ngp",
    "neo-geo-pocket-color": "ngpc",
    "neogeoaes": "neogeo",
    "neogeomvs": "neogeo",
    "neo-geo-cd": "neogeocd",
    "atari-jaguar-cd": "atarijaguarcd",
    "jaguar": "atarijaguar",
    "philips-cd-i": "cdimono1",
    "sharp-x68000": "x68000",
    "vic-20": "vic20",
    "3do": "3do",
    "sfam": "snes",
    "famicom": "nes",
}


def launchbox_platform(slug: str) -> str:
    """LaunchBox's name for a platform, or the slug when it has none."""
    return LAUNCHBOX_PLATFORMS.get(str(slug or "").lower(), str(slug or ""))


def esde_folder(slug: str) -> str:
    return ESDE_FOLDERS.get(str(slug or "").lower(), str(slug or ""))


def _pretty(root: ET.Element) -> str:
    raw = ET.tostring(root, encoding="utf-8")
    return minidom.parseString(raw).toprettyxml(indent="  ", encoding="utf-8") \
        .decode("utf-8")


def to_launchbox(rows: list[dict]) -> str:
    """A LaunchBox platform XML.

    LaunchBox stores one file per platform under `Data/Platforms/`, and its
    importer reads the same shape. `ApplicationPath` is what it launches, so a
    row with no path is skipped rather than written as an entry that cannot
    start -- a dead tile is worse than a missing one, because it looks like a
    working library until somebody clicks it.
    """
    root = ET.Element("LaunchBox")
    written = 0
    for row in rows:
        path = str(row.get("path") or "").strip()
        if not path:
            continue
        game = ET.SubElement(root, "Game")
        ET.SubElement(game, "ApplicationPath").text = path
        ET.SubElement(game, "Title").text = str(row.get("title") or "")
        ET.SubElement(game, "Platform").text = launchbox_platform(
            row.get("platform"))
        ET.SubElement(game, "ID").text = str(row.get("id") or "")
        if row.get("region"):
            ET.SubElement(game, "Region").text = str(row["region"])
        if row.get("notes"):
            ET.SubElement(game, "Notes").text = str(row["notes"])
        # ROMarr knows something LaunchBox has no field for: whether the file
        # verified against a DAT. It goes in Notes rather than being dropped,
        # because it is the most useful thing in the row.
        if row.get("verified"):
            existing = game.find("Notes")
            note = f"ROMarr: {row['verified']}"
            if existing is None:
                ET.SubElement(game, "Notes").text = note
            else:
                existing.text = f"{existing.text}\n{note}"
        written += 1
    log.info("LaunchBox export: %d of %d rows had a path", written, len(rows))
    return _pretty(root)


def to_gamelist(rows: list[dict]) -> str:
    """An ES-DE / EmulationStation `gamelist.xml` for ONE system.

    Paths are written relative with a leading `./`, which is what
    EmulationStation expects and what makes a gamelist survive the ROM
    directory being mounted somewhere else -- the normal case when the files
    live on a NAS and the frontend runs on a handheld.
    """
    root = ET.Element("gameList")
    for row in rows:
        name = str(row.get("filename") or row.get("path") or "").strip()
        if not name:
            continue
        game = ET.SubElement(root, "game")
        ET.SubElement(game, "path").text = f"./{name.lstrip('./')}"
        ET.SubElement(game, "name").text = str(row.get("title") or name)
        if row.get("desc"):
            ET.SubElement(game, "desc").text = str(row["desc"])
        if row.get("region"):
            ET.SubElement(game, "region").text = str(row["region"])
    return _pretty(root)


def to_playnite(rows: list[dict]) -> str:
    """JSON for the Playnite extension in `contrib/`.

    Deliberately flat and explicit. The consumer is a PowerShell script, and
    anything that needs a schema to interpret is a script that breaks the
    first time a field is absent.
    """
    return json.dumps({
        "source": "ROMarr",
        "version": 1,
        "games": [
            {
                "name": str(row.get("title") or ""),
                "platform": launchbox_platform(row.get("platform")),
                "romPath": str(row.get("path") or ""),
                "id": str(row.get("id") or ""),
                "verified": str(row.get("verified") or ""),
            }
            for row in rows if row.get("path")
        ],
    }, indent=2)


FORMATS = {
    "launchbox": {"label": "LaunchBox", "render": to_launchbox,
                  "content_type": "application/xml; charset=utf-8",
                  "filename": "LaunchBox.xml"},
    "gamelist": {"label": "ES-DE / EmulationStation", "render": to_gamelist,
                 "content_type": "application/xml; charset=utf-8",
                 "filename": "gamelist.xml"},
    "playnite": {"label": "Playnite", "render": to_playnite,
                 "content_type": "application/json; charset=utf-8",
                 "filename": "romarr-playnite.json"},
}
