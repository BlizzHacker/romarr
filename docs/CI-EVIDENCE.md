# Verified pytest evidence

ROMarr's `scripts/ci_evidence.py` independently implements ideas from
[TenchiMisaki/romarr-ci-controller](https://github.com/TenchiMisaki/romarr-ci-controller):
immutable source snapshots, separate evidence gates, anonymous case accounting,
and sanitized artifacts. No controller implementation was copied; that
repository did not provide a license when reviewed on November 14, 2026.

This guard verifies source-test evidence. It does not certify Docker images,
browser behavior, releases, or production. It is not a sandbox against malicious
code sharing its runner. A changed PR head or base makes historical results
`STALE_OR_UNVERIFIED`, even when pytest succeeded.

The four commands use Python's standard library. Supply identical absolute
`--root`, `--private`, `--public`, and `--scope full|structural` arguments in each
step. The public directory must be new and separate from raw private output.

| Step | Purpose | Credentials |
| --- | --- | --- |
| `pin` | Verify official `BlizzHacker/romarr` base, expected full head SHA and clean checkout; record head/base | Optional read-only `GH_TOKEN` |
| `run` | Run offline pytest with a 20 minute limit; save raw JUnit/log privately | No token; inherited credentials are removed from child environment |
| `finalize` | Reconcile JUnit case totals; independently read live head/base and local Git status; write anonymous evidence | Optional read-only `GH_TOKEN` |
| `gate` | Recheck hashes, case accounting, four gates and successful artifact upload | None |

On PR runs, set `CI_EVIDENCE_PR` to the official upstream PR number and
`CI_EVIDENCE_HEAD` to its full head SHA. Check out that exact SHA with
`persist-credentials: false`. On main/tag runs leave the PR number empty and use
`GITHUB_REF` plus the expected full commit SHA. The workflow repository must be
`BlizzHacker/romarr`; unrelated repositories are rejected.

Install the ordinary test prerequisites, including pinned `rom-hub`, Archive.org
fixture, Node.js and required archive tools, before running. Missing Python,
Node or Python packages fail the environment gate. Skips remain explicit in the
counts and require review; a successful source-test report does not imply every
optional platform/tool test executed.

Run `finalize` after test failure as well as success. Upload only these files,
and only after finalization succeeds:

- `summary.json`: immutable head/base, scope, exit, counts, and TEST, COLLECTION,
  ENVIRONMENT and SOURCE statuses.
- `cases.json`: ordinal numbers and outcome states, without test names,
  parameters, file paths, tracebacks or captured messages.
- `manifest.json`: SHA-256 of both public JSON documents.

Never upload the private directory, JUnit XML or raw pytest log. Give `gate`
`ARTIFACT_STATUS=success` only from the actual upload step's successful outcome;
missing or failed upload blocks PASS. Do not inject `GITHUB_TOKEN` or other
credentials into the pytest step. Tokens belong only to separate read-only
metadata steps. A failed or unavailable metadata lookup also blocks current
source verification.

The public artifact is useful for accounting and handoff. It deliberately
contains no test names, so diagnosing individual failures requires reviewing
private raw evidence in an appropriately restricted environment.
