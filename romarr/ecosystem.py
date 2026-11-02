"""The projects ROMarr stands on, in one place.

ROMarr acquires ROMs and files them. It does not store a library, serve a
player, publish a DAT, index a tracker, or run a download -- every one of
those is somebody else's work, and without them there is nothing for ROMarr
to automate. This module is the credit made concrete: each entry carries the
project's own repository and site, a one-line description in its authors'
terms, and -- where the project ships one -- the command that installs it,
so the respect is also a convenience.

Categories order the way a person actually assembles the stack: the library
server first, first-party RomM apps, console URL feeds, RetroArch WebDAV sync,
community apps, developer protocols, external 1G1R tooling (Igir v5.8.0),
players, acquisition, and the databases that make verification possible.
"""

from __future__ import annotations

from dataclasses import dataclass


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
    #: Visible tab grouping for the Ecosystem & Hub UI.
    tab: str = "all"


#: category label -> the projects under it, in assembly order.
ECOSYSTEM: dict[str, list[Project]] = {
    "Library servers — where your games live": [
        Project(
            "RomM",
            "A beautiful, powerful, self-hosted ROM manager and player.",
            repo="https://github.com/rommapp/romm", site="https://romm.app",
            install="docker pull rommapp/romm:latest", tab="servers"),
        Project(
            "Gaseous",
            "A self-hosted game library manager and emulator, IGDB-backed.",
            repo="https://github.com/gaseous-project/gaseous-server",
            install="docker pull gaseousgames/gaseous-server:latest", tab="servers"),
        Project(
            "Retrom",
            "A centralized game library and launcher for all your devices.",
            repo="https://github.com/JMBeresford/retrom", tab="servers"),
        Project(
            "Gameyfin",
            "Manage your video games -- an open-source game library manager.",
            repo="https://github.com/gameyfin/gameyfin",
            install="docker pull grimsi/gameyfin:latest", tab="servers"),
    ],
    "External tooling — 1G1R sorting & DAT verification": [
        Project(
            "Igir Collection Manager",
            "Zero-setup ROM collection manager (v5.8.0) that sorts, filters (1G1R), "
            "extracts, and verifies ROMs against No-Intro and Redump DATs with "
            "native {romm} output folder tokens.",
            repo="https://github.com/emmercm/igir/releases/tag/v5.8.0",
            site="https://igir.io",
            install="npm install -g igir@5.8.0",
            tab="igir"),
    ],
    "First-party apps — maintained by the RomM team": [
        Project(
            "Argosy Launcher",
            "Android launcher that browses and launches your RomM library on mobile.",
            repo="https://github.com/rommapp/argosy-launcher",
            site="https://docs.romm.app/latest/ecosystem/",
            tab="first-party"),
        Project(
            "Grout",
            "Linux handheld companion for muOS and NextUI devices.",
            repo="https://github.com/rommapp/grout",
            site="https://docs.romm.app/latest/ecosystem/",
            tab="first-party"),
        Project(
            "RomM Playnite Plugin",
            "Windows desktop plugin that imports your RomM library directly into Playnite.",
            repo="https://github.com/rommapp/romm-playnite-plugin",
            site="https://docs.romm.app/latest/ecosystem/",
            tab="first-party"),
    ],
    "Feeds — URL endpoints for third-party console homebrew": [
        Project(
            "Tinfoil",
            "Nintendo Switch homebrew for installing .nsp and .xci directly from a RomM URL feed.",
            site="https://docs.romm.app/latest/ecosystem/",
            tab="feeds"),
        Project(
            "pkgj",
            "PS Vita and PSP homebrew installer consuming RomM URL feed endpoints.",
            repo="https://github.com/blastrock/pkgj",
            site="https://docs.romm.app/latest/ecosystem/",
            tab="feeds"),
        Project(
            "pkgi",
            "PS3, PS Vita, and PSP homebrew installer using the older CSV feed format.",
            repo="https://github.com/bucanero/pkgi-ps3",
            site="https://docs.romm.app/latest/ecosystem/",
            tab="feeds"),
        Project(
            "fpkgi",
            "PS4 and PS5 homebrew package installer consuming RomM JSON feeds.",
            repo="https://github.com/ItsJokerZz/FpKGi",
            site="https://docs.romm.app/latest/ecosystem/",
            tab="feeds"),
        Project(
            "Kekatsu",
            "Nintendo DS multiboot loader consuming RomM URL feed endpoints.",
            repo="https://github.com/Spitfire-Nek22/Kekatsu-DS",
            site="https://docs.romm.app/latest/ecosystem/",
            tab="feeds"),
    ],
    "RetroArch — built-in WebDAV Cloud Sync": [
        Project(
            "RetroArch Cloud Sync",
            "RomM acts as a direct WebDAV target for RetroArch's built-in Cloud Sync, "
            "syncing saves and states between RetroArch and your library with no companion app.",
            repo="https://github.com/libretro/RetroArch",
            site="https://docs.romm.app/latest/ecosystem/",
            tab="retroarch"),
    ],
    "Community apps — desktop, mobile, handhelds & bots": [
        Project(
            "romm-ios-app",
            "Native iOS companion app for browsing and managing your RomM library.",
            site="https://docs.romm.app/latest/ecosystem/",
            tab="community"),
        Project(
            "romm-mobile",
            "Cross-platform Android and iOS companion app for RomM.",
            site="https://docs.romm.app/latest/ecosystem/",
            tab="community"),
        Project(
            "RommBrowser",
            "Electron desktop app for browsing and launching from a RomM library.",
            site="https://docs.romm.app/latest/ecosystem/",
            tab="community"),
        Project(
            "RomMate",
            "Desktop companion client for managing and syncing RomM collections.",
            site="https://docs.romm.app/latest/ecosystem/",
            tab="community"),
        Project(
            "romm-client",
            "Desktop client for browsing, syncing, and launching games from RomM.",
            site="https://docs.romm.app/latest/ecosystem/",
            tab="community"),
        Project(
            "DeckRommSync",
            "Steam Deck synchronization companion for RomM libraries and saves.",
            site="https://docs.romm.app/latest/ecosystem/",
            tab="community"),
        Project(
            "SwitchRomM",
            "Nintendo Switch homebrew NRO companion for RomM.",
            site="https://docs.romm.app/latest/ecosystem/",
            tab="community"),
        Project(
            "RetroArch Sync",
            "Community save and state synchronization helper for RetroArch.",
            site="https://docs.romm.app/latest/ecosystem/",
            tab="community"),
        Project(
            "romm-comm",
            "Discord bot for querying and interacting with your RomM library.",
            site="https://docs.romm.app/latest/ecosystem/",
            tab="community"),
        Project(
            "GG Requestz",
            "A modern game discovery and request platform, RomM-integrated -- "
            "the Overseerr of games.",
            repo="https://github.com/XTREEMMAK/ggrequestz",
            site="https://docs.romm.app/latest/ecosystem/",
            tab="community"),
    ],
    "Build your own — developer APIs, sync protocols & OpenAPI": [
        Project(
            "Client API Tokens",
            "How to authenticate your companion app and run the device-pairing flow.",
            site="https://docs.romm.app/latest/ecosystem/",
            tab="build-your-own"),
        Project(
            "Device Sync Protocol",
            "Wire-level reference for save, state, and play-session synchronization.",
            site="https://docs.romm.app/latest/ecosystem/",
            tab="build-your-own"),
        Project(
            "SSH Sync",
            "Server-side SSH configuration for push/pull sync to Linux handhelds.",
            site="https://docs.romm.app/latest/ecosystem/",
            tab="build-your-own"),
        Project(
            "API Reference & Consuming OpenAPI",
            "Every REST endpoint, OpenAPI specification, and client codegen patterns.",
            site="https://docs.romm.app/latest/ecosystem/",
            tab="build-your-own"),
        Project(
            "WebSockets & Netplay",
            "Live-update WebSocket channels and Netplay session coordination.",
            site="https://docs.romm.app/latest/ecosystem/",
            tab="build-your-own"),
    ],
    "Players — how a library is played": [
        Project(
            "EmulatorJS",
            "Self-hosted, browser-based emulation for tons of retro systems.",
            repo="https://github.com/EmulatorJS/EmulatorJS",
            site="https://emulatorjs.org", tab="players"),
        Project(
            "Ruffle",
            "A Flash Player emulator written in Rust -- the reason a "
            "`.swf` still opens in a browser at all.",
            repo="https://github.com/ruffle-rs/ruffle",
            site="https://ruffle.rs", tab="players"),
        Project(
            "Emularity",
            "Easily embed emulators -- the loader behind Archive.org's "
            "software library, wrapping MAME, EM-DOSBOX and the Scripted "
            "Amiga Emulator.",
            repo="https://github.com/db48x/emularity",
            site="https://archive.org/details/softwarelibrary", tab="players"),
        Project(
            "js-dos",
            "The simplest API to run DOS/Win 9x programs in browser or node.",
            repo="https://github.com/caiiiycuk/js-dos",
            site="https://js-dos.com",
            install="npm install js-dos", tab="players"),
        Project(
            "v86",
            "x86 PC emulator and x86-to-wasm JIT, running in the browser.",
            repo="https://github.com/copy/v86",
            site="https://copy.sh/v86/", tab="players"),
        Project(
            "Nostalgist.js",
            "A JavaScript library used for running emulators of retro "
            "consoles inside browsers.",
            repo="https://github.com/arianrhodsandlot/nostalgist",
            site="https://nostalgist.js.org",
            install="npm install nostalgist", tab="players"),
        Project(
            "RetroArch / libretro",
            "The reference frontend and the cores nearly everything runs on.",
            repo="https://github.com/libretro/RetroArch",
            site="https://www.libretro.com", tab="players"),
        Project(
            "Moonlight",
            "An open-source implementation of NVIDIA GameStream -- the "
            "client every self-hosted game-streaming host speaks to.",
            repo="https://github.com/moonlight-stream/moonlight-qt",
            site="https://moonlight-stream.org", tab="players"),
        Project(
            "Wolf",
            "Stream virtual desktops and games running in Docker -- a "
            "multi-user, container-native Moonlight server.",
            repo="https://github.com/games-on-whales/wolf",
            site="https://games-on-whales.github.io/wolf/stable/",
            install="docker pull ghcr.io/games-on-whales/wolf:stable", tab="players"),
        Project(
            "Sunshine",
            "A self-hosted game stream host for Moonlight.",
            repo="https://github.com/LizardByte/Sunshine",
            site="https://app.lizardbyte.dev/Sunshine/",
            install="docker pull lizardbyte/sunshine:latest", tab="players"),
        Project(
            "Steam Headless",
            "A Docker image for running Steam in a container with a full "
            "desktop, streamed out over Sunshine or noVNC.",
            repo="https://github.com/Steam-Headless/docker-steam-headless",
            install="docker pull josh5/steam-headless:latest", tab="players"),
        Project(
            "ES-DE",
            "EmulationStation Desktop Edition -- a frontend for many emulators.",
            site="https://es-de.org", tab="players"),
        Project(
            "Batocera",
            "A retro-gaming distribution that turns a PC into a console.",
            site="https://batocera.org", tab="players"),
        Project(
            "Playnite",
            "A video game library manager for your PC, launchers and emulators.",
            site="https://playnite.link",
            repo="https://github.com/JosefNemec/Playnite", tab="players"),
        Project(
            "LaunchBox",
            "A game launcher and frontend for Windows, with a big-screen mode.",
            site="https://www.launchbox-app.com", tab="players"),
    ],
    "Acquisition — finding and fetching ROMs": [
        Project(
            "ROMarr",
            "The *arr for games -- request it, ROMarr finds it, verifies 1G1R, and files it.",
            repo="https://github.com/BlizzHacker/romarr",
            install="docker pull ghcr.io/blizzhacker/romarr:latest",
            is_self=True, tab="acquisition"),
        Project(
            "Prowlarr",
            "An indexer manager and proxy -- one connection to all your "
            "trackers and Usenet indexers.",
            repo="https://github.com/Prowlarr/Prowlarr",
            site="https://prowlarr.com", tab="acquisition"),
        Project(
            "qBittorrent",
            "A free and reliable BitTorrent client, the one ROMarr defaults to.",
            repo="https://github.com/qbittorrent/qBittorrent",
            site="https://www.qbittorrent.org", tab="acquisition"),
        Project(
            "ROM Hub",
            "ROMarr's plugin factory -- where a source or Igir 1G1R runner is written, run and "
            "sandboxed.",
            repo="https://github.com/BlizzHacker/rom-hub", tab="acquisition"),
    ],
    "Preservation databases — what makes verification real": [
        Project(
            "No-Intro",
            "The reference database of cartridge dumps -- ROMarr and Igir verify "
            "against its DATs.",
            site="https://no-intro.org", tab="dats"),
        Project(
            "Redump",
            "The reference database of disc dumps, per-track -- the other "
            "half of verification.",
            site="http://redump.org", tab="dats"),
    ],
}


def as_dict() -> dict:
    """The ecosystem shaped for the API and the page."""
    return {
        category: [
            {"name": p.name, "blurb": p.blurb, "repo": p.repo,
             "site": p.site, "install": p.install, "is_self": p.is_self,
             "tab": p.tab}
            for p in projects
        ]
        for category, projects in ECOSYSTEM.items()
    }


def all_projects() -> list[Project]:
    return [p for projects in ECOSYSTEM.values() for p in projects]
