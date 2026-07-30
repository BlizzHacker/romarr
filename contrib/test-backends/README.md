# Test backends

Throwaway Gaseous and Retrom servers for exercising `romarr/libraries.py`.

`tests/test_libraries.py` is mocked, and should stay that way — it is fast, it
runs in CI, and it pins the request shapes a real server was observed to want.
This directory covers the job a mock cannot do: noticing that the server
changed its mind.

Reach for it when you touch a backend in `romarr/libraries.py`, or when you
want to re-test against a newer Gaseous or Retrom release.

## Run it

```bash
docker compose -f contrib/test-backends/docker-compose.yml up -d
python contrib/test-backends/verify.py                      # fixtures local
python contrib/test-backends/verify.py --host 192.168.0.94  # fixtures elsewhere
docker compose -f contrib/test-backends/docker-compose.yml down -v
```

`verify.py` asks both backends the four questions Romarr asks — is it up, how
many games, which games, please rescan — through Romarr's own `build_library`,
so there is no second copy of the request shapes to drift out of step. A fresh
fixture holds no games, so a count of zero is a pass; what is under test is
that every call is accepted and decodes.

Gaseous listens on 5198 and Retrom on 5101, chosen to miss RomM's 8080 so this
can run alongside a real library.

## Gaseous ships with no account, and that is not optional

An unconfigured Gaseous has an empty `Users` table, no seeded admin, and no
registration endpoint. Until an account exists, every call 302s to
`/Identity/Account/Login`, which then 404s under `/api` — so the failure
arrives looking like a wrong path rather than a missing session.

`verify.py` handles this by POSTing to `/api/v1.1/FirstSetup/0`, the
unauthenticated bootstrap the web UI's own first-run wizard uses:

```bash
curl -X POST http://localhost:5198/api/v1.1/FirstSetup/0 \
  -H 'Content-Type: application/json' \
  -d '{"userName":"romarr@example.com","email":"romarr@example.com","password":"Romarr-Test-1","confirmPassword":"Romarr-Test-1"}'
```

The password must be at least 10 characters. The route disappears once a user
exists, so it is safe to call on every run — a 404 means the fixture is already
set up. Those credentials are throwaway, and `verify.py` hardcodes them so the
fixture needs no `.env` at all.

This is the part that a compose file alone cannot capture, and the reason the
first attempt at recreating these instances failed.

## Images are pinned by digest

The digests in `docker-compose.yml` are the exact builds the Gaseous and Retrom
backends were corrected against on 2026-07-29. Floating on `:latest` would let
the fixture quietly stop matching the behaviour `tests/test_libraries.py`
asserts, which is the one thing it exists to detect. Bumping a digest is how
you deliberately re-test against a newer release.

Worth knowing before you bump:

* **Gaseous `:latest` is a year stale.** It resolves to a build from
  2025-07-31, reporting version 1.7.14.0 — that is what the backend was written
  against. `v2.0.0-rc.3` is far newer and has never been checked against
  Romarr.
* **Retrom `:latest`** was built 2026-06-14. Its grpc-web framing and protobuf
  field numbers are what `RetromLibrary` decodes.
* The Postgres and MariaDB digests are incidental and safe to bump.

IGDB credentials are optional. Gaseous starts, reports healthy, and answers all
four of Romarr's questions with `igdbclientid` and `igdbclientsecret` empty —
Romarr never reads IGDB. It will fill its log with Twitch OAuth 400s while
trying to enrich platform metadata, which is noise, not failure. Set them in
`.env` (see `.env.example`) only if you are specifically testing Gaseous
metadata.

## Findings from the last run — 2026-07-29

Recreated from empty volumes and checked end to end against the working tree of
that day, which carried in-flight multi-library work on top of 0.3.0. Retrom
passes every question. Gaseous passes reachability, count and listing, with two
open observations:

* **`rescan` returns False on Gaseous 1.7.14.** `GaseousLibrary.rescan` posts
  to `/api/v1.1/ContentManager/Rescan`, which 404s. That path is absent from
  the build's own OpenAPI document, and so is any other scan trigger — 1.7.14
  seems to scan only on its background-task schedule. Romarr treats rescan as a
  courtesy call by design, so an import still succeeds and is reported as such;
  the call is simply a no-op here. It may exist on 2.x, which would be a reason
  to bump the digest.
* **Cover URLs are unverified.** `GaseousLibrary.games` builds
  `/api/v1.1/Games/{id}/cover/image`, but the OpenAPI document lists only
  `/cover` and `/cover/image/{size}`. An empty fixture has no covers to fetch,
  so proving this needs a ROM imported first.

## Do not leave it running

`restart: "no"` is deliberate. An earlier copy of this file used
`unless-stopped`, and four "temporary" containers survived every reboot for a
day before anyone noticed. `down -v` removes the volumes too; the images are
another ~12 GB and are worth dropping as well if you are done:

```bash
docker rmi ghcr.io/gaseous-project/gaseousserver:latest ghcr.io/jmberesford/retrom-service:latest
```
