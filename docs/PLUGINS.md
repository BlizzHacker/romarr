# ROM Hub plugins

**ROM Hub** is the plugin host. A **plugin** adds a source or capability: for example, searching a homebrew catalogue or looking up metadata. Installing ROMarr and installing a plugin are separate steps.

## Is ROM Hub already installed?

| ROMarr installation | Availability |
|---|---|
| Published amd64 or arm64 Docker image | ROM Hub and Git are included. Open **Hub → Plugins** (`/#hub`). |
| Published 32-bit ARM/armv7 Docker image | ROM Hub is not included. Use an amd64/arm64 host for plugins, or use ROMarr's indexers and normal download clients. |
| Proxmox LXC or Python source checkout | Check **Hub → Plugins**. If the host is missing, install it into the same Python environment that runs ROMarr, as below. |
| Native Windows app | The Windows ZIP includes ROM Hub. Linux seccomp confinement is unavailable on Windows. |
| Windows source helper | ROM Hub is optional. The PowerShell helper installs ROMarr's base requirements; see the command below for the additional host package. |

The top of the Plugins page reports whether plugins can run confined on your host. Installing a plugin runs third-party code with access available to the ROMarr process, so read its repository and install sources you trust.

## Install and use a searchable plugin

1. Open **Hub → Plugins**.
2. Search by name or use the `search` capability filter. Read the description and any key requirement.
3. Click **Install**. Confirm the card shows **Enabled**; use **Enable** if needed.
4. Open **Settings → Download Clients → Add** and choose **ROM Hub**. Set a writable save/staging directory as ROMarr sees it, such as `/downloads/rom-hub` in Docker. Test and save.
5. Return to Plugins. It should say **Enabled plugins join search** and list the searchable plugins.
6. Use **Interactive Search** with a title and platform. Plugin results can be grabbed through the ROM Hub client and then go through normal import and verification.

A metadata-only plugin does not become a game search source. Other capabilities, including assets and cores, belong to ROM Hub and may require its CLI; installing them does not automatically configure an emulator or frontend. For a plugin marked **key needed**, follow that plugin's repository instructions for credentials/configuration.

## Examples in the catalogue

These entries and their tagged source repositories were checked on **2026-10-24**. They are also present in the catalogue pinned by ROMarr's Docker build. This confirms that the plugins are available to install; each upstream service can still be unavailable, change its API, or restrict individual files.

