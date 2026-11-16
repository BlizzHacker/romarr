# CasaOS / ZimaOS AppStore

`Apps/ROMarr/` is a one-click app for the official
[IceWhaleTech/CasaOS-AppStore](https://github.com/IceWhaleTech/CasaOS-AppStore)
(which also feeds the ZimaOS store): a `docker-compose.yml` with the top-level
`x-casaos` metadata block (store id `app.romarr.romarr`, category `Media`,
port 6868), plus the icon, thumbnail and three screenshots the build pipeline
turns into store assets.

- Config in `/DATA/AppData/romarr/config`, games filed into `/DATA/Media/Games`,
  completed downloads read from `/DATA/Downloads`.
- Starts as root only long enough to fix `/config` ownership, then drops to
  `PUID:PGID` (1000:1000 by default).

## Validation (2026-11-15)

From a checkout of the AppStore repo with this folder copied to `Apps/ROMarr`:

```bash
python3 .github/actions/validate-compose/scripts/validate_compose.py \
  --app-path ROMarr --report-json out/report.json      # status success, 0 issues
./scripts/build_dist.sh                                  # "OK app.romarr.romarr"
```

The compose was also brought up for real (with `/DATA` remapped to a temp dir):
`ROMarr: starting as 1000:1000`, healthy, `/api/health` answered, and
`romarr.json` was written owned by 1000:1000.

## Submit

1. Fork `IceWhaleTech/CasaOS-AppStore`, copy `Apps/ROMarr/` into `Apps/`.
2. Open a PR titled `feat(appstore): add ROMarr 1.10.0` (their convention) and
   say it is a new app, validated with the two commands above. Third-party app
   PRs are being merged there (Nametag and Upload Assistant in November 2026).
3. Updates: bump `image`, `x-casaos.version`, `update_at` and `release_notes`
   in one PR titled `chore(romarr): update to <version>`.

Alternative door: BigBear's universal store (one `app.json` that BigBear converts
for CasaOS, Umbrel, Cosmos, Runtipi, Portainer and Dockge). Their own CasaOS store
does not accept outside PRs.
