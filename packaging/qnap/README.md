# QNAP QTS / QuTS hero

The user guide is [docs/NAS.md](../../docs/NAS.md#qnap-qts-and-quts-hero).

| Folder | What |
|---|---|
| [`container-station/`](container-station/) | Compose application for Container Station 3 |
| [`qpkg/romarr/`](qpkg/romarr/) | QDK project for `romarr_<version>.qpkg` |
| [`test/`](test/) | `qts-qpkg-smoke.sh`, which runs the QPKG's service script the way QTS does |

## The QPKG

The QPKG ships no Python. `shared/romarr.sh` runs `ghcr.io/blizzhacker/romarr:<QPKG version>` through Container Station's Docker. `QPKG_REQUIRE` is `container-station >= 3.0`, so App Center installs Container Station first.

- **Paths and ids** are in `romarr.env`. The script reads only known `KEY=value` lines and never executes the file. QDK keeps the file across upgrades because it's listed in `QPKG_CONFIG`.
- **Settings** live in `/share/Container/romarr`, so uninstalling never deletes them. The `remove` action deletes only the container.
- **Missing folders.** A missing library or downloads folder stops the start, and the reason is written to the system log.

### Build (2026-11-23, QDK `b7b5f4c`)

In `debian:12`, with QDK's `shared/` installed to `/usr/share/QDK` and `qpkg_encrypt` compiled from its `src/`:

```bash
cd packaging/qnap/qpkg/romarr && /usr/share/QDK/bin/qbuild   # build/romarr_1.13.2.qpkg, 29 KB
```

The package ends with QDK's `QNAPQPKG` checksum trailer.

### Install test

```bash
PORT=16869 packaging/qnap/test/qts-qpkg-smoke.sh romarr_1.13.2.qpkg
```

The script unpacks the QPKG the way QDK's installer does and stubs `/sbin/getcfg` and `/sbin/log_tool`. The 2026-11-23 run against Docker 20.10 ended with `QTS QPKG SMOKE PASSED`:

1. **Start.** It started `ghcr.io/blizzhacker/romarr:1.13.2` as uid 1000, and `/api/health` answered.
2. **Restart** reused the same container.
3. **Missing library.** A missing library folder refused to start.
4. **Remove** deleted the container and kept `/share/Container/romarr/romarr.json`.

QTS itself wasn't booted: it runs only on QNAP hardware. Shellcheck is clean.

### Distribution

- **QNAP App Center.** This needs a QNAP developer account and QNAP's code signing, which Wade has to set up.
- **Community store.** [MyQNAP.org](https://www.myqnap.org) needs a submitter account.
- **Until then.** The QPKG is attached to each GitHub release and installed with **App Center → Install Manually**.
