"""Reading the launchers already installed on a gaming PC.

**This module exists because a claim in ROMarr's own README was wrong.** It
said EA, Battle.net, Epic and Nintendo "have no API", so their libraries
could not be connected. Playnite and LaunchBox have managed those libraries
for years, which was the counter-example that mattered -- and the way they
do it is not a secret API at all:

    the launcher already wrote your library to disk.

Steam keeps an `appmanifest_*.acf` per installed game. Epic writes a JSON
`.item` per game. GOG Galaxy keeps a SQLite database. Battle.net keeps a
protobuf `product.db` naming each installed product. EA writes an
`installerdata.xml` beside each game. None of that needs a credential, an
OAuth dance or a scraped cookie -- it needs read access to the machine the
person is already sitting at.

So this is the honest architecture for store libraries: the *web* APIs
(Steam public profile, GOG public profile, Xbox, PSN, itch.io) cover what
you own remotely, and this covers what is installed locally, including
every store that has no usable web API at all. Between them there is no
"cannot connect" left worth printing in a README.

Everything here is a pure function from a path to a list of titles, so all
of it is tested, and `scan_all` degrades per-launcher: a machine with no
Epic simply contributes no Epic games rather than failing the scan.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class LocalGame:
    """One game a launcher says is installed."""

    name: str
    launcher: str
    path: str = ""


# --- Steam ------------------------------------------------------------------

#: Valve's own scaffolding, which is installed as though it were a game.
_STEAM_NOISE = ("Steamworks Common Redistributables", "Steam Linux Runtime",
                "Proton", "Steamworks Shared")

_ACF_NAME = re.compile(r'^\s*"name"\s*"(.+)"\s*$', re.MULTILINE)


_VDF_PATH = re.compile(r'^\s*"path"\s*"(.+)"\s*$', re.MULTILINE)


def steam_library_paths(steamapps: Path) -> list[Path]:
    """Every steamapps directory Steam knows about, itself included.

    Steam records its libraries in `libraryfolders.vdf`, which is the only
    reliable way to find them: people put libraries on second drives with
    names nothing can guess, and a drive that is currently unplugged must
    simply contribute nothing rather than break the scan.
    """
    steamapps = Path(steamapps)
    found = [steamapps] if steamapps.is_dir() else []
    index = steamapps / "libraryfolders.vdf"
    try:
        text = index.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return found
    for raw in _VDF_PATH.findall(text):
        library = Path(raw.replace("\\\\", "\\")) / "steamapps"
        if library.is_dir() and library not in found:
            found.append(library)
    return found


def steam_games(steamapps: Path) -> list[LocalGame]:
    """Installed Steam games, from the appmanifest files Steam writes."""
    out = []
    for manifest in sorted(Path(steamapps).glob("appmanifest_*.acf")):
        try:
            text = manifest.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        match = _ACF_NAME.search(text)
        if not match:
            continue
        name = match.group(1).strip()
        if not name or any(noise in name for noise in _STEAM_NOISE):
            continue
        out.append(LocalGame(name=name, launcher="steam",
                             path=str(manifest.parent)))
    return out


# --- Epic -------------------------------------------------------------------

def epic_games(manifests: Path) -> list[LocalGame]:
    """Installed Epic games, from the .item manifests the launcher writes.

    Read with utf-8-sig: Epic writes a BOM, and a plain utf-8 read fails on
    the very first character of every file.
    """
    out = []
    for item in sorted(Path(manifests).glob("*.item")):
        try:
            data = json.loads(item.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            continue
        name = str(data.get("DisplayName") or "").strip()
        if name:
            out.append(LocalGame(name=name, launcher="epic",
                                 path=str(data.get("InstallLocation") or "")))
    return out


# --- GOG Galaxy -------------------------------------------------------------

def gog_galaxy_games(database: Path) -> list[LocalGame]:
    """Owned GOG titles from Galaxy's local SQLite database.

    Opened read-only through a file: URI so a running Galaxy is never
    disturbed, and every schema variation is tolerated: Galaxy has moved
    these tables between versions, so a missing table is "no games here",
    not an error.
    """
    path = Path(database)
    if not path.exists():
        return []
    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error:
        return []
    names: list[str] = []
    try:
        for query in (
            "SELECT DISTINCT value FROM GamePieces "
            "JOIN GamePieceTypes ON GamePieces.gamePieceTypeId = GamePieceTypes.id "
            "WHERE GamePieceTypes.type = 'originalTitle'",
            "SELECT DISTINCT title FROM LimitedDetails",
        ):
            try:
                rows = connection.execute(query).fetchall()
            except sqlite3.Error:
                continue
            for (value,) in rows:
                text = str(value or "").strip()
                # 'originalTitle' rows are JSON blobs: {"title": "..."}.
                if text.startswith("{"):
                    try:
                        text = str(json.loads(text).get("title") or "").strip()
                    except ValueError:
                        continue
                if text:
                    names.append(text)
            if names:
                break
    finally:
        connection.close()
    return [LocalGame(name=n, launcher="gog") for n in sorted(set(names))]


# --- Battle.net -------------------------------------------------------------

#: Blizzard's product codes, as they appear in Battle.net's product.db. The
#: whole catalogue is small enough to name, which is why this is a table and
#: not a parser: product.db is protobuf, and decoding it properly to recover
#: strings nobody disputes would be work spent to learn "d2r".
BATTLENET_PRODUCTS = {
    "d2r": "Diablo II: Resurrected",
    "diablo3": "Diablo III",
    "fenris": "Diablo IV",
    "osi": "Diablo Immortal",
    "wow": "World of Warcraft",
    "wow_classic": "World of Warcraft Classic",
    "wowc": "World of Warcraft Classic",
    "s1": "StarCraft: Remastered",
    "s2": "StarCraft II",
    "hero": "Heroes of the Storm",
    "hs_beta": "Hearthstone",
    "hsb": "Hearthstone",
    "pro": "Overwatch 2",
    "prometheus": "Overwatch 2",
    "w3": "Warcraft III: Reforged",
    "wlby": "Crash Bandicoot 4",
    "rtro": "Blizzard Arcade Collection",
    "zeus": "Call of Duty: Black Ops Cold War",
    "odin": "Call of Duty: Modern Warfare",
    "lazr": "Call of Duty: Modern Warfare II",
    "auks": "Call of Duty: Vanguard",
    "fore": "Call of Duty: Modern Warfare III",
    "spot": "Call of Duty: Black Ops 6",
    "anbs": "Diablo II: Resurrected (PTR)",
    "viper": "Call of Duty: Black Ops 4",
}


def battlenet_games(product_db: Path) -> list[LocalGame]:
    """Installed Blizzard games, by product code, from Battle.net's product.db.

    The file is protobuf; the product codes inside it are plain ASCII, so
    the codes are recovered by scanning rather than by decoding a schema
    Blizzard does not publish. A code that is not in the table is reported
    under its own code rather than dropped -- an unknown Blizzard game is
    still a game, and the name can be corrected later.
    """
    path = Path(product_db)
    try:
        blob = path.read_bytes()
    except OSError:
        return []
    found: set[str] = set()
    for token in re.findall(rb"[a-z0-9_]{2,20}", blob):
        code = token.decode("ascii", "ignore")
        if code in BATTLENET_PRODUCTS:
            found.add(code)
    return [LocalGame(name=BATTLENET_PRODUCTS[c], launcher="battlenet")
            for c in sorted(found)]


# --- EA ---------------------------------------------------------------------

_EA_TITLE = re.compile(r"<gameTitle[^>]*>([^<]+)</gameTitle>", re.IGNORECASE)
_EA_NAME = re.compile(r"<title[^>]*>([^<]+)</title>", re.IGNORECASE)


def ea_games(roots: list[Path]) -> list[LocalGame]:
    """Installed EA games, from the installerdata.xml EA writes per game.

    EA Desktop and the older Origin both drop `__Installer/installerdata.xml`
    into each game's directory, and it names the title. That file is why the
    "EA has no API" line was wrong: nothing needs to be asked of EA at all.
    """
    out = []
    for root in roots:
        root = Path(root)
        if not root.is_dir():
            continue
        for xml in sorted(root.glob("*/__Installer/installerdata.xml")):
            try:
                text = xml.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            match = _EA_TITLE.search(text) or _EA_NAME.search(text)
            name = (match.group(1).strip() if match
                    else xml.parent.parent.name.strip())
            if name:
                out.append(LocalGame(name=name, launcher="ea",
                                     path=str(xml.parent.parent)))
    return out


# --- everything at once -----------------------------------------------------

def default_locations() -> dict[str, list[Path]]:
    """Where each launcher keeps its library on a stock Windows install."""
    program_data = Path(os.environ.get("ProgramData", r"C:\ProgramData"))
    local = Path(os.environ.get("LOCALAPPDATA", ""))
    drives = [Path(f"{letter}:\\") for letter in "CDEFG"
              if Path(f"{letter}:\\").exists()]
    return {
        "steam": [Path(r"C:\Program Files (x86)\Steam\steamapps")]
                 + [d / "SteamLibrary" / "steamapps" for d in drives],
        "epic": [program_data / "Epic" / "EpicGamesLauncher" / "Data" / "Manifests"],
        "gog": [local / "GOG.com" / "Galaxy" / "storage" / "galaxy-2.0.db"],
        "battlenet": [program_data / "Battle.net" / "Agent" / "product.db"],
        "ea": [d / "Program Files" / "EA Games" for d in drives]
              + [d / "Program Files (x86)" / "Origin Games" for d in drives]
              + [d / "EA Games" for d in drives],
    }


def scan_all(locations: dict[str, list[Path]] | None = None) -> list[LocalGame]:
    """Every game every installed launcher knows about, best effort.

    One launcher failing never stops the others: a machine with no Epic
    contributes no Epic games rather than failing the scan.
    """
    places = locations or default_locations()
    games: list[LocalGame] = []

    for path in places.get("steam", []):
        try:
            # Follow Steam's own library index, so libraries on other drives
            # are found rather than guessed at.
            for library in steam_library_paths(path):
                games += steam_games(library)
        except Exception as exc:            # noqa: BLE001 - best effort
            log.warning("steam scan failed at %s: %s", path, exc)
    for path in places.get("epic", []):
        try:
            games += epic_games(path)
        except Exception as exc:            # noqa: BLE001
            log.warning("epic scan failed at %s: %s", path, exc)
    for path in places.get("gog", []):
        try:
            games += gog_galaxy_games(path)
        except Exception as exc:            # noqa: BLE001
            log.warning("gog scan failed at %s: %s", path, exc)
    for path in places.get("battlenet", []):
        try:
            games += battlenet_games(path)
        except Exception as exc:            # noqa: BLE001
            log.warning("battle.net scan failed at %s: %s", path, exc)
    try:
        games += ea_games(places.get("ea", []))
    except Exception as exc:                # noqa: BLE001
        log.warning("ea scan failed: %s", exc)

    # One title per (name, launcher): Steam libraries on two drives list the
    # same game twice, and so does a game reinstalled elsewhere.
    seen, unique = set(), []
    for game in games:
        key = (game.name.lower(), game.launcher)
        if key in seen:
            continue
        seen.add(key)
        unique.append(game)
    return unique
