# App Store & Package Directory Submissions

This document tracks all official store listings, community catalogs, and package registry submissions for ROMarr.

If you maintain a package registry or community catalog (such as TrueNAS Scale, Umbrel, CasaOS, Flathub, Winget, Homebrew, or Proxmox Community Scripts) and want to package or index ROMarr, please consult [docs/OFFICIAL-DISTRIBUTION.md](OFFICIAL-DISTRIBUTION.md) or open an issue.

---

## Active & Verified Directory Listings

| Store / Platform | Listing & Package Info | Status | Link |
|---|---|---|---|
| **Google Play (Android)** | App ID: `org.romarr.android` (Internal Beta Track) | Active Beta (12 Testers Needed) | [Join Android Beta](https://play.google.com/apps/internaltest/4700888732790045566) |
| **Unraid Community Applications** | Official Template: `BlizzHacker/cartridge-unraid` | Live in Unraid CA | [Unraid CA Listing](https://ca.unraid.net/apps/romarr-04jslct0riva81?q=romarr) |
| **GitHub Container Registry** | Image: `ghcr.io/blizzhacker/romarr:latest` | Continuous Multi-Arch | [GHCR Package](https://github.com/BlizzHacker/romarr/pkgs/container/romarr) |
| **Microsoft Store / Partner Center** | Product ID: `9PN1VX549NSW` (`MOVEWEIGHT.ROMarr`) | In Submission / Built MSIX | [Windows Desktop Releases](https://github.com/BlizzHacker/romarr/releases) |

---

## Community Packaging & Submission Targets

We are actively expanding ROMarr into the following stores and catalogs:

1. **Flathub (Linux / Steam Deck / ChromeOS)**:
   - Packaging ROMarr with embedded loopback and PySide6 / system webview.
2. **Proxmox Community Scripts**:
   - Official Proxmox LXC install script exists in `/proxmox/ct/romarr.sh`.
3. **TrueNAS SCALE / TrueCharts**:
   - App catalog template using official `ghcr.io/blizzhacker/romarr` image.
4. **CasaOS / ZimaOS AppStore**:
   - One-click docker-compose application template.
5. **Umbrel App Store**:
   - Self-hosted media stack manifest.
6. **Winget (Windows Package Manager)** & **Homebrew (macOS)**:
   - CLI / standalone manifest recipes.

To contribute or maintain an ecosystem package template, open a pull request or visit the [ROMarr issues tracker](https://github.com/BlizzHacker/romarr/issues).
