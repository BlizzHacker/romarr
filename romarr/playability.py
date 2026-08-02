"""How a platform will play, answered before the grab rather than after.

A ROM filed under a platform with no route is *imported but dead*: it appears
in the library, it has a cover, it has metadata, and clicking it does nothing.
Nothing about the library afterwards says why. ROMarr could not say anything
about this at all, which is how the README came to claim that disc platforms
"cannot be streamed into a browser emulator" while nine of them shipped with
EmulatorJS cores in stock RomM.

**Four routes, and the fourth is not a failure.**

  * ``local``    -- EmulatorJS, in the browser, from the library server itself.
  * ``stream``   -- the headless RetroArch stream server, rendering server-side
                    and delivering video. This is how the machines EmulatorJS
                    has no core for are played.
  * ``archive``  -- Archive.org's own in-page emulator. Opening a ``/details/``
                    page *is* playing the game there.
  * ``download`` -- always available. A ROM in a library is a ROM you can take
                    away and run on anything, and an operator who asked for
                    that has been served.

Nothing here refuses an import, and nothing here is allowed to over-claim.
A platform wrongly reported playable sends somebody to a game that displays
and does nothing; a platform wrongly reported unplayable costs them a warning
they did not need. Those are not symmetric, so every table below fails in the
second direction.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

from .platforms import Platform, resolve

log = logging.getLogger(__name__)

LOCAL = "local"
STREAM = "stream"
ARCHIVE = "archive"
DOWNLOAD = "download"

#: Best first. `download` is last because it is the floor, not because it is
#: bad -- it is the only route that is always true.
_ORDER = (LOCAL, STREAM, ARCHIVE, DOWNLOAD)

#: The RomM release `EJS_CORES` was read from.
#:
#: Vendored rather than fetched, for the reason `rom-hub` states and this
#: inherits: RomM publishes no endpoint for its core map, `/api/config` does
#: not carry it, and the map lives in compiled frontend JavaScript. So it is a
#: copy, it is dated, and it will go stale -- which is fine as long as it says
#: which release it is stale relative to.
ROMM_VERSION = "4.9.2"

#: RomM's `_EJS_CORES_MAP`, restricted to the platforms ROMarr models, keyed by
#: ROMarr's slug.
#:
#: **Nine of these are optical systems**, which is the entire factual basis for
#: removing the disc exclusion: psx, psp, saturn, segacd, 3do, philips-cd-i,
#: pc-fx, turbografx-cd and amiga-cd32 play in a browser on a stock RomM, with
#: no configuration and no stream server.
#:
#: Two ROMarr slugs are not keys in RomM's map and are covered by a key naming
#: the same machine -- `genesis-slash-megadrive` by `genesis`,
#: `turbografx-16-slash-pc-engine-cd` by `turbografx-cd`. That equivalence is
#: recorded here rather than acted on anywhere else: this module *reports*, and
#: re-filing a ROM under a neighbouring slug is a different and much worse idea
#: than telling somebody which core will run it.
EJS_CORES: dict[str, tuple[str, ...]] = {
    # -- cartridges ---------------------------------------------------------
    "nes": ("fceumm", "nestopia"),
    "snes": ("snes9x",),
    "gb": ("gambatte", "mgba"),
    "gbc": ("gambatte", "mgba"),
    "gba": ("mgba",),
    "n64": ("mupen64plus_next", "parallel_n64"),
    "genesis-slash-megadrive": ("genesis_plus_gx",),   # RomM key: `genesis`
    "sms": ("genesis_plus_gx",),
    "gamegear": ("genesis_plus_gx",),
    "atari2600": ("stella2014",),
    "atari7800": ("prosystem",),
    "lynx": ("handy",),
    "turbografx16--1": ("mednafen_pce",),              # RomM key: `tg16`
    "wonderswan": ("mednafen_wswan",),
    "neo-geo-pocket": ("mednafen_ngp",),
    "virtualboy": ("beetle_vb",),
    "nds": ("melonds", "desmume", "desmume2015"),
    # -- optical ------------------------------------------------------------
    "psx": ("pcsx_rearmed", "mednafen_psx_hw"),
    "psp": ("ppsspp",),
    "saturn": ("yabause",),
    "segacd": ("genesis_plus_gx", "picodrive"),
    "3do": ("opera",),
    "philips-cd-i": ("same_cdi",),
    "pc-fx": ("mednafen_pcfx",),
    "turbografx-16-slash-pc-engine-cd": ("mednafen_pce",),  # RomM: turbografx-cd
    "amiga-cd32": ("puae",),
}

#: Deliberately absent from the table above, and why, so that "we checked"
#: cannot decay into "nobody looked". Each was confirmed to have no core in
#: RomM 4.9.2's base map and no other slug in that map covering the same
#: machine. They are not dead ends -- `ps2`, `ngc`, `wii`, `dc` and `3ds` all
#: play on the stream tier -- they simply do not play in a browser.
NO_EJS_CORE: dict[str, str] = {
    "ps2": "EmulatorJS ships no PlayStation 2 core; PCSX2 is server-side only",
    "ngc": "EmulatorJS ships no GameCube core; Dolphin is server-side only",
    "wii": "EmulatorJS ships no Wii core; Dolphin is server-side only",
    "dc": "EmulatorJS ships no Dreamcast core; flycast is server-side only",
    "3ds": "azahar is in RomM's nightly cores, which need netplay enabled",
    "neo-geo-cd": "no Neo Geo CD core in RomM's map or on the stream server",
    "atari-jaguar-cd": "no Jaguar CD core in RomM's map or on the stream server",
}

#: When ARCHIVE_EMULATED was measured, and against what.
#:
#: Archive.org records the emulator for an item in its `emulator` metadata
#: field -- 272,424 items carry one -- so this is their data, not an estimate.
#: Counts came from `advancedsearch.php?q=emulator:"<driver>"`.
ARCHIVE_MEASURED_ON = "2026-08-02"

#: Platforms Archive.org's in-page emulator actually runs, with the item count
#: behind each.
#:
#: **The disc systems are absent and that is the finding, not an omission.**
#: The drivers return nothing: `psj`/`psu`/`pse` 0 items, `saturn` 0, `3do` 0,
#: `dc` 2, `segacd` 1. Archive.org is a *source* for disc images and not a
#: player of them, so offering this route for a PlayStation request would send
#: an operator to a details page with a download button and no emulator.
ARCHIVE_EMULATED: dict[str, int] = {
    "genesis-slash-megadrive": 12877,   # drivers `genesis` + `megadriv`
    "atari2600": 3123,
    "nes": 514,
    "snes": 538,
    "gbc": 553,
    "gb": 553,
    "gba": 483,
    "n64": 8,
}

#: How long to wait on a stream server. It is a LAN service answering out of an
#: in-memory table, and one that is down must not hold up a page.
STREAM_TIMEOUT = 5.0

#: The one endpoint this module calls. A GET that reads a routing table: it
#: allocates no display, starts no emulator and creates no session.
PLAY_ROUTE_PATH = "/api/play/route"


@dataclass(frozen=True)
class Route:
    """One way this platform can be played."""

    kind: str
    detail: str


@dataclass(frozen=True)
class Playability:
    """Every route open to one platform, best first."""

    platform: str
    routes: tuple[Route, ...]
    #: Set when a stream server was configured and could not be reached, so
    #: the absence of a stream route can be told apart from a refusal.
    stream_unreachable: str = ""

    @property
    def kinds(self) -> tuple[str, ...]:
        return tuple(r.kind for r in self.routes)

    @property
    def plays_without_downloading(self) -> bool:
        """Whether somebody can press play and have a game start.

        The honest question, and the reason `download` is not counted: taking
        a 4GB image away to run it elsewhere is a real answer to "can I have
        this", and not an answer to "can I play this here".
        """
        return any(r.kind != DOWNLOAD for r in self.routes)

    def summary(self) -> str:
        """One line for a UI, an API field or a log."""
        parts = [f"{self.platform}:"]
        parts.extend(r.detail for r in self.routes)
        if self.stream_unreachable:
            parts.append(f"(stream server unreachable: {self.stream_unreachable})")
        return " ".join(parts)


def routes_for(platform, *, stream=None) -> Playability:
    """How `platform` will play. Accepts a `Platform` or any name that resolves.

    `stream` is an optional object with `.tier(slug) -> str | None`, normally a
    `StreamServer`. It is asked last and trusted first: it is the only source
    that knows which cores are actually installed on the operator's own
    machine, while the tables here know only what the software ships with.
    """
    if isinstance(platform, Platform):
        resolved, name, slug = platform, platform.name, platform.slug
    else:
        resolved = resolve(str(platform or ""))
        slug = resolved.slug if resolved else str(platform or "")
        name = resolved.name if resolved else slug

    found: dict[str, Route] = {}
    unreachable = ""

    cores = EJS_CORES.get(slug)
    if cores:
        found[LOCAL] = Route(
            LOCAL,
            f"plays in the browser on EmulatorJS ({', '.join(cores)})")

    if stream is not None:
        tier = None
        try:
            tier = stream.tier(slug)
        except Exception as exc:            # a LAN service being down is normal
            log.warning("stream server did not answer for %r: %s", slug, exc)
            unreachable = str(exc)
        if not unreachable and not getattr(stream, "reachable", True):
            unreachable = "no answer"
        where = getattr(stream, "label", "the stream server")
        if tier == STREAM:
            found[STREAM] = Route(
                STREAM,
                f"streams server-side from {where} (headless RetroArch)")
        elif tier == LOCAL and LOCAL not in found:
            # The server distinguishes the tiers, and so must this: reporting
            # "streamed server-side" for something the browser runs is a lie
            # about where the work happens.
            found[LOCAL] = Route(
                LOCAL, f"plays in the browser on EmulatorJS (per {where})")

    if slug in ARCHIVE_EMULATED:
        found[ARCHIVE] = Route(
            ARCHIVE,
            f"plays in the page on Archive.org "
            f"({ARCHIVE_EMULATED[slug]:,} emulated items)")

    if LOCAL not in found and STREAM not in found:
        # Prefer the stream server's own words when it gave any.
        #
        # It knows things this module cannot: which cores are installed, and
        # whether the firmware they need is present. Without that, a platform
        # whose core is installed but BIOS-less reads "configure a stream
        # server to play it here" to an operator who has already configured
        # one -- pointing at the wrong fix, and hiding the one that works.
        # That is the same firmware gate that exists because a core with no
        # BIOS does not fail loudly: it draws an error screen and streams it
        # at a perfectly healthy 30fps.
        served = _stream_reason(stream, slug) if stream is not None else ""
        if served:
            detail = f"download only -- {served}"
        else:
            reason = NO_EJS_CORE.get(slug, "no browser core for this platform")
            detail = (f"download only -- {reason}; configure a stream server "
                      "to play it here")
        found[DOWNLOAD] = Route(DOWNLOAD, detail)
    else:
        found[DOWNLOAD] = Route(DOWNLOAD, "downloadable")

    ordered = tuple(found[k] for k in _ORDER if k in found)
    return Playability(name, ordered, stream_unreachable=unreachable)


def _stream_reason(stream, slug: str) -> str:
    """The stream server's own explanation, if it offered one."""
    why = getattr(stream, "why", None)
    if not callable(why):
        return ""
    try:
        return str(why(slug) or "")
    except Exception:
        return ""


