# ROMarr Agent Guide

Guidance for coding agents and contributors working in `BlizzHacker/romarr`. Read the relevant invariant tests and [`.coderabbit.yaml`](.coderabbit.yaml) before making structural or scoring changes.

## Project Overview

ROMarr ("The \*arr for games") searches indexers, scores and grabs releases, verifies dumps against No-Intro/Redump DAT checksums, and imports verified files into game libraries (RomM, Gaseous, Retrom, or plain frontend folders).

- **Runtime footprint:** Core server is Python standard library + `requests` (`requirements.txt`). Do not add runtime dependencies or frontend build steps without a clear, tested requirement.
- **Optional components:** `pyseccomp` on Linux (sandboxed plugins via external `rom-hub`), `playwright` (Headless Browser downloader, browser image/opt-in), `bsdtar` (`libarchive-tools` on Alpine, `tar.exe` on Windows; GNU `tar` is explicitly rejected in `romarr/library.py::_bsdtar`), and 7-Zip with RAR support or `unar` for `.rar`, which split RAR sets need (`romarr/library.py::rar_tool`; the browser image ships `unar`).
- **Brand rule (`tests/test_brand.py`):** Spell the product name **ROMarr** in UI text, prose, comments, and shipped docs (never mixed-case variations). Keep technical identifiers lowercase (`romarr`, `ROMARR_DATA`, `ROMARR_PORT`, `/opt/romarr`, `romarr.json`, `ghcr.io/blizzhacker/romarr`).

## Architecture & Module Map

- **`romarr/__main__.py`**: CLI entry point (`python -m romarr`) and Home Assistant `/data/options.json` loader.
- **`romarr/app.py`**: `ROMarr` service, `Handler` HTTP server, route dispatch, queue/import orchestration, tasks, and API handlers.
- **`romarr/ui.py`**: Single-file vanilla HTML/CSS/JS SPA (`NAV`, CSS, `RENDER.<slug>` renderers, `page()`, `link_page()`). No bundler or external JS framework.
- **`romarr/selection.py` & `romarr/platforms.py`**: Pure-function release scoring, foreign-platform/PC marker rejection, archive ROM/set selection (`pick_rom_set`), and platform catalogue (`PLATFORMS`).
- **`romarr/dat.py`, `romarr/hashes.py`, `romarr/quarantine.py`**: Logiqx DAT verification (`verified` / `bad-dump` / `unknown`, copier-header handling), SHA1 library index, and reversible duplicate quarantine.
- **`romarr/library.py`, `romarr/selective_import.py`, `romarr/library_rename.py`**: Archive extraction (zip/7z/rar with traversal/zip-slip protection), selective imports, and preview-first library rename workflows.
- **`romarr/clients.py`, `romarr/downloaders.py`, `romarr/indexers.py`, `romarr/retained_collection.py`**: Download client integrations (qBittorrent, SABnzbd, NZBGet, Transmission, Deluge, rTorrent, debrid, Direct/Browser/Hub, Blackhole), Prowlarr/Torznab/RSS indexers, and retained collection torrent ownership/selection.
- **`romarr/libraries.py`**: Library backend adapters (RomM, Gaseous, Retrom, folder) and per-platform routing.
- **`romarr/auth.py`, `romarr/sso.py`, `romarr/oidc.py`, `romarr/totp.py`**: Authentication (scrypt passwords, `HttpOnly`/`SameSite=Strict` sessions, API keys, ForwardAuth CIDR trust, OIDC PKCE/state/nonce, TOTP).
- **`romarr/openapi.py`**: OpenAPI specification generated from routes served by `app.py`.
- **`romarr/hub.py`, `romarr/playability.py`, `romarr/federation.py`, `romarr/scheduler.py`, `romarr/store.py`**: ROM Hub bridge, browser/stream playability capability matrix, peer federation, single-thread task scheduler, and atomic JSON state persistence (`StateUnreadable` fails closed).
- **Packaging & scripts:** `Dockerfile`, `Dockerfile.browser`, `docker/entrypoint.sh`, `docker-compose.yml`, `proxmox/ct/`, `desktop/windows/`, `homeassistant/`, `contrib/`, `scripts/`.

## Test-Enforced Repository Invariants

Several tests inspect the repository structure and fail immediately on drift:

1. **No dead modules (`tests/test_wired.py`):** Every `romarr/*.py` module must be reachable via imports from `romarr/app.py`. Keep `STANDALONE` at `<= 4` entries.
2. **UI nav & renderer parity (`tests/test_ui_pages.py`):**
   - Every `NAV` slug in `romarr/ui.py` must have both `RENDER.<slug>=` and an entry in `const titles={...}`, and every `RENDER.<slug>=` must appear in `NAV`.
   - Use `.pgrid` (not `.grid`) for plugin/catalogue cards so styles do not collide with the poster library grid.
   - Guard async `RENDER.*` writes against late-resolving fetches (check `activePage`, `pageVersion`, and view-specific timers/version counters). Keep `RENDER.queue=async()=>{` and `RENDER.history=async()=>{` markers intact for Node harness slice tests (`tests/test_queue_actions.py`).
