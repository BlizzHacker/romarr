# Update channels and owner-approved PR previews

ROMarr distinguishes published releases, Nightly revisions and manually
selected pull requests. Choosing a channel does not publish a release, install
an update or make this installation writable.

## Release stages

Use Settings → General or the Update page to select a channel. Recognized
release tags use `vMAJOR.MINOR.PATCH` with an optional numeric stage suffix:
`-alpha.N`, `-beta.N` or `-rc.N`. For example,
`v1.10.0-alpha.1` → `v1.10.0-beta.1` → `v1.10.0-rc.1` → `v1.10.0`.
Unsupported spellings and draft releases are excluded.

| Selected channel | Eligible published stages | Automatic application |
| --- | --- | --- |
| Stable | Stable | Existing explicit auto-update preference, for verified eligible source installs only |
| RC | RC and Stable | Manual only |
| Beta | Beta, RC and Stable | Manual only |
| Alpha | Alpha, Beta, RC and Stable | Manual only |
| Nightly | Verified official `main` revision ancestry | Informational checks; no automatic application or update notification |

Versions are ordered numerically, then by stage and numeric prerelease number.
Selecting a less stable channel allows more stable releases as well; selecting
Stable never offers Alpha, Beta or RC tags. Preview catalogs inspect at most
three pages of sixty official GitHub releases, including prereleases. This is
a bounded catalog, not a claim to have enumerated every historical release.
The existing Stable check uses GitHub's latest stable-release endpoint.

An unknown version or development build never becomes eligible for automatic
stable application by changing its channel. A release-channel check records
what was compared; changing channel or build invalidates an old verdict.
Nightly checks retain their separate official-revision and ancestry proof.

## Installation capabilities

| Installation | What the Update page can do |
| --- | --- |
| Clean Git/source checkout with local virtual environment and a verified active systemd service | Launch the existing confined detached worker, subject to branch, remote, clean-tree, queue and ownership checks |
| Docker image | Show release/PR metadata and host-owned update/build guidance; cannot replace its own image |
| Packaged Windows or Android application | Show metadata and guidance for a separately built and verified package; cannot mutate bundled code through Git |
| Unknown, detached or unsupported source layout | Explain refusal; no fallback to arbitrary commands or destructive reset |

Git SSH origins, including `git+ssh://` and `ssh+git://`, remain unsupported by
the confined worker. The checkout, Git metadata, local virtual environment,
branch, remote and service identity must remain valid. Pending downloads and
imports defer application. Ignored owner files are protected from merge
replacement; source modifications and untracked files block application.

## Inspecting a pull request

The Update page can list open PRs from `BlizzHacker/romarr`. Inspection records
the PR number, author, title, exact forty-character head and base commits, draft
state and CI check results. Only an open PR targeting official `main` qualifies.
A passing `test` check is required; pending or failed check runs and failing or
pending commit-status contexts block application. Incomplete/truncated check
metadata also blocks it. CI success is evidence, not a guarantee that a PR is
safe: review its code, author and current discussion yourself.

Applying a PR is always a separate manual action. It requires the inspected
head/base identities, explicit confirmation, and typing `APPLY PR #N`. The
server reads current GitHub metadata again; a changed head or base requires a
new inspection. PR previews never enter scheduled auto-application.

## Source-only fast-forward application

Only a clean fast-forward descendant of the current checkout may be installed.
The worker fetches the exact `refs/pull/N/head`, verifies its pinned SHA, checks
that official `main` still matches the inspected base, and repeats local
identity and cleanliness checks. It then uses a non-overwriting fast-forward,
installs requirements in the checkout-local environment, records the verified
code identity, restarts the verified service and requires two successful health probes. A diverged/non-fast-forward
PR is refused; local commits and files are preserved. There is no force checkout
or automatic branch replacement to make such a PR fit.

After a source update, generated version and provenance files are replaced from
its verified target. A release uses its exact tag and SHA. Nightly and PR code
use development provenance and an unknown semantic version rather than retaining
the old release number. Restoring a tagged commit restores that tag's identity;
an untagged previous commit remains development code. Linked metadata paths
block publication and retain the recovery claim.

The worker's atomic claim also owns preview preparation. A busy or repeated
request cannot overwrite an existing preview's marker, backup or rollback
identity. Restore an active preview before installing another one.

Before launch, ROMarr copies the existing JSON configuration into its private
Git metadata directory, flushes it and verifies its SHA-256. Records live under
`.git/romarr-preview-backups/` in a normal clone. The backup directory is mode
0700 and configuration files are mode 0600. They contain private settings and
credentials: keep them private, never attach them to public issues or upload
them as CI artifacts. A rollback ref preserves the previous commit. Automatic
updates are turned off before preview code runs, and build provenance identifies
the running preview as development code with its PR ref and exact SHA.

## Restore and recovery

Restore requires explicit owner confirmation and the active preview's backup
identity. The checkout must still be on its recorded branch and exact preview
commit, with no modified or untracked files and no active downloads/imports.
The detached worker checks out the recorded previous commit with
`--no-overwrite-ignore`, then compare-and-swaps the recorded local branch ref
and reattaches HEAD. Ignored-file or parent-path collisions block rollback,
and a changed branch ref is never forced. An interruption during these steps
retains its recovery claim for manual inspection. It uses
the same dependency, service and health verification as application.

**Restore rolls back code only. Current settings are preserved.** The private
JSON snapshot is retained for a separate, deliberate configuration recovery;
ROMarr does not silently overwrite settings you changed while trying a PR.
Automatic updates remain disabled after restoration until the owner explicitly
re-enables them. Code rollback cannot undo arbitrary side effects of untrusted
PR code, a package installation or a data migration.

Failed or interrupted workers retain their claim and log for inspection under
Git metadata (`romarr-selfupdate.lock` and `romarr-selfupdate.log`). No later
request silently steals that claim. If identity, branch, bytes or destination
changed, preserve the checkout and backups and recover manually; never resolve
a refusal by deleting owner files or running an unreviewed hard reset.

## Non-fast-forward, Docker and packaged previews

Use a separate checkout/build for a PR that cannot safely fast-forward this
installation. Inspect and pin its full SHA first. Fetch only its official pull
ref and verify `FETCH_HEAD` equals that SHA before building. Run it with a
separate configuration/data directory, port and library fixtures. Do not point
unreviewed preview code at a production library merely because its CI is green.

For Docker, build a distinct preview image from that pinned tree with development
provenance (`ROMARR_BUILD_KIND=development`, exact `ROMARR_SOURCE_REVISION`,
`ROMARR_SOURCE_REF=refs/pull/N/head`, official source repository). Preserve the
previous production image digest and deployment settings. For Windows/Android,
follow the pinned tree's platform build instructions and keep the preview
package separate from your installed stable application. ROMarr does not
pretend that selecting a PR changes an immutable image or packaged executable.

## API

All routes use the existing authenticated API gate:

- `GET /api/v1/update/releases?channel=alpha|beta|rc|stable`: bounded eligible release catalog.
- `GET /api/v1/update/prs`: bounded open-PR list.
- `POST /api/v1/update/pr` with `{ "number": N }`: inspect identities/checks.
- `POST /api/v1/update/pr/apply`: inspected `number`, `head`, `base`, `confirm: true`, and `acknowledgement: "APPLY PR #N"`.
- `POST /api/v1/update/pr/restore`: active `backup_id` and `confirm: true`.

Read the returned refusal or guidance; a successful launch acknowledges a
worker start, not a verified completed installation. Completion is established
by the worker's durable log and health checks.
