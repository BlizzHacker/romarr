# Library counts: on disk, catalogued, and where the catalogue came from

**Date:** 2026-08-11
**Status:** implemented
**Code:** [`romarr/libraries.py`](../../romarr/libraries.py)
(`classify_provenance`, `library_counts`, `Game.provenance`),
[`romarr/clients.py`](../../romarr/clients.py) (`Romm.counts`),
[`romarr/app.py`](../../romarr/app.py) (`_refresh_counts`,
`_publish_library`, `library_split`, `library_view`, `counts`, `stats`),
[`romarr/ui.py`](../../romarr/ui.py)
**Tests:** [`tests/test_library_counts.py`](../../tests/test_library_counts.py)

The question was: *"Where are my 250,000 games from Archive.org + my 130,000
Flashpoint games? I have 72,000 local games too. My numbers aren't right
across all areas."*

The numbers were not right, and they were wrong in two separate ways at once:
ROMarr was adding two unlike things together and calling the sum "games", and
RomM genuinely is not cataloguing most of what has been indexed. This
documents what is actually there, how it was measured, and which half of the
problem was ROMarr's.

> **The short version.** RomM holds 166,548 rows: **72,120 files on disk** and
> **94,428 catalogue entries** for things that stream. Of those 94,428,
> **71,695 are Flashpoint** and **22,720 are Archive.org**. ROMarr had already
> indexed **69,360 more Flashpoint** titles and **138,612 more Archive.org**
> titles that RomM has never been told about. Nothing is lost — it was never
> catalogued.

---

## 1. What is actually on the live library

Measured against `romm.moveweight.com` (**RomM 5.1.0**) on 2026-08-11, using
RomM's own server-side filters rather than a walk, so these are database
counts and not samples.

| Category | Count | How |
|---|---:|---|
| Every row RomM holds | **166,548** | `GET /api/roms?limit=1` → `total` |
| **On disk here** | **72,120** | `&missing=false` |
| **Catalogued, not on disk** | **94,428** | `&missing=true` |
| Platforms with content | 84 | `/api/stats` |
| Bytes on disk | 17.98 TB | `/api/stats` → `TOTAL_FILESIZE_BYTES` |

72,120 + 94,428 = 166,548 exactly. Nothing is double-counted and nothing is
dropped — which matters, because "double counted" was the first hypothesis
and it is wrong.

The owner's "roughly 72,000 local" is right to within 120.

### The catalogued half, broken down

94,415 of the 94,428 catalogued rows sit on **one** platform: `browser` /
fs_slug `flash` / "Browser (Flash/HTML5)", every one of them under `fs_path`
`roms/flash`, and **zero** of that platform's rows are on disk. Classified
against the source indexes on LXC 182 by exact filename match:

| Source | Count |
|---|---:|
| Flashpoint | **71,695** |
| Archive.org | **22,720** |
| Neither (see §4) | 13 |

Zero unmatched, zero overlap between the two index name sets. This is exact,
not estimated.

---

## 2. What ROMarr has indexed but RomM has not been told about

ROMarr's own streaming indexers on LXC 182 (`build_fp_index.py`,
`build_index.py`) have already done the hard part. Their output is what is
missing from RomM:

| Index on 182 | Entries | In RomM | **Absent from RomM** |
|---|---:|---:|---:|
| `fp-index.jsonl` — Flashpoint | 141,055 | 71,695 | **69,360** |
| `flash-index.jsonl` — Archive.org Flash | 22,720 | 22,720 | 0 |
| `idx-c64.jsonl` — Archive.org | 65,416 | 0 | **65,416** |
| `idx-appleii.jsonl` — Archive.org | 22,439 | 0 | **22,439** |
| `idx-dos.jsonl` — Archive.org | 21,476 | 0 | **21,476** |
| `idx-amiga.jsonl` — Archive.org | 13,196 | 0 | **13,196** |
| `idx-zxs.jsonl` — Archive.org | 12,292 | 0 | **12,292** |
| `idx-acpc.jsonl` — Archive.org | 3,793 | 0 | **3,793** |
| **Archive.org, all platforms** | **161,332** | 22,720 | **138,612** |

The six non-Flash Archive.org indexes have **zero** overlap with RomM —
checked name by name against every row on the matching RomM platforms, not
inferred from the counts. RomM reports 0 catalogued entries on all six.

Upstream of the indexes: `flashpoint.sqlite` on 182 holds **200,332** games,
**182,274** of which have `game_data`. The indexer attempted 182,267 and got
sizes back for 141,055 — the gap is titles whose GameZip could not be reached
or whose zip tail held no `.swf`.

### Against what was believed

| Belief | Measured |
|---|---|
| ~250,000 Archive.org | 161,332 indexed by ROMarr; **22,720 catalogued** in RomM |
| ~130,000 Flashpoint | 200,332 exist upstream, 141,055 indexed, **71,695 catalogued** |
| ~72,000 local | **72,120** — right |

Flashpoint is *bigger* than believed and much less catalogued. Archive.org is
smaller than believed as an index and far smaller than that as a catalogue.
Neither shortfall is a ROMarr counting bug: those rows do not exist in RomM.

---

## 3. What distinguishes an Archive.org entry from a Flashpoint one

Investigated rather than assumed, because the obvious answers all fail:

* **`flashpoint_id` / `flashpoint_metadata`** — RomM 5.1 has both columns.
  They are `null` and `{}` on **all 94,415** catalogued rows. Unusable.
* **`platform_slug`, `fs_path`** — identical (`browser`, `roms/flash`) for
  both catalogues. Unusable.
* **`url_cover`, `metadatum`, hashes** — empty on both. Unusable.
* **`metadata_providers=flashpoint`** — a valid RomM filter that returns **0**.

