# Review a completed bundle before importing

Open **Queue → Preview import** on a completed download that paused for review
or already has a saved selection. ROMarr lists each
platform-compatible game/package, its required companions, proposed destination,
and expanded bytes when available. Manuals, artwork and unrelated console
executables appear under excluded files. PC installers remain part of their game
set. Preview does not publish files or change download priorities.

Bundles containing several games and packages whose base/update/DLC type has not
been inspected pause as **import-review**. Nothing enters the library until you
save a selection. Choose one base game and optionally label its updates and DLC.
Use **Collection / multiple games** to deliberately import additional games.
Companions such as CUE/BIN, GDI tracks and playlist discs are selected as a group.

This initial version does not inspect encrypted package metadata or establish a
package's title/version. Filename and extension are not proof of a base game,
update or DLC. The preview says when identity is unknown; you must verify the
packages and associate updates/DLC with their base manually. Update-only and
DLC-only selections cannot fulfil a base-game request: select a base game too.
Copying a package does not install or apply it to an emulator.

The saved selection survives restarts and import retries. If the source names,
size or timestamps change, ROMarr requires another review rather than adding
newly discovered files. Imports with a saved selection retain the original
download, including excluded members. This does not change torrent file priority
or selectively download archive members.

API clients can GET `/api/v1/queue/import-plan?index=0` for a read-only plan and
POST `/api/v1/queue/import-plan` with `index`, `confirm: true`, the returned
`token`, `mode` (`single` or `collection`) and `choices` entries containing
`primary` and `role` (`base`, `update`, `dlc`, `additional-game`). The next import
sweep uses exactly those game sets. Queue indices should be refreshed before
submission; the source token is validated again at publication.

## Switch packages targeting RomM

For a RomM library, reviewed Switch NSP/XCI packages use a game folder. A base named `Fixture Game.nsp` goes into `switch/Fixture Game/Fixture Game.nsp`; its selected updates go into `Fixture Game/update/`, and DLC into `Fixture Game/dlc/`. Structure B puts the same game subtree beneath `switch/roms/`. RomM Structure B must also be configured in RomM with the appropriate filesystem template; it is not automatically detected. Custom platform folder names and the Translations directory are respected.

After choosing roles, click **Review destinations**, inspect the final tree, then click **Confirm these destinations**. Nothing is imported by previewing. Confirmation saves the roles and exact routes, and the import sweep rechecks them before writing. Optional packages for several base games in one selection are held for review; import each base and its associated packages separately. Package identity remains manual and unverified.

To add a later update or DLC, choose **Attach to existing game** and select the game folder. Select only the optional packages and identify their roles. The folder must contain an existing base NSP/XCI at its root. This workflow preserves that base and does not fulfil a missing base-game request or mark it available to an external requester. Sources remain retained.

ROMarr does **not** automatically move a pre-existing loose base file into a folder. It stops with a review message. To make that transition explicitly, stop scanning/imports, back up the loose base, create a same-named game folder, move the base into it without overwriting anything, and verify its bytes before rescanning RomM. Keep a record of the original path so the move can be reversed while scans are stopped. Then preview the optional download and choose that existing game folder. If a directory or package collision appears, reconcile it manually; ROMarr neither silently adopts a different base nor overwrites existing packages.

The filesystem scanner from official RomM 5.4.1 was checked in an isolated, network-disabled container with synthetic NSP bytes: both configured layouts discovered one multi-file game containing the base and the update/DLC files with their respective categories. This is filesystem discovery proof, not playable-package, title-id extraction, emulator-installation, or a live metadata/database-rescan claim. Official reference: https://docs.romm.app/latest/getting-started/folder-structure/#multi-file-games
