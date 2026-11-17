# Switch inventory and release preferences

The Switch Updates & DLC page reads tagged local filenames in the effective routed Switch and Switch 2 platform folders. It never imports, extracts, attaches, deletes or acquires files. Supported extensions are `.nsp`, `.nsz`, `.xci`, `.xcz` and `.nup`. A single bracketed 16-digit title ID is required; `[v65536]` supplies an optional numeric version. File contents are not validated by this report.

Loose files and game subfolders are grouped by application family. TitleDB version histories identify known update gaps. Missing/malformed version tags remain unknown. Switch 2 does not borrow Switch 1 version histories. DLC entries are known regional catalogue gaps, **not an exhaustive list**: the optional `TITLED_NAMES=1` regional catalogue improves names and coverage but is large. Ignoring one DLC only changes tracking preferences; its underlying inventory facts remain intact. Resume tracking restores the gap.

An empty shelf does not start a metadata download. Opening the page scans on demand, with a three-second filesystem budget, 100,000 entry limit and depth limit. Linked/reparse directories, hidden folders, quarantine and media folders are skipped. Permission failures, changed paths and limits mark the scan incomplete. Refresh rebuilds the scan; ordinary requests reuse it for up to 30 minutes. `/api/v1/switch/gaps` only reads a fresh existing cache and never waits for a scan or downloads metadata. All three Switch endpoints use normal API authentication.

Profiles now offer NTSC/PAL timing preferences, optionally refusing known opposing hints. Explicit timing labels or bracketed region tags supply hints; unknown and mixed-region tags stay neutral. Platform profiles override global settings. Preferred formats are ordered extensions per platform (for example `psx = chd, cue, bin` globally, or `chd, cue, bin` on a console profile). Only a parsed final extension counts; a selected torrent member takes precedence over the archive/release filename. These preferences add visible scoring reasons and never bypass normal eligibility or safety checks.

## Updates and DLC go with their game

When ROMarr imports a Switch update or DLC and that game already has a folder of its own, the file goes inside it: updates to `<game>/update/`, DLC to `<game>/dlc/`. RomM shows those folders as the game's extra content instead of listing each file as another game. The import preview shows the destination before anything is written.

This needs exactly one bracketed title ID in the file name, such as `[01007960049A0800]`. ROMarr then looks for the one game folder holding the matching base game, using the same bounded scan as the report. If the base game is loose in the platform folder, is in two folders, or isn't there at all, the file keeps its normal destination. ROMarr never follows a link or reparse point to reach the game folder, and checks again just before writing. Files you placed into a reviewed bundle keep the route you chose.

Turn it off under **Settings → Media Management → File Switch updates and DLC inside their game's folder** (`switch_attach_extras`, on by default). Files already in your library are never moved.

Title ID structure reference: [Switchbrew game title list](https://switchbrew.org/w/index.php?title=Title_list%2FGames); catalogue: [blawar/TitleDB](https://github.com/blawar/titledb).
