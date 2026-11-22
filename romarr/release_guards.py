"""Refusals that read a release title for what it is, not what it is called.

Two checks live here, both behaviour learned from Gamarr (GPL-3.0; nothing
copied, the rules re-derived and the word lists written for ROM releases):

* **Demo, kiosk and trial builds.** A demo is not the game, however good the
  rest of the release looks, so it is refused rather than docked. The old
  check was the substring ``"demo"``, which docked *Demon's Souls* and
  *Demolition Man* exactly as much as a real demo, while a kiosk build or a
  trial version went through untouched.
* **Executables.** ``Halo 3 PS3.iso.exe`` is a program wearing a disc image's
  name. On a console or handheld any executable is refused; on PC an
  installer is normal, but one that hides behind a ROM or media extension
  never is.
"""

from __future__ import annotations

import datetime as _dt
import re
from email.utils import parsedate_to_datetime

# --- demo, kiosk and trial builds -----------------------------------------

#: Phrases that name a demo wherever they appear. Matched as whole words on a
#: title whose dots and underscores have become spaces.
_DEMO_PHRASES = re.compile(
    r"(?:^|[\s(\[\-])kiosk\b"
    r"|\b(?:store|shop|playable|tech(?:nical)?|timed|trade\s*show|press|public|kiosk)\s+demo\b"
    r"|\bdemo\s+(?:build|version|disc|disk)\b"
    r"|\btrial\s+(?:build|version|edition)\b"
    r"|\bpreview\s+(?:build|version)\b"
    r"|\bshareware\b",
    re.IGNORECASE,
)

#: A No-Intro or Redump tag group that says demo: ``(Demo)``, ``(USA) (Kiosk)``,
#: ``(Sample)``, ``(Trial Version)``, ``[Demo]``.
_DEMO_TAG = re.compile(
    r"[(\[][^)\]]*\b(?:demo|kiosk|sample|trial)\b[^)\]]*[)\]]",
    re.IGNORECASE,
)

#: A bare DEMO, TRIAL or PREVIEW word. Only a demo when nothing but release
#: metadata follows it, which is what keeps *Trial of Mana* a game.
_BARE_DEMO_WORD = re.compile(r"\b(demo|trial|preview)\b", re.IGNORECASE)

#: Mixed-case scene tags that count as metadata after a bare demo word. Upper
#: case tokens, numbers and versions count already; these are the ones that
#: do not look like it.
_METADATA_WORDS = frozenset({
    "internal", "readnfo", "eshop", "proper", "repack", "rerip", "dirfix",
    "nfofix", "multi", "nsw", "nsp", "xci", "nsz", "xcz", "pkg", "iso",
    "cso", "chd", "rvz", "wbfs", "cia", "3ds", "nds", "usa", "eur", "jpn",
    "pal", "ntsc", "en", "de", "fr", "es", "it", "ja", "rev", "disc", "eng",
})
_VERSION_TOKEN = re.compile(r"^v?\d+(?:\.\d+)*[a-z]?$", re.IGNORECASE)
_MULTI_TOKEN = re.compile(r"^multi\d+$", re.IGNORECASE)
_TRAILING_GROUP = re.compile(r"-[A-Za-z0-9]+$")
_TOKEN_SPLIT = re.compile(r"[\s()\[\],+]+")


def _spaced(title: str) -> str:
    return re.sub(r"[._]+", " ", str(title or "")).strip()


