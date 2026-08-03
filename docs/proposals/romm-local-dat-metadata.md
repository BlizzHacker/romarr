# Proposal: offline metadata identification from local DAT files

Submitted upstream to [RomM](https://github.com/rommapp/romm) as a gift from
the Cartridge project. The same proposal suits Gaseous and Retrom, which have
the same gap.

## The gap, stated against what RomM already has

RomM already identifies ROMs by hash, and does it well. `hasheous_handler.py`
sends CRC/MD5/SHA1 to [Hasheous](https://hasheous.org) and
`playmatch_handler.py` uses IGDB's PlayMatch. Both are better than filename
matching and this proposal does not replace either.

Both are also **network services**. That means hash identification today:

- requires the server to reach a third party, so it does not work on an
  airgapped or LAN-only install;
- depends on that service staying up, staying free, and not rate-limiting a
  first scan of a large library;
- cannot be verified or audited locally — you get an answer, not a reason.

There is no offline path. `grep -rl "no-intro\|logiqx\|datfile" backend/`
returns nothing relevant.

## The proposal

Add a metadata handler that reads **local Logiqx DAT files** — the format
No-Intro and Redump publish — and identifies a ROM by looking its hash up in
them.

This is not a new idea in the preservation world; it is what RomVault, Igir
and clrmamepro have done for twenty years. It is new *to this class of
application*, and it composes with what RomM has rather than competing:

```
hash -> local DAT      (offline, exact, auditable)   <- proposed
     -> Hasheous       (online, broad)               <- exists
     -> PlayMatch      (online, IGDB-linked)         <- exists
     -> filename       (fallback)                    <- exists
```

Three things fall out of it that RomM cannot currently do:

1. **Identification with no network at all.** The DATs are files the operator
   already has or can fetch once.
2. **A verification verdict, not just a name.** A DAT says what the correct
   dump *is*, so a file can be reported as `verified`, `bad-dump` (right size,
   wrong checksum — corrupt or patched) or `unknown`. RomM currently has no
   way to tell a user that a ROM in their library is corrupt.
3. **An exact metadata key.** A verified file has a canonical No-Intro name,
   so the subsequent IGDB/Moby/SS lookup can be keyed on `Chrono Trigger`
   rather than on whatever `Chrono.Trigger.USA.Retranslated.v1.2.[!].smc`
   parses to. This is where most wrong cover art comes from, and it fails
   silently — a cover for the wrong game looks exactly like a cover for the
   right one.

## Reference implementation

Working, tested Python, AGPL-compatible, and free to take wholesale:

- [`romarr/dat.py`](https://github.com/BlizzHacker/romarr/blob/main/romarr/dat.py)
  — Logiqx parser, hash index, 1G1R, ~330 lines
- [`romarr/metadata.py`](https://github.com/BlizzHacker/romarr/blob/main/romarr/metadata.py)
  — the "verified name becomes the lookup key" part
- [`tests/test_dat.py`](https://github.com/BlizzHacker/romarr/blob/main/tests/test_dat.py)
  — 28 tests including the traps below

### The traps, because they are the whole difficulty

Anyone implementing this will hit these, and each one produces a feature that
looks broken rather than one that looks wrong:

**Copier headers.** No-Intro hashes cartridge *contents*. iNES (16 bytes),
fwNES (16), Lynx (64) and Atari 7800 (128) headers are added afterwards, and a
Super Nintendo dump may or may not carry a 512-byte copier header with nothing
in the extension to say which. Hash the file as it sits and you get a checksum
that appears in no DAT ever published — so every NES and SNES ROM comes back
`unknown` and the feature looks useless. The SNES rule is arithmetic:
cartridge data is a whole number of 32 KB banks, so a file 512 bytes over a
multiple of 32768 is headered.

**Discs are sets.** Redump lists every track of a disc as its own `<rom>`, so
a `.cue` with a correct checksum beside a corrupt `.bin` is not a good dump.
Judging the set by its first member misses exactly the case worth catching.

**`unknown` is not `bad`.** Absence from a DAT means homebrew, a translation,
a new release, or an out-of-date DAT. Treating absence as corruption would
have RomM telling users their perfectly good files are broken.

**SHA1 over CRC32 where both exist.** CRC32 is 32 bits and collides; across a
full DAT collection that stops being theoretical.

**Stream the hash.** A PS2 image is several gigabytes. Deciding whether to
skip a 512-byte header must be done from the file's *length*, not by reading
it — that mistake reintroduces a full-file buffer one line above the chunked
hashing that exists to prevent it.

## Why this is being offered rather than built as a plugin

Cartridge maintains [ROM Hub](https://github.com/BlizzHacker/rom-hub), a
plugin host that sits beside a library server and never modifies it, and this
could live there. It is being offered upstream because identification is a
property of the library rather than of a tool beside it: a ROM that RomM knows
is a verified No-Intro dump is more useful to *every* RomM client — the web
UI, the mobile apps, EmulatorJS — than one that only ROMarr knows about.

If it is not wanted upstream, that is a completely reasonable answer and no
follow-up is needed; ROM Hub will expose it through its `metadata` capability
instead.

## Scope

Deliberately small. One handler, one settings block pointing at a DAT
directory, one nullable `verification_status` on the ROM model. No UI beyond
surfacing the verdict where the hash is already shown, no changes to existing
handlers, and no new runtime dependency — the DAT format is XML and hashing is
`hashlib` and `zlib`.

Happy to open the PR if there is interest in the shape.
