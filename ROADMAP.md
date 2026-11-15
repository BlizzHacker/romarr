# ROMarr roadmap

This is a working list of priorities and delivery milestones, updated **2026-11-15**. Shipped features are separated from proposals so you can tell what an update will actually give you.

---

## Available in v1.10.x & Latest Releases

| Milestone | What is available | Details |
|---|---|---|
| **Android Companion & Handheld App** | `org.romarr.android` packaged with embedded core or remote connection, available via Google Play Internal Beta. | Handheld support for Odin 2, Retroid Pocket 4/5, Ayaneo, and AYN with Turnip/Adreno driver awareness. [Google Play Beta](https://play.google.com/apps/internaltest/4700888732790045566). |
| **macOS, Linux & SteamOS Standalone** | `scripts/start-desktop.sh` launcher mirrors Windows standalone experience without requiring Docker or manual Python setup. | Auto-detects SteamOS / Steam Deck, defaults to EmuDeck MicroSD storage (`/run/media/mmcblk0p1/Emulation/roms`), and connects to desktop browsers. |
| **Supportive Library Shell & Device Fleet** | Device Fleet (`/api/v1/hub/fleet`) syncs verified 1G1R sets and `gamelist.xml` across LAN/SMB/SSH. | Out-of-the-box profiles for ES-DE, RetroPie / Arcade Punks images, Batocera 40+, Argosy Launcher, Grout (muOS/NextUI), Steam Deck, and RetroArch WebDAV. |
| **Emulator Hub (1,214+ Catalog Entries)** | Searchable catalog of 1,214 emulators, forks, and tools with 350 RetroArch cores. | Includes missing firmware checks, source discovery, and safe inspection. |
| **Standalone Download Stack** | Built-in Mister Download Manager (MDM) for HTTP/BitTorrent and native NNTP/NZB engine. | Avoids requiring five external services while preserving qBittorrent, Prowlarr, and SABnzbd support. |
| **Unraid Community Applications** | Official Unraid CA template in `BlizzHacker/cartridge-unraid`. | Live in Unraid CA (`ca.unraid.net/apps/romarr-04jslct0riva81`). |
| **Windows Desktop & Store Package** | Native Windows desktop app with embedded WebView2 and MSIX packaging. | Ready for Microsoft Store distribution (`9PN1VX549NSW`). |

---

## Available in v1.9.x

- **Library Scan Progress**: Live scanning metrics showing games read and elapsed time to prevent silent pauses on large NAS mounts.
- **Usenet & Newznab Clarity**: Explicit separation of indexer search results, provider status, and download client routing.
- **qBittorrent & Deluge Polish**: Hardened selective grabbing from multi-file torrents and improved base game/DLC matching.
- **Proxmox LXC Automation**: Standalone helper in `/proxmox/ct/romarr.sh` with verified architecture templates.

---

## Available in v1.8.0

- **[Setup Guide (#77)](https://github.com/BlizzHacker/romarr/issues/77)**: A skippable first-run sequence for library, downloads, sources, DATs and plugins.
- **[Windows Portable App (#78)](https://github.com/BlizzHacker/romarr/issues/78)**: A portable Windows x64 ZIP with bundled Python, MinGit, fixed WebView2, and ROM Hub.
- **[Canonical Names on Import (#79)](https://github.com/BlizzHacker/romarr/issues/79)**: Verified single-file imports use DAT ROM filenames with source preservation.
- **[Existing-Library Rename Preview (#80)](https://github.com/BlizzHacker/romarr/issues/80)**: Stats shows DAT-verified single-file rename proposals with journaled rollback.

---

## Available in v1.7.0

- **Duplicate Space Reclamation (#73)**: Previews byte-identical duplicates with quarantine and restore.
- **Set Torrent Batch Fetching (#72)**: Staged selective downloads from large torrents within configured size limits.
- **Reliable DAT Loading (#74)**: Full Logiqx/XML detection and concurrent reload fixes.
- **Modern Console Detection**: Import recognition for PS4, PS5, and PS Vita formats.

---

## Next Priorities (Road to 2.0)

| Priority | Focus Area | Completion Criteria |
|---|---|---|
| **1** | **Ecosystem Package Distribution** | Community-submitted packages across Flathub, TrueNAS SCALE, CasaOS, Umbrel, Winget, and Homebrew. Tracked in `docs/SUBMISSIONS.md`. |
| **1** | **Google Play Production Graduation** | Complete 12-tester closed beta track for `org.romarr.android` and graduate to public Google Play listing. |
| **2** | **Full 2.0 Stability & Hardening** | 100% test coverage across all download clients, zero regressions on NAS/SMB storage, and hardened multi-platform fleet synchronization. |
| **2** | **Direct Device Transfer Protocols** | Expand direct LAN transfer workflows for handheld consoles, micro-consoles, and network-attached emulation frontends. |

---

## Where the Priorities Came From

The [r/ARR community thread](https://www.reddit.com/r/ARR/comments/1xv2ps9/) and GitHub feedback continue to drive concrete improvements:

- **Setup, platform folders, ES-DE and missing games** → Setup Guide, folder/path explanations and the missing-collection walkthrough.
- **Reorganizing and renaming existing files without Prowlarr** → Preview-first single-file rename workflow and rollback journal.
- **Non-Server & Handheld Use** → Native Windows portable ZIP, macOS/Linux/SteamOS `start-desktop.sh`, and Android Play Store preview.
- **Ecosystem Fleet Integration** → Device Fleet manager supporting Batocera, RetroPie, Arcade Punks images, muOS, and EmuDeck.
- **Deduplication & Safety** → 1G1R Igir integrations and safe quarantine workflows.

To suggest a change or report an issue, [open an issue on GitHub](https://github.com/BlizzHacker/romarr/issues/new).
