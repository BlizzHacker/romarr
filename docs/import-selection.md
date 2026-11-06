# Review a completed bundle before importing

Open **Queue → Preview import** on a completed download. ROMarr lists each
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
