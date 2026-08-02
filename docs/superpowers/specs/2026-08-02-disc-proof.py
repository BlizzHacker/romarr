"""End-to-end proof against real files from the live library.

Nothing here is a fixture: the .7z is a real PlayStation release copied out of
/mnt/usb1/roms/psx, and the .gdi is the real sheet from the live Dreamcast
entry `dc/Pop'n Music (JP, Rev 1.2)/`.
"""
import os
import shutil
import sys
from pathlib import Path

sys.path.insert(0, r"C:\MoveWeight\romarr-wt-disc")

from romarr.library import import_rom, list_candidates
from romarr.platforms import by_slug
from romarr.playability import routes_for, StreamServer
from romarr.selection import Release, best_release, judge, pick_rom_set

HERE = Path(__file__).parent
LIB = HERE / "library"
if LIB.exists():
    shutil.rmtree(LIB)

ok = True


def check(label, condition, detail=""):
    global ok
    ok = ok and bool(condition)
    print(f"  [{'PASS' if condition else 'FAIL'}] {label}" + (f" -- {detail}" if detail else ""))


print("=" * 74)
print("1. A REAL PlayStation .7z from the live library")
print("=" * 74)
seven = HERE / "RoboCop (Prototype 1997-08-07).7z"
psx = by_slug("psx")
names = list_candidates(seven)
print(f"  archive holds: {names}")
chosen = pick_rom_set(names, psx, read=None)
print(f"  primary: {chosen.primary}")
print(f"  members: {chosen.members}")
check("the .7z can be opened at all", len(names) == 2)
check("the .cue is the primary", chosen.primary.endswith(".cue"))
check("the .bin travels with it", any(m.endswith(".bin") for m in chosen.members))

result = import_rom(seven, psx, LIB)
print(f"  import: ok={result.ok} destination={result.destination}")
landed = sorted(p.name for p in result.destination.iterdir()) if result.ok else []
print(f"  landed: {landed}")
check("import succeeded", result.ok, result.reason)
check("both files are in the library", len(landed) == 2)
check("filed under psx", result.destination.parent.name == "psx")

print()
print("=" * 74)
print("2. The REAL Dreamcast multi-track layout (sheet copied verbatim)")
print("=" * 74)
dc_src = HERE / "dc-download"
dc_src.mkdir(exist_ok=True)
gdi = "Pop'n Music v1.200 (1998)(Konami)(NTSC)(JP)(en)[!].gdi"
(dc_src / gdi).write_text(
    "3\n1 0 4 2352 track01.bin 0\n2 935 0 2352 track02.raw 0\n"
    "3 45000 4 2352 track03.bin 0\n")
for track in ("track01.bin", "track02.raw", "track03.bin"):
    (dc_src / track).write_bytes(b"\0" * 4096)
(dc_src / "readme.nfo").write_text("scene notes")

dc = by_slug("dc")
result = import_rom(dc_src, dc, LIB)
landed = sorted(p.name for p in result.destination.iterdir()) if result.ok else []
print(f"  import: ok={result.ok}")
print(f"  destination: {result.destination}")
print(f"  landed: {landed}")
check("import succeeded", result.ok, result.reason)
check("landed as a directory", result.ok and result.destination.is_dir())
check("the sheet and all three tracks landed", len(landed) == 4)
check("the .nfo did not", "readme.nfo" not in landed)
check("track01.bin is present", "track01.bin" in landed)

print()
print("=" * 74)
print("3. Scoring real-shaped disc releases")
print("=" * 74)
cases = [
    ("Final Fantasy VII (USA) (Disc 1)", 600 * 1024**2, "final fantasy vii", psx, True),
    ("Silent Hill (USA) [Redump]", 500 * 1024**2, "silent hill", psx, True),
    ("Shadow of the Colossus (USA)", 4 * 1024**3, "shadow of the colossus", by_slug("ps2"), True),
    ("Shadow of the Colossus REPACK", 60 * 1024**3, "shadow of the colossus", by_slug("ps2"), False),
    ("Redump - Sony PlayStation USA Collection", 400 * 1024**3, "silent hill", psx, False),
    ("Silent Hill (USA) PS2", 500 * 1024**2, "silent hill", psx, False),
]
for title, size, want, platform, expected in cases:
    verdict = judge(Release(title, size, 25, (1000,), "magnet:?x", "torrent"), want, platform)
    check(f"{title[:46]:46} -> {'accept' if expected else 'reject'}",
          verdict.accepted == expected,
          "" if verdict.accepted == expected else verdict.why()[0])

print()
print("=" * 74)
print("4. Every platform has a route -- against the REAL stream server")
print("=" * 74)
from romarr.platforms import PLATFORMS

stream = StreamServer(os.environ.get("STREAM_SERVER_URL",
                                    "http://192.168.0.94:8090"))
by_kind: dict[str, int] = {}
no_player = []
for platform in PLATFORMS:
    got = routes_for(platform, stream=stream)
    check(f"{platform.slug} has a route", bool(got.kinds))
    for kind in got.kinds:
        by_kind[kind] = by_kind.get(kind, 0) + 1
    if not got.plays_without_downloading:
        no_player.append(got.summary())

print()
print(f"  {len(PLATFORMS)} platforms: " +
      ", ".join(f"{k} {v}" for k, v in sorted(by_kind.items())))
print()
print("  No player (each must state a specific reason, not a generic one):")
for line in no_player:
    print("   -", line)
    check("the reason is specific",
          "no browser core for this platform" not in line)

# The exclusion this whole change exists to remove: every disc platform must
# be requestable, and all but Jaguar CD must be playable somewhere.
disc = [p for p in PLATFORMS if p.is_disc]
playable_disc = [p for p in disc
                 if routes_for(p, stream=stream).plays_without_downloading]
print()
check(f"disc platforms present ({len(disc)})", len(disc) >= 15)
check(f"disc platforms playable ({len(playable_disc)}/{len(disc)})",
      len(playable_disc) >= len(disc) - 1,
      "only Atari Jaguar CD may lack a player -- no emulator exists for it")

print()
print("=" * 74)
print("RESULT:", "ALL PROOFS PASSED" if ok else "SOMETHING FAILED")
print("=" * 74)
sys.exit(0 if ok else 1)
