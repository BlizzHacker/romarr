"""How a platform will play, answered before the grab rather than after.

A ROM filed under a platform with no route is *imported but dead*: it appears
in the library, it has a cover, it has metadata, and clicking it does nothing.
Nothing about the library afterwards says why. ROMarr could not say anything
about this at all, which is how the README came to claim that disc platforms
"cannot be streamed into a browser emulator" while nine of them shipped with
EmulatorJS cores in stock RomM.

**Four routes, and the fourth is not a failure.**

  * ``local``    -- EmulatorJS, in the browser, from the library server itself.
  * ``stream``   -- a stream server, rendering server-side and delivering
                    video. This is how the machines EmulatorJS has no core for
                    are played. Two kinds answer here: the headless RetroArch
                    stream server, which knows about platforms and can be
                    asked; and a Moonlight host -- Wolf, Sunshine, or Steam
                    Headless -- which is a desktop and cannot be. See
                    `MoonlightHost` for what that difference costs.
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

import base64
import http.client
import json
import logging
import re
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

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
    # -- consoles, handhelds and boards -------------------------------------
    "jaguar": ("virtualjaguar",),
    "arcade": ("mame2003", "mame2003_plus", "fbneo",
               "fbalpha2012_cps1", "fbalpha2012_cps2"),
    "neogeoaes": ("fbneo",),
    "neogeomvs": ("fbneo",),
    "atari5200": ("a5200",),
    "colecovision": ("gearcoleco",),
    "sega32": ("picodrive",),
    "supergrafx": ("mednafen_pce",),
    "wonderswan-color": ("mednafen_wswan",),
    "neo-geo-pocket-color": ("mednafen_ngp",),
    "fds": ("fceumm", "nestopia"),
    "famicom": ("fceumm", "nestopia"),
    "sfam": ("snes9x",),
    # -- home computers -----------------------------------------------------
    "c64": ("vice_x64sc", "vice_x64"),
    "c128": ("vice_x128",),
    "vic-20": ("vice_xvic",),
    "amiga": ("puae",),
    "acpc": ("cap32", "crocods"),
    "zxs": ("fuse",),
    "dos": ("dosbox_pure",),
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
    "neo-geo-cd": (
        "EmulatorJS ships no Neo Geo CD core; NeoCD is server-side only "
        "(fbneo's AES/MVS drivers are cartridge hardware and cannot open a "
        "cue or a chd)"),
    "atari-jaguar-cd": (
        "no emulator plays Jaguar CD. virtualjaguar is the only Jaguar core "
        "in libretro and declares j64|jag|rom|abs|cof|bin|prg -- cartridges "
        "only, no cue and no chd"),
    # Home computers EmulatorJS has no core for. All four run on the stream
    # tier, and all four want firmware the operator supplies -- which is a
    # different problem from having no emulator, and is said differently.
    "msx": "EmulatorJS ships no MSX core; blueMSX is server-side only",
    "msx2": "EmulatorJS ships no MSX2 core; blueMSX is server-side only",
    "vectrex": "EmulatorJS ships no Vectrex core; vecx is server-side only",
    "intellivision": (
        "freeintv is in RomM's nightly cores, which need netplay enabled; "
        "the stream server runs it server-side"),
    "sharp-x68000": (
        "EmulatorJS ships no X68000 core; px68k is server-side only"),
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
        # Who answered, and what is doing the rendering there. The engine used
        # to be the literal string "headless RetroArch", which was true of the
        # only stream server that existed and became a lie the moment a
        # Moonlight host could answer here -- an operator streaming PS2 off
        # Wolf's PCSX2 does not want to be told RetroArch is running it.
        #
        # Asked per platform rather than read off the object, because with
        # more than one server configured the one that answered for *this*
        # slug is the only one worth naming.
        where, engine = _stream_attribution(stream, slug)
        if tier == STREAM:
            found[STREAM] = Route(
                STREAM,
                f"streams server-side from {where} ({engine})")
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


def _stream_attribution(stream, slug: str) -> tuple[str, str]:
    """Who to name for `slug`, and what to call the thing rendering there.

    A single server answers for itself. A `StreamSources` holding several has
    to work out which of them claimed this platform, which is why the lookup
    takes a slug at all -- naming the first configured server for a route the
    second one granted is exactly the kind of confident wrong sentence this
    module exists to avoid.
    """
    attribute = getattr(stream, "attribution", None)
    if callable(attribute):
        try:
            label, engine = attribute(slug)
            return str(label), str(engine)
        except Exception:
            pass
    return (getattr(stream, "label", "the stream server"),
            getattr(stream, "engine", "headless RetroArch"))


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

    Answers are cached, because a Library page must not make one HTTP call per
    row. **With a TTL, not for the process lifetime**, which is what it was
    until installing a core proved the assumption wrong: the routing table is
    built from what is on disk, and "that does not change while the server is
    up" is false the moment somebody installs a core and restarts it. Neo Geo
    CD went from unplayable to streaming and ROMarr kept saying "no emulator
    exists" until it was restarted, which is a confusing way to be told that
    your work succeeded.

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

    #: How long a successful answer is trusted. Short enough that installing a
    #: core is visible without restarting ROMarr, long enough that browsing
    #: costs nothing.
    CACHE_TTL = 300.0

    def __init__(self, base_url: str, timeout: float = STREAM_TIMEOUT):
        self.base_url = (base_url or "").rstrip("/")
        self.timeout = timeout
        self._cache: dict[str, tuple[str | None, float]] = {}
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
        cached = self._cache.get(slug)
        if cached is not None and time.monotonic() < cached[1]:
            return cached[0]
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
            self._cache[slug] = (tier, time.monotonic() + self.CACHE_TTL)
            self._down_until = 0.0
        else:
            self._down_until = time.monotonic() + self.DOWN_COOLDOWN
        return tier


# ===========================================================================
# Moonlight hosts: Wolf, Sunshine, Steam Headless
# ===========================================================================
#
# A different animal from the RetroArch stream server above, and the
# difference is the whole reason this section is long.
#
# The stream server was *built* to answer "can you play this platform". It has
# `/api/play/route`, it reads its own core directory, and one GET settles the
# question. A Moonlight host has no such endpoint and never will, because it
# is not a platform router -- it is a machine with a desktop on it. Wolf runs
# containers, Sunshine runs whatever you put in `apps.json`, Steam Headless
# runs Steam. None of them has any concept of "PlayStation 2"; they have a
# list of *applications*, and what those applications can open is a question
# about somebody's filesystem that no API exposes.
#
# So the honest position, and the one every table below implements:
#
#   * A reachable Moonlight host is worth reporting. It is a real play route
#     for a human being, and a status page that omits it is lying by silence.
#   * A reachable Moonlight host grants a *platform* a stream route only when
#     an app on it names an emulator that plays exactly one machine. "PCSX2"
#     is evidence. "RetroArch" and "Steam" are not, and are refused below by
#     name rather than by accident.
#   * When the app list could not be read at all -- which is the default,
#     because reading it needs an admin credential ROMarr may not have -- the
#     host grants nothing and says so. Reachability is not capability.
#
# The over-claim this avoids is specific and was worth designing around: an
# operator with Wolf running sees "PS2 streams from Wolf", clicks a PS2 game,
# and gets Wolf's app list with Firefox and Steam on it. That is a worse
# outcome than never having offered the route.


#: Wolf, Sunshine and NVIDIA's original GameStream all speak this. The base
#: port is 47989 for plain HTTP and 47984 for TLS; Wolf hardcodes the same two
#: (`state/data-structures.hpp`, `HTTP_PORT`/`HTTPS_PORT`) and Sunshine maps
#: them off the same base, so one probe reaches every implementation.
MOONLIGHT_HTTP_PORT = 47989
MOONLIGHT_HTTPS_PORT = 47984

#: Sunshine's admin API and web UI, which is the Moonlight base port plus one.
#: HTTPS with a self-signed certificate, always -- see `_sunshine_call` for
#: why that forces an unverified context and what is given up by it.
SUNSHINE_WEB_PORT = 47990

#: The one thing every Moonlight host answers without a credential, without a
#: certificate and without being paired.
#:
#: It is served on the *plain HTTP* port by both implementations
#: (`nvhttp.cpp` `http_server.resource["^/serverinfo$"]` in Sunshine,
#: `servers.cpp` `server->resource["^/serverinfo$"]` in Wolf) and it returns
#: XML naming the host, its version, its HTTPS port and whether a game is
#: currently running. Everything else in the Moonlight protocol -- `/applist`,
#: `/launch`, `/resume` -- is registered on the HTTPS server behind a *client
#: certificate check*, so this is not a starting point that leads anywhere:
#: it is the entire unauthenticated surface, and it is a dead end by design.
SERVERINFO_PATH = "/serverinfo"

#: Host kinds. Declared by the operator, never sniffed.
#:
#: There is no reliable way to tell Wolf from Sunshine over `/serverinfo`.
#: Wolf emits the same field set, including the literal `SUNSHINE_SERVER_FREE`
#: in `root.state` (`moonlight.cpp`), because Moonlight clients require that
#: exact string. Wolf's stock `hostname` is "Wolf", which is a hint and not a
#: fact -- it is the first thing an operator renames -- so it is not acted on.
WOLF = "wolf"
SUNSHINE = "sunshine"
STEAM_HEADLESS = "steam-headless"
MOONLIGHT_KINDS = (WOLF, SUNSHINE, STEAM_HEADLESS)

#: What each kind is called, for the UI and the route detail.
#:
#: Steam Headless is not a fourth protocol and is not modelled as one: it is a
#: container that runs Sunshine (`ENABLE_SUNSHINE=true`, port 47990) next to a
#: noVNC/neko desktop. Its streaming surface *is* Sunshine's, so it reuses
#: Sunshine's client wholesale and differs only in what the UI calls it and in
#: carrying a web-desktop URL alongside.
KIND_LABELS: dict[str, str] = {
    WOLF: "Wolf",
    SUNSHINE: "Sunshine",
    STEAM_HEADLESS: "Steam Headless",
}

#: Wolf's REST API prefix. UNIX socket only unless the operator proxies it.
WOLF_API_PREFIX = "/api/v1"

#: Sunshine's admin REST API prefix, on `SUNSHINE_WEB_PORT`.
SUNSHINE_API_PREFIX = "/api"

#: Emulators that play exactly one machine, so their presence on a host's app
#: list is evidence rather than a guess.
#:
#: Matched against the app *title*, case-insensitively. Every entry here is an
#: emulator whose entire purpose is one console -- there is no configuration
#: of PCSX2 that plays a Dreamcast disc -- which is what makes the inference
#: safe. Dolphin carries two slugs because Dolphin genuinely is the GameCube
#: *and* Wii emulator, not because the table is hedging.
#:
#: Anything whose scope depends on what the operator installed inside it is
#: deliberately absent; see `MULTI_MACHINE_APPS`. The empty tuples are entries
#: that were considered and rejected, kept so the reasoning is on record.
EMULATOR_APPS: dict[str, tuple[str, ...]] = {
    "pcsx2": ("ps2",),
    "dolphin": ("ngc", "wii"),
    "flycast": ("dc",),
    "redream": ("dc",),
    "reicast": ("dc",),
    "duckstation": ("psx",),
    "ppsspp": ("psp",),
    "azahar": ("3ds",),
    "citra": ("3ds",),
    "lime3ds": ("3ds",),
    "melonds": ("nds",),
    "desmume": ("nds",),
    "yabause": ("saturn",),
    "dosbox": ("dos",),
    # Considered and rejected, kept so the reasoning is on record rather than
    # rediscovered. Mesen was briefly mapped to `nes` and that was wrong:
    # Mesen 2 also runs SNES, Game Boy and PC Engine, so the name does not
    # narrow to one machine and the whole basis of this table collapses.
    "mesen": (),
    "mednafen": (),          # multi-system, for the same reason
    "scummvm": (),           # not a platform ROMarr models
}

#: Apps that are real and useful and prove nothing about any one platform.
#:
#: This table exists so that "we checked" cannot decay into "nobody looked" --
#: the same reason `NO_EJS_CORE` exists. Each of these ships in Wolf's stock
#: `config.v5.toml` or is the obvious thing an operator adds, each will happily
#: play half this library, and none of them can be *asked* what it will play.
#: A RetroArch with no cores installed and a RetroArch with forty look
#: identical from outside the container.
MULTI_MACHINE_APPS: dict[str, str] = {
    "retroarch": ("RetroArch plays whatever cores are installed inside it, "
                  "and the app list does not say which"),
    "emulationstation": ("EmulationStation is a frontend; what it launches "
                         "is decided by config files ROMarr cannot see"),
    "pegasus": ("Pegasus is a frontend; what it launches is decided by "
                "config files ROMarr cannot see"),
    "steam": "Steam plays PC games, not the platforms in this library",
    "lutris": "Lutris runs whatever runners are installed inside it",
    "kodi": "Kodi is a media player",
    "firefox": "Firefox is a browser",
    "prismlauncher": "Prism Launcher runs Minecraft",
    "desktop": ("a desktop session plays whatever is installed on it, which "
                "is not a question any API answers"),
    "testball": "Wolf's built-in test pattern",
}

#: How long to wait on a Moonlight host. Same reasoning as `STREAM_TIMEOUT`:
#: a LAN service that is down must not hold up a page.
MOONLIGHT_TIMEOUT = 5.0

#: One sentence, kept in one place, because it has to appear in the API, the
#: UI and the docs and must not drift between them.
PAIRING_IS_MANUAL = (
    "Moonlight pairing cannot be automated. The PIN is created by your "
    "Moonlight client, on your device -- no host API can produce it or skip "
    "it. Start pairing in Moonlight, read the PIN it shows you, and type it "
    "here.")


def _match_app(title: str) -> tuple[str, tuple[str, ...]] | None:
    """Which table entry an app title matches, if any.

    Compared against the title with its spaces and punctuation removed,
    because the titles in the wild are "PCSX2", "pcsx2-qt", "Dolphin
    Emulator", "RetroArch (Flatpak)" and Wolf's own "Test ball" -- and a
    word-boundary match misses half of those. The single-machine table is
    consulted first so that a host with both PCSX2 and RetroArch on it still
    gains PS2 rather than being talked out of it by the generic entry.
    """
    squashed = re.sub(r"[^a-z0-9]+", "", (title or "").lower())
    if not squashed:
        return None
    for name, slugs in EMULATOR_APPS.items():
        if name in squashed:
            return name, slugs
    for name in MULTI_MACHINE_APPS:
        if name in squashed:
            return name, ()
    return None


def platforms_from_apps(titles) -> dict[str, str]:
    """slug -> the app title that proves it, for every app that proves one.

    The public form of the rule this whole section exists to state: an app
    list becomes platform coverage only through `EMULATOR_APPS`, and only for
    machines ROMarr actually models.
    """
    found: dict[str, str] = {}
    for title in titles or ():
        matched = _match_app(title)
        if not matched:
            continue
        for slug in matched[1]:
            if resolve(slug) is not None:
                found.setdefault(slug, title)
    return found


@dataclass
class MoonlightPairing:
    """What a human has to do, and what ROMarr can do around it.

    **Moonlight pairing cannot be automated, and this class does not pretend
    otherwise.** The protocol is a four-phase exchange in which the *client*
    generates a PIN, both sides derive `SHA256(SALT + PIN)[0:16]` as an AES
    key, and the client proves possession of it. Wolf's own field
    documentation says it outright -- `PairRequest.pin` in `api/api.hpp` is
    annotated "The PIN created by the remote Moonlight client". The PIN exists
    on a screen in somebody's hand. No API on the host side can produce it,
    guess it, or skip it, and any ROMarr feature that claimed to pair a client
    on its own would be a lie with a progress bar.

    What ROMarr *can* do is everything on either side of the human:

      * notice that a client is waiting (Wolf lists them; Sunshine does not),
      * be the box the PIN is typed into, and post it to the host,
      * report afterwards whether the host now lists the client as paired.

    That is a real convenience -- it is one fewer admin panel to find, and on
    Wolf it replaces hunting through container logs for a URL with a secret
    in the fragment -- and it is the whole of what is possible.
    """

    #: Pending requests the host is willing to name. Wolf answers this from
    #: `/api/v1/pair/pending` with `{pair_secret, client_ip}` per request.
    #: Sunshine has no equivalent endpoint at all, so this is always empty for
    #: Sunshine and Steam Headless and the UI must not read empty as "nobody
    #: is waiting" there.
    pending: list[dict] = field(default_factory=list)
    #: False when the host has no way to report pending requests, so that the
    #: absence of a list can be told apart from an empty one.
    can_list_pending: bool = True
    #: Why pending requests could not be listed, in one line.
    detail: str = ""


class MoonlightHost:
    """Read-mostly client for a Wolf, Sunshine or Steam Headless host.

    Duck-types `StreamServer` -- `tier`, `why`, `reachable`, `label` -- so
    `routes_for` consumes it unchanged. That was the point of extending the
    stream tier rather than inventing a fifth route: "played somewhere else
    and delivered as video" is one idea, and an operator does not care whether
    the pixels came from RetroArch or from Wolf.

    Three things this can do, and it is worth being blunt that the list is
    short because the hosts are what they are, not because the client is
    unfinished:

      * **Probe.** `GET /serverinfo` on the plain HTTP port, no credential.
        Works on every implementation. Yields the host's name, version and
        whether a game is running. This is the only call that never fails for
        want of configuration.
      * **List apps.** Needs the admin API: Sunshine's `GET /api/apps` behind
        basic auth on 47990, or Wolf's `GET /api/v1/apps` over its UNIX
        socket. Without one of those, ROMarr knows the host is up and knows
        nothing else, which is exactly what it then reports.
      * **Relay a pairing PIN.** See `MoonlightPairing` for why "relay" is
        the strongest verb available.

    Three things it deliberately cannot do:

      * **Launch a game.** `/launch` is on the Moonlight HTTPS server behind a
        paired client certificate, in both implementations. Launching would
        mean ROMarr pairing *itself* as a Moonlight client -- implementing the
        four-phase crypto, holding a client key, and asking a human for a PIN
        on ROMarr's behalf -- and it would still have nowhere to put the video
        afterwards. The UI hands over a host address and a command line
        instead.
      * **Deep-link into a Moonlight client.** There is no registered
        `moonlight://` URI scheme to hand a browser. moonlight-qt uses that
        string internally in `computermanager.cpp` to parse a typed-in host
        address and registers no scheme handler; the request to add one
        (moonlight-qt#29) is still a request. So the UI offers the
        `moonlight stream <host>` command line, which does exist.
      * **Know what an app can open.** See the header of this section.
    """

    #: Same breaker and TTL reasoning as `StreamServer`, and for the same
    #: measured reason: without them a status page makes one timing-out
    #: request per platform against a host that is simply switched off.
    DOWN_COOLDOWN = 30.0
    CACHE_TTL = 300.0

    def __init__(self, host: str, *, kind: str = WOLF,
                 username: str = "", password: str = "",
                 socket_path: str = "", api_url: str = "",
                 desktop_url: str = "", timeout: float = MOONLIGHT_TIMEOUT):
        self.host, self.port = _split_host(host, MOONLIGHT_HTTP_PORT)
        self.kind = kind if kind in MOONLIGHT_KINDS else WOLF
        self.username = username
        self.password = password
        #: Wolf only. The UNIX socket its API listens on -- `WOLF_SOCKET_PATH`
        #: in Wolf's own environment, `/var/run/wolf/wolf.sock` by convention.
        self.socket_path = socket_path
        #: Wolf only. Set when the operator has put the documented nginx proxy
        #: in front of the socket, which is the only way Wolf blesses for
        #: reaching its API across a container boundary.
        self.api_url = (api_url or "").rstrip("/")
        #: Steam Headless only. Its noVNC/neko desktop, which is a genuine
        #: second way in and is a link, not an integration.
        self.desktop_url = (desktop_url or "").rstrip("/")
        self.timeout = timeout

        self._reachable = False
        self._problem = "not probed"
        self._info: dict = {}
        self._apps: list[str] = []
        self._apps_read = False
        self._apps_problem = ""
        self._coverage: dict[str, str] = {}
        self._down_until = 0.0
        self._fresh_until = 0.0

    # -- the shape `routes_for` consumes ------------------------------------

    @property
    def label(self) -> str:
        name = self._info.get("hostname") or ""
        where = f"{self.host}:{self.port}"
        return f"{name} ({where})" if name else where

    @property
    def engine(self) -> str:
        """What the STREAM route detail calls the thing doing the rendering."""
        return KIND_LABELS.get(self.kind, "Moonlight")

    @property
    def reachable(self) -> bool:
        return self._reachable

    def tier(self, slug: str) -> str | None:
        """`"stream"` when an app on this host plays `slug`, else None.

        Never `"local"`: a Moonlight host renders somewhere else and sends
        video, which is the definition of the stream tier and is never the
        browser tier however the pixels get drawn.
        """
        self.refresh()
        if not self._reachable:
            return None
        return STREAM if slug in self._coverage else None

    def why(self, slug: str) -> str:
        """Why this host does not play `slug`, in terms an operator can act on.

        Three different sentences for three different fixes, because "I have
        not been told your admin password", "your app list has no PS2
        emulator" and "your host is off" want three different next steps.
        """
        if not self._reachable:
            return ""
        if not self._apps_read:
            return (f"{self.engine} is reachable at {self.host}:{self.port} "
                    "but ROMarr cannot read its app list "
                    f"({self._apps_problem or 'no admin credential configured'})"
                    ", so it cannot say which platforms it plays")
        if slug in self._coverage:
            return ""
        return (f"{self.engine} is reachable and its app list has no emulator "
                "for this platform; install one and it appears here")

    # -- probing ------------------------------------------------------------

    def refresh(self, *, force: bool = False) -> None:
        """Probe, and read the app list if there is a way in.

        Cached with the same TTL as `StreamServer` for the same reason: a
        Library page must not make one HTTP call per row, and installing an
        emulator must become visible without restarting ROMarr.
        """
        now = time.monotonic()
        if not force:
            if now < self._fresh_until:
                return
            if now < self._down_until:
                self._reachable = False
                return

        self._probe()
        if self._reachable:
            self._read_apps()
            self._fresh_until = now + self.CACHE_TTL
            self._down_until = 0.0
        else:
            self._down_until = now + self.DOWN_COOLDOWN
            self._fresh_until = 0.0

    def _probe(self) -> None:
        """`GET /serverinfo`, the one call that needs nothing."""
        url = f"http://{self.host}:{self.port}{SERVERINFO_PATH}"
        try:
            with urllib.request.urlopen(url, timeout=self.timeout) as response:
                body = response.read().decode("utf-8", "replace")
        except Exception as exc:                # a host being off is normal
            log.info("moonlight host %s did not answer: %s", self.label, exc)
            self._reachable, self._problem = False, str(exc)
            self._info = {}
            self._apps, self._apps_read, self._coverage = [], False, {}
            return
        self._info = _serverinfo_fields(body)
        # Answering at all is the signal. Both implementations hardcode a 200
        # status_code into the body, so it is read and reported rather than
        # used as a gate ROMarr invented.
        self._reachable, self._problem = True, ""

    def _read_apps(self) -> None:
        """Fetch the app list, if this host has given ROMarr a way to."""
        self._apps, self._apps_read, self._apps_problem = [], False, ""
        self._coverage = {}
        try:
            if self.kind == WOLF:
                titles = self._wolf_apps()
            else:
                titles = self._sunshine_apps()
        except _NoWayIn as exc:
            self._apps_problem = str(exc)
            return
        except Exception as exc:
            log.warning("could not read apps from %s: %s", self.label, exc)
            self._apps_problem = str(exc)
            return
        self._apps = titles
        self._apps_read = True
        self._coverage = platforms_from_apps(titles)

    def _wolf_apps(self) -> list[str]:
        body = self._wolf_get("/apps")
        return [str(a.get("title") or a.get("name") or "")
                for a in (body.get("apps") or [])]

    def _sunshine_apps(self) -> list[str]:
        body = self._sunshine_get("/apps")
        return [str(a.get("name") or "") for a in (body.get("apps") or [])]

    # -- pairing ------------------------------------------------------------

    def pairing(self) -> MoonlightPairing:
        """Clients waiting to pair, when the host will name them."""
        if self.kind != WOLF:
            return MoonlightPairing(
                can_list_pending=False,
                detail=("Sunshine has no endpoint that lists waiting clients "
                        "-- its API can accept a PIN and cannot tell you one "
                        "is wanted. Start the pairing from your Moonlight "
                        "client, then type the PIN it shows."))
        try:
            body = self._wolf_get("/pair/pending")
        except Exception as exc:
            return MoonlightPairing(can_list_pending=False, detail=str(exc))
        out = []
        for req in body.get("requests") or []:
            secret = str(req.get("pair_secret") or "")
            out.append({
                "pair_secret": secret,
                "client_ip": str(req.get("client_ip") or ""),
                # The page Wolf logs at startup, reconstructed rather than
                # fished out of container logs. Wolf serves it on the plain
                # HTTP port and puts the secret in the fragment, which is why
                # it can be rebuilt from the secret alone.
                "pin_url": (f"http://{self.host}:{self.port}/pin/#{secret}"
                            if secret else ""),
            })
        return MoonlightPairing(pending=out)

    def submit_pin(self, pin: str, *, pair_secret: str = "",
                   name: str = "ROMarr") -> dict:
        """Hand the host a PIN a human read off their Moonlight client.

        Returns `{ok, detail}` and is careful about what `ok` means, because
        neither host will actually tell you the pairing succeeded:

          * Wolf resolves its internal promise and answers 200 as soon as the
            secret matches. Whether the PIN was *right* is decided afterwards,
            inside the crypto, and is never reported back on this call.
          * Sunshine's `POST /api/pin` returns `{"status": true}` whenever any
            pairing request is outstanding, correct PIN or not -- reported
            upstream as Sunshine#3944. Its `false` is meaningful (`nvhttp::pin`
            returns false only when no client is waiting, or when the PIN is
            not four digits); its `true` is not.

        So a success here means "the host accepted the submission", and the
        only way to know the client actually paired is to look at the paired
        client list afterwards. The UI says that in those words.
        """
        pin = (pin or "").strip()
        if not pin:
            return {"ok": False, "detail": "no PIN given"}
        try:
            if self.kind == WOLF:
                if not pair_secret:
                    return {"ok": False,
                            "detail": ("Wolf needs the pair secret of the "
                                       "waiting request; refresh the pending "
                                       "list and try again")}
                self._wolf_post("/pair/client",
                                {"pair_secret": pair_secret, "pin": pin})
            else:
                body = self._sunshine_post("/pin", {"pin": pin, "name": name})
                if body.get("status") in (False, "false"):
                    return {"ok": False,
                            "detail": ("Sunshine refused it: either no "
                                       "Moonlight client is waiting to pair, "
                                       "or the PIN was not four digits")}
        except Exception as exc:
            log.warning("PIN submission to %s failed: %s", self.label, exc)
            return {"ok": False, "detail": str(exc)}
        return {"ok": True,
                "detail": ("Submitted. The host does not report whether the "
                           "PIN was correct -- check that your client now "
                           "appears in the paired list.")}

    def paired_clients(self) -> list[dict]:
        """Who this host considers paired. The only honest pairing verdict."""
        try:
            if self.kind == WOLF:
                body = self._wolf_get("/clients")
                return [{"id": str(c.get("client_id") or ""), "name": ""}
                        for c in body.get("clients") or []]
            body = self._sunshine_get("/clients/list")
            rows = body.get("named_certs") or body.get("clients") or []
            return [{"id": str(c.get("uuid") or ""),
                     "name": str(c.get("name") or "")} for c in rows]
        except Exception as exc:
            log.info("could not list paired clients on %s: %s", self.label, exc)
            return []

    # -- reporting ----------------------------------------------------------

    def status(self) -> dict:
        """Everything the UI needs to describe this host without over-claiming."""
        self.refresh()
        pairing = (self.pairing() if self._reachable
                   else MoonlightPairing(can_list_pending=False,
                                         detail="host not reachable"))
        return {
            "configured": True,
            "kind": self.kind,
            "kind_label": self.engine,
            "host": self.host,
            "port": self.port,
            "label": self.label,
            "ok": self._reachable,
            "problem": self._problem,
            "hostname": self._info.get("hostname", ""),
            "appversion": self._info.get("appversion", ""),
            "https_port": self._info.get("HttpsPort", ""),
            "busy": self._info.get("state", "") == "SUNSHINE_SERVER_BUSY",
            "current_game": self._info.get("currentgame", ""),
            "apps_read": self._apps_read,
            "apps": list(self._apps),
            "apps_problem": self._apps_problem,
            "platforms": dict(self._coverage),
            "paired": self.paired_clients() if self._reachable else [],
            "desktop_url": self.desktop_url,
            "pairing": {
                "can_list_pending": pairing.can_list_pending,
                "pending": pairing.pending,
                "detail": pairing.detail,
                # Stated on every response rather than in a doc nobody opens.
                "manual": PAIRING_IS_MANUAL,
            },
            # Not a link. There is no `moonlight://` scheme handler to link
            # to; this is the command line that does exist.
            "connect_hint": f"moonlight stream {self.host}",
        }

    # -- transports ---------------------------------------------------------

    def _wolf_get(self, path: str) -> dict:
        return self._wolf_call("GET", path, None)

    def _wolf_post(self, path: str, payload: dict) -> dict:
        return self._wolf_call("POST", path, payload)

    def _wolf_call(self, method: str, path: str, payload) -> dict:
        """Wolf's API, over its UNIX socket or the operator's own TCP proxy.

        Wolf binds this to a UNIX socket and nothing else. Its documentation
        is emphatic about why -- "via the API you can pair clients to the
        server, execute arbitrary commands, and more" -- and the only exposure
        route it blesses is an nginx `proxy_pass` to the socket, which the
        operator sets up and secures. ROMarr therefore takes either a socket
        path it can open directly (same host, socket mounted) or that proxy's
        URL, and takes neither by default.
        """
        url_path = f"{WOLF_API_PREFIX}{path}"
        body = json.dumps(payload).encode() if payload is not None else None
        headers = {"Content-Type": "application/json"} if body else {}

        if self.api_url:
            return _json_request(f"{self.api_url}{url_path}", method,
                                 body, headers, self.timeout)
        if not self.socket_path:
            raise _NoWayIn(
                "Wolf's API is on a UNIX socket; set WOLF_SOCKET_PATH to a "
                "mounted wolf.sock, or WOLF_API_URL to an nginx proxy in "
                "front of it")
        if not hasattr(socket, "AF_UNIX"):
            raise _NoWayIn(
                "this platform has no AF_UNIX, so Wolf's socket cannot be "
                "opened here; use WOLF_API_URL with the documented proxy")
        conn = _UnixHTTPConnection(self.socket_path, timeout=self.timeout)
        try:
            conn.request(method, url_path, body=body, headers=headers)
            response = conn.getresponse()
            raw = response.read().decode("utf-8", "replace")
            if response.status >= 400:
                raise RuntimeError(f"HTTP {response.status}: {raw[:200]}")
            return json.loads(raw) if raw.strip() else {}
        finally:
            conn.close()

    def _sunshine_get(self, path: str) -> dict:
        return self._sunshine_call("GET", path, None)

    def _sunshine_post(self, path: str, payload: dict) -> dict:
        return self._sunshine_call("POST", path, payload)

    def _sunshine_call(self, method: str, path: str, payload) -> dict:
        """Sunshine's admin API: HTTPS on 47990, basic auth, self-signed.

        Two things about this are worth stating rather than leaving to be
        discovered.

        The certificate is generated by Sunshine on first run and signed by
        nobody, so verification is turned off -- which means this connection
        is encrypted and *not* authenticated, and somebody on the path could
        impersonate the host and collect the admin password. That is a real
        hole, it is the operator's LAN either way, and it belongs in
        SECURITY.md rather than in a comment apologising for itself.

        CSRF is not in the way, and that is by Sunshine's design rather than
        by luck: `validate_csrf_token` in `confighttp.cpp` returns true
        outright when a request carries neither `Origin` nor `Referer`, on the
        reasoning that a page in a browser cannot make a non-browser client
        issue requests. A server-side call from here sends neither header.
        """
        if not (self.username and self.password):
            raise _NoWayIn(
                "Sunshine's API needs its admin username and password; set "
                "MOONLIGHT_USER and MOONLIGHT_PASS")
        url = (f"https://{self.host}:{SUNSHINE_WEB_PORT}"
               f"{SUNSHINE_API_PREFIX}{path}")
        token = base64.b64encode(
            f"{self.username}:{self.password}".encode()).decode()
        headers = {"Authorization": f"Basic {token}"}
        body = None
        if payload is not None:
            body = json.dumps(payload).encode()
            # Sunshine's `check_content_type` rejects the request outright
            # without this, before authentication is even considered.
            headers["Content-Type"] = "application/json"
        return _json_request(url, method, body, headers, self.timeout,
                             insecure=True)


class _NoWayIn(Exception):
    """ROMarr has not been given a route to this host's admin API.

    Distinct from a network failure on purpose: one is fixed by configuring
    ROMarr and the other by fixing the host, and reporting both as "error"
    sends operators to the wrong one.
    """


class _UnixHTTPConnection(http.client.HTTPConnection):
    """HTTP over a UNIX socket, which `http.client` almost supports already.

    Wolf's API has no TCP port to point `urllib` at, and taking a dependency
    to get eight lines of socket setup would be the wrong trade in a project
    that ships stdlib plus `requests`.
    """

    def __init__(self, socket_path: str, timeout: float = MOONLIGHT_TIMEOUT):
        super().__init__("localhost", timeout=timeout)
        self.socket_path = socket_path

    def connect(self):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        sock.connect(self.socket_path)
        self.sock = sock


def _json_request(url: str, method: str, body, headers: dict,
                  timeout: float, *, insecure: bool = False) -> dict:
    """One JSON call, with the errors kept legible."""
    request = urllib.request.Request(url, data=body, method=method,
                                     headers=headers or {})
    context = None
    if insecure:
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    try:
        with urllib.request.urlopen(request, timeout=timeout,
                                    context=context) as response:
            raw = response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:200]
        if exc.code in (401, 403):
            raise _NoWayIn(f"HTTP {exc.code}: the credential was refused")
        raise RuntimeError(f"HTTP {exc.code}: {detail}") from exc
    return json.loads(raw) if raw.strip() else {}


def _serverinfo_fields(xml: str) -> dict:
    """The flat fields out of a `/serverinfo` body.

    Read with a regex rather than an XML parser, deliberately: this is
    untrusted input from a service on somebody's LAN, `xml.etree` is
    documented as unsafe against hostile documents, and the ten scalar fields
    wanted here do not justify a `defusedxml` dependency. The nested
    display-mode list is not wanted and is not extracted.
    """
    found: dict[str, str] = {}
    for tag, value in re.findall(r"<([A-Za-z][\w.]*)>([^<]*)</\1>", xml or ""):
        found.setdefault(tag, value.strip())
    return found


def _split_host(value: str, default_port: int) -> tuple[str, int]:
    """`host`, `host:port` or a full URL -> (host, port).

    A URL is accepted because operators paste one; its scheme is discarded
    rather than honoured, because the Moonlight probe port is not an HTTPS
    port and pasting `https://host` must not quietly produce a host nothing
    can reach.
    """
    text = (value or "").strip()
    if "://" in text:
        parsed = urllib.parse.urlsplit(text)
        return parsed.hostname or "", parsed.port or default_port
    if text.count(":") == 1:
        host, _, port = text.partition(":")
        try:
            return host.strip(), int(port)
        except ValueError:
            return host.strip(), default_port
    return text, default_port


class StreamSources:
    """Several stream servers behind the one slot `routes_for` has.

    An operator can plausibly run both -- the headless RetroArch server for
    the retro machines it was built for, and Wolf or Sunshine for a PS2
    emulator on a real GPU -- and `routes_for` takes a single `stream`. Rather
    than teach it to take a list, and have every caller and every test learn a
    second shape, this presents any number of sources as one.

    First answer wins, in the order given, and that order is not arbitrary:
    the sources are not equally sure of themselves. The RetroArch server
    *knows* which cores it has installed, while a Moonlight host is inferring
    from app names, and an inferred answer must not displace a known one.
    """

    def __init__(self, *sources):
        self.sources = [s for s in sources if s is not None]

    @property
    def label(self) -> str:
        for source in self.sources:
            if getattr(source, "reachable", True):
                return getattr(source, "label", "the stream server")
        return "the stream server"

    @property
    def engine(self) -> str:
        for source in self.sources:
            if getattr(source, "reachable", True):
                return getattr(source, "engine", "headless RetroArch")
        return "headless RetroArch"

    @property
    def reachable(self) -> bool:
        # True when *any* source answered, because the flag exists to tell
        # "cannot play this" apart from "nothing was reachable", and one live
        # server means the first of those is the honest reading.
        return any(getattr(s, "reachable", True) for s in self.sources)

    def tier(self, slug: str) -> str | None:
        """The first source that claims `slug`, asked in confidence order."""
        for source in self.sources:
            try:
                answer = source.tier(slug)
            except Exception as exc:
                # One dead source must not hide a live one. `StreamServer`
                # signals failure by raising, so swallowing here and carrying
                # on is the difference between "Wolf plays your PS2 games" and
                # a blank page because RetroArch was rebooting.
                log.warning("stream source %r failed for %r: %s",
                            getattr(source, "label", source), slug, exc)
                continue
            if answer:
                return answer
        return None

    def attribution(self, slug: str) -> tuple[str, str]:
        """Name the source that actually claimed `slug`, not the first one.

        Re-asks rather than remembering which source answered a moment ago.
        Both source types cache their answers with a TTL, so the second ask is
        free, and a remembered value would be wrong the moment two requests
        overlapped -- the status page walks 59 platforms, and a threaded
        server can be walking two lists at once.
        """
        for source in self.sources:
            try:
                if source.tier(slug):
                    return (getattr(source, "label", "the stream server"),
                            getattr(source, "engine", "headless RetroArch"))
            except Exception:
                continue
        return self.label, self.engine

    def why(self, slug: str) -> str:
        """The first source that has something to say about `slug`.

        Sources that cannot explain themselves are skipped rather than allowed
        to end the search with an empty string -- otherwise a silent RetroArch
        server would suppress Wolf's much more useful "I am up, but you have
        not given me a credential, so I cannot tell you what I play".
        """
        for source in self.sources:
            said = _stream_reason(source, slug)
            if said:
                return said
        return ""
