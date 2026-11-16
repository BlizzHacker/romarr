# Stores and package managers

Where ROMarr can be installed from, what is ready, what was verified, and the
exact steps left to publish each one. The packaging itself lives in
[`packaging/`](../packaging/); every folder there has a README with the full
validation log and submission guide. Live listings (Unraid CA, Google Play beta,
Microsoft Store, GHCR) are tracked in [SUBMISSIONS.md](SUBMISSIONS.md).

Status as of 2026-11-15, packaging ROMarr **1.10.0**. Nothing below has been
submitted yet: each step that opens a pull request on someone else's repository
is for a maintainer to do.

| Store | Package | Validated with | Status | Next step |
|---|---|---|---|---|
| **Flathub** (Linux, Steam Deck, ChromeOS) | [`packaging/flatpak`](../packaging/flatpak/) `app.romarr.ROMarr` | `org.flatpak.Builder` build, `flatpak-builder-lint` manifest + repo + appstream, `desktop-file-validate`, app run | Ready | PR to `flathub/flathub` `new-pr` |
| **Proxmox VE Community Scripts** | [`packaging/proxmox`](../packaging/proxmox/) `ct/` `install/` `json/` | `bash -n`, `shellcheck`, real install + update on PVE 8.4 with the current engine | Ready, **not yet eligible** | Submit after 2027-01-28 **and** 1,000 stars |
| **TrueNAS SCALE** (Community train) | [`packaging/truenas`](../packaging/truenas/) `ix-dev/community/romarr` | `port_validation.py`, `check_metadata.py`, `ci.py` render + run (healthy) | Ready | PR to `truenas/apps` |
| **CasaOS / ZimaOS** | [`packaging/casaos`](../packaging/casaos/) `Apps/ROMarr` | `validate_compose.py` (0 issues), `build_dist.sh`, real compose run | Ready | PR to `IceWhaleTech/CasaOS-AppStore` |
| **Umbrel** | [`packaging/umbrel`](../packaging/umbrel/) `romarr/` | `lint-apps.mjs --check-images` (no issues) | Ready | Update existing PR getumbrel/umbrel-apps#5947 |
| **winget** (Windows) | [`packaging/winget`](../packaging/winget/) `MoveWeight.ROMarr` | `winget validate` | Ready | `wingetcreate submit` to `microsoft/winget-pkgs` |
| **Homebrew** (macOS, Linux) | [`packaging/homebrew`](../packaging/homebrew/) `Formula/romarr.rb` | `brew style`, `brew audit --strict --new --online`, source install, `brew test` | Ready | Publish tap `BlizzHacker/homebrew-romarr`, then homebrew-core |

## Flathub

ROMarr as a desktop app: its own server on `127.0.0.1` only, in a native GTK 4 /
WebKitGTK window (GNOME 51 runtime), with `--headless` for Steam Deck game mode.
Built and run once on 2026-11-15; the window showed the first-run password screen.

1. Fork `flathub/flathub`, branch from **`new-pr`**.
2. Copy everything in `packaging/flatpak/` except its README to the branch root.
3. Open a PR to `new-pr` titled `Add app.romarr.ROMarr`.
4. Be ready to justify `--filesystem=home` (+ `/run/media`, `/media`, `/mnt`): the
   library and download folders are wherever the user keeps them.
5. After merge, put Flathub's verification token at
   `https://romarr.app/.well-known/org.flathub.VerifiedApps.txt`.

## Proxmox VE Community Scripts

The three files follow `community-scripts/DevScripts`' agent guide: release tarball via
`fetch_and_deploy_gh_release`, Python 3.13 via `setup_uv`, state in `/opt/romarr_data`
so updates never touch it. ROMarr's own self-contained installer
(`proxmox/ct/romarr.sh`) stays the documented one-liner.

**Blocked by their rules:** new scripts need an app at least 6 months old (ROMarr:
2026-07-28, so **2027-01-28**) with **1,000+ GitHub stars** (371 today). Earlier PRs are
labelled "requirements missing" and closed after 7 days. When both hold:

1. Fork `community-scripts/DevScripts`, add `ct/romarr.sh`, `install/romarr-install.sh`,
   `json/romarr.json`.
2. Open the PR with the prepared body in `packaging/proxmox/PR.md`.

## TrueNAS SCALE

An app for the official Community train on iX's shared library, running as 568:568
with Config, Game Library and extra storage. TrueCharts dropped TrueNAS SCALE in 2024,
so it is not a target. Users can install today via **Install via YAML** with
`packaging/truenas/custom-app.yaml`.

1. Fork `truenas/apps`, copy `ix-dev/community/romarr/`.
2. PR titled `Add romarr to community train`; link the icon and a screenshot for iX
   to host (URLs in `packaging/truenas/README.md`).

## CasaOS / ZimaOS

A compose app with the `x-casaos` block for IceWhale's official store, which feeds both
CasaOS and ZimaOS and is merging third-party apps.

1. Fork `IceWhaleTech/CasaOS-AppStore`, copy `Apps/ROMarr/`.
2. PR titled `feat(appstore): add ROMarr 1.10.0`, noting the validator and build passed.

## Umbrel

PR getumbrel/umbrel-apps#5947 has been open since 2026-08-02 at ROMarr 0.7.0 with no
maintainer response.

1. On branch `BlizzHacker/umbrel-apps:add-romarr`, replace `romarr/umbrel-app.yml` and
   `romarr/docker-compose.yml` with these, and add `romarr/data/config/.gitkeep`.
2. Refresh the PR body to their current template and link the logo and screenshots.
3. Comment once to request review.

## winget

Installs the portable ZIP and adds a `romarr` command.

```powershell
wingetcreate submit --prtitle "New package: MoveWeight.ROMarr version 1.10.0" packaging\winget\manifests\m\MoveWeight\ROMarr\1.10.0
```

The winget-pkgs pipeline installs it in a clean VM; that run is the install test
(not done locally, to keep 1.6 GB off the system drive).

## Homebrew

Builds from source into a Python 3.14 virtualenv, binds loopback until claimed,
`brew services` ready.

1. Create the public repo `BlizzHacker/homebrew-romarr` with `Formula/romarr.rb`;
   users then run `brew install blizzhacker/romarr/romarr`.
2. Later, homebrew-core: ROMarr is past the self-submission notability bar. Adding a
   `pyproject.toml` to ROMarr first lets the formula use
   `virtualenv_install_with_resources`, which core reviewers expect.

## Keeping them current

`tests/test_packaging.py` fails if the version, image tag or port drifts between these
packages, so a release bump has to touch all of them together. Per release:

- Flatpak: Flathub's checker opens the bump PR; add a `<release>` to the metainfo.
- TrueNAS: their Renovate bot bumps the image; CasaOS/Umbrel: one PR each with the new
  tag and digest.
- winget: `wingetcreate update MoveWeight.ROMarr --version <v> --urls <zip> --submit`.
- Homebrew: `brew bump-formula-pr --url=<tarball> blizzhacker/romarr/romarr`.
