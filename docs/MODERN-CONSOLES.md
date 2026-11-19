# PlayStation 4, PlayStation 5 and Vita

ROMarr recognizes PS4 (ps4), PS5 (ps5) and PlayStation Vita (psvita).
These are acquisition and library organization targets. They do not add
browser emulation or turn ROMarr into software that runs on the console.

PS4 and PS5 accept complete .pkg files; Vita accepts .vpk, .pkg and intact
.zip packages. The shared .pkg extension does not identify the console:
select the platform explicitly. Search still needs a configured indexer or
source that actually provides compatible results. ROMarr supplies no keys,
licenses, firmware, or guaranteed source catalogue.

Use your existing download client and library importer. DAT verification is
only possible when a matching DAT is loaded; an imported file without a DAT
match remains unknown, never automatically verified. Console installation
and external playback depend on the user's own device and software.
Folder dumps, split packages and installation on hardware are not
implemented by these platform declarations. Base game, update and DLC
handling for PS4 and PS5 is described below.

## Base game, updates and DLC (PS4 and PS5)

A PS4 game usually comes as several `.pkg` files: the base game, an update
(patch) and DLC (add-ons). Indexers carry each one as its own upload, plus
combined packs such as `Game + Update 1.05 + 3 DLC`.

**Search.** ROMarr reads which parts a release title holds and shows it on
the Search page (`base game`, `update`, `DLC`, or a mix such as
`base game + update + DLC`) and in the scoring reasons. Update words are
`Update`, `Patch` and `Hotfix`, plus package names such as `A0105-V0100`
and `CUSA12345-patch`. DLC words are `DLC`, `Add-On`, `Season Pass` and
`CUSA12345-ac`. A word joined to the game by `+`, `&`, `and`, `with` or
`incl` means the pack includes that part. The same word standing on its own,
as in `Game Update 1.05`, means the release is only that part.

- A request for the game never takes a lone update or DLC, and prefers a pack
  that also holds the update and DLC.
- On the Request page, PS4 and PS5 show checkboxes for **Base game**,
  **Update** and **DLC**. Asking for all three prefers the pack that covers
  the most. The game is still required first. Whatever the imported release
  did not hold goes back on Wanted as a request for just those parts.
- Ask for **Update** or **DLC** without the base game when you already have
  it. Releases that include the base game again rank lower.
- The API takes the same choice as `parts`, a list of `base`, `update` and
  `dlc`, on `POST /api/request` and `POST /api/v1/wanted/add`.

**Import.** RomM files a multi-file game as one folder and reads each file's
category from the first folder inside it. ROMarr imports a PS4 or PS5
download the same way:

```text
<library>/ps4/<Title>/<base game>.pkg
<library>/ps4/<Title>/update/<update>.pkg
<library>/ps4/<Title>/dlc/<dlc>.pkg
```

`<Title>` is the requested game's name. When a library folder already holds
the base game with the same title ID (`CUSA12345`), that folder is used
instead, so a later update or DLC joins its game. For a PS4 package on disk,
the role comes from the package header: its content type, plus the patch
flags for updates. Only the first 128 bytes are read. Downloads whose
packages are all identified this way import without review. Packages inside
an archive, or without a readable header, still go to Queue review. The
roles you choose there decide the folders; a file name never decides on its
own.

A download holding more than one base game stays flat, and so does a game
folder reached through a link. Turn the layout off under **Settings → Media
Management → File PS4 and PS5 packages as one game folder**
(`ps4_game_folders`, on by default). Files already in your library are never
moved. PS5 packages get the title classification and the folder layout, but
their headers are not read.
