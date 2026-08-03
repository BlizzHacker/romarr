"""Finding a plugin, installing your own, and getting one featured.

ROM Hub already installs plugins by slug from a catalogue. Three things were
missing, and they are the three things that make a catalogue a catalogue
rather than a list:

  * **search**, because a list you have to read end to end stops working at
    about thirty entries;
  * **install your own**, because the interesting plugin is usually the one
    you just wrote and it is not in anybody's catalogue yet;
  * **submit**, because the way a plugin gets into the catalogue should not be
    "know which repository to open a pull request against".

Submission prepares, and never sends. ROMarr builds the entry, validates it,
and hands back a prefilled link the operator clicks -- publishing something
under somebody's name is their decision, and a tool that posts on their behalf
because they clicked "submit" in a settings page has made it for them. It also
means the flow works with no token configured, which is the normal case.
"""

from __future__ import annotations

import logging
import re
import urllib.parse
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

#: Where a featured submission goes.
CATALOGUE_REPO = "BlizzHacker/rom-hub"

#: A plugin slug: lower-case, hyphenated, no surprises. This is used as a
#: directory name and a CLI argument, so it is validated rather than trusted.
SLUG = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")

#: Only these hosts may be installed from directly.
#:
#: A plugin is code that ROMarr will execute. "Install from any URL" is a
#: remote code execution feature with a text box, so the source has to be
#: somewhere the operator can read the code first and somewhere a takedown is
#: possible. Self-hosted forges are common in this crowd, so the list is
#: configurable -- but it is a list, never "anything".
DEFAULT_ALLOWED_HOSTS = ("github.com", "gitlab.com", "codeberg.org",
                         "git.sr.ht", "bitbucket.org")


def search(items: list[dict], query: str = "", *, capability: str = "",
           platform: str = "", installed: bool | None = None) -> list[dict]:
    """Filter and rank a catalogue.

    Ranked rather than merely filtered: an exact slug match is what somebody
    typing `nointro-archive` meant, and burying it under six fuzzy name
    matches makes the search feel broken even though it worked.
    """
    text = str(query or "").strip().lower()
    wanted_capability = str(capability or "").strip().lower()
    wanted_platform = str(platform or "").strip().lower()

    scored = []
    for item in items:
        if installed is not None and bool(item.get("installed")) != installed:
            continue
        capabilities = [str(c).lower() for c in item.get("capabilities") or []]
        if wanted_capability and wanted_capability not in capabilities:
            continue
        platforms = [str(p).lower() for p in item.get("platforms") or []]
        if wanted_platform and wanted_platform not in platforms:
            continue

        if not text:
            scored.append((0, item))
            continue

        slug = str(item.get("slug") or "").lower()
        name = str(item.get("name") or "").lower()
        description = str(item.get("description") or "").lower()
        author = str(item.get("author") or "").lower()

        if slug == text:
            rank = 0
        elif text in slug:
            rank = 1
        elif text in name:
            rank = 2
        elif text in author:
            rank = 3
        elif text in description:
            rank = 4
        else:
            continue
        scored.append((rank, item))

    scored.sort(key=lambda pair: (pair[0], str(pair[1].get("slug") or "")))
    return [item for _, item in scored]


def facets(items: list[dict]) -> dict:
    """The capabilities and platforms present, for filter menus.

    Built from what is actually in the catalogue rather than a hardcoded list,
    so a new capability appears in the UI the moment a plugin declares one.
    """
    capabilities: dict[str, int] = {}
    platforms: dict[str, int] = {}
    for item in items:
        for capability in item.get("capabilities") or []:
            capabilities[str(capability)] = capabilities.get(str(capability), 0) + 1
        for platform in item.get("platforms") or []:
            platforms[str(platform)] = platforms.get(str(platform), 0) + 1
    return {
        "capabilities": sorted(capabilities.items()),
        "platforms": sorted(platforms.items()),
    }


@dataclass
class SourceCheck:
    ok: bool
    url: str = ""
    host: str = ""
    reason: str = ""


def check_source(url: str, allowed_hosts=None) -> SourceCheck:
    """Whether a repository URL may be installed from.

    A plugin is code ROMarr executes in a subprocess. "Install from any URL"
    is remote code execution with a text box in front of it, so this refuses
    anything that is not https on a known forge -- somewhere the operator can
    read the source first and somewhere a takedown is possible.
    """
    raw = str(url or "").strip()
    if not raw:
        return SourceCheck(False, reason="no URL given")
    parts = urllib.parse.urlsplit(raw)
    if parts.scheme != "https":
        # Not pedantry: http means anybody on the path chooses what code runs.
        return SourceCheck(False, url=raw,
                           reason="must be https -- plain http lets anyone on "
                                  "the network choose what code you run")
    host = (parts.hostname or "").lower()
    if not host:
        return SourceCheck(False, url=raw, reason="no host in the URL")
    allowed = [h.lower() for h in (allowed_hosts or DEFAULT_ALLOWED_HOSTS)]
    if host not in allowed:
        return SourceCheck(
            False, url=raw, host=host,
            reason=f"{host} is not an allowed source. Allowed: "
                   f"{', '.join(allowed)}. Add it under Settings if you host "
                   "your own forge.")
    if not parts.path.strip("/"):
        return SourceCheck(False, url=raw, host=host,
                           reason="that is a host, not a repository")
    return SourceCheck(True, url=raw, host=host)


@dataclass
class Submission:
    """A catalogue entry somebody wants featured."""

    slug: str = ""
    name: str = ""
    repository: str = ""
    author: str = ""
    description: str = ""
    capabilities: list[str] = field(default_factory=list)
    platforms: list[str] = field(default_factory=list)

    def problems(self, allowed_hosts=None) -> list[str]:
        """Everything wrong with this, all at once.

        All of them rather than the first: a form that reveals one error per
        submission takes five round trips to fill in, and each of those is a
        chance to give up.
        """
        out = []
        if not SLUG.match(self.slug or ""):
            out.append("slug must be lower-case words joined by hyphens, "
                       "e.g. my-plugin")
        if not (self.name or "").strip():
            out.append("name is required")
        if not (self.description or "").strip():
            out.append("description is required -- it is what somebody reads "
                       "before deciding to run your code")
        source = check_source(self.repository, allowed_hosts)
        if not source.ok:
            out.append(f"repository: {source.reason}")
        if not self.capabilities:
            out.append("declare at least one capability, or nothing will ever "
                       "call the plugin")
        return out

    def as_entry(self) -> dict:
        return {
            "slug": self.slug,
            "name": self.name,
            "author": self.author,
            "repository": self.repository,
            "description": self.description,
            "capabilities": list(self.capabilities),
            "platforms": list(self.platforms),
        }


def submission_link(submission: Submission, repo: str = CATALOGUE_REPO) -> str:
    """A prefilled issue URL for the operator to click.

    ROMarr does not post it. Publishing something under somebody's name is
    their decision, and a tool that submits on their behalf because they
    clicked a button in a settings page has made that decision for them. It
    also means this works with no token configured, which is the normal case.
    """
    body = "\n".join([
        "Proposed catalogue entry:", "",
        "```json",
        _pretty_entry(submission.as_entry()),
        "```", "",
        f"Repository: {submission.repository}",
        "",
        "_Prepared by ROMarr._",
    ])
    query = urllib.parse.urlencode({
        "title": f"Add plugin: {submission.slug}",
        "body": body,
        "labels": "plugin-submission",
    })
    return f"https://github.com/{repo}/issues/new?{query}"


def _pretty_entry(entry: dict) -> str:
    import json

    return json.dumps(entry, indent=2)
