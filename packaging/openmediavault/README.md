# openmediavault 7

`docker-compose.yml` is for the openmediavault-compose plugin. Setup steps are in [docs/NAS.md](../../docs/NAS.md#openmediavault).

- **Required paths.** `ROMARR_CONFIG`, `ROMARR_ROMS` and `ROMARR_DOWNLOADS` come from the file's Environment box and use `${VAR:?}`, so an unset one stops the run.
- **Validation.** It's validated with `docker compose config`, and CI checks it.
- **Store.** openmediavault has no app store to submit to.
