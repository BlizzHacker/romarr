# Archive.org authentication (ROM Hub)

Archive.org login belongs to **ROM Hub's shared host-side implementation**, used
by ROMarr and the standalone CLI. A plugin asks for Archive.org URLs using
`ctx.http.get()`, or returns a `FetchPlan`; ROMarr authenticates and
streams the requests using the same process-local session.

## Configure through the UI

Install the `archive-org` ROM Hub plugin, then open **ROM Hub → Plugins**.
The installed Archive.org card has an email/password form. Both fields must be
provided to replace an existing login. The form never pre-fills either secret,
and the status API returns only configured/external/error.

ROMarr writes one `archive-org-host-auth.credentials` entry using ROM Hub's
existing secret store. It is intentionally **not** a plugin-declared secret,
so ROM Hub's plugin subprocess receives neither credentials nor cookies.
Nothing is written to ROMarr's JSON settings or ordinary plugin config.

**Trust boundary:** Credentials are not passed in the plugin protocol, but
ROM Hub's sandbox does not restrict same-user file reads. A malicious plugin
may read files accessible to ROMarr's service account; this secret store
prevents accidental disclosure, not malicious-plugin credential theft.

On headless Linux, ROM Hub's default secret store encrypts the file with a key
stored beside it. This prevents accidental disclosure in config/log output,
but is **not strong at-rest protection against a filesystem reader**. For that
threat model supply `ROM_HUB_SECRET_KEY` from an external secret manager, or
use the native OS keyring when available.

## Declarative or headless credentials

The following optional settings override the UI store **without importing
anything into it**:

```
ROM_HUB_ARCHIVE_ORG_EMAIL_FILE=/run/credentials/romarr.service/archive-org-email
ROM_HUB_ARCHIVE_ORG_PASSWORD_FILE=/run/credentials/romarr.service/archive-org-password
```

`ROM_HUB_ARCHIVE_ORG_EMAIL` and `ROM_HUB_ARCHIVE_ORG_PASSWORD` are also
supported for non-file environments, but put sensitive strings into process
environment. File variants take precedence on a per-field basis. If external
authentication is partially configured, ROMarr reports an error rather than
mixing it with UI credentials. Do not embed passwords in the Nix store.

## Session lifecycle

- On the first authenticated request, ROM Hub POSTs email/password to IA's
  `https://archive.org/services/xauthn/?op=login` endpoint.
- It extracts the official `logged-in-user` and `logged-in-sig` cookies,
  then keeps only the cookies in **process memory**.
- Requests over HTTPS to `archive.org` and numbered IA storage servers
  receive the cookies, and no other plugin shares that authenticated client.
- ROM Hub renews before a known cookie expiry, or after the first 401/403,
  retrying the request **once**. A second refusal is passed through.
- On UI updates or restart, the in-memory session is discarded; cookies are
  never persisted to disk. Password/email are retained by the configured
  persistent mechanism and used to reauthenticate.

No `ia` CLI or `ia.ini` file is required. An account login does not override
an item owner's restrictions: authorized access must still be verified using
the actual ZIP listing and download URLs.

## Implementation status

Offline tests cover login, cookie host scoping, renewal, one-time failure
retries, persistent UI credentials, and external file override. A live
authenticated ZIP-member download has not yet been verified.


The legacy `ROMARR_ARCHIVE_ORG_*` variables remain supported. Any
`ROM_HUB_ARCHIVE_ORG_*` variable selects the new family exclusively; partial
families are rejected. Existing saved credentials are read without migration.
The UI and its configured/external/error status contract are unchanged.

## Standalone CLI

No ROMarr service is required. In the CLI virtual environment:

```sh
rom-hub auth login archive-org
rom-hub auth status archive-org
rom-hub search "Final Fantasy VIII" --platform psx --live
```

Use the same `ROM_HUB_HOME` and store settings for all commands. The CLI
home and ROMarr's service home are separate unless explicitly configured to
share one. `rom-hub auth logout archive-org` clears the CLI home's saved record;
it refuses when externally managed credentials override that record.

ROMarr searches plugins live and downloads their selected plan. It does not
read the standalone CLI census. Configure a ROM Hub download client with a
writable staging directory to enable the acquisition lane.

## Companion dependencies

This contribution requires the shared authentication implementation in ROM Hub
commit `2c66cd22d24a8ec4861a74d7b47f23eab2a8d7ca` and the Archive.org plugin's
live set-search/import implementation (validated against fork develop commit
`0dd3e61072c3fed9212c7b19ea66f68a7f6a24df`). The Hub dependency is pinned
explicitly until its companion contribution lands upstream. Install the plugin
from its matching contribution revision when testing set search and download;
the upstream catalogue entry alone does not select that revision.

File-set census is a separate contribution. Authentication does not require it.