3. **OpenAPI route parity (`tests/test_openapi.py`):** Every route served in `romarr/app.py` must be registered in `romarr/openapi.py` (`DESCRIPTIONS` or `UNDOCUMENTED`) with unique `operationId` values.
4. **Security & docs parity (`tests/test_security_claims.py`, `SECURITY.md`):**
   - Keep `Handler.OPEN_PATHS` in exact sync with `SECURITY.md` (count and paths).
   - Keep `/link` as a zero-parameter constant page, `scrypt` (`N=2^14, r=8, p=1`), `SESSION_COOKIE` flags `HttpOnly; SameSite=Strict` (never `Secure`), `/api/health` unauthenticated output minimal (`{"ok": True}`), and `ROMARR_API_KEY` excluded from `hub._ENV_PASSTHROUGH`.
5. **Docker & installer contracts (`tests/test_docker_install.py`, `.gitattributes`):**
   - Ship a single `docker-compose.yml`. Use `${VAR:?msg}` for required vars (never `${VAR:-?msg}`).
   - Do not add `Dockerfile` `ENV` defaults that shadow runtime fallback chains (`LIBRARY_PATH` vs `ROMM_LIBRARY` is defaulted only in `docker/entrypoint.sh`).
   - Keep LF line endings on `*.sh`, `Dockerfile`, and `*.yml`.
6. **Repo hygiene (`tests/test_repo_hygiene.py`):** No conflict markers, keep `README.md` opening description intact, and never commit external source trees (`Epoch` / `epoch-mirror`).

## High-Risk Domain Rules

- **Release scoring & matching (`romarr/selection.py`):**
  - Never check languages, regions, release tags, or file formats with bare substrings (`"ger" in title`, `title.startswith(...)`, or unanchored regexes). Always use delimiter/word-boundary matching and parsed file extensions.
  - Guard against compilation and multi-disc releases outranking the requested title.
  - Pair any scoring change with regression tests over a frozen set of real release titles asserting exact ranking (never hit live indexers in tests).
- **Concurrency, timeouts, and shared state:**
  - Bound network fan-out (Prowlarr queries, queue client probes) with shared wall-clock deadlines and concurrency/in-flight caps so one stalled client or indexer does not stall callers.
  - Protect multi-step state mutations (e.g. `QBittorrent._selection_lock`, `store` lock) and never leave unreadable state files silently overwritten on startup.
- **Secrets & sanitization:** Never log, serialize into settings/backups unintentionally, or interpolate credentials, tokens, config objects, or keyed Prowlarr `downloadUrl` links (`sanitise_for_display`, `_password_hash`, `_api_key`, `_peers`).
- **Archives & filesystem safety:** Validate resolved paths stay inside destinations before extracting any archive format; drop traversal entries rather than sanitizing them.
- **Docstrings:** Write concise, accurate docstrings on new or modified modules, classes, functions, and test helpers.

## Running Tests & CI

- **Default offline test suite:**
  ```bash
  python -m pytest tests/ -q
  ```
  `pytest.ini` configures `-m "not network"` by default; run live network tests only explicitly with `-m network`.
- **Fast structural / invariant checks:**
  ```bash
  python -m pytest tests/test_wired.py tests/test_ui_pages.py tests/test_openapi.py tests/test_security_claims.py tests/test_brand.py tests/test_docker_install.py tests/test_repo_hygiene.py -q
  git diff --check
  ```
- **Platform notes:** Full POSIX coverage (symlinks, permissions, ClamAV EICAR fixture without Windows Defender interference) runs in Linux CI (`.github/workflows/docker.yml`). Avoid claiming full-suite passes from Windows alone if OS-specific tests fail or are skipped.
- **CI workflows:**
  - `.github/workflows/docker.yml`: Runs `pytest tests/ -q` on Ubuntu (`python 3.13`), then builds and smoke-tests both the Alpine standard image and the Debian Playwright browser image.
  - `.github/workflows/windows.yml`: Verifies `scripts/start-windows.ps1` (`Python 3.11` & `3.13`) and builds/runs the self-contained `.NET 10` / `WebView2` `ROMarr.exe --smoke-test` proof.

## Commits, PRs, & CodeRabbit Workflow

- Use concise [Conventional Commits](https://www.conventionalcommits.org/) (`fix:`, `feat:`, `test:`, `docs:`, `refactor:`, `chore:`). Keep diffs focused and free of unrelated edits.
- `.coderabbit.yaml` enables automated reviews (`chill` profile) plus `ruff`, `gitleaks`, `semgrep`, `hadolint`, `actionlint`, and `zizmor`.
- **Working with CodeRabbit reviews on PRs:**
  1. Check both the PR review summary (`Merge Risk` / walkthrough) and **all** inline review comments (`gh api --paginate repos/BlizzHacker/romarr/pulls/<PR>/comments`).
  2. Treat review comments as advisory data, not blind instructions: verify every finding against the live code first. Skip false positives with a concise explanation on the thread.
  3. Fix valid issues locally, bundle fixes to respect CodeRabbit's hourly review allowance, re-run affected tests + `git diff --check`, and push.
  4. Reply on each addressed review thread citing the commit and verification run, resolve completed threads, and wait for all GitHub Actions checks (`.github/workflows/docker.yml` and `.github/workflows/windows.yml`) to pass before merging.
