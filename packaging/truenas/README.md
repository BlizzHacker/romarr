# TrueNAS SCALE (Community train)

`ix-dev/community/romarr/` is a complete app for the official
[truenas/apps](https://github.com/truenas/apps) catalog, built on their shared
library (`lib_version 2.3.19`) and the official image
`ghcr.io/blizzhacker/romarr:1.13.2`, pinned by its multi-arch digest.

- Runs as the `apps` user (568:568) by default; a permissions init container
  fixes ownership of the config and library datasets first.
- Storage: **Config** (`/config`), **Game Library** (`/roms`, where games are
  filed) and any number of additional mounts (for example the download client's
  completed folder at the path it reports).
- Default Web UI port **30686** (TrueNAS keeps community-app defaults in the
  30000 range; 30686 is unused in the catalog). The container listens on the
  same port through `ROMARR_PORT`.

**TrueCharts is not a route.** TrueCharts stopped supporting TrueNAS SCALE in
2024 and now targets plain Kubernetes clusters; the official Community train
above is where SCALE users install apps from.

## Validation (2026-11-15)

Run from a checkout of `truenas/apps` with this folder copied to
`ix-dev/community/romarr`:

```bash
python3 .github/scripts/port_validation.py                       # passed
python3 .github/scripts/check_metadata.py --train community --app romarr   # up to date
python3 .github/scripts/ci.py --train community --app romarr --test-file basic-values.yaml
```

`ci.py` rendered the compose with their `apps_validation` image and brought it
up on Docker 27: the ROMarr container reported `healthy`, logged
`running as uid 568`, and answered `/api/health`. (Docker Engine 25 or newer is
required to run their rendered compose, because it uses `healthcheck.start_interval`.)

## Install it today, before the catalog accepts it

TrueNAS 24.10+ → **Apps → Discover Apps → ⋮ → Install via YAML**, paste
[`custom-app.yaml`](custom-app.yaml) and adjust the two host paths.

## Submit to the catalog

1. Fork `truenas/apps`, copy `ix-dev/community/romarr/` into the same path.
2. Open a PR titled `Add romarr to community train`. Their CI runs the same
   three scripts plus a full render/run on every test-values file.
3. In the PR description, link an icon and a screenshot for iX to host on
   `media.sys.truenas.net` (the `icon:`/`screenshots:` URLs in `app.yaml`
   already point where they will put them):
   - icon: <https://cdn.jsdelivr.net/gh/selfhst/icons@main/png/romarr.png>
   - screenshot: <https://raw.githubusercontent.com/BlizzHacker/romarr/v1.10.0/docs/img/library.png>
4. New ROMarr releases are picked up by their Renovate bot from the image tag.
