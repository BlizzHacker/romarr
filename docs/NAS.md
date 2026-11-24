# ROMarr on a NAS

ROMarr runs on every major NAS. Find yours in the table, then read its section.

| NAS | How | Package |
|---|---|---|
| [Synology](#synology-dsm-72) | Container Manager project, or the native SynoCommunity-style package | [`packaging/synology`](../packaging/synology/) |
| [QNAP](#qnap-qts-and-quts-hero) | Container Station application, or the QPKG | [`packaging/qnap`](../packaging/qnap/) |
| [Asustor](#asustor-adm) | App Central package, or a Portainer stack | [`packaging/asustor`](../packaging/asustor/) |
| [TerraMaster](#terramaster-tos-6) | Docker Manager project | [`packaging/terramaster`](../packaging/terramaster/) |
| [UGREEN](#ugreen-ugos-pro) | Docker project | [`packaging/ugreen`](../packaging/ugreen/) |
| [ZimaOS / CasaOS](#zimaos-and-casaos) | App Store, or a custom install | [`packaging/casaos`](../packaging/casaos/) |
| [TrueNAS SCALE](#truenas-scale) | Community app, or Install via YAML | [`packaging/truenas`](../packaging/truenas/) |
| [Unraid](#unraid) | Community Applications | [BlizzHacker/cartridge-unraid](https://github.com/BlizzHacker/cartridge-unraid) |
| [openmediavault](#openmediavault) | Compose plugin | [`packaging/openmediavault`](../packaging/openmediavault/) |
| [Umbrel](#umbrel) | Umbrel App Store | [`packaging/umbrel`](../packaging/umbrel/) |

Store status and submission steps for each one are in [STORES.md](STORES.md).

## What every NAS install has in common

- **Image.** `ghcr.io/blizzhacker/romarr`, built for `amd64`, `arm64` and `armv7`. All three were started and answered `/api/health` for 1.13.2. On `armv7` the ROM Hub plugin host is missing (its pydantic dependency has no 32-bit ARM build), so the Hub tab reports plugins as unavailable. Everything else works.
- **Port.** The web UI is on **6868**. There is no default password: the first visit sets it, so open ROMarr right after installing. To claim it before anyone else can reach it, set `ROMARR_PASSWORD`.
- **Three folders.**
  - `/config` holds settings, history, the API key and plugins. Back it up.
  - `/roms` is your game library: the folder RomM, Gaseous or your emulator frontend reads.
  - The downloads folder is your client's completed folder. Mount it at the path your client reports, character for character. ROMarr asks the client where a download is and then opens that path itself. If the paths cannot match, add a remap under **Settings → Media Management**.
- **PUID and PGID.** These pick the user and group that own the games ROMarr writes. Use the same ids as RomM or your media user. To find them, SSH in and run `id <user>`. The vendor defaults in each section are starting points, not facts about your NAS. Don't use 0: games would be owned by root.
- **Permissions.**
  - ROMarr changes ownership of `/config` only, so it can always read its own settings.
  - It never chowns or chmods your library or downloads. That could take hours on a big share, and it would undo choices you made.
  - New files are created with mode 0664 and new folders with 0775 (`UMASK=002`).
  - The PUID user needs read/write on the library and read on downloads.
- **Missing folders stop the install.** Each template uses `create_host_path: false` for the library and downloads mounts. A mistyped path fails with *bind source path does not exist*. Without that, Docker would quietly create an empty folder, and ROMarr would file games into it while every status showed green.
- **Hardware transcoding: not needed.** ROMarr moves and checks files; it never encodes video. Don't pass through `/dev/dri` or a GPU.
- **Settings are read once.** Environment values like `LIBRARY_PATH` are copied into ROMarr's settings on the first start. After that, use the Settings page.
- **Updates.** Each package pins a ROMarr version, so updating means installing the next package or changing the image tag. ROMarr's own update notice tells you when a new version is out. On Docker, `latest` follows releases if you prefer that.

### Memory and low-RAM models

Measured with `docker stats`, with `DAT_PATH` pointing at 111 No-Intro and Redump DATs from libretro-database (53 MB on disk):

| Load | 1.13.2 | Releases after 1.13.2 |
|---|---|---|
| Idle, no DATs | about 50 MB | about 50 MB |
| DATs loaded, first start | about 237 MB | about 99 MB (105 MB at peak while loading) |
| DATs loaded, after a restart | about 234 MB | about 95 MB |
| Container capped at 192 MB | killed for running out of memory while loading | runs |
| Container capped at 128 MB | not tried | runs, on first start and after a restart |

DATs are loaded only when `DAT_PATH` is set, and their memory grows with what you give ROMarr. After 1.13.2, a loaded DAT takes about as much memory as its size on disk. In 1.13.2 and earlier it took about 3.5 times as much. With the DATs above, don't cap the container below 128 MB (256 MB on 1.13.2). On a 512 MB or 1 GB NAS, you can still point `DAT_PATH` at just the systems you collect. The full No-Intro and Redump XML packs are much larger than libretro's trimmed copies, so budget for that before loading them.

### Cloud folders (FUSE) in a container

To mount a cloud folder as a library (see [CLOUD-STORAGE.md](CLOUD-STORAGE.md#mounts)), the container needs `/dev/fuse` and `SYS_ADMIN`. On hosts with AppArmor, including Ubuntu and Debian-based NAS systems, it also needs `apparmor=unconfined`. Without `/dev/fuse` everything else works, and the Cloud page says mounts are unavailable. Backup, mirror, offload and import don't need FUSE.

In 1.13.2 and earlier, mounts fail in every Docker container that runs as a PUID, with *fusermount3: could not determine username*. The entrypoint fix that gives your PUID a name inside the container ships in the first release after 1.13.2. The NAS templates pin 1.13.2 until then, so to mount cloud folders now, use the `latest` tag once that release is out. Mounts were checked with `PUID`/`PGID` set to 1026:100 (Synology), 99:100 (Unraid), 568:568 (the TrueNAS YAML install), 1000:1000 and 0:0.

The fix applies only when the container starts as root and drops to `PUID`. A container started directly as another user — `docker run --user`, Kubernetes `runAsUser`, or the TrueNAS Community app, which runs as `568:568` — keeps an unnamed uid, so mounts still fail there. The Cloud page then says why. For cloud mounts on TrueNAS, use the YAML install, which sets `PUID`/`PGID` to 568.

Compose:

```yaml
    devices:
      - /dev/fuse:/dev/fuse
    cap_add:
      - SYS_ADMIN
    security_opt:
      - apparmor=unconfined
```

## Synology DSM 7.2

There are two ways to install, and both run the same ROMarr.

- **Container Manager** works on models that support Container Manager: most Plus and Value models, and some ARM `j` and value models.
- **The native package** (`romarr_noarch-dsm7_<version>.spk`) runs on every DSM 7 model, including ARM models without Container Manager. It can't open `.7z` or `.rar` archives (see below), so use Container Manager if your model has it. It needs SynoCommunity's Python 3.12 package. It's built with SynoCommunity's spksrc from [`packaging/synology/spksrc`](../packaging/synology/spksrc/).

### Container Manager

1. In **File Station**, create these folders:
   - `docker/romarr`, for settings
   - your games shared folder, for example `games`
   - your downloads folder, if it doesn't exist yet
2. **Container Manager → Project → Create**:
   - Project name: `romarr`
   - Path: `/volume1/docker/romarr`
   - Source: *Create docker-compose.yml*
   - Paste [`packaging/synology/container-manager/docker-compose.yml`](../packaging/synology/container-manager/docker-compose.yml).
3. Edit the left side of each path if your shares differ. A path that doesn't exist stops the project with *bind source path does not exist*, so a typo can't file games into a new empty folder.
4. Open `http://<nas>:6868` and set the password.

- **PUID/PGID.** The template uses `1026:100`: the first user created on a DiskStation and its `users` group. To check yours, enable SSH (**Control Panel → Terminal & SNMP**) and run `id <user>`. A dedicated `romarr` user in the `users` group, with read/write on the games share, keeps permissions tidy.
- **Permissions.** Give the PUID user read/write on the games share and read on the downloads share (**Control Panel → Shared Folder → Edit → Permissions**).
- **Downloads.** Download Station runs on DSM itself and reports `/volume1/downloads/...`, so the template mounts that folder at the same path inside the container. If your client runs in a container, mount the folder at the path that client reports instead.
- **Port.** 6868. If **Control Panel → Security → Firewall** is on, allow it.
- **Updates.** Change the image tag in the project's YAML (**Project → romarr → YAML**), then **Build**. Or use `latest`, then **Action → Stop**, **Clean**, and **Build**.

### Native package (.spk)

1. Add SynoCommunity to Package Center (**Settings → Package Sources**, `https://packages.synocommunity.com/`) and install **Python 3.12**.
2. Download `romarr_noarch-dsm7_<version>.spk` from the [release](https://github.com/BlizzHacker/romarr/releases), then **Package Center → Manual Install**.
3. The wizard asks for the games shared folder. It's created if missing, and the package user `sc-romarr` gets read/write on it.
4. Give `sc-romarr` read access to the downloads share. Edit the share, open **Permissions**, choose **System internal user**, and tick **sc-romarr**.
5. Open ROMarr from the main menu, or `http://<nas>:6868`.

- **PUID/PGID.** These don't apply: DSM runs the package as `sc-romarr`, and games are owned by that user. Grant access per share as above.
- **Settings** live in `/var/packages/romarr/var` (`romarr.json`, plugins, backends), which DSM keeps across upgrades. `romarr.env` there holds extra environment values, one `KEY=value` per line, for example `DAT_PATH=/volume1/dats`. Restart the package after editing it.
- **What's different from Docker.** This package is pure Python for every architecture, so it leaves out the compiled tools the Docker image carries:
  - **Archives.** `.zip` and loose files import. `.7z` and `.rar` need libarchive's `bsdtar` and 7-Zip, which DSM doesn't ship. Most disc platforms (PlayStation, PS2, Wii) are distributed as `.7z`, so collect those with the Container Manager install. The System page shows which archive tools ROMarr found.
  - **ROM Hub.** The plugin host isn't included, because its pydantic dependency is compiled.
- **Updates.** Install the newer `.spk` over the old one with **Manual Install**. Once it's in SynoCommunity's repository, Package Center offers updates itself.
- **Why there's no DSM VM test.** DSM's licence permits it only on Synology hardware. Instead, `packaging/synology/test/dsm-spk-smoke.sh` replays Package Center's install, start, upgrade and remove sequence in a container laid out like a DiskStation, and CI runs it.

## QNAP QTS and QuTS hero

QNAP needs **Container Station 3**, from App Center.

### Container Station application

1. Create a `Games` folder in `Multimedia`, or edit the path.
2. **Container Station → Applications → Create**. Name it `romarr` and paste [`packaging/qnap/container-station/docker-compose.yml`](../packaging/qnap/container-station/docker-compose.yml).
3. Open `http://<nas>:6868`.

### QPKG

1. Download `romarr_<version>.qpkg` from the [release](https://github.com/BlizzHacker/romarr/releases).
2. Install it from **App Center → Install Manually**. QTS warns that the package isn't signed by QNAP; that's expected for packages outside QNAP's store.
3. Edit `romarr.env` in the package folder (`/share/CACHEDEV1_DATA/.qpkg/romarr/romarr.env` on most units), then restart ROMarr in App Center. Missing library or downloads folders stop the start, and the reason is written to the system log.

- **PUID/PGID.** `1000:100` by default: the first user created after `admin`, and the `everyone` group. Check yours with `id <user>` over SSH. Don't use `admin` (uid 0).
- **Folders.**
  - Settings: `/share/Container/romarr`. Removing the QPKG keeps them.
  - Library: `/share/Multimedia/Games`
  - Downloads: `/share/Download`, mounted at the same path, because Download Station reports it.
- **Permissions.** Give the PUID user read/write on the library share in **Control Panel → Shared Folders → Edit Shared Folder Permission**.
- **Port.** 6868. App Center's **Open** button goes there.
- **Updates.**
  - QPKG: install the newer one over the old one. The container is recreated on the new image, and settings stay.
  - Container Station: edit the image tag in the application and **Update**.

## Asustor ADM

ADM needs the **Docker Engine** app from App Central.

### App Central package

1. Download `romarr_<version>_any.apk` from the [release](https://github.com/BlizzHacker/romarr/releases), then **App Central → Manual Install**.
2. On first install the package creates:
   - `/share/Docker/romarr`, for settings
   - `/share/Docker/romarr/romarr.env`, for paths and ids
   - `/share/Media/Games`, as the library
3. Open ROMarr from ADM's desktop, or `http://<nas>:6868`. To change folders or ids, edit `romarr.env` and restart ROMarr in App Central.

### Portainer stack

Install **Portainer CE** from App Central. Then go to **Stacks → Add stack** and paste [`packaging/asustor/portainer/docker-compose.yml`](../packaging/asustor/portainer/docker-compose.yml).

- **PUID/PGID.** `1000:100` by default. ADM's `admin` is uid 999; the first user you create is 1000, and `users` is 100. Check yours with `id <user>`.
- **Folders.**
  - Settings: `/share/Docker/romarr`
  - Library: `/share/Media/Games`
  - Downloads: `/share/Download`, mounted at the same path, for Download Center.
- **Permissions.** Give the PUID user read/write on the library in **Access Control → Shared Folders**.
- **Port.** 6868.
- **Updates.**
  - APK: install the newer one over the old one. An edited `romarr.env` is kept.
  - Portainer: change the tag and **Update the stack**.

## TerraMaster TOS 6

1. Create the `docker/romarr`, `Games` and `Download` folders on Volume 1, or edit the paths.
2. **Docker Manager → Project → Create**:
   - Project name: `romarr`
   - Path: `/Volume1/docker/romarr`
   - Use [`packaging/terramaster/docker-compose.yml`](../packaging/terramaster/docker-compose.yml) as the compose file.
3. Open `http://<nas>:6868`.

- **PUID/PGID.** `1000:100` in the template. TOS numbers users from 1000; check yours with `id <user>` over SSH. Some guides use `PUID=0` on TOS. Don't: games would be owned by root, and your library app couldn't change them.
- **Permissions.** Give the PUID user read/write on the `Games` share in **Control Panel → Shared Folders**.
- **Port.** 6868.
- **Updates.** Change the tag in the project, then redeploy it.
- **Store.** TerraMaster's TOS App Center accepts third-party apps only from registered developers, so for now the compose project is the install.

## UGREEN UGOS Pro

1. Create `docker/romarr`, `Games` and `Downloads` on Volume 1, or edit the paths.
2. **Docker → Project → Create**:
   - Name: `romarr`
   - Storage path: `/volume1/docker/romarr`
   - Paste [`packaging/ugreen/docker-compose.yml`](../packaging/ugreen/docker-compose.yml).
3. Open `http://<nas>:6868`.

- **PUID/PGID.** `1000:10` in the template. UGOS Pro users are in group 10 (`admin`). Some units number the first user 999, so check with `id <user>` over SSH (**Control Panel → Terminal**).
- **Permissions.** Give the PUID user read/write on `Games`.
- **Port.** 6868.
- **Updates.** Change the tag in the project and redeploy.
- **Store.** UGREEN's app center has no third-party submission yet.
- **Models.** Docker isn't available on the DH series.

## ZimaOS and CasaOS

ROMarr is in IceWhale's App Store submission ([CasaOS-AppStore#1156](https://github.com/IceWhaleTech/CasaOS-AppStore/pull/1156)). Until that's merged, use **App Store → Custom Install → Import** and paste [`packaging/casaos/Apps/ROMarr/docker-compose.yml`](../packaging/casaos/Apps/ROMarr/docker-compose.yml).

- **PUID/PGID.** `1000:1000` by default: the CasaOS/ZimaOS user. You can change them in the install dialog.
- **Folders.**
  - Settings: `/DATA/AppData/romarr/config`
  - Library: `/DATA/Media/Games`
  - Downloads: `/DATA/Downloads`
- **Port.** 6868.
- **Updates.** The App Store offers the new version once the store has it. For a custom install, edit the tag.

## TrueNAS SCALE

ROMarr is in the Community train ([truenas/apps#6524](https://github.com/truenas/apps/pull/6524)): **Apps → Discover**, search ROMarr. You can also use **Install via YAML** with [`packaging/truenas/custom-app.yaml`](../packaging/truenas/custom-app.yaml).

- **PUID/PGID.** `568:568`, the `apps` user. Give `apps` read/write on the library dataset and read on downloads in the dataset's ACL.
- **Folders.** Use datasets you create: for example `apps/romarr` for config, and your games and downloads datasets.
- **Port.** 6868.
- **Cloud folder mounts.** The Community app starts the container directly as `568:568`, so FUSE mounts can't work there (see [Cloud folders](#cloud-folders-fuse-in-a-container)). For mounts, use the YAML install: it sets `PUID`/`PGID` to 568 and needs `/dev/fuse`, `SYS_ADMIN` and `apparmor=unconfined` added.
- **Updates.** The Community train is kept current by iX's Renovate bot. For a YAML install, edit the tag.

## Unraid

Search **ROMarr** in **Apps** (Community Applications). The template is [BlizzHacker/cartridge-unraid](https://github.com/BlizzHacker/cartridge-unraid/blob/main/templates/romarr.xml).

- **PUID/PGID.** `99:100` (`nobody:users`), Unraid's convention.
- **Folders.**
  - Settings: `/mnt/user/appdata/romarr`
  - Library: `/mnt/user/roms`
  - Downloads: `/mnt/user/downloads`
  The container side of downloads must match what your client reports.
- **Port.** 6868.
- **Cloud folder mounts.** Add the `/dev/fuse` device and put `--cap-add SYS_ADMIN` in **Extra Parameters**.
- **Updates.** The template follows `latest`, so **Check for Updates** on the Docker tab picks up each release.

## openmediavault

1. Install **openmediavault-compose** (omv-extras).
2. **Services → Compose → Files → Create**:
   - Name: `romarr`
   - File: paste [`packaging/openmediavault/docker-compose.yml`](../packaging/openmediavault/docker-compose.yml)
   - Environment: set `ROMARR_CONFIG`, `ROMARR_ROMS` and `ROMARR_DOWNLOADS` to your shared folders' absolute paths (**Storage → Shared Folders** shows them).
3. **Up**, then open `http://<nas>:6868`.

- **PUID/PGID.** `1000:100`: the compose plugin's `appuser` and `users`. Check yours with `id <user>`.
- **Missing paths.** All three paths are required, so an unset one stops the file before it starts.
- **Port.** 6868.
- **Updates.** Change the tag, or use `latest` and the plugin's **Pull**, then **Up**.

## Umbrel

ROMarr is submitted to the Umbrel App Store ([getumbrel/umbrel-apps#5947](https://github.com/getumbrel/umbrel-apps/pull/5947)). Install it from the store once it's listed. It needs Prowlarr from the store.

- **PUID/PGID.** `1000:1000`, Umbrel's user. This is fixed by the app.
- **Folders.** Finished games go in `roms` inside your Downloads storage, so they show up in the Files app.
- **Port.** 6868, behind Umbrel's app proxy.
- **Updates.** The Umbrel App Store updates it.

## NAS without a package

- **WD My Cloud (OS 5), Buffalo LinkStation and TeraStation, Netgear ReadyNAS.** There's no supported Docker runtime and no open package format to build for. ReadyNAS OS 6 had Docker on some x86 models, but it's end-of-life. Run ROMarr on another machine and point the library at the NAS share over SMB or NFS. [NAS-LIBRARIES.md](NAS-LIBRARIES.md) covers mounting a share as the library.
- **Synology DSM 6.** Container Manager and the DSM 7 package framework need DSM 7. Use DSM 7.2.
