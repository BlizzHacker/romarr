# Synology DSM 7

There are two packages. The user guide is [docs/NAS.md](../../docs/NAS.md#synology-dsm-72).

| Folder | What |
|---|---|
| [`container-manager/`](container-manager/) | Compose project for Container Manager (DSM 7.2+) |
| [`spksrc/`](spksrc/) | Native package in SynoCommunity's spksrc layout: `spk/romarr` and `cross/romarr` |
| [`test/`](test/) | `dsm-spk-smoke.sh`, which replays Package Center's install sequence |

## The native package

- **Architecture.** `noarch`, DSM 7.0+. It's pure Python on SynoCommunity's `python312` package, with requests and its four dependencies shipped as `py3-none-any` wheels and installed offline into a venv.
- **Source.** The release tarball, checked against `cross/romarr/digests`. That's the same SHA-256 the Flatpak and Homebrew packages pin.
- **Package user.** DSM runs it as `sc-romarr`. The install wizard asks for the games shared folder; DSM creates it and gives `sc-romarr` read/write through the `data-share` resource worker.
- **State.** Everything lives in `/var/packages/romarr/var`: `romarr.json`, `rom-hub/`, `backends/`, and `romarr.env`, which is written once at install. DSM keeps that folder across upgrades.
- **Limits.** No `bsdtar` or 7-Zip, so `.7z` and `.rar` don't import, and there's no ROM Hub. Both are compiled; see the guide.

### Build (2026-11-23, spksrc `f81c394`)

In the spksrc image (`ghcr.io/synocommunity/spksrc`), with `spk/romarr` and `cross/romarr` copied into an spksrc checkout:

```bash
make -C cross/romarr digests        # SHA256 ce3802aa... = GitHub's v1.15.0 tarball
make -C spk/romarr arch-noarch-7.1  # packages/romarr_noarch-dsm7_1.15.0-1.spk, 1.7 MB
```

The generated `INFO` and `conf/` contain:

- `arch="noarch"`, `os_min_ver="7.0-40000"`, `install_dep_packages="python312"`
- `run-as: package`, user `sc-romarr`
- a `data-share` grant for `{{wizard_games_share}}`
- `adminport="6868"`

### Install test

```bash
packaging/synology/test/dsm-spk-smoke.sh romarr_noarch-dsm7_1.15.0-1.spk
```

This runs in a `python:3.12-slim` container laid out like DSM 7.2: `/var/packages`, `/volume1/games`, the `sc-romarr` user, and Python 3.12 at `/var/packages/python312/target`. The 2026-11-27 run went through every step and ended with `DSM SPK SMOKE PASSED`:

1. **Install.** `preinst`, `package.tgz` extracted into `target/`, then `postinst`: the venv was created and five wheels installed offline. `romarr.env` got `LIBRARY_PATH=/volume1/games`.
2. **Start** as `sc-romarr`. `/api/health` answered `{"ok": true}`. The log showed `listening on http://0.0.0.0:6868, library=/volume1/games`.
3. **Stop.** `status` reported not running.
4. **Upgrade.** `preupgrade`, `preuninst` and `postuninst`, a new `target/`, `postinst` and `postupgrade`. `romarr.json` was byte-identical afterwards and `romarr.env` still named the share. It started healthy again.
5. **Uninstall.**

DSM itself wasn't booted: its licence allows it only on Synology hardware.

Shellcheck is clean on `service-setup.sh` and the smoke script.

### Submit to SynoCommunity

1. Fork `SynoCommunity/spksrc` and copy `spksrc/spk/romarr` and `spksrc/cross/romarr` into it.
2. Open a PR titled `romarr: add package v1.15.0`, using their template.

## Container Manager

`docker compose config` validates the project. Its library and downloads mounts are long-form binds with `create_host_path: false`. Verified with Compose 2.39: a missing `/volume1/games` stops `up` with `bind source path does not exist`, instead of creating an empty folder.
