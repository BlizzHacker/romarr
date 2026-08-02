# Every game, every platform: removing the disc-based exclusion

**Status:** approved for implementation
**Date:** 2026-08-02

## The claim being removed

`README.md` says:

> Disc-based platforms are excluded — multi-gigabyte images that browser
> emulators cannot stream.

Both halves are false, and the evidence is in this ecosystem's own code.

**Browser emulators do run disc systems.** RomM 4.9.2's `_EJS_CORES_MAP`,
vendored verbatim in `rom-hub/src/rom_hub/playability.py`, gives EmulatorJS
cores for `psx` (`pcsx_rearmed`, `mednafen_psx_hw`), `psp` (`ppsspp`),
`saturn` (`yabause`), `segacd` (`genesis_plus_gx`, `picodrive`), `3do`
(`opera`), `philips-cd-i` (`same_cdi`), `pc-fx` (`mednafen_pcfx`),
`turbografx-cd` (`mednafen_pce`), `amiga-cd32` (`puae`) and `dos`
(`dosbox_pure`). Ten optical systems, in the base map, on any stock RomM.

**What EmulatorJS cannot run, the stream server already does.**
`RommStreamServer/tiers.py` routes `ps2` → `pcsx2`, `ngc`/`wii` → `dolphin`,
`dc`/`naomi`/`atomiswave` → `flycast`, `3ds` → `citra`, `saturn` →
`mednafen_saturn`, `n64` → `mupen64plus_next`, all server-side under headless
RetroArch, captured and delivered over WebRTC or HLS. The client never sees
the multi-gigabyte image.

**And the library already holds them.** The production RomM at
`/mnt/usb1/roms` carries 2,621 psx, 2,409 ps2, 1,470 wii, 1,182 psp, 992 dc,
718 saturn, 626 ngc, 189 segacd and 167 3do entries — sitting there, playable,
acquired by hand because ROMarr refuses to acquire them.

So the exclusion never described a limitation. It described ROMarr's importer.

## What actually blocks disc platforms

Five places, all in ROMarr:

1. **`platforms.py`** — sixteen cartridge platforms and nothing else. A disc
   platform cannot be requested because it cannot be named.
2. **`selection.py::judge`** — the size ceiling rejects anything over the
   platform's `max_size`, wording the rejection "too big for a … cartridge".
3. **`selection.py::pick_rom_file`** — returns **one** filename. A `.cue`
   without its `.bin`, or a `.gdi` without its tracks, is a dead file. This is
   the single most damaging assumption: it does not fail, it imports something
   broken.
4. **`library.py`** — copies one file, and `ARCHIVE_SUFFIXES = (".zip",)`.
   The live disc library is overwhelmingly `.7z`.
5. **Nothing tells the operator how a platform will play.** Three real play
   routes exist and ROMarr surfaces none of them.

## Design

### 1. Platforms gain a medium

`Platform` gains `media: "cartridge" | "disc" | "computer"`. It changes three
behaviours — the size ceiling's magnitude, the wording when a release is
rejected for size, and whether a multi-file set is expected — and it makes the
platform table self-describing rather than encoding "disc" implicitly in a
number.

Every slug added is a RomM `fs_slug` verified against **both** the live
library's 394 platform folders and RomM 4.9.2's own core map. No slug is
invented.

Disc ceilings are per medium, not per title: CD-based systems (psx, saturn,
segacd, 3do, dc, pc-fx, philips-cd-i, turbografx-cd, amiga-cd32, neo-geo-cd)
get headroom for one disc plus packaging; DVD-based (ps2, ngc, wii, psp,
xbox) get headroom for a dual-layer image. The ceiling still does real work —
it is what keeps a 60 GB PC repack out of a PS2 request.

### 2. A ROM set, not a ROM file

`pick_rom_file` is kept and reimplemented on top of a new `pick_rom_set`,
which returns a `RomSet(primary, members)`:

- **Cartridge platforms** — one member, unchanged behaviour.
- **Disc platforms** — the preferred image by extension order, plus every
  sidecar it references. `.cue` pulls its `.bin`/`.img` tracks; `.gdi` pulls
  `.bin`/`.raw`. Preference order and sidecar rules are taken from
  `RommStreamServer/archives.py`, which is the same knowledge already proven
  against this library rather than a fresh guess.
