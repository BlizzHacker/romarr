# Hash-based duplicate import checks

With **Hash index** enabled, manual and completed-download imports compare each
selected single-file ROM's physical SHA-1 with the global library hash index,
including other platforms and differently named files. Incoming ZIPs are compared
by their selected ROM member bytes. Successful single-file imports also record
their physical SHA-1 when no DAT identifies the dump.

An index hit is only a hint: ROMarr opens the indexed path under a configured
library root and checks its current bytes. A retained raw file or a bounded,
safe ZIP member must still match. Missing files, changed bytes, unknown paths,
paths outside configured roots, linked paths and unreadable/unsupported evidence
do not cause an "already in the library" refusal. The staged incoming payload is
checked again before publication, and identical selected members in one batch
publish only one copy.

The refusal names the retained platform and path. It preserves the download and
any existing destination, leaves scheduled requests unfulfilled, and reports the
queue import failure for review. Disabling **Hash index** disables this check.

Multi-file disc/installer sets are not deduplicated from an individual track's
hash. Retained archive-member confirmation currently covers ZIP, with bounded
metadata and decompressed sizes; other archived member evidence is skipped.
The index stores one path per SHA-1. If that path is stale while another
unindexed copy exists, import proceeds; this check does not scan the entire
library or invent a second retained path. SHA-1 is used as the existing ROM
identity checksum, not as a malware or authenticity verdict.