What does distinguish them is the **filename**, because ROMarr's own indexers
are what wrote it:

| Source | Written by | Shape |
|---|---|---|
| Flashpoint | `build_fp_index.py` | `f"{label}__{gid[:8]}.swf"` — the first 8 hex of the Flashpoint GUID |
| Archive.org | `build_index.py` | `f"{identifier}__{basename}"` — item identifier, then the member file |

`classify_provenance()` reads that. Validated against the 141,055 Flashpoint
and 22,720 Archive.org names ROMarr had indexed:

* **141,055 of 141,055** Flashpoint names matched the `__<8 hex>.<ext>` rule —
  100% recall.
* **18 of 22,720** Archive.org names matched it too — 0.079% false positives,
  items like `jcgerbil__00000001.swf` whose own member filename happens to be
  eight hex digits.
* Run over all 94,415 live rows and compared against exact index membership:
  **99.98% agreement** (94,397 / 94,415).

Anything matching neither rule is reported as `cloud` — "catalogued, source
unknown" — and is never assigned to whichever catalogue is larger. A
breakdown that adds up tidily by guessing is the failure being fixed here,
not a fix for it.

### The new code, run over the live library

`Romm.counts()` and `Romm.games()` were staged in a temp directory on LXC 182
and run against the live RomM (read-only; the running install was not
touched). All 166,548 rows, one pass:

```
Romm.counts()  →  {"total": 166548, "on_disk": 72120, "catalogued": 94428}
provenance     →  {"local": 72120, "flashpoint": 71713,
                   "archive": 22702, "cloud": 13}
```

`local` matches the server's own `on_disk` count exactly, so the two
mechanisms — a server-side filter and a per-row field — agree. `flashpoint`
and `archive` differ from the index-membership ground truth (71,695 /
22,720) by exactly the 18 rows described above, in the direction predicted.
The 13 `cloud` rows are the strays in §4.

---

## 4. Things found along the way

**RomM is indexing non-games as ROMs.** 12 of the 13 catalogued rows that are
not on the `browser` platform are emulator configuration and scraper output
that has since been deleted from disk: `gamelist.xml` on six platforms, plus
`vba.ini`, `Fusion.ini`, `neoragex5.0.ini`, `VirtuaNES.ini`,
`TurboEngine.ini`. They are counted as games by RomM and therefore by ROMarr.
The 13th is a real absence: `Super Mario 3D All-Stars [NSP]` (5.09 GB,
Switch) has a row and no file. Fixing this means changing RomM's scan
exclusions, which is out of scope here — ROMarr reads RomM, it does not
curate it.

**One platform, several RomM rows.** `amiga` appears twice (fs_slug `amiga`
and `amiga1200`), `arcade` twice (`arcade`, `fbneo`), `turbografx-cd` three
times. ROMarr merges them by display name in `_refresh_counts`, and the
cached shelf uses the same display name, so the chips agree — this was
checked for double-counting and is clean.

---

## 5. What ROMarr was getting wrong, and what changed

Every number ROMarr printed was arithmetically correct. The **labels** were
wrong, in four places:

| Surface | Before | Why it was wrong |
|---|---|---|
| Nav badge `games` | `166548` | The sum of two unlike things, labelled as one |
| Stats "Games in library" | `166548` | Same sum, same label, no split anywhere |
| Library page "All ⟨n⟩" | `len(cache)` | The *cached* count, so the Library page and the nav badge showed different numbers for "games" on the same screen |
| Platform chips | `Browser 94,415` | Sat beside `Nintendo DS 8,112` looking like the same claim. Not one of the 94,415 is a file on this disk |

`origin` (`local` / `cloud`) already existed on `Game` and was correct — but
it was only a *filter dropdown*, shown only when the shelf happened to
contain more than one value, and it was never a headline. Nothing on any page
answered "how many of these are actually here".

### The changes

1. **`Romm.counts()`** — three cheap `limit=1` queries using RomM's
   `missing` filter, returning `{total, on_disk, catalogued}`. If the three
   do not reconcile the split is reported as `None`, not guessed: RomM 4.x
   has no `missing` parameter and FastAPI drops undeclared query parameters
   silently, so an unguarded version of this would report "everything is on
   disk **and** everything is catalogued" on older RomMs.
2. **`libraries.library_counts(backend)`** — the protocol seam. Backends that
   can split do; a folder library reports everything as on disk, which is
   true of it.
3. **`Game.provenance`** — `local` / `flashpoint` / `archive` / `cloud`, set
   from the filename by `classify_provenance()`. `origin` still answers "is
   it here"; `provenance` answers "where did it come from". Two questions,
   two fields.
4. **`ROMarr.library_split()`** — one place every surface reads, with
   `counted_from` saying whether the numbers came from the library server or
   from the walked rows. All three always come from the same source: a server
   total beside a cache-derived split gives three numbers that do not add up.
5. **Every surface** now shows on-disk / catalogued / total distinctly:
   `/api/v1/system/counts` gains `games_on_disk` and `games_catalogued`,
   `/api/v1/stats` gains `library_on_disk`, `library_catalogued` and
   `library_sources`, `/api/v1/game` gains `totals` (and keeps `grand_total`
   meaning what it always meant — how many rows are cached right now).
   Platform chips carry `on_disk`/`catalogued`. The shelf can be filtered by
   `source`.

### What this does not do

It does not import anything. The 69,360 Flashpoint and 138,612 Archive.org
entries missing from RomM stay missing until somebody runs the catalogue
step; this work makes their absence visible and countable rather than hidden
inside a single number that looked large enough to be reassuring.
