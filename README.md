# <img src="docs/img/brand/romarr-icon.svg" alt="" width="44" height="40" align="absmiddle"> ROMarr

![ROMarr — Find, verify and organize your game library](docs/img/brand/romarr-hero-banner.jpg)

The *arr for games.

Find games, verify ROMs, and import them into your library.

**New here?** Start with [one system and one game](docs/FIRST-GAME.md). ROMarr finds and organizes; an emulator plays. RomM is an optional library frontend. You can bring files you already own or connect search sources when you are ready.

[![Tests and images](https://github.com/BlizzHacker/romarr/actions/workflows/docker.yml/badge.svg)](https://github.com/BlizzHacker/romarr/actions/workflows/docker.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue)](LICENSE)
[![Discord](https://img.shields.io/badge/Discord-Join_ROMarr-5865F2?logo=discord&logoColor=white)](https://discord.gg/ywNFceuAd)
[![Open Collective](https://opencollective.com/romarr/tiers/badge.svg)](https://opencollective.com/romarr)
[![GG Requestz](https://img.shields.io/badge/Companion-GG_Requestz-8A2BE2?logo=gamepad&logoColor=white)](https://github.com/XTREEMMAK/ggrequestz)
[![RomM Ecosystem](https://img.shields.io/badge/Ecosystem-RomM-blue?logo=gamepad&logoColor=white)](https://github.com/rommapp/romm)
[![r/romm](https://img.shields.io/badge/Reddit-r%2Fromm-FF4500?logo=reddit&logoColor=white)](https://www.reddit.com/r/romm)
[![r/arr](https://img.shields.io/badge/Reddit-r%2Farr-FF4500?logo=reddit&logoColor=white)](https://www.reddit.com/r/arr)
[![Android Beta](https://img.shields.io/badge/Google_Play-Android_Beta-brightgreen?logo=googleplay&logoColor=white)](https://play.google.com/apps/internaltest/4700888732790045566)
[![Android APK](https://img.shields.io/badge/Android-Direct_APK-3DDC84?logo=android&logoColor=white)](https://github.com/BlizzHacker/romarr/releases)
[![Argosy Compatible](https://img.shields.io/badge/Frontend-Argosy_Launcher-purple?logo=android&logoColor=white)](https://github.com/rommapp/argosy-launcher)
[![Unraid Community Apps](https://img.shields.io/badge/Unraid-Community_Apps-orange?logo=unraid&logoColor=white)](https://ca.unraid.net/apps/romarr-04jslct0riva81?q=romarr)
[![Microsoft Store](https://img.shields.io/badge/Microsoft_Store-Windows_App-0078D4?logo=windows&logoColor=white)](https://github.com/BlizzHacker/romarr/releases)
[![Submit Package](https://img.shields.io/badge/Package_Submissions-Submit_ROMarr-purple?logo=github&logoColor=white)](docs/SUBMISSIONS.md)

**[Install](#install)** · **[First setup](docs/GETTING-STARTED.md)** · **[Discord](https://discord.gg/ywNFceuAd)** · **[Open Collective](https://opencollective.com/romarr)** · **[GG Requestz](https://github.com/XTREEMMAK/ggrequestz)** · **[RomM](https://github.com/rommapp/romm)** · **[r/romm](https://www.reddit.com/r/romm)** · **[r/arr](https://www.reddit.com/r/arr)** · **[Android App & APK](docs/ANDROID.md)** · **[Unraid App](https://ca.unraid.net/apps/romarr-04jslct0riva81?q=romarr)** · **[Plugins](docs/PLUGINS.md)** · **[Roadmap](ROADMAP.md)** · **[Releases](https://github.com/BlizzHacker/romarr/releases)** · **[Submit Package](docs/SUBMISSIONS.md)** · **[Privacy](docs/PRIVACY.md)**

> [!NOTE]
> **Community & Support:**  
> Join the official **[ROMarr Discord](https://discord.gg/ywNFceuAd)**, follow or sponsor the project on **[Open Collective](https://opencollective.com/romarr)**, or hang out on Reddit in **[r/romm](https://www.reddit.com/r/romm)** and **[r/arr](https://www.reddit.com/r/arr)**. Connect companion game request workflows via **[GG Requestz](https://github.com/XTREEMMAK/ggrequestz)** and frontend setups via **[RomM](https://github.com/rommapp/romm)**.

> [!TIP]
> **Android Beta, Handhelds & Argosy Launcher Integration!**  
> Run ROMarr natively on your phone, tablet, or gaming handheld (Ayn Odin, Retroid Pocket, Anbernic, Logitech G Cloud):  
> - **Google Play Test Track:** [Join the Android Beta on Google Play](https://play.google.com/apps/internaltest/4700888732790045566) (12 testers needed)  
> - **Direct APK Sideload:** [Download the Standalone APK](https://github.com/BlizzHacker/romarr/releases) with bundled Chaquopy Python runtime — no Termux or Docker required.  
> - **Argosy Launcher & Emulator Pairing:** Connect [Argosy Launcher](https://github.com/rommapp/argosy-launcher) directly to ROMarr via native RomM API support, and launch games into RetroArch, Dolphin, PPSSPP, NetherSX2, DuckStation, and Citra with one tap!

![ROMarr interactive search, with scores and reasons beside each result](docs/img/interactive-search-live.png)

Forks and code reuse are welcome; independent projects should use their own branding. [Attribution and branding policy](docs/OFFICIAL-DISTRIBUTION.md#attribution-and-branding).

**Unraid:** install the [official ROMarr listing](https://ca.unraid.net/apps/romarr-04jslct0riva81?q=romarr), using `ghcr.io/blizzhacker/romarr`. **ROMarrNG is a separate fork and is not supported by this project.** [Official builds and support policy](docs/OFFICIAL-DISTRIBUTION.md).

## What can I use it for?

| I want to… | Start here | What you need |
|---|---|---|
| Import ROMs I already have | **Manual Import** | A destination folder and access to the source files. No indexer or download client needed. |
| Find and download a particular game | **Add New** or **Interactive Search** | A search source, a matching download client, and a destination folder. |
| Check what a collection is missing | **Collections** | A No-Intro or Redump DAT and your local library. |
| Report a suspicious or unwanted file | **Activity → Reports & Help** | A relative library path; available scanners record evidence, quarantine is your choice. [How reporting works](docs/REPORTING.md). |
| Connect an AI assistant | **Hub → Library Shell → AI agents (MCP)** | Your server URL and API key. [Claude, Codex, Hermes and Antigravity setup](docs/MCP.md). |
| Chat with your own model about your library | **Hub → Chat Agent** | Ollama, an OpenAI-compatible endpoint or an Anthropic key. Searches, explains refusals and finds set gaps; asks before grabbing or changing Wanted. [How it works](docs/CHAT-AGENT.md). |
| Reclaim space taken by exact duplicates | **Stats → Audit** | A local library; review the preview before moving duplicates to quarantine. Restore is available. |
| Add another game source | **Hub → Plugins** | ROM Hub, included in the Windows app and amd64/arm64 Docker images. [Details](docs/PLUGINS.md). |
| Adopt an existing RomM collection | **Settings → Library & Hub** | Point ROMarr at RomM's roms directory, keep DAT verification automatic, and pair with [Argosy Launcher](docs/ANDROID.md#argosy-launcher-and-frontend-integration). |
| Sync with Handhelds & Emulators | **Hub → Handheld Fleet** | Sync verified libraries to Android handhelds, Steam Deck, Batocera, RetroPie, and muOS with 1,200+ emulator presets. |

You can use a plain **folder** as your library. RomM, Gaseous, Retrom and other library integrations are optional. Your emulator or frontend plays the games; ROMarr handles finding, checking and importing files.

Imports can be checked against DAT checksums. The result is **verified**, **bad dump**, or **unknown**. Without a matching DAT, a file is unknown; that does not mean it is damaged. **Rename on import** can use a verified single-file ROM's DAT name while keeping its source extension; unknown filenames are retained and existing files are not overwritten. For files already in your library, use the [rename preview and rollback guide](docs/EXISTING-LIBRARY-RENAME.md). Review each proposed change; multi-file disc sets and ambiguous matches are skipped.

## Editions & Screenshots

ROMarr shares one unified interface across browser deployments, Windows standalone, Android phones/handhelds, and [`ROMarr.app`](https://romarr.app). All captures below are taken from a live 76,064-game library running ROMarr v1.10.0.

### Platform Editions & Authentication

| Edition | Highlights | Preview |
|---|---|---|
| **Browser Web UI** | 76,064 games cached across 73 platforms with cover art, fast filters, and status badges | ![ROMarr Browser Edition — 76,064 games across 73 platforms](docs/img/editions/romarr-browser-edition.png) |
| **Windows Desktop App** | Portable x64 WebView2 window, tray host, Cartridge player menu, and 1,220-entry Emulator Hub | ![ROMarr Windows Desktop Edition with Cartridge menu and Emulator Hub](docs/img/editions/romarr-windows-edition.png) |
| **Android App (Mobile & Handhelds)** | Bundled Chaquopy server plus HTTPS remote profile switching over mobile touch UI | ![ROMarr Android App connected to a remote server](docs/img/editions/romarr-android-edition.png) |
| **Android Slide-Out Drawer** | Touch navigation drawer with expandable hub sections and integrated Color Changer | ![ROMarr Android navigation drawer and theme controls](docs/img/editions/romarr-android-drawer.png) |
| **ROMarr.app Website** | Project landing page synced with real scored search results and live accent swatches | ![ROMarr.app landing page showcase](docs/img/editions/romarr-website-edition.png) |
| **Sign-In & OIDC SSO** | Built-in password/API-key authentication alongside external SSO providers such as Authentik | ![ROMarr sign-in and OIDC SSO screen](docs/img/editions/romarr-signin-sso.png) |

### Vector SVG Branding & Live Color Changer

The separated vector icon ([`romarr-icon.svg`](docs/img/brand/romarr-icon.svg)) and stacked lockup dynamically bind to `var(--accent)` so the logo, wordmark, active navigation, and charts shift cleanly together in real time:

| Steel Blue (`#75adff`) | Warm Amber (`#f2a33c`) | Emerald Green (`#3ecf6d`) | Royal Violet (`#c084fc`) |
|---|---|---|---|
| ![Steel Blue accent theme](docs/img/editions/theme-blue.png) | ![Warm Amber accent theme](docs/img/editions/theme-amber.png) | ![Emerald Green accent theme](docs/img/editions/theme-emerald.png) | ![Royal Violet accent theme](docs/img/editions/theme-violet.png) |

### Library Discovery, Confined Plugins & Device Sync

| Feature | Preview |
|---|---|
| **Library Overview** — 76,056 files on disk, platform pills, genre/region/decade filters, and cover grid | ![ROMarr library view across 73 platforms](docs/img/library.png) |
| **Discover Shelf** — 42,210 metadata-matched games from your library with top-rated, hidden-gem, and franchise filters | ![ROMarr Discover shelf with 42,210 games](docs/img/discover.png) |
| **ROM Hub Plugins** — 40+ confined subprocess sources across ROMs (`archive-org`, `vimm-net`, `minerva-archive`, `theromdepot`), mods (`curseforge`, `moddb`, `nexusmods`, `modrinth`, `thunderstore`, `gamebanana`), guides/tutorials (`gamefaqs`, `strategywiki`, `game-manuals`, `youtube-tutorials`), and open-source games & bots (`github-games`) | ![ROM Hub showing enabled plugins](docs/img/hub-plugins.png) |
| **Device Fleet & 1G1R Planner** — Non-destructive Igir sync plans and frontend exports for ES-DE, RetroPie, Batocera, Steam Deck, and Argosy | ![Device Fleet non-destructive 1G1R sync planner](docs/img/device-fleet.png) |

<details>
<summary>Tour more views: MCP AI Connector, Device Cards, Emulator Hub, Collections, Friends & System Telemetry</summary>

| View | Preview |
|---|---|
| **Device Profiles & Handheld Cards** | ![Device Fleet cards for ES-DE, RetroPie, Batocera, Steam Deck, and Android handhelds](docs/img/portal-fleet.png) |
| **AI Agents (MCP) Connector** | ![Authenticated MCP endpoint config for Claude Code, Codex, Hermes, and Antigravity](docs/img/ai-agents-mcp.png) |
| **Emulator Hub & Cartridge Setup** | ![Cartridge plug-and-play toolbar and 1,220-entry Emulator Hub](docs/img/emulators-cartridge.png) |
| **Ecosystem (Igir v5.8.0 & Backends)** | ![Igir v5.8.0 runner and RomM/Gaseous/Retrom/Gameyfin integrations](docs/img/ecosystem.png) |
| **Collections & Set Torrents** | ![1G1R DAT comparison and budgeted qBittorrent set acquisition](docs/img/collections.png) |
| **Statistics & Indexer Telemetry** | ![Library totals, platform imports, and grabs by indexer](docs/img/stats.png) |
| **Friends & Federation** | ![Confirmed peer invitations and 198,106-hash netplay verification](docs/img/friends.png) |
| **Browse Shared Friend Shelf** | ![Browsing a peer server library and adding titles to Wanted](docs/img/friend-library.png) |
| **Import Lists & Storefront Connectors** | ![One-click Steam, GOG, Epic, Battle.net, PlayStation, Xbox, and itch.io lists](docs/img/lists.png) |
| **Storefront Session Token Modal** | ![PlayStation session paste modal in dark Steel Blue UI](docs/img/connect-modal.png) |
| **Activity History** | ![Grabbed, ignored, and failed event trail with indexer sources](docs/img/activity-history.png) |
| **Notification Providers** | ![Discord, Slack, Webhook, ntfy.sh, Gotify, Telegram, Pushover, and Apprise](docs/img/connect.png) |
| **Library Backends** | ![Configured library destinations and status checks](docs/img/libraries.png) |
| **System Health & GG Requestz** | ![Service health checks and GG Requestz webhook configuration](docs/img/status.png) |
| **Scheduled Tasks** | ![Automated import, missing search, RSS, list sync, and hash indexing tasks](docs/img/tasks.png) |
| **Live Process Logs** | ![Streaming service logs inside the web interface](docs/img/logs.png) |

</details>

## Install

### Choose a method

| Method | Best fit | Guide |
|---|---|---|
| **Unraid** | Unraid NAS & server arrays | [Install from Unraid Community Applications](https://ca.unraid.net/apps/romarr-04jslct0riva81?q=romarr) |
| **Docker Compose** | Most servers, NAS hosts, or Docker Desktop | [Quick start below](#docker-compose-quick-start) |
| **Windows app** | Windows 10/11 on x64 | [Download the Windows ZIP](docs/INSTALL.md#windows-app), extract it and double-click **ROMarr.exe**. No Python, PowerShell or .NET installation required. |
| **macOS, Linux & SteamOS** | Desktop PCs, MacBooks, Steam Deck & Handhelds (Non-Server) | [Standalone Desktop Launcher](docs/INSTALL.md#macos-linux--steamos-standalone-launcher-non-server-users): run `./scripts/start-desktop.sh` |
| **Android & Handhelds** | Android 7+ on ARM64/x86_64 (Odin, Retroid, phones, tablets) | [Join Google Play Beta](https://play.google.com/apps/internaltest/4700888732790045566) or download APK from [Releases](https://github.com/BlizzHacker/romarr/releases). Embedded Chaquopy core, emulator handoff, and [Argosy Launcher](docs/ANDROID.md#argosy-launcher-and-frontend-integration) compatibility. Read [Android setup](docs/ANDROID.md). |
| **Proxmox LXC** | A Proxmox host | [LXC installer](docs/INSTALL.md#proxmox-lxc) |
| **Chrome / ChromeOS** | A running ROMarr server | [Install the web app or server-launcher extension](docs/CHROME.md). Online connection required. |
| **Linux/macOS source checkout** | Development or an existing Python host | [Source instructions](docs/INSTALL.md#from-source) |
| **Home Assistant add-on** | A Home Assistant installation | [Add-on instructions](docs/INSTALL.md#home-assistant); this packaging needs more field testing. |
| **Package Submissions** | Community Catalogs (TrueNAS, CasaOS, Flathub) | [Submission Directory & Registry Guides](docs/SUBMISSIONS.md) |

For Docker, have Docker Engine or Docker Desktop with Compose installed. Reserve port **6868** and choose where settings, downloads, and ROMs will live. ROMs and download space are usually much larger than the app itself.

### Docker Compose quick start

These commands are for a Linux or macOS shell, or an SSH terminal on your Docker host. They create a new folder library. If you already use RomM or another library, mount its real ROM directory instead of `./roms`.

```bash
mkdir -p romarr/config romarr/roms romarr/downloads
cd romarr
```

Save this as `compose.yaml` in that directory:

```yaml
services:
  romarr:
    image: ghcr.io/blizzhacker/romarr:latest
    container_name: romarr
    restart: unless-stopped
    ports:
      - "6868:6868"
    environment:
      PUID: 1000
      PGID: 1000
      UMASK: "002"
      TZ: Etc/UTC
      LIBRARY_KIND: folder
      LIBRARY_PATH: /roms
    volumes:
      - ./config:/config
      - ./roms:/roms
      - ./downloads:/downloads
```

Set `PUID` and `PGID` to the numeric owner/group of your ROM and download folders (`id -u` and `id -g` on Linux), then start:

```bash
docker compose up -d
```

Open **http://localhost:6868** on that computer, or **http://YOUR-SERVER-IP:6868** from another device. Set a password, then follow **Settings → Setup Guide** (`/#setup`). You can skip steps you do not need and reopen the guide later.

Already have a download client? Its completed files must be visible to ROMarr. If the client reports `/downloads`, both containers should use that path for the same files. A client in a different container is reached by its Docker service name on a shared network, or by its host's address; `localhost` inside ROMarr means ROMarr itself. [Path examples and troubleshooting](docs/GETTING-STARTED.md#paths-that-need-to-agree).

The image includes archive tools. **ROM Hub is included on amd64/arm64; it is unavailable in the armv7 image.** [Image tags, permissions, backup and updates](docs/INSTALL.md).

## Get your first result

**Already have ROMs?** Put one file in the mounted downloads folder, open **Manual Import**, and scan `/downloads`. Check the platform and verification result before pressing **Import**. [Walkthrough](docs/GETTING-STARTED.md#import-a-rom-you-already-have).

**Want automatic downloads?** Add a library, a download client and a search source in **Setup Guide**. Use **Interactive Search** for your first game so you can inspect the results before grabbing one. Follow it through **Queue** and **History**. [Walkthrough](docs/GETTING-STARTED.md#find-and-download-a-game).

**Want plugins?** Open **Hub → Plugins**, choose a plugin, click **Install**, and add a **ROM Hub** download client for searchable plugins. ROM Hub is the plugin host; each plugin adds a particular source or capability. [Available examples and exact steps](docs/PLUGINS.md).

## What else is included?

- Wanted lists and scheduled searches, with the reason shown when a release is rejected.
- Prowlarr, Torznab/Newznab and torrent RSS sources; external or standalone download clients (including built-in NNTP Usenet and companion [Mister Download Manager](https://github.com/anefzaoui/misterdownloadmanager) CLI/Remote BitTorrent—with exact single-file selection on release grabs—& multi-connection HTTP).
- DAT collection comparison, region/1G1R selection, and staged acquisition from an existing qBittorrent set torrent.
- Exact duplicate audit, quarantine and restore; a preview comes before any move.
- Preview-first renaming for DAT-verified single files already in a library, with a saved restore journal.
- Multiple library destinations, notifications, import lists and frontend exports.
- Game pages, a wishlist that saves games without searching, and monitoring you can switch on or off later. [Monitoring guide](docs/MONITORING.md).
- Game soundtracks found by title or picked from your music folder, uploaded into RomM's Jukebox with exact undo. [Soundtracks guide](docs/SOUNDTRACKS.md).
- A Switch Updates & DLC report; new updates and DLC are filed inside their game's folder where RomM finds them; plus NTSC/PAL and preferred-format release preferences. [Switch guide](docs/SWITCH-INVENTORY.md).
- Base game, update and DLC handling for PlayStation 3/4/5, PSP, Vita, 3DS, Wii U, Switch, Xbox 360 and PC: search tells packs, updates and DLC apart, requests can ask for the update and DLC too, and imports file `<Title>/`, `update/` and `dlc/` the way RomM reads them. [Updates and DLC guide](docs/UPDATES-AND-DLC.md).
- ZIP, 7z, RAR and compressed TAR downloads, read without extracting unsafe paths.
- PS4, PS5 and PS Vita search/import formats, with [explicit playback and installation limits](docs/MODERN-CONSOLES.md).

Source access, available files, credentials and platform support vary. Check the result and the task history; a configured connection does not guarantee a particular game is available.

## Play with Cartridge

**Cartridge** is the dedicated player ecosystem for self-hosted game libraries, connecting seamlessly to [RomM](https://romm.app) or directly to **ROMarr's embedded RomM-API backend**. While ROMarr finds, verifies, and organizes your files, Cartridge provides controller-first frontends to browse, sync saves, and launch games across your screens:

| Platform | Client | Best Fit | Features & Downloads |
|---|---|---|---|
| **Windows** | [Cartridge for Windows](https://github.com/BlizzHacker/Cartridge) | Desktop PCs, laptops, and Windows handhelds (ROG Ally, Legion Go) | Embedded RomM console player, standalone window, no external browser required. [Download Portable EXE](https://github.com/BlizzHacker/romarr/releases/download/v1.8.0/Cartridge-Windows-Portable-0.2.0-x64.exe) or visit [BlizzHacker/Cartridge](https://github.com/BlizzHacker/Cartridge). |
| **Xbox** | [Cartridge for Xbox](https://github.com/BlizzHacker/Cartridge) | Xbox Series X\|S and Xbox One consoles | Native UWP/WebView2 gamepad experience, big-screen TV navigation, and EmulatorJS cores. Available on [Microsoft Store (App ID: 9MXC51W17LH4)](https://apps.microsoft.com/detail/9MXC51W17LH4) and [BlizzHacker/Cartridge](https://github.com/BlizzHacker/Cartridge). |
| **Android** | [Cartridge for Android](https://github.com/BlizzHacker/Cartridge) | Retro handhelds (Odin 2, Retroid Pocket, Anbernic), Android TV, tablets, phones | Gamepad-native launcher with bidirectional save-sync, rich metadata, on-demand downloads, and automated emulator handoff to RetroArch, Dolphin, PPSSPP, and NetherSX2. [Download APK from Releases](https://github.com/BlizzHacker/Cartridge/releases) or visit [BlizzHacker/Cartridge](https://github.com/BlizzHacker/Cartridge). |

You can use Cartridge alongside your existing setup or keep using your favorite frontend (Playnite, LaunchBox, ES-DE, Pegasus). The Windows desktop app's **Cartridge** menu and the setup guides link to all three clients.

## Community & Support

- **Discord:** Join the [ROMarr Discord server](https://discord.gg/ywNFceuAd) for release help, plugin tips, and troubleshooting.
- **Open Collective:** Back ROMarr on [opencollective.com/romarr](https://opencollective.com/romarr) ([contributors badge](https://opencollective.com/romarr/tiers/backer/badge.svg?label=Backer&color=brightgreen)).

## Help and documentation

| Need | Read |
|---|---|
| First setup and a successful import | [Getting started](docs/GETTING-STARTED.md) |
| Usenet providers, indexers and failed searches | [Usenet setup and troubleshooting](docs/USENET.md) |
| Docker Desktop, NAS mounts and slow library scans | [NAS library guide](docs/NAS-LIBRARIES.md) |
| Safely rename existing library files | [Preview, selection and rollback guide](docs/EXISTING-LIBRARY-RENAME.md) |
| Installation, Windows, backups, updates and environment variables | [Install guide](docs/INSTALL.md) |
| Plugin availability and installation | [ROM Hub guide](docs/PLUGINS.md) |
| API, library routing, players and browser downloads | [Feature reference](docs/FEATURES.md) |
| Current work and requested features | [Roadmap](ROADMAP.md) |
| Test evidence and known validation gaps | [Proof notes](docs/PROOF.md) — dated records, not a guarantee for every integration |

If something fails, [open an issue](https://github.com/BlizzHacker/romarr/issues/new) with your ROMarr version, install method, the action you tried, and the relevant error or log. Remove passwords, API keys and webhook URLs. Existing-install settings are changed in the UI; most connection environment variables only seed the first run.

## Contribute

Bug reports, documentation fixes and integration field reports are welcome. For code changes, install the [source environment](docs/INSTALL.md#from-source), install `pytest`, and run `python -m pytest tests/ -q`. The [CI workflow](.github/workflows/docker.yml) runs the suite and image smoke checks.

Source plugins live in [ROM Hub](https://github.com/BlizzHacker/rom-hub); see its [contribution guide](https://github.com/BlizzHacker/rom-hub/blob/master/CONTRIBUTING.md). ROMarr's API schema is available at `/api/v1/openapi.json` on your own install.

### Acknowledgements

Thanks to the library, emulator and preservation projects ROMarr works with, and to [Radarr](https://github.com/Radarr/Radarr), [Sonarr](https://github.com/Sonarr/Sonarr), [Questarr](https://github.com/Doezer/Questarr) and [gamarr](https://github.com/JeremiahM37/gamarr) for ideas and familiar workflows.

Batch decompression hands disc images to [Compressatorium](https://github.com/pacnpal/compressatorium) by pacnpal over its HTTP API, through the preinstalled [Compressatorium plugin](https://github.com/BlizzHacker/rom-hub-compressatorium); no Compressatorium code is included.

ScreenScraper and SteamGridDB metadata are ported from MIT-licensed [RetroArr](https://github.com/RiDDiX/RetroArr), Copyright (c) 2026 RetroArr.

Demo and executable refusals, Usenet minimum age and retention, and complete-edition detection follow the behaviour of [Gamarr](https://github.com/gamarr-app/Gamarr); no Gamarr code is included.

Signed webhooks, notification links and NZB detection in Torznab feeds follow the behaviour of [Questarr](https://github.com/Doezer/Questarr); no Questarr code is included.

Standalone direct HTTP and BitTorrent downloads use [Mister Download Manager](https://github.com/anefzaoui/misterdownloadmanager) by anefzaoui (GPL-3.0-or-later), run as a separate program or over its Remote HTTPS API, with its settings in the preinstalled [Mister Download Manager plugin](https://github.com/BlizzHacker/rom-hub-mdm); no Mister Download Manager code is included.

Soundtracks, the Switch report, wishlist monitoring and plugin start-up hardening build on MIT-licensed work from [romarrng](https://github.com/Squeaks72/romarrng), Copyright (c) 2026 MOVE WEIGHT.

MIT — [LICENSE](LICENSE). ROMarr is an independent project and is not endorsed by its integrations.
