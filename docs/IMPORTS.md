# Importing without losing your downloads

Under **Settings → Media Management**, enable **Prefer hardlinks; copy when unavailable** to save space when importing unchanged files. It is off by default. Source and library paths must support a real hardlink: being on the same physical disk is not sufficient when containers use separate mounts.

Successful imports report `hardlink`, `copy`, `mixed`, or `extracted` in the import result and History. Copy fallback includes the reason. Cross-device links and unsupported link permissions fall back to copying; unrelated I/O failures remain failures.

ROMarr never changes download file permissions to make a link work. Files with private permissions copy instead. Archives normally extract; arcade archives that are themselves playable can link. Hardlinked files share their bytes: editing a linked library file also changes the seeded download. Choose independent copying if external tools will edit files in place.

**Keep completed downloads for seeding after import** is enabled by default. This is independent of the linking option. Disabling it permits cleanup only after a fully successful import when the torrent is not shared by other retained requests. Shared collection torrents are protected even with retention disabled; remove them deliberately in the downloader when all seeding and requests are finished. Failed or partially successful imports always retain the source.

Files are staged on the library filesystem, checked, then published without overwriting a racing destination. Existing zero-byte destinations require inspection. If a set partially publishes before an error, reconcile the completed members before retrying; existing files are never blindly accepted as successful imports.

## Request another game from a retained MiNERVA collection

ROMarr reuses the existing client/infohash only when its persisted ownership, category, tags and earlier file identities match. Requesting game B adds B without resetting A or any other nonzero file priority. It does not delete/re-add or pause/resume a running torrent. Each requested file imports as soon as that file is complete, even if another selected file is still downloading.

A stopped retained torrent stays stopped. Queue shows **Resume retained torrent** on the earlier job or the pending failed grab. Use that explicit action, then retry the grab. The downloader continues to control seeding policy.

Ownership survives restart and clearing old queue rows through a separate retained ownership ledger. Missing/changed legacy ownership or an unexpected tag/category asks for review instead of guessing. Do not share control of one collection torrent between tools; external changes can interrupt selection. In that case ROMarr preserves existing bytes and reports the problem. It never falls back to downloading the whole collection.
