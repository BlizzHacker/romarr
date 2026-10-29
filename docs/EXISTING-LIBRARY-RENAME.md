# Rename verified files in an existing library

This tool is for cleaning up files already in a configured library. It does not need Prowlarr, a download client or an indexer. It does need a loaded DAT that identifies the file by checksum.

Open **Stats**, choose **Fix existing filenames**, select the platform and click **Preview safe renames**. Leave the optional subfolder blank to scan the whole platform folder. Enter a relative subfolder to work through a large library in smaller sections. The preview checks one page at a time and shows the current path, proposed path and why each file is skipped.

ROMarr only suggests names when the bytes match one unambiguous, single-file DAT entry. It leaves unknown or bad dumps, disc images, multi-file DAT entries and files referenced by CUE, GDI or M3U playlists untouched. Case-insensitive destination collisions and unsafe Windows filenames are refused. For flat layouts, a file nested below a platform folder is proposed at that platform folder; empty source folders are left in place. Nested library layouts are refused with an explanation so a rename cannot move a ROM out of the folder RomM scans. ROMarr never deletes another file to make room.

Select the rows you want and confirm. ROMarr rechecks the file fingerprint and checksum, then writes a durable journal under .romarr-renames in the library folder. If a filesystem cannot create a safe same-volume hard link, the operation stops without replacing a file. After a rename, run a library scan in RomM so its database follows the new path.

To undo a completed batch, return to **Fix existing filenames** and choose **Restore original names** in Rename history. The restore checks the recorded checksum and refuses to overwrite a file that has appeared or changed since the rename. Keep the rename journal with the library until you are sure you no longer need that recovery record.

The preview handles up to 50,000 candidate files per scope and shows pages of 500. If a platform is larger, split the collection into subfolders before scanning. The subfolder field can scan only files inside that folder; it cannot reach root-level files omitted after the 50,000-file limit. Each apply is limited to the rows shown in the current preview; run another preview for the next batch.
