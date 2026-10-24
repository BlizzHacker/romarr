# ROMarr roadmap

This is a working list of priorities, updated **2026-10-24**. Shipped features are separated from proposals so you can tell what an update will actually give you. There are no promised delivery dates; scope and validation can change the order.

## Available in v1.7.0

| Request | What is available | Limits |
|---|---|---|
| Reclaim duplicate space | **Stats → Audit** previews byte-identical duplicates, keepers and recoverable bytes, then offers quarantine and restore. | Review first. Quarantine is not permanent deletion and does not merge similar titles. [Issue #73](https://github.com/BlizzHacker/romarr/issues/73). |
| Fetch missing entries from a set torrent in batches | **Collections** can preview and stage selected files from an existing qBittorrent torrent within a configured budget, verify imports, and restore file priorities. | Original torrent data remains; the staging limit is not a total cap on all source storage. [Issue #72](https://github.com/BlizzHacker/romarr/issues/72). |
| Reliable DAT loading | XML/Logiqx detection, duplicate DAT selection and concurrent reload handling were corrected. | Your DAT still needs to cover the files/platform you are checking. [Issue #74](https://github.com/BlizzHacker/romarr/issues/74). |
| Completion matching and useful notification errors | URL-added qBittorrent jobs carry per-job tags; unmatched jobs are logged. Webhook tests expose useful HTTP error details. | An upstream 403 still needs the appropriate access/credentials. [Issues #75](https://github.com/BlizzHacker/romarr/issues/75) and [#33](https://github.com/BlizzHacker/romarr/issues/33). |
| Modern-console file support | Search/import recognition for PS4, PS5 and PS Vita formats. | No console installer or new browser emulator. [Support limits](docs/MODERN-CONSOLES.md). |

The fixes are in [v1.7.0](https://github.com/BlizzHacker/romarr/releases/tag/v1.7.0). See the release's checks and evidence for that build rather than assuming every integration has been tested on your hardware.

## In this onboarding update

- **[Setup Guide (#77)](https://github.com/BlizzHacker/romarr/issues/77): a skippable sequence for library, downloads, sources, DATs and plugins, with links back to existing configuration forms.
- **[Clearer documentation and plugin discovery (#81)](https://github.com/BlizzHacker/romarr/issues/81): a short README, an existing-ROM path that does not require Prowlarr, and a separate plugin guide.
- **[Windows starter (#78)](https://github.com/BlizzHacker/romarr/issues/78): a foreground PowerShell launcher that prepares a virtual environment and starts ROMarr on localhost. This is not an MSI/EXE installer or a Windows service. Actual Windows startup checks run in CI.
- **[Canonical names on import (#79)](https://github.com/BlizzHacker/romarr/issues/79): verified single-file imports can use the DAT ROM filename when Rename on import is enabled. Source files are preserved; unknown filenames are retained. This does not rename an existing library in place.

These changes are being reviewed separately from v1.7.0. Check their merged PR/release before expecting them in a pulled image.

## Next priorities

| Priority | Work | What completion needs to mean |
|---|---|---|
| 1 | [Existing-library rename and folder migration preview (#80)](https://github.com/BlizzHacker/romarr/issues/80) | Show every proposed old/new name, platform folder, collision and ambiguous DAT match before changing anything. Preserve a recoverable record; do not quietly reorganize a collection. |
| 1 | Better platform-folder guidance for ES-DE and similar frontends | Show the output layout for the selected destination, explain frontend mappings, and make a missing-files comparison easy to find. |
| 1 | Windows installation experience | Field-test the launcher with real collections and archive tools, document permissions and upgrades, then assess installer/service packaging. |
| 2 | Integration field reports | Expand live validation for library backends, download clients, Home Assistant packaging and smaller hardware. Record test conditions and remaining gaps. |
| 2 | Plugin setup and source diagnostics | Make host-versus-plugin availability, credentials, searchable capability and source failures easier to understand without reading logs. |

## Research and proposals

- **Transfer to Xbox Series X or another device:** establish a real destination protocol, app requirements and safe transfer flow before promising a button. [RomM for Xbox](https://github.com/BlizzHacker/RommForXbox) is a separate project; linking to it is not a ROMarr transfer implementation.
- **Broader playback integration:** report what configured players can actually run, including format, core and firmware requirements. A reachable streaming host does not prove a particular game will play.

## Where the priorities came from

The [r/ARR community thread](https://www.reddit.com/r/ARR/comments/1x9abzl/) raised concrete gaps in the experience:

- [Setup, platform folders, ES-DE and missing games](https://www.reddit.com/r/ARR/comments/1x9abzl/comment/phqpinw/) → Setup Guide, folder/path explanations and the missing-collection walkthrough.
- [Reorganizing and renaming existing files without Prowlarr](https://www.reddit.com/r/ARR/comments/1x9abzl/comment/19rjgioltvemd/) → separate local-import instructions now; a safe bulk rename/migration planner next.
- [Native Windows use](https://www.reddit.com/r/ARR/comments/1x9abzl/comment/19rjhvwh3yg1s/) → foreground launcher and Windows CI, with installer/service work still open.
- [Roadmap and deduplication](https://www.reddit.com/r/ARR/comments/1x9abzl/comment/phlnak1/) → this page and the shipped duplicate quarantine workflow.
- [Where plugins are](https://www.reddit.com/r/ARR/comments/1x9abzl/comment/phmlahe/) and [LXC installation](https://www.reddit.com/r/ARR/comments/1x9abzl/comment/phlgsc8/) → verified catalogue links, architecture limits and correct-environment install commands.
- [Large-set downloads](https://www.reddit.com/r/ARR/comments/1x9abzl/comment/phleeke/) → the staged qBittorrent workflow shipped in v1.7.0.
- [Xbox transfers](https://www.reddit.com/r/ARR/comments/1x9abzl/comment/php6c7v/) → research, with no transfer support claimed yet.

To suggest a change, [open an issue](https://github.com/BlizzHacker/romarr/issues/new) describing the task you are trying to finish, your setup, and what the current flow prevents. For bugs, include your version and a redacted error/log so the fix can be reproduced and checked.
