"""Decide which torrents in a shared client are plausibly game sets.

The Collections page offers existing qBittorrent and Transmission torrents
as sources for missing DAT files. Those clients are usually shared with
Radarr, Sonarr and Lidarr, so listing every torrent puts films and TV
seasons in a picker that can only ever use a ROM set. This module keeps the
picker to torrents that look like games and says how many it hid.

The test is positive, not merely negative: a torrent has to show a game
signal (ROMarr's own category, a games/ROMs category, a set marker such as
No-Intro or Redump, or a console name) and no media signal (a Radarr or
Sonarr category, or a video release tag such as S01E02, 1080p or x264).
"""
from __future__ import annotations

import re
from collections.abc import Iterable

from . import platforms

#: Category or label words that mean another *arr or a media library owns it.
#: Every alternative is delimited, so "mylarge-roms" is not Mylar's. ROMarr's
#: own soundtrack category ("romarr-soundtracks") is audio, not a ROM set.
_MEDIA_CATEGORY = re.compile(
    r"(?:^|[^a-z])(?:radarr|sonarr|lidarr|readarr|whisparr|mylar|"
    r"movies?|films?|tv|series|shows?|music|audio|soundtracks?|osts?|books?|"
    r"ebooks?|audiobooks?|anime|xxx|comics?|podcasts?)(?:$|[^a-z])")

#: Category or label words that mean games.
_GAME_CATEGORY = re.compile(
    r"(?:^|[^a-z])(?:games?|roms?|retro|emulation|emu|romsets?|romarr)(?:$|[^a-z])")

#: Release tags that only video carries. Matched against the torrent name.
_VIDEO_NAME = re.compile(
    r"\bs\d{1,2}(?:e\d{1,3})?\b|\bseason[ ._-]?\d+\b|\bcomplete[ ._-]series\b|"
    r"\b(?:480|576|720|1080|1440|2160|4320)[pi]\b|\b(?:uhd|hdr10\+?|dolby[ ._-]?vision)\b|"
    r"\b(?:x|h)[ .]?26[45]\b|\b(?:hevc|xvid|divx|avc)\b|"
    r"\b(?:blu-?ray|bdrip|brrip|bdremux|remux|web-?dl|web-?rip|hdtv|dvdrip|hdrip)\b|"
    r"\b(?:ddp?[ .]?[257][ .][01]|truehd|atmos|dts(?:-hd)?)\b")

#: Words that name a ROM set or a set-making tool, matched as whole words.
_SET_MARKERS = re.compile(
    r"\b(?:no-?intro|non-?redump|redump|tosec|mame|fbneo|final ?burn|"
    r"roms?|rom ?sets?|romsets?|1g1r|retool|myrient|minerva|"
    r"chd|rvz|wbfs|nsp|xci)\b")

#: Platform names too common as plain words to count on their own. Each of
#: these platforms still matches through a longer name ("Sega Saturn").
_GENERIC_LABELS = frozenset({
    "arcade", "dos", "genesis", "ibm", "jaguar", "mac", "macintosh",
    "palm", "pc", "saturn", "spectrum", "switch", "win", "windows",
    "microsoft windows", "pc windows", "pc dos", "ms dos", "ibm pc",
    "classic mac os", "palm os", "pc compatibles", "ibm pc compatibles",
    "games on demand", "apple macintosh",
})

_WORDS = re.compile(r"[^a-z0-9]+")


def _platform_labels() -> frozenset[str]:
    """Normalised platform names safe to treat as a game signal."""
    return frozenset(
        label for label in platforms._BY_LABEL
        if len(label) >= 3 and not label.replace(" ", "").isdigit()
        and label not in _GENERIC_LABELS)


_LABELS = _platform_labels()
_LONGEST_LABEL = max(len(label.split()) for label in _LABELS)


def _names_a_platform(name: str) -> bool:
    """Whether the name contains a platform name as whole words."""
    words = _WORDS.sub(" ", name.lower()).split()
    for size in range(1, _LONGEST_LABEL + 1):
        for start in range(len(words) - size + 1):
            if " ".join(words[start:start + size]) in _LABELS:
                return True
    return False


def looks_like_game_torrent(name: str, labels: Iterable[str] = (), *,
                            own_categories: Iterable[str] = ()) -> bool:
    """Whether one torrent should be offered as a collection source.

    `labels` holds the torrent's category, tags or Transmission labels.
    `own_categories` are the categories ROMarr itself downloads with, which
    count as games whatever the name says.
    """
    tags = [str(label or "").strip().lower() for label in labels]
    tags = [tag for tag in tags if tag]
    own = {str(c or "").strip().lower() for c in own_categories} - {""}
    if own.intersection(tags):
        return True
    if any(_MEDIA_CATEGORY.search(tag) for tag in tags):
        return False
    lowered = str(name or "").lower()
    if _VIDEO_NAME.search(lowered.replace("_", " ")):
        return False
    if any(_GAME_CATEGORY.search(tag) for tag in tags):
        return True
    return bool(_SET_MARKERS.search(lowered.replace("_", " "))
                or _names_a_platform(lowered))


def torrent_labels(torrent: dict) -> list[str]:
    """Category, qBittorrent tags and Transmission labels of one torrent."""
    out = [str(torrent.get("category") or "")]
    tags = torrent.get("tags")
    if isinstance(tags, str):
        out.extend(tags.split(","))
    labels = torrent.get("labels")
    if isinstance(labels, (list, tuple)):
        out.extend(str(label) for label in labels)
    return [label.strip() for label in out if label and label.strip()]