- A single-file image (`.chd`, `.rvz`, `.iso`, `.cso`, `.pbp`) is a set of
  one, and is preferred over a cue/bin pair when both are present, because it
  is one file that cannot be separated from its tracks.

**Sidecars are resolved by parsing the sheet, not by pattern.** A `.cue` names
its tracks in `FILE "…" BINARY` lines; a `.gdi` lists them positionally. A
glob of "every `.bin` next to it" is wrong for a directory holding two discs.
When the sheet cannot be read, every same-stem companion is taken — over-
inclusive, which costs disk, rather than under-inclusive, which costs a
playable game.

### 3. Importing a set

`import_rom` keeps its signature and return type. When the chosen set has one
member the destination is unchanged: `<library>/<slug>/<file>`. When it has
several, the destination is a directory — `<library>/<slug>/<stem>/` holding
every member — which is the layout the live library already uses (`dc/102
Dalmatians …/` holds a `.gdi` and five tracks).

`ARCHIVE_SUFFIXES` grows to `.zip`, `.7z`, `.rar`. Zip stays on stdlib
`zipfile`; 7z and rar go through `bsdtar`, which is what the stream server
uses and reads all three uniformly. Zip-slip checking is applied to every
format, not just zip — the check moves to a shared `safe_names` helper so one
format cannot be protected and another not. Where `bsdtar` is absent the
import fails with a message naming the missing tool, never silently.

### 4. Playability, stated up front

A new `romarr/playability.py` answers one question per platform: **how will
this play?** Four answers, in preference order:

- `local` — EmulatorJS in the browser (RomM's base core map).
- `stream` — the headless RetroArch stream server, when one is configured and
  says it can.
- `archive` — an Archive.org `/details/` page, which runs Emularity in the
  page; the Hub's existing `browser` handover.
- `download` — always true. A ROM in the library is a ROM you can download,
  and that is a legitimate answer rather than a failure.

Nothing here refuses an import — that is `rom-hub/playability.py`'s rule and
it is the right one. The answer is displayed beside the platform in the UI and
returned on `/api/v1/system/status`, so the operator knows before the grab
rather than at the point of clicking play.

The stream-server tier is asked at runtime over the two read-only GETs
`StreamServerClient` already uses, and its answer is cached per platform for
the process lifetime. When no stream server is configured the answer degrades
to `local`/`archive`/`download` — never to an error.

### 5. Scoring changes

- The size-ceiling message says "disc image" for disc platforms.
- `FOREIGN_PLATFORM_MARKERS` keeps every marker; the existing `own`-set
  removal already exempts the requested platform's own aliases, and each new
  platform declares aliases covering the markers that name it (`psx` declares
  `ps1`, `psx`, `playstation`).
- `COMPILATION_MARKERS` is unchanged and still correct: a multi-game disc
  collection is no more importable for a single-game request than a romset.
- **Multi-disc releases are not compilations.** `(Disc 1)` / `(Disc 2)` must
  survive scoring, because that is how the live library stores them. A new
  test pins this.

## Testing

- `pick_rom_set` against real filename layouts: cue+bin, gdi+tracks, a
  directory holding two discs, a bare `.chd`, a `.7z` of any of them.
- `import_rom` proving a multi-member set lands as a directory with every
  member present, and that a `.cue` never arrives without its `.bin`.
- Zip-slip refused identically across zip, 7z and rar.
- Scoring: a 4 GB PS2 image accepted, a 60 GB repack rejected, a 2.3 MB
  Genesis dump still preferred over a compilation, `(Disc 1)` not rejected.
- Playability: every platform in the table resolves to at least one route, and
  the route for each disc platform is asserted against RomM's vendored core
  map rather than restated.

## Proof before the README changes

The README is rewritten **last**, and only against a real run: a disc-platform
request searched, scored, grabbed, picked as a set, imported to a disposable
backend (`demoromm` on CT 104 — never production `romm`), and the resulting
entry shown to route to a real play tier.
