# TerraMaster TOS 6

`docker-compose.yml` is a Docker Manager project. Setup steps are in [docs/NAS.md](../../docs/NAS.md#terramaster-tos-6).

- **Validation.** It's validated with `docker compose config`, and CI checks it (`.github/workflows/nas-packages.yml`). The library and downloads binds use `create_host_path: false`, so a mistyped path fails instead of creating an empty folder.
- **Store.** TerraMaster's App Center takes third-party apps only from registered developers, so there's no self-submission.
