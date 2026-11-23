# App Store & Package Directory Submissions

This document tracks all official store listings, community catalogs, and package registry submissions for ROMarr.

If you maintain a package registry or community catalog (such as TrueNAS Scale, Umbrel, CasaOS, Flathub, Winget, Homebrew, or Proxmox Community Scripts) and want to package or index ROMarr, please consult [STORES.md](STORES.md) and [OFFICIAL-DISTRIBUTION.md](OFFICIAL-DISTRIBUTION.md), or open an issue.

---

## Active & Verified Directory Listings

| Store / Platform | Listing & Package Info | Status | Link |
|---|---|---|---|
| **Google Play (Android)** | App ID: `org.romarr.android` (Internal Beta Track) | Active Beta (12 Testers Needed) | [Join Android Beta](https://play.google.com/apps/internaltest/4700888732790045566) |
| **Unraid Community Applications** | Official Template: `BlizzHacker/cartridge-unraid` | Live in Unraid CA | [Unraid CA Listing](https://ca.unraid.net/apps/romarr-04jslct0riva81?q=romarr) |
| **GitHub Container Registry** | Image: `ghcr.io/blizzhacker/romarr:latest` | Continuous Multi-Arch | [GHCR Package](https://github.com/BlizzHacker/romarr/pkgs/container/romarr) |
| **SynoCommunity (Synology)** | Package: `romarr` (noarch, DSM 7) | Submitted, awaiting review | [SynoCommunity/spksrc#7826](https://github.com/SynoCommunity/spksrc/pull/7826) |
| **QNAP / Asustor native packages** | `romarr_<v>.qpkg`, `romarr_<v>_any.apk` | Attached to releases by `nas-packages.yml` from 1.13.2 on; vendor stores need Wade's developer accounts | [Releases](https://github.com/BlizzHacker/romarr/releases) |
| **Microsoft Store / Partner Center** | Product ID: `9PN1VX549NSW` (`MOVEWEIGHT.ROMarr`) | In Submission / Built MSIX | [Windows Desktop Releases](https://github.com/BlizzHacker/romarr/releases) |

---

## Community Packaging & Submission Targets

Packaging for Flathub, Proxmox VE Community Scripts, TrueNAS SCALE, CasaOS / ZimaOS,
Umbrel, winget, Homebrew, Synology, QNAP, Asustor, TerraMaster, UGREEN and
openmediavault is in [`packaging/`](../packaging/), each one validated
with that store's own linter or build tool. The NAS install guide for every platform
is **[NAS.md](NAS.md)**. Status and the exact submission steps
for every store are in **[STORES.md](STORES.md)**.

To contribute or maintain an ecosystem package template, open a pull request or visit the [ROMarr issues tracker](https://github.com/BlizzHacker/romarr/issues).
