# Importing without losing your downloads

Under **Settings → Media Management**, enable **Prefer hardlinks; copy when unavailable** to save space when importing unchanged files. It is off by default. Source and library paths must support a real hardlink: being on the same physical disk is not sufficient when containers use separate mounts.

Successful imports report `hardlink`, `copy`, `mixed`, or `extracted` in the import result and History. Copy fallback includes the reason. Cross-device links and unsupported link permissions fall back to copying; unrelated I/O failures remain failures.

ROMarr never changes download file permissions to make a link work. Files with private permissions copy instead. Archives normally extract; arcade archives that are themselves playable can link. Hardlinked files share their bytes: editing a linked library file also changes the seeded download. Choose independent copying if external tools will edit files in place.

**Keep completed downloads for seeding after import** is enabled by default. This is independent of the linking option. Disable it only if you want ROMarr's existing selective-torrent cleanup after a fully successful import. Failed or partially successful imports always retain the source.

Files are staged on the library filesystem, checked, then published without overwriting a racing destination. Existing zero-byte destinations require inspection. If a set partially publishes before an error, reconcile the completed members before retrying; existing files are never blindly accepted as successful imports.
