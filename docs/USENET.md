# Usenet: get one search working

A Usenet setup has three separate parts:

| Part | What it does | Where you configure it |
| --- | --- | --- |
| Provider | Supplies an NNTP server, login and access to articles | Built-in Usenet, or your SABnzbd/NZBGet server settings |
| Newznab indexer | Searches release listings and returns NZB files | ROMarr → Settings → Indexers, directly or through Prowlarr |
| Download client | Reads an NZB and retrieves its articles from your provider | ROMarr → Settings → Download Clients |

A provider account alone does not add search results. You can use Usenet without a torrent indexer. A source still needs to list the game you want, and coverage differs between systems and indexers.

## Connect the download side

Open **Settings → Setup Guide**, choose **Find and download games**, then open **Downloads**.

Choose **Built-in Usenet** to enter your provider's NNTP hostname, TLS port, username and password directly. Pick a writable Save Path on the ROMarr host and use **Test** before saving.

If you already run SABnzbd or NZBGet, choose **Connect an existing client** instead. Configure the provider in that app, then connect ROMarr using that app's address and API credentials. Its Test checks that connection. Check the provider connection in the app too.

The guide also offers standalone MDM torrents and direct HTTP, plus the ROM Hub client. MDM needs its engine installed from **Download Clients**. These are separate acquisition paths; they do not create a Usenet indexer.

## Connect the search side

In **Search sources**, choose **Add a Usenet indexer**. Enter the Newznab indexer's API URL and API key, enable it and use **Test**. Use the indexer's credentials here, not the provider's NNTP login.

If Prowlarr already manages your indexers, connect it instead. Check the individual indexer's Test, game categories and API access in Prowlarr. An empty Prowlarr installation has no sources to search.

Open **Interactive Search** and try one title and platform. Compare it with the same search on your indexer's website. A working Test confirms a connection; it cannot confirm that a particular game is available.

## Find the step that stopped

| What happened | What to check next |
| --- | --- |
| No releases returned | The source is enabled, Test succeeds, its game categories/API access are correct, and it lists that title. Try a shorter title and the right platform. No download has started yet. |
| Results are rejected | Read the scoring reasons for platform, region, format and other preferences. |
| Grab is refused | An enabled download client supports that result's protocol. Usenet results need a Usenet client. |
| Download started, then failed | Queue/History and the client's error: provider authentication/TLS, missing articles, limits, or storage. Check the provider in the client before changing indexers. |
| Download finished, import failed | Finished files are visible to ROMarr and the library path is writable. Check mounts and remote path mappings. See the [NAS library guide](NAS-LIBRARIES.md). |

## Other ways to start

[ROM Hub plugins](PLUGINS.md) offer other supported sources, including direct downloads. Configure a ROM Hub download client and an enabled plugin that supports search. Each source has its own coverage and access requirements.

You can also use **Manual Import** for files you already have, with no provider, indexer or download client. Start with one small import before adding a large Wanted list.
