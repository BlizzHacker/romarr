# Homebrew (macOS and Linux)

`Formula/romarr.rb` builds ROMarr from the release tarball into a private
Python 3.14 virtualenv (PyPI sdists for `requests`, `urllib3`, `idna`,
`charset-normalizer`; Homebrew's own `certifi`; `libarchive` on Linux for
`.7z`/`.rar`). It installs a `romarr` command and a `brew services` definition.

```bash
brew install romarr          # once it is in a tap or homebrew-core
romarr                       # http://127.0.0.1:6868, first visit sets the password
brew services start romarr   # keep it running at login
```

Defaults chosen for a laptop rather than a server:

- Binds **127.0.0.1** until you set `ROMARR_HOST=0.0.0.0`, because a fresh install
  is unclaimed until someone sets its password.
- State in `~/Library/Application Support/ROMarr` (macOS) or `~/.local/share/romarr`
  (Linux); games filed into `~/Games/ROMs`, or `ROMARR_LIBRARY` if set on first start.
  An inherited `LIBRARY_PATH` (often the linker's search path on a Mac) is ignored.
- ROMarr's self-updater stays off (no git checkout), so `brew upgrade` owns updates.

There is no macOS binary in the GitHub release, so the formula builds from source;
it is pure Python, so it needs no compiler.

## Validation (2026-11-15)

In the `homebrew/brew` container after `brew update`, with the formula in a local tap:

```bash
brew style local/romarr/romarr                        # no offenses
brew audit --strict --new --online local/romarr/romarr   # no problems
brew install --build-from-source local/romarr/romarr     # installed
brew test local/romarr/romarr                            # server up, /api/health ok
```

The installed tree reported version `1.10.0`. Linux only; macOS was not available
to test, and the formula has no macOS-specific code paths beyond the state directory.

## Submit

**Recommended first: a tap you control** (no review queue):

1. Create the public repo `BlizzHacker/homebrew-romarr` with this file at
   `Formula/romarr.rb`.
2. Users run `brew install blizzhacker/romarr/romarr`.
3. On each release, update `url` and `sha256`
   (`brew bump-formula-pr --url=<tarball> blizzhacker/romarr/romarr` works on taps).

**homebrew-core** (the plain `brew install romarr`): ROMarr clears the notability bar
for a self-submission (371 stars on 2026-11-15; the threshold is 225). Fork
`Homebrew/homebrew-core`, add `Formula/r/romarr.rb`, run the four commands above
against it, and open the PR. Expect maintainers to ask for a `pyproject.toml` so the
formula can use `virtualenv_install_with_resources` instead of copying the package
by hand; adding one to ROMarr first would make the PR smoother.