def _words(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", _spaced(text).lower()))


def _is_metadata(token: str) -> bool:
    if not token:
        return True
    lowered = token.lower()
    return (
        lowered in _METADATA_WORDS
        or bool(_MULTI_TOKEN.match(token))
        or bool(_VERSION_TOKEN.match(token))
        or (token.isupper() and any(ch.isalpha() for ch in token))
    )


def _bare_demo_word(title: str) -> str:
    """The trailing DEMO/TRIAL/PREVIEW word, if only metadata follows it."""
    found = list(_BARE_DEMO_WORD.finditer(title))
    if not found:
        return ""
    last = found[-1]
    rest = _TRAILING_GROUP.sub("", title[last.end():].strip())
    if all(_is_metadata(token) for token in _TOKEN_SPLIT.split(rest)):
        return last.group(1).lower()
    return ""


def _without_file_extension(title: str) -> str:
    """The title with a terminal ROM, disc or archive extension removed.

    "Super Metroid (USA) Demo.smc" must read as ending in Demo, not in a
    token "smc" that is not release metadata.
    """
    text = str(title or "")
    head, dot, ext = text.rpartition(".")
    if dot and head and ("." + ext.lower()) in MEDIA_EXTENSIONS:
        return head
    return text


def _demo_kind(title: str) -> tuple[str, bool]:
    """(kind, explicit): what demo a title names, and whether a tag or phrase
    said so outright rather than a bare word that may belong to the name."""
    spaced = _spaced(_without_file_extension(title))
    if not spaced:
        return "", False
    tag = _DEMO_TAG.search(spaced)
    if tag:
        return tag.group(0).strip("()[]").strip().lower(), True
    phrase = _DEMO_PHRASES.search(spaced)
    if phrase:
        return phrase.group(0).strip(" ([-").lower(), True
    return _bare_demo_word(spaced), False


def demo_build(title: str) -> str:
    """What kind of demo a release title names, or "" when it is not one."""
    return _demo_kind(title)[0]


def demo_refusal(title: str, wanted: str = "") -> str:
    """Why a title is refused as a demo, or "".

    Somebody who asked for a demo disc by name gets it: when the wanted title
    reads as a demo too, the release is not refused for being one.
    """
    kind, explicit = _demo_kind(title)
    if not kind or (wanted and demo_build(wanted)):
        return ""
    # "TRIAL.OF.MANA.USA" ends in metadata-looking capitals after TRIAL, and
    # the word is part of the game's own name: a bare word the request names
    # is not the demo marker. An explicit tag is -- "Trial of Mana (Trial)"
    # is still the trial build.
    if not explicit and kind in _words(wanted):
        return ""
    return f"a demo, kiosk or trial build ({kind}), not the game"


# --- executables -----------------------------------------------------------

#: Extensions Windows will run. `.com` is only refused when it is disguised:
#: "Game - SomeSite.com" is a title naming a website, not a program.
EXECUTABLE_EXTENSIONS = frozenset({
    ".appx", ".bat", ".cmd", ".com", ".exe", ".jar", ".js", ".jse", ".msi",
    ".msix", ".pif", ".ps1", ".scr", ".vbe", ".vbs", ".wsf", ".wsh",
})

#: Extensions that name a ROM, disc image, archive or media file. One of these
#: just before an executable extension is a disguise.
MEDIA_EXTENSIONS = frozenset({
    ".7z", ".a26", ".a78", ".avi", ".bin", ".cdi", ".chd", ".cia", ".cso",
    ".cue", ".gb", ".gba", ".gbc", ".gcm", ".gdi", ".gen", ".gg", ".img",
    ".iso", ".lnx", ".md", ".mds", ".mkv", ".mp4", ".n64", ".nds", ".nes",
    ".nsp", ".nsz", ".pbp", ".pce", ".pdf", ".pkg", ".rar", ".rvz", ".sfc",
    ".smc", ".sms", ".v64", ".vpk", ".wbfs", ".wua", ".wud", ".wux", ".xci",
    ".xcz", ".z64", ".zip", ".3ds",
})

_LAST_EXTENSIONS = re.compile(r"(\.[a-z0-9_]{1,5})?(\.[a-z0-9]{1,5})$", re.IGNORECASE)

PC_PLATFORM_SLUGS = frozenset({"win", "dos"})


def executable_refusal(title: str, platform_slug: str = "") -> str:
    """Why a release title is refused as an executable, or ""."""
    text = str(title or "").strip()
    if not text or "://" in text:
        return ""
    found = _LAST_EXTENSIONS.search(text)
    if not found:
        return ""
    last = found.group(2).lower()
    if last not in EXECUTABLE_EXTENSIONS:
        return ""
    before = (found.group(1) or "").lower()
    if before in MEDIA_EXTENSIONS:
        return f"a program disguised as a {before} file ({before}{last})"
    if last == ".com" or platform_slug in PC_PLATFORM_SLUGS:
        return ""
    return f"the release is an executable ({last}), not a game image"


# --- when a release was posted ---------------------------------------------

def published_iso(value) -> str:
    """An indexer's posting date as ISO-8601 UTC, or "" when unreadable.

    Torznab and Newznab send RFC-822 (`<pubDate>`, `usenetdate`); Prowlarr
    sends ISO-8601 (`publishDate`). A date with no zone is taken as UTC.
    """
    text = str(value or "").strip()
    if not text:
        return ""
    when = None
    try:
        when = _dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:
            when = parsedate_to_datetime(text)
        except (TypeError, ValueError, IndexError):
            return ""
    if when is None:
        return ""
    if when.tzinfo is None:
        when = when.replace(tzinfo=_dt.timezone.utc)
    return when.astimezone(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def release_age_minutes(published: str, now: _dt.datetime | None = None) -> float | None:
    """Minutes since `published` (ISO-8601), or None when it is unknown."""
    if not published:
        return None
    try:
        when = _dt.datetime.fromisoformat(str(published).replace("Z", "+00:00"))
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=_dt.timezone.utc)
    now = now or _dt.datetime.now(_dt.timezone.utc)
    return max(0.0, (now - when).total_seconds() / 60.0)


def is_nzb_link(url: str, mime: str = "") -> bool:
    """Whether an indexer link is an NZB rather than a torrent.

    A Torznab feed can carry Usenet results; the enclosure type says so, and
    so does a link whose path or query names an `.nzb` file. Sending one of
    those to a torrent client fails every time.
    """
    if "nzb" in str(mime or "").lower():
        return True
    from urllib.parse import parse_qsl, urlsplit
    try:
        parts = urlsplit(str(url or ""))
    except ValueError:
        return False
    if parts.scheme == "magnet":
        return False
    if parts.path.lower().endswith(".nzb"):
        return True
    return any(str(v).lower().endswith(".nzb")
               for _, v in parse_qsl(parts.query, keep_blank_values=True))
