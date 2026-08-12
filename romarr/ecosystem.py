"""The projects ROMarr stands on, in one place.

ROMarr acquires ROMs and files them. It does not store a library, serve a
player, publish a DAT, index a tracker, or run a download -- every one of
those is somebody else's work, and without them there is nothing for ROMarr
to automate. This module is the credit made concrete: each entry carries the
project's own repository and site, a one-line description in its authors'
terms, and -- where the project ships one -- the command that installs it,
so the respect is also a convenience.

Categories order the way a person actually assembles the stack: the library
server first, then what plays from it, what finds and fetches ROMs, the
databases that make verification possible, and the launchers a folder
library feeds. `ROLE_ROMARR` marks where ROMarr itself sits, so the app can
render "you are here" honestly rather than implying it is the center.

Nothing here is a hard dependency the code imports; it is documentation with
structure, kept in code so one list feeds both the README and the app's
Ecosystem page and the two cannot drift.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Project:
    name: str
    blurb: str            # in the project's own framing, not ours
    repo: str = ""
    site: str = ""
    #: A copy-pasteable install line, when the project ships one. Empty when
    #: install is genuinely project-specific -- better silent than wrong.
    install: str = ""
    #: True for ROMarr's own row, so the UI can mark it without string-matching.
    is_self: bool = False


#: category label -> the projects under it, in assembly order.
ECOSYSTEM: dict[str, list[Project]] = {
    "Library servers — where your games live": [
        Project(
            "RomM",
            "A beautiful, powerful, self-hosted ROM manager and player.",
            repo="https://github.com/rommapp/romm", site="https://romm.app",
            install="docker pull rommapp/romm:latest"),
        Project(
            "Gaseous",
            "A self-hosted game library manager and emulator, IGDB-backed.",
            repo="https://github.com/gaseous-project/gaseous-server",
            install="docker pull gaseousgames/gaseous-server:latest"),
        Project(
            "Retrom",
            "A centralized game library and launcher for all your devices.",
            repo="https://github.com/JMBeresford/retrom"),
        Project(
            "Gameyfin",
            "Manage your video games -- an open-source game library manager.",
            repo="https://github.com/gameyfin/gameyfin",
            install="docker pull grimsi/gameyfin:latest"),
    ],
    "Players — how a library is played": [
        Project(
            "EmulatorJS",
            "Self-hosted, browser-based emulation for tons of retro systems.",
            repo="https://github.com/EmulatorJS/EmulatorJS",
            site="https://emulatorjs.org"),
        Project(
            "Ruffle",
            "A Flash Player emulator written in Rust -- the reason a "
            "`.swf` still opens in a browser at all.",
            repo="https://github.com/ruffle-rs/ruffle",
            site="https://ruffle.rs"),
        Project(
            "Emularity",
            "Easily embed emulators -- the loader behind Archive.org's "
            "software library, wrapping MAME, EM-DOSBOX and the Scripted "
            "Amiga Emulator.",
            repo="https://github.com/db48x/emularity",
            site="https://archive.org/details/softwarelibrary"),
        Project(
            "js-dos",
            "The simplest API to run DOS/Win 9x programs in browser or node.",
            repo="https://github.com/caiiiycuk/js-dos",
            site="https://js-dos.com",
            install="npm install js-dos"),
        Project(
            "v86",
            "x86 PC emulator and x86-to-wasm JIT, running in the browser.",
            repo="https://github.com/copy/v86",
            site="https://copy.sh/v86/"),
        Project(
            "Nostalgist.js",
            "A JavaScript library used for running emulators of retro "
            "consoles inside browsers.",
            repo="https://github.com/arianrhodsandlot/nostalgist",
            site="https://nostalgist.js.org",
            install="npm install nostalgist"),
        Project(
            "RetroArch / libretro",
            "The reference frontend and the cores nearly everything runs on.",
            repo="https://github.com/libretro/RetroArch",
            site="https://www.libretro.com"),
        Project(
            "Moonlight",
            "An open-source implementation of NVIDIA GameStream -- the "
            "client every self-hosted game-streaming host speaks to.",
            repo="https://github.com/moonlight-stream/moonlight-qt",
            site="https://moonlight-stream.org"),
        Project(
            "Wolf",
            "Stream virtual desktops and games running in Docker -- a "
            "multi-user, container-native Moonlight server.",
            repo="https://github.com/games-on-whales/wolf",
            site="https://games-on-whales.github.io/wolf/stable/",
            install="docker pull ghcr.io/games-on-whales/wolf:stable"),
        Project(
            "Sunshine",
            "A self-hosted game stream host for Moonlight.",
            repo="https://github.com/LizardByte/Sunshine",
            site="https://app.lizardbyte.dev/Sunshine/",
            install="docker pull lizardbyte/sunshine:latest"),
        Project(
            "Steam Headless",
            "A Docker image for running Steam in a container with a full "
            "desktop, streamed out over Sunshine or noVNC.",
            repo="https://github.com/Steam-Headless/docker-steam-headless",
            install="docker pull josh5/steam-headless:latest"),
        Project(
            "ES-DE",
            "EmulationStation Desktop Edition -- a frontend for many emulators.",
            site="https://es-de.org"),
        Project(
            "Batocera",
            "A retro-gaming distribution that turns a PC into a console.",
            site="https://batocera.org"),
        Project(
            "Playnite",
            "A video game library manager for your PC, launchers and emulators.",
            site="https://playnite.link",
            repo="https://github.com/JosefNemec/Playnite"),
        Project(
            "LaunchBox",
            "A game launcher and frontend for Windows, with a big-screen mode.",
            site="https://www.launchbox-app.com"),
    ],
    "Acquisition — finding and fetching ROMs": [
        Project(
            "ROMarr",
            "The *arr for games -- request it, ROMarr finds it and files it.",
            repo="https://github.com/BlizzHacker/romarr",
            install="docker pull ghcr.io/blizzhacker/romarr:latest",
            is_self=True),
        Project(
            "GG Requestz",
            "A modern game discovery and request platform, RomM-integrated -- "
            "the Overseerr of games.",
            repo="https://github.com/XTREEMMAK/ggrequestz"),
        Project(
            "Prowlarr",
            "An indexer manager and proxy -- one connection to all your "
            "trackers and Usenet indexers.",
            repo="https://github.com/Prowlarr/Prowlarr",
            site="https://prowlarr.com"),
        Project(
            "qBittorrent",
            "A free and reliable BitTorrent client, the one ROMarr defaults to.",
            repo="https://github.com/qbittorrent/qBittorrent",
            site="https://www.qbittorrent.org"),
        Project(
            "ROM Hub",
            "ROMarr's plugin factory -- where a source is written, run and "
            "sandboxed.",
            repo="https://github.com/BlizzHacker/rom-hub"),
    ],
    "Preservation databases — what makes verification real": [
        Project(
            "No-Intro",
            "The reference database of cartridge dumps -- ROMarr verifies "
            "against its DATs.",
            site="https://no-intro.org"),
        Project(
            "Redump",
            "The reference database of disc dumps, per-track -- the other "
            "half of verification.",
            site="http://redump.org"),
    ],
}


def as_dict() -> dict:
    """The ecosystem shaped for the API and the page."""
    return {
        category: [
            {"name": p.name, "blurb": p.blurb, "repo": p.repo,
             "site": p.site, "install": p.install, "is_self": p.is_self}
            for p in projects
        ]
        for category, projects in ECOSYSTEM.items()
    }


def all_projects() -> list[Project]:
    return [p for projects in ECOSYSTEM.values() for p in projects]