| Plugin | Purpose | Source |
|---|---|---|
| **Homebrew Hub** | Search/import GB/GBC homebrew from gbdev's catalogue | [rom-hub-homebrew v0.3.0](https://github.com/BlizzHacker/rom-hub-homebrew/tree/v0.3.0) |
| **ScummVM freeware games** | Search/import the supported freeware collection | [rom-hub-scummvm-freeware v0.2.0](https://github.com/BlizzHacker/rom-hub-scummvm-freeware/tree/v0.2.0) |
| **Aminet** | Search/import from the Amiga archive | [rom-hub-aminet v0.2.0](https://github.com/BlizzHacker/rom-hub-aminet/tree/v0.2.0) |
| **libretro Thumbnails** | Metadata/artwork capability, rather than game search | [rom-hub-libretro-thumbnails v0.2.0](https://github.com/BlizzHacker/rom-hub-libretro-thumbnails/tree/v0.2.0) |
| **Igir 1G1R Collection Manager** | Igir v5.8.0 1G1R DAT verification, patching (BPS/IPS/UPS/Xdelta3/PMSR) & cleanup for RomM, Gaseous, Retrom, ES-DE & Batocera | [rom-hub-igir v0.1.0](https://github.com/BlizzHacker/rom-hub-igir/tree/v0.1.0) |

The [full catalogue](https://github.com/BlizzHacker/rom-hub/blob/master/catalog/plugins.json) also includes Archive.org, Demozoo, IF Archive and other sources. Read the individual plugin's limits: for example, the itch.io entry is explicitly **play in browser, no import**. A catalogue listing does not promise downloadable copies of every game on that site.

## Add ROM Hub to a source or LXC installation

Install Git using your operating system's package manager. Then install the host into **ROMarr's own virtual environment**. This example uses the same pinned Hub revision as the Docker image:

```bash
# Proxmox installation path; change it for your own source checkout.
/opt/romarr/.venv/bin/python -m pip install \
  "rom-hub @ https://github.com/BlizzHacker/rom-hub/archive/f30123ff9113c2a8067e76e177a2405ca66561ab.tar.gz"
```

For a checkout using `.venv`, use `.venv/bin/python` instead. Installing into system Python or another virtual environment will not make the package available to ROMarr. Refresh the Plugins page after installing; restart ROMarr after a package upgrade if it has already loaded an older module.

For the advanced [Windows source helper](INSTALL.md#windows-source-install), run from the repository directory:

```powershell
.\.venv-windows\Scripts\python.exe -m pip install "rom-hub @ https://github.com/BlizzHacker/rom-hub/archive/f30123ff9113c2a8067e76e177a2405ca66561ab.tar.gz"
```

Use your chosen virtual-environment path if you changed `-VenvDirectory`. Git must be on `PATH` when installing plugin repositories. The Hub page reports the lack of Linux sandbox support on Windows; do not assume it is confined there.

Installed plugin state lives under `ROM_HUB_HOME`. Docker sets this to `/config/rom-hub`, so it survives container recreation when `/config` is mounted. Source installs should set it to a writable persistent directory before starting ROMarr.

## Troubleshooting

| Message or symptom | What to check |
|---|---|
| **ROM Hub is not installed** | Architecture and Python environment in the availability table |
| Plugin installation fails | Git is installed; the repository/ref exists; the host can reach it; Hub's home is writable |
| **Plugins are installed but not part of search yet** | Add, test and save a **ROM Hub** download client |
| A plugin never appears in search | It is enabled and declares `search`; check its supported platforms and source-specific requirements |
| Source returns no files or an access error | Read the plugin's output and upstream access rules; another source may be needed |

Plugin authors can use ROM Hub's [contribution guide](https://github.com/BlizzHacker/rom-hub/blob/master/CONTRIBUTING.md). ROMarr's Plugins page also supports checking a repository URL and submitting a plugin for the shared catalogue.

## Supportive Library Shell, Device Fleet Sync, Igir v5.8.0 & Open-Eyes MCP

ROMarr's **Hub → Library Shell** (`/#portal`), **Hub → Plugins** (`/#hub`), and **System → Ecosystem** (`/#ecosystem`) views empower your existing stack without replacing it:

- **Supportive Library Shell (Iframe Portal)**: Embed **RomM**, **Retrom**, **Gaseous**, **Gameyfin**, or **GG Requestz** directly inside ROMarr with one-click API rescans, Igir v5.8.0 1G1R verification, Netplay SHA-1 hash seeding, and reverse-proxy CSP header guidance.
- **Multi-Device & Network Fleet Manager (`/api/v1/hub/fleet`)**: Synchronize verified 1G1R sets and `gamelist.xml` / Playnite / LaunchBox manifests across local LAN/SMB/NFS shares, SSH/Tailscale handhelds, and cloud WebDAV targets — supporting **ES-DE**, **Raspberry Pi / RetroPie / ArcadePunks / Batocera**, **Argosy Launcher**, **Grout** (muOS/NextUI), **DeckRommSync** (Steam Deck), **SwitchRomM / Tinfoil**, **pkgj / pkgi / fpkgi**, **Kekatsu**, and **RetroArch Cloud Sync**.
- **Igir v5.8.0 1G1R Front-End (`/api/v1/hub/igir`)**: Full GUI and API wrapper for Igir v5.8.0 with native `{romm}`, `{es}`, and `{batocera}` output folder tokens.
- **Open-Eyes & ROMarr MCP Bridge (`/api/v1/mcp`)**: Ready-to-copy `mcpServers` configuration pairing [BlizzHacker/open-eyes](https://github.com/BlizzHacker/open-eyes) desktop control with ROMarr's API tools.
