# Proxmox VE Community Scripts

`ct/romarr.sh`, `install/romarr-install.sh` and `json/romarr.json` are ROMarr in
the exact layout and house style of
[community-scripts](https://community-scripts.org): new scripts go to
[`community-scripts/DevScripts`](https://github.com/community-scripts/DevScripts)
(formerly ProxmoxVED) first, and are promoted to `ProxmoxVE` once tested.

These files only work **inside** a community-scripts checkout (they source the
shared `build.func` engine). ROMarr's own self-contained installer stays at
[`proxmox/ct/romarr.sh`](../../proxmox/ct/romarr.sh) and is still the command
the README documents, because it does not depend on anyone else's repository.

What the community version does:

- Debian 13 LXC, 1 CPU, 512 MB RAM, 4 GB disk, unprivileged.
- `fetch_and_deploy_gh_release` the latest ROMarr release tarball to `/opt/romarr`,
  Python 3.13 through `setup_uv`, `libarchive-tools` for `.7z`/`.rar`.
- All state in `/opt/romarr_data` (`.env`, `romarr.json`, plugins), so an update's
  clean reinstall of `/opt/romarr` never touches it. Games are filed to `/mnt/roms`.
- `update_script` re-deploys only when GitHub has a newer release.

## Validation (2026-11-15)

- `bash -n` (their CI) and `shellcheck -S warning` are clean.
- Real install on Proxmox VE 8.4.21 with the current `community-scripts/core`
  engine, unattended (`mode=default`): container created, ROMarr 1.10.0 active,
  `/api/health` answered from the LAN.
- Update path: with `~/.romarr` set to 1.9.3 inside the container, running the CT
  script there reported `Update available: romarr 1.9.3 -> 1.10.0`, redeployed,
  restarted, and `/opt/romarr_data/romarr.json` survived.
- The test container was destroyed afterwards.

To reproduce on a PVE host (the engine is a checkout of `community-scripts/core`):

```bash
cd packaging/proxmox
DIAGNOSTICS=no mode=default var_ctid=9182 var_container_storage=local-lvm \
  var_template_storage=local COMMUNITY_SCRIPTS_CORE_DIR=/path/to/core bash ct/romarr.sh
```

## Submitting: blocked by their eligibility rules

community-scripts only accepts applications that are **at least 6 months old**
and have **1,000+ GitHub stars**. ROMarr was created on 2026-07-28 (eligible on
**2027-01-28**) and has 371 stars as of 2026-11-15. A PR opened earlier is
labelled "requirements missing" and auto-closed after 7 days.

When both are met:

1. Fork `community-scripts/DevScripts`, add the three files at the same paths.
2. Open the PR with the body in [`PR.md`](PR.md) (their template, filled in).
3. Their CI runs `bash -n`; a maintainer runs `/test` on their PVE runners.
   Answer review comments by applying suggestions exactly as written.
