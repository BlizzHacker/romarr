# Asustor ADM

The user guide is [docs/NAS.md](../../docs/NAS.md#asustor-adm).

| Folder | What |
|---|---|
| [`apk/`](apk/) | App Central package source: `CONTROL/` and `romarr.env.default` |
| [`portainer/`](portainer/) | Compose stack for Portainer CE |
| [`test/`](test/) | `adm-apk-smoke.sh`, which installs and runs the `.apk` the way ADM does |

## The APK

- **What it runs.** `architecture: any`, and it depends on `docker-ce`. `CONTROL/start-stop.sh` runs `ghcr.io/blizzhacker/romarr:<config.json version>`.
- **On install.** `post-install.sh` creates `/share/Docker/romarr` and writes `romarr.env` once. An edited file is never replaced.
- **On uninstall.** `pre-uninstall.sh` removes only the container.

### Build (2026-11-23)

Asustor's `apkg-tools.py` is all-rights-reserved, so it isn't in this repo. CI fetches it from a pinned commit and checks its SHA-256 (`.github/workflows/nas-packages.yml`).

```bash
python3 apkg-tools.py create packaging/asustor/apk --destination dist   # romarr_1.14.2_any.apk
```

The output is a zip of `apkg-version`, `control.tar.gz` and `data.tar.gz`, as ADM expects.

### Install test

```bash
PORT=16870 packaging/asustor/test/adm-apk-smoke.sh romarr_1.14.2_any.apk
```

The 2026-11-27 CI smoke run ended with `ADM APK SMOKE PASSED`:

1. **post-install** wrote `romarr.env`.
2. **Start** ran `ghcr.io/blizzhacker/romarr:1.14.2`. `/api/health` answered, and `/share/Media/Games` was created.
3. **Reinstall** kept an edited `romarr.env`.
4. **Reload** started it healthy again.
5. **Mistyped downloads.** A misspelled downloads path refused to start.
6. **pre-uninstall** removed the container and kept the settings.

ADM itself wasn't booted: it runs only on Asustor hardware. Shellcheck is clean.

### Distribution

- **App Central.** Submission is through Asustor's developer program, and Wade has to create the account.
- **Until then.** The `.apk` is attached to each GitHub release and installed with **App Central → Manual Install**.
