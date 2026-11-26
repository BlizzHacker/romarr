# Base games, updates and DLC

Games on consoles with online updates and add-ons come as several packages:
the base game, an update (patch) and DLC. Indexers carry each one as its own
upload, plus combined packs such as `Game + Update 1.05 + 3 DLC`. ROMarr
tells them apart when it searches, and files them the way RomM reads
multi-file games when it imports.

This applies to PlayStation 3, PlayStation 4, PlayStation 5, PSP, PS Vita,
Nintendo 3DS, Wii U, Switch, Switch 2, Xbox 360 and PC. Retro and cartridge
systems have no updates or DLC, and there "patch" means a ROM hack, so their
rules are unchanged.

## Search

ROMarr reads which parts a release title holds. It shows the result on the
Search page (`base game`, `update`, `DLC`, or a mix such as
`base game + update + DLC`) and in the scoring reasons.

- Update words: `Update`, `Patch`, `Hotfix` and `Title Update`, plus package
  names such as `A0105-V0100` and `CUSA12345-patch`.
- DLC words: `DLC`, `Add-On`, `Season Pass` and `CUSA12345-ac`.
- A word joined to the game by `+`, `&`, `and`, `with` or `incl` means the
  pack includes that part. The same word standing on its own, as in
  `Game Update 1.05`, means the release is only that part.

How requests use this:

- A request for the game never takes a lone update or DLC, and prefers a pack
  that also holds the update and DLC.
- On the Request page, these platforms show checkboxes for **Base game**,
  **Update** and **DLC**. Asking for all three prefers the pack that covers
  the most. The game is still required first. Whatever the import did not
  bring goes back on Wanted as a request for just those parts. What arrived is
  read from the imported files, not from the release title.
- Ask for **Update** or **DLC** without the base game when you already have
  it. Releases that include the base game again rank lower.
- The API takes the same choice as `parts`, a list of `base`, `update` and
  `dlc`, on `POST /api/request` and `POST /api/v1/wanted/add`.
- A console grab needs evidence that the release is for that console: the
  platform name, its file extension, a title ID, its indexer category or a
  ROM Hub source. Package words alone never make a release usable. PC keeps
  its own category rules, because PC releases rarely name their platform.

## Import

RomM files a multi-file game as one folder and reads each file's category
from the first folder inside it. ROMarr imports these platforms into RomM
libraries the same way. Other library types (Folder, RetroBat, Gaseous,
Retrom, Gameyfin) keep their flat layout, because frontends such as RetroBat
expect one file per game in each system folder.

```text
<library>/<platform>/<Title>/<base game>
<library>/<platform>/<Title>/update/<update>
<library>/<platform>/<Title>/dlc/<dlc>
```

`<Title>` is the requested game's name. When a library folder already holds
the base game of the same title, that folder is used instead, so a later
update or DLC joins its game. Whether a package is the base, an update or DLC
comes from the best evidence each platform offers:

| Platform | What identifies a package |
|---|---|
| PS4 | The package header: content type, plus the patch flags for updates. Only the first 128 bytes are read. |
| Switch, Switch 2 | The bracketed 16-digit title ID: base, update (`…800`) or DLC. |
| 3DS | The title ID's high word: `00040000` base, `0004000E` update, `0004008C` DLC. |
| Wii U | The title ID's high word: `00050000` base, `0005000E` update, `0005000C` DLC. |
| PS3, PS5, PSP, Vita | The serial or content ID (`BLUS30443`, `PCSE00120`) says which game; folder and file names say what it is. |
| Xbox 360, PC | Folder and file names. |

Downloads whose packages a PS4 header identifies import without review, and
so do Switch, Switch 2 and 3DS packages whose title IDs all belong to one
game.
Everything else follows the existing review rules in Queue. The roles you
choose there decide the folders, and Queue shows the resulting destination
tree for you to confirm before anything is written. A file name never decides
on its own. Packages naming different title IDs go to review, so an update
for another game never joins this one.

Some downloads keep the flat layout:

- a download holding more than one base game;
- a game folder reached through a link;
- an install set of many files, such as a PC repack or a Wii U folder dump.
  These keep their existing one-folder layout.

Switch libraries on RomM keep their reviewed bundle routing, which already
uses `update/` and `dlc/` (see [Switch inventory](SWITCH-INVENTORY.md)).

Turn the layout off under **Settings → Media Management → In RomM libraries,
file games with updates and DLC as one game folder** (`game_package_folders`,
on by default).
Files already in your library are never moved.
