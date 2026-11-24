# Umbrel App Store

`romarr/` is the app folder for [getumbrel/umbrel-apps](https://github.com/getumbrel/umbrel-apps):
`umbrel-app.yml` (manifest v1, port 6868, depends on Prowlarr, `STORAGE_DOWNLOADS`
permission) and `docker-compose.yml` (the official image pinned by digest behind
Umbrel's `app_proxy`).

- Umbrel's login sits in front of the UI; `/api/*` is whitelisted so API-key
  clients (Chrome launcher, Android app, other *arrs) still reach ROMarr's own
  authenticated API.
- Settings in the app's data dir; games filed into `Downloads/roms`, so they show
  in the Files app and over SMB; completed downloads read from Umbrel's shared
  Downloads storage.

## Validation

From a checkout of `umbrel-apps` with this folder copied to `romarr/`:

```bash
npm ci --ignore-scripts
node .tools/lint-apps.mjs --root . --check-images romarr   # "No issues found."
```

`--check-images` fetches the pinned digest from GHCR. As a control, a copy with a
wrong digest, a missing tagline and a broken `APP_HOST` produced 4 errors, so the
clean result is real.

## Submit

The [official store submission](https://github.com/getumbrel/umbrel-apps/pull/5947) uses `BlizzHacker/umbrel-apps:add-romarr`. Keep `releaseNotes` and `gallery` empty until the first submission is accepted.

1. Overwrite `romarr/umbrel-app.yml` and `romarr/docker-compose.yml` on that
   branch with these and add `romarr/data/config/.gitkeep`.
2. Retitle the PR `Add ROMarr` and refresh its body to their current template
   (Type: New app; App ID `romarr`; Version 1.10.0; lint result above). Umbrel
   creates the final gallery assets, so link screenshots and the logo instead:
   - logo: <https://cdn.jsdelivr.net/gh/selfhst/icons@main/svg/romarr.svg>
   - screenshots: `docs/img/library.png`, `interactive-search.png`, `collections.png`
     at <https://github.com/BlizzHacker/romarr/tree/v1.10.0/docs/img>
3. Comment once to ask for a review; the PR has had no maintainer response.
