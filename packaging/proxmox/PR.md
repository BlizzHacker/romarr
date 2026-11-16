<!-- PR title: "ROMarr" -- base: community-scripts/DevScripts main -->

## ✍️ Description

ROMarr is the *arr for games: it searches indexers through Prowlarr, sends releases to qBittorrent, SABnzbd or NZBGet, and files the verified game into RomM, Gaseous, Retrom or a folder tree. Standard pattern: release tarball via `fetch_and_deploy_gh_release`, Python 3.13 via `setup_uv`, one systemd service, all state in `/opt/romarr_data`. One deviation: there is no `pyproject.toml`, so dependencies go in with `uv venv` + `uv pip install -r requirements.txt` instead of `uv sync`. Tested unattended on Proxmox VE 8.4.21 (install, then the update path from 1.9.3 to 1.10.0 with data kept). Drafted with Claude Opus 5.5 (high reasoning) following `AGENTS.md` and `pve-script-creator.agent.md`, then reviewed and tested by the author.

## 🔗 Related PR / Issue

Link: #

## ✅ Prerequisites  (**X** in brackets)

- [x] **Self-review completed** – Code follows project standards.
- [x] **Tested thoroughly** – Changes work as expected.
- [x] **No breaking changes** – Existing functionality remains intact.
- [x] **No security risks** – No hardcoded secrets, unnecessary privilege escalations, or permission issues.

---

## 🧩 Script

**Name:** ROMarr

**Tool:** The *arr for games. Request a title and ROMarr finds it through Prowlarr, downloads it with your client, verifies it and files it into your game library.

### 🗂️ Category (**X** in brackets)

- [x] **CT** – LXC container (`ct/` + `install/`)
- [ ] **VM** – Virtual machine (`vm/`)
- [ ] **Addon** – `tools/addon/`
- [ ] **PVE Tool** – `tools/pve/`
- [ ] **Other** – path given in the Description

### 🖥️ Platform – tested on (**X** in brackets)

- [x] **Proxmox VE** – Created on a Proxmox VE host and the app works.
- [ ] **Incus** – Created on an Incus host and the app works.

### 🏗️ Architecture (**X** in brackets)

- [ ] **arm64 supported** - Tested and supported on arm64.
- [x] **arm64 not tested** - Assumed to work on arm64, but testing has not been done.
- [ ] **arm64 not supported** - Confirmed upstream dependencies or binaries do not support arm64.

---

## 🛠️ Type of Change (**X** in brackets)

- [ ] 🐞 **Bug fix** – Resolves an issue without breaking functionality.
- [ ] ✨ **New feature** – Adds new, non-breaking functionality.
- [ ] 💥 **Breaking change** – Alters existing functionality in a way that may require updates.
- [x] 🆕 **New script** – A fully functional and tested script or script set.
- [ ] 🌍 **Website update** – Changes to website-related JSON files or metadata.
- [ ] 🔧 **Refactoring / Code Cleanup** – Improves readability or maintainability without changing functionality.
- [ ] 📝 **Documentation update** – Changes to `README`, `AppName.md`, `CONTRIBUTING.md`, or other docs.

---

## 🔍 Code & Security Review  (**X** in brackets)

- [x] **Follows `CODE-AUDIT.md` & `CONTRIBUTING.md` guidelines**
- [x] **Uses correct script structure (`AppName.sh`, `AppName-install.sh`, `AppName.json`)**
- [x] **No hardcoded credentials**
- [x] **No Docker / Docker Compose** – The application is installed bare-metal; Docker is not used.
- [x] **No git pull** – Updates use `fetch_and_deploy_gh_release`, `fetch_and_deploy_codeberg_release`, `fetch_and_deploy_gl_release`, or `fetch_and_deploy_from_url` instead of `git pull`.

---

## 📋 Additional Information (optional)

There is no default credential: the first visit to the web UI sets the password (noted in the JSON).

---

## 📦 Application Requirements (for new scripts)

> ⚠️ Do not remove this section.

- [ ] The application is **at least 6 months old** <!-- first release 2026-07-28; tick on or after 2027-01-28 -->
- [x] The application is **actively maintained**
- [ ] The application has **1,000+ GitHub stars** <!-- 371 on 2026-11-15 -->
- [x] Official **release tarballs** are published
- [x] I understand that not all scripts will be accepted due to various reasons and criteria by the community-scripts ORG

## 🌐 Source

https://github.com/BlizzHacker/romarr

---

## 🤖 AI Assistance (**X** in brackets)

- [ ] **No AI used** – Scripts were written without AI assistance.
- [x] **AI was used** – I confirm the scripts were built using [`AGENTS.md`](https://github.com/community-scripts/DevScripts/blob/main/AGENTS.md) and [`.github/agents/pve-script-creator.agent.md`](https://github.com/community-scripts/DevScripts/blob/main/.github/agents/pve-script-creator.agent.md) as guidance, and the output has been reviewed and corrected to match those guidelines.

Claude Opus 5.5, high reasoning, drafted all three files from the agent guide and the Floppy script as a reference, then ran them on a Proxmox VE 8.4.21 host (fresh install and the update path).
