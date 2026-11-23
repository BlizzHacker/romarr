# Cloud storage and file-host accounts

**Settings → Cloud & File Hosts** connects ROMarr to your cloud storage and to premium file-host accounts. With it you can:

- **Back up** a library or platform folder to the cloud, on demand or every N hours.
- **Mirror** a folder, so files deleted locally are also removed from the cloud copy.
- **Offload** a folder: each file is uploaded and checked, then deleted locally to free disk.
- **Import** games *from* a cloud folder, or from a Google Drive share link, through the normal import. That import does DAT verification, renaming and platform folders. A scheduled import watches a folder and only takes files it hasn't taken before.
- **Back up ROMarr's settings**, with every credential masked, keeping the newest N copies.
- **Mount** a cloud folder as a local folder, so it can be a library root.
- **Share** a file or folder with a public link, where the provider supports it.
- **Download from Rapidgator and 1fichier** with your own premium account. **MultiUp** links are split into their mirrors automatically.

## Two engines

| Engine | Providers | Needs |
|---|---|---|
| **rclone** (recommended) | Every provider rclone supports, 70+ including Google Drive, OneDrive, Dropbox, MEGA, Box, pCloud, Amazon S3, Backblaze B2, Wasabi, Cloudflare R2, MinIO, iCloud Drive, Proton Drive, Yandex, Koofr, Mail.ru, Filen, Internxt, put.io, Premiumize, pixeldrain, Gofile, 1fichier, WebDAV/Nextcloud, SFTP, SMB, FTP, Azure Blob and Google Cloud Storage. `crypt` adds client-side encryption on top of any of them. | The `rclone` program on the ROMarr host |
| **Built in** | Google Drive and OneDrive | Your own OAuth app (see [below](#your-own-oauth-app)); nothing to install |

ROMarr drives rclone through its remote-control API (`rclone rcd`), bound to `127.0.0.1` on a random port with a random password. ROMarr uses its own `rclone.conf`, never your `~/.config/rclone`, so remotes you created by hand for something else are left alone.

### Installing rclone

- **Docker image:** included, together with `fuse3`.
- **Debian, Ubuntu or a Proxmox container:** `apt install rclone fuse3`. If your release's package is old, use rclone's own installer: `curl https://rclone.org/install.sh | bash`.
- **Windows:** `winget install Rclone.Rclone`. ROMarr finds it on `PATH`.
- **Anywhere else:** download it from <https://rclone.org/downloads/>, then either set `ROMARR_RCLONE=/path/to/rclone` or fill in **rclone location** on the page.

The page shows the rclone version it found, and whether FUSE mounts are possible on this host.

## Connecting an account

1. Open **Settings → Cloud & File Hosts → Add a connection**.
2. Pick the provider, give the connection a short **name** (letters, digits, `-`, `_`) and a **label**.
3. Continue, depending on the provider:
   - **Browser sign-in** (Google Drive, OneDrive, Dropbox, Box, pCloud, Yandex, Zoho, put.io and others): press **Sign in**, then follow [the steps below](#browser-sign-in-on-a-headless-server).
   - **Everything else** (MEGA, S3, B2, WebDAV, SFTP, SMB and so on): fill in the fields, then press **Save connection**. The fields come from rclone itself. Tick **Show advanced options** for the rest. Passwords are obscured by rclone before they are written.
4. Press **Check** in the Space column to confirm the account answers and see how much space is left.

### Browser sign-in on a headless server

ROMarr usually runs on a server with no browser, and the OAuth providers send the browser back to `127.0.0.1`. ROMarr handles that with a paste-back step:

1. Press **Sign in**. The provider's consent page opens in a new tab.
2. Sign in and approve. **ROMarr never sees your password.**
3. Your browser now tries to open an address starting with `http://127.0.0.1:53682/?state=…&code=…` (Microsoft: `http://localhost:53682/…`), and shows "This site can't be reached". That's expected.
4. Copy that **whole address** from the address bar. Back in ROMarr, press **Paste from clipboard**, or paste it into the box and press **Finish sign-in**. You have 10 minutes; the page counts down.

ROMarr checks that the address belongs to the sign-in it started (the `state`) and hands it to rclone's sign-in listener on the server. rclone then exchanges it for a token, which goes straight into ROMarr's `rclone.conf`. Only one sign-in can be waiting at a time, and it expires after 10 minutes.

### Google Drive: rclone's shared app is being retired

rclone ships a shared Google app, and Google is retiring it during 2026. Until it stops working, Google Drive signs in with no setup. For something that keeps working, create your own app ([below](#your-own-oauth-app)), save it under **Your own OAuth apps**, then tick **Use my own OAuth app** when you sign in. Your own app also gets its own API quota rather than sharing rclone's with everyone else.

## Your own OAuth app

You need your own app for the built-in engine, and it's optional (but recommended for Google) with rclone. Either way you register two redirect addresses. The page shows both:

- The paste-back address, which works anywhere: `http://127.0.0.1:53682/` for Google, `http://localhost:53682/` for Microsoft.
- `https://<your ROMarr>/api/v1/cloud/oauth/callback`, for the built-in engine's direct return. It only works if your browser can reach ROMarr at that address.

**Google (Drive):**

1. Go to <https://console.cloud.google.com/>, create a project, then enable the **Google Drive API**.
2. Open **Google Auth Platform → Branding** and fill in the app name and your e-mail.
3. Under **Audience**, choose **External**. Then press **Publish app**, so the status reads **In production**. Unverified apps work for your own account; Google shows an "unverified app" warning you can click through. Leave it in *Testing* and Google expires your sign-in every 7 days.
4. Under **Clients → Create client**, choose **Desktop app** (for paste-back). Choose **Web application** instead if you want the callback, and add the callback address under *Authorized redirect URIs*. A Desktop app accepts `http://127.0.0.1:53682/` with no extra step.
5. Copy the **client ID** and **client secret** into **Your own OAuth apps → Google**, then press **Save**.

**Microsoft (OneDrive, personal or work):**

1. Go to <https://entra.microsoft.com/> → **App registrations → New registration**.
2. Under *Supported account types*, choose **Accounts in any organizational directory and personal Microsoft accounts**.
3. Set the platform to **Mobile and desktop applications** with the redirect `http://localhost:53682/`. Then add a **Web** redirect with the callback address if you want the direct return.
4. Under **API permissions**, add Microsoft Graph delegated permissions: `Files.ReadWrite.All`, `offline_access` and `User.Read`.
5. A public client needs no secret. For a Web redirect, create one under **Certificates & secrets**.
6. Copy the **Application (client) ID**, and the secret if you made one, into **Your own OAuth apps → Microsoft**.

## Jobs

**Add a job** asks for the kind, a connection, a cloud folder and, depending on the kind, a local folder or a platform. Set **Run every** to 0 for manual-only, or to a number of hours. **Run now** starts a job straight away, and the runs table shows its progress.

| Kind | What it does | Deletes anything? |
|---|---|---|
| Back up | Copies new and changed files to the cloud | No |
| Mirror | Same as back up, and removes cloud files that are gone locally | Cloud copies only |
| Offload | Uploads each file, checks it, then deletes it locally. Empty sub-folders are removed; the folder itself is kept | **Local files**, after a checked upload. You must confirm when you create it |
| Import | Downloads new files and imports them like Manual Import | No |
| Settings backup | Writes `romarr-settings-<time>.json` with all credentials masked, keeping the newest N | Its own older backups |

**Offload and Mirror need an rclone connection.** They delete files, and rclone checks each one by checksum before anything is deleted. The built-in Google Drive and OneDrive clients do backup, import and settings backup only.

Local folders are limited to the library and download folders ROMarr already knows. Offload is limited to library folders, and ROMarr's own data folder can never be offloaded or mirrored.

Imports are staged in `<library>/.romarr-cloud-import/` (the same disk as the library, so the final move is cheap), then removed. A file the import refuses, such as a bad dump or one already in the library, is reported in the run's result.

## Import from a Google Drive share link

Paste a link like `https://drive.google.com/file/d/<id>/view` or `https://drive.google.com/drive/folders/<id>` under **Browse and import**, pick a Google Drive connection, then press **Import share link**.

- **rclone engine:** the file or folder is copied server-side into `ROMarr import/` on *your* Drive. It's then downloaded, imported and the temporary copy deleted. Because the copy is made under your account, a public file whose download quota is used up still comes through.
- **Built-in engine:** the files are downloaded directly with your token.

## Mounts

A mount shows a cloud folder as a local folder, for example `/mnt/cloud-roms`, which you can then add as a library under **Settings → Libraries**.

- It needs rclone and FUSE. In a **Proxmox container**, enable *Options → Features → FUSE* and restart the container. In **Docker**, add `--device /dev/fuse --cap-add SYS_ADMIN` (on some hosts also `--security-opt apparmor:unconfined`).
- Mount folders must be empty, and must sit under `/mnt`, `/media`, `/srv`, a library or download folder, or a folder listed in `ROMARR_MOUNT_ROOTS`.
- **Cache `full`** is best for games. Files are cached on local disk when opened, so emulators can seek in them. The cache lives in `rclone-cache/` beside `romarr.json`. If that disk is small, set `ROMARR_RCLONE_CACHE` to a folder on a bigger one. **Cache limit** (10 GB by default) is a target rather than a hard cap: rclone evicts the oldest unused files when it is over, so files open at that moment can push it above the limit for a while.
- **Let other users read it** is needed when another program, such as RomM or a file server, reads the mount as a different user. It also needs `user_allow_other` in `/etc/fuse.conf`.
- Mounts marked *Mount when ROMarr starts* come back after a restart. They are unmounted when ROMarr stops.

Scanning a big library over a mount is slow, because every folder listing is a request to the provider. Mounts suit a cold archive. For a library you browse all the time, offload to the cloud and keep the mount as the place to get files back.

## File-host accounts

When a release links to a file host, ROMarr tries, for each link and its mirrors:

1. The host's public API, where it has one (pixeldrain).
2. A direct file link.
3. **Your premium account on that host:** Rapidgator (e-mail, password, and the 2FA code if the account has 2FA) or 1fichier (the account's API key; premium only, by 1fichier's rules).
4. A debrid service from **Download Clients**: Real-Debrid, AllDebrid, Premiumize, TorBox, Debrid-Link, Offcloud, put.io or Linksnappy.

**MultiUp** links stand for copies of one file on several hosts. ROMarr asks MultiUp's public API whether the file still exists, reads its mirror list, then tries each mirror with the routes above. No MultiUp account is needed.

The account routes give the file link to your direct download client (Mister Download Manager or Direct HTTP), so one of those must be set up.

ROMarr never solves captchas or waits out free-user countdowns.

## Where credentials live

- OAuth tokens for rclone connections are in `rclone.conf`, beside `romarr.json`, with mode `0600`.
- Built-in tokens, your OAuth app secrets and file-host passwords and keys are in `cloud-secrets.json`, also beside `romarr.json` and mode `0600`.
- On Windows, mode `0600` has no meaning; the two files rely on the folder's own permissions, so keep ROMarr's data folder in your user profile (the default) rather than a shared location.
- None of these are in `romarr.json`, so the settings API, the settings backup job and support bundles never carry them. No API route returns them. Forms show "Saved — leave blank to keep it".
- Nothing secret goes on a command line. rclone's control password is passed in its environment, and tokens go in request bodies to it.
- Back up these two files yourself if you want your connections to survive a reinstall. Treat them like passwords.

## API

Everything on the page is available under `/api/v1/cloud` with your API key. See the API reference (`/api/v1/openapi.json`) for the full list. Common calls:

```bash
# overview
curl -H "X-Api-Key: $KEY" https://romarr.example/api/v1/cloud
# list a folder
curl -H "X-Api-Key: $KEY" "https://romarr.example/api/v1/cloud/browse?remote=gdrive&path=ROMs"
# run a job
curl -H "X-Api-Key: $KEY" -H 'Content-Type: application/json' \
     -d '{"id":"j-1234abcd"}' https://romarr.example/api/v1/cloud/jobs/run
```

## Troubleshooting

| Message | Meaning and fix |
|---|---|
| *rclone is not installed* | Install rclone ([above](#installing-rclone)), or set its location on the page. |
| *rclone did not start a sign-in (is port 53682 in use?)* | Another program, or another sign-in, holds port 53682 on the server. Cancel the other sign-in, or stop the program. |
| *that address belongs to a different sign-in* | You pasted an address from an older attempt. Press **Sign in** again and paste the newest one. |
| Google says *Access blocked* / *invalid_client* | The shared rclone app has been retired. Use your own app. |
| Google sign-in stops working after 7 days | Your app's publishing status is *Testing*. Publish it (*In production*). |
| *This connection needs signing in again* | The built-in token or the app was removed. Remove and add the connection again. |
| */dev/fuse is missing* | Enable FUSE for the container ([Mounts](#mounts)). |
| A Rapidgator grab is refused | The account isn't premium or its traffic is used up. The page reports which when you press **Save & test**. |
