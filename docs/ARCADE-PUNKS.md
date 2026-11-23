# Arcade Punks

[Arcade Punks](https://www.arcadepunks.com) publishes pre-built retro gaming images: Batocera, RetroBat, CoinOps, Attract Mode and LaunchBox builds, Raspberry Pi, Steam Deck and handheld images, Dreamcast conversions of Atomiswave arcade games, and artwork and video packs. Their guides recommend Usenet: [How to Get Started with NZBGet](https://www.arcadepunks.com/how-to-get-started-with-nzbget/) and [How to Set Up Usenet for Arcade Punks](https://www.arcadepunks.com/how-to-set-up-usenet-for-arcade-punks-retro-gaming-content/).

ROMarr ships the [Arcade Punks plugin](https://github.com/BlizzHacker/rom-hub-arcade-punks) preinstalled, and its card on **Hub → Plugins** does two things:

- **Search** the public catalogue. Each result links to its post and says what it is: a **build** for a front end or device, a **game** for one console, or a **media** pack, with the size read from the title.
- **Download** a link you paste from a post's download area. The download area is shown to signed-in Arcade Punks members (registration is free). ROMarr never asks for your Arcade Punks login.

## The four kinds of link

Every post offers the same release four ways. Paste whichever suits your setup:

| Link | Example | ROMarr sends it to |
| --- | --- | --- |
| NZB | `https://arcade-punks.com/arcadepunks/2025-01/nzb/tb.lg.rEAP.zip` | NZBGet, SABnzbd or the built-in Usenet client |
| Torrent | `https://arcade-punks.com/arcadepunks/2025-01/torrent/TP-LG.torrent` | your torrent client |
| Mega | `https://mega.nz/file/<id>#<key>` | A debrid service (Real-Debrid, AllDebrid, …) |
| Rapidgator | `https://rapidgator.net/file/<id>/…` | Your Rapidgator premium account, or a debrid service |

The card shows which of your clients each kind goes to. A kind with no client set up says so before anything is sent.

- **NZB.** Arcade Punks ships each NZB inside a zip. NZBGet cannot open a zipped NZB from a link, so ROMarr downloads the zip, checks it holds exactly one valid NZB, and hands the NZB itself to your Usenet client. Their releases are RAR sets with PAR2 recovery files, posted to `alt.binaries.cores`.
- **Torrent.** The torrent holds the same RAR set. A client that takes `.torrent` links gets the link; Real-Debrid and other magnet-only services get the file.
- **Mega and Rapidgator.** These go through ROMarr's file-host route, the same one grabs use:
  - A **Rapidgator premium account** (**Settings → Cloud & File Hosts**) unlocks Rapidgator links for your direct download client.
  - A **debrid service** (**Settings → Download Clients**) unlocks Mega and Rapidgator.

  ROMarr never waits out a free download timer. For a *folder* link, open the folder and paste its file links, one per part.

Keep the full Mega link, including everything after `#`: that part is the decryption key.

## Setting up Usenet the way the guides do

1. Sign up with a Usenet provider that carries `alt.binaries.cores` with long retention. The guides use Newshosting (`news.newshosting.com`, port 563, SSL, up to 100 connections).
2. Install [NZBGet](https://nzbget.com/). In NZBGet open **Settings → News-Servers** and enter the provider's host, port 563 with encryption on, and your provider username and password. Press **Test Connection**, then **Save all changes**.
3. Leave PAR check and unpacking on, so NZBGet repairs and extracts the RAR set.
4. In ROMarr add NZBGet under **Settings → Download Clients**, or set `NZBGET_URL`, `NZBGET_USER` and `NZBGET_PASS`. Downloads go to its ROMarr category (`romarr` unless you changed it).

SABnzbd works the same way: add the provider under **Config → Servers**, then add SABnzbd to ROMarr. You can also enter the provider in ROMarr's built-in Usenet client and skip a separate app. The [Usenet guide](USENET.md) covers each client.

Your provider login stays in your Usenet client. ROMarr stores only how to reach that client.

## What happens to the download

- **A game for one platform** (for example *The Rumble Fish 2 … for the Sega Dreamcast – Atomiswave conversion*): ROMarr reads the platform from the title, or you choose one on the card. The job is queued like any grab and imported into that platform's library when it finishes.
- **A whole build or media pack**: there is no ROM platform to import into. ROMarr sends it to the client and records it in History, and the files stay where the client puts them. Flash or copy the build as its post describes.

## Privacy and limits

- The plugin reaches one host, `www.arcadepunks.com`, and only its public `/wp-json/wp/v2/posts` API. It never requests the pages Arcade Punks' `robots.txt` hides from AI crawlers.
- ROMarr fetches an NZB or torrent only from `arcade-punks.com/arcadepunks/…`, over https, without following redirects. Zips over 64 MiB and NZBs over 256 MiB are refused.
- Links carrying a login, a port or a query string are refused.

Arcade Punks builds and hosts these packs. ROMarr includes none of their content and is not made by Arcade Punks; please support them through their [membership](https://www.arcadepunks.com/membership/).