class StreamServer:
    """Read-only client for a headless RetroArch stream server's routing.

    One endpoint, one question: could this server play this platform, and on
    which tier. It has no method that starts anything -- the session routes
    take a ROM name that must resolve inside the server's own directory, so
    there is nothing here for ROMarr to hand them anyway.

    Answers are cached for the process lifetime. The routing table is built
    from what is installed on disk, which does not change while the server is
    up, and a Library page must not make one HTTP call per row.

    **A server that is down stops being asked**, which is not an optimisation.
    Only successful answers are cached -- a failure must not be remembered as
    "cannot play this" -- so without a breaker the first status page after the
    stream server goes down makes one timing-out request per platform. At
    thirty-odd platforms and a five-second timeout that is nearly three
    minutes of hanging page for a service that is optional. Measured at 69
    seconds before this existed.
    """

    #: How long to stop asking after a failure. Long enough that a down server
    #: costs one timeout per page rather than thirty; short enough that a
    #: restarted one is picked up without restarting ROMarr.
    DOWN_COOLDOWN = 30.0

    def __init__(self, base_url: str, timeout: float = STREAM_TIMEOUT):
        self.base_url = (base_url or "").rstrip("/")
        self.timeout = timeout
        self._cache: dict[str, str | None] = {}
        self._reachable = True
        self._problem = ""
        self._down_until = 0.0
        self._why: dict[str, str] = {}

    def why(self, slug: str) -> str:
        """Why this server cannot play `slug`, in its own words.

        The server distinguishes "no core exists", "the core is not installed"
        and "supply firmware" -- three very different problems for an operator,
        and only it knows which applies. Blank when it has not been asked, or
        answered with a tier.
        """
        return self._why.get(slug, "")

    @property
    def label(self) -> str:
        return self.base_url or "the stream server"

    @property
    def reachable(self) -> bool:
        return self._reachable

    def tier(self, slug: str) -> str | None:
        """"local", "stream", or None when the server cannot play it.

        None is also the answer when the server is unreachable. The caller
        tells the two apart with `reachable`, because "cannot play this" and
        "did not answer" lead an operator to different fixes.
        """
        if not self.base_url or not slug:
            return None
        if slug in self._cache:
            return self._cache[slug]
        if time.monotonic() < self._down_until:
            self._reachable = False
            return None

        url = (f"{self.base_url}{PLAY_ROUTE_PATH}?"
               + urllib.parse.urlencode({"platform": slug}))
        try:
            with urllib.request.urlopen(url, timeout=self.timeout) as response:
                body = json.loads(response.read().decode("utf-8"))
            tier = body.get("tier")
            tier = tier if isinstance(tier, str) else None
            self._reachable = True
        except urllib.error.HTTPError as exc:
            # 404 is the server's documented "unplayable" answer and is a real
            # answer, not a failure -- it carries `why`.
            self._reachable = True
            tier = None
            if exc.code == 404:
                try:
                    said = json.loads(exc.read().decode("utf-8")).get("why")
                    if isinstance(said, str) and said:
                        self._why[slug] = said
                except Exception:
                    pass
            else:
                self._reachable, self._problem = False, f"HTTP {exc.code}"
        except Exception as exc:
            self._reachable, self._problem = False, str(exc)
            tier = None

        if self._reachable:
            self._cache[slug] = tier
            self._down_until = 0.0
        else:
            self._down_until = time.monotonic() + self.DOWN_COOLDOWN
        return tier
