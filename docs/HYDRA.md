# Set up a Hydra Launcher source

A Hydra source is a JSON list of game titles and download links. ROMarr searches it as an indexer. The source itself needs no account, API key, or Hydra Launcher installation. A download client or file host may need its own credentials.

## Check and save a source

1. Open **Settings → Indexers → Hydra Launcher sources**.
2. Paste the source's JSON URL and choose **Check source**, or choose **Browse the library** and then **Check** beside a source. The [Hydra library](https://library.hydra.wiki) is a community catalogue. Its trust labels are supplied by that catalogue. Use sources you trust and files you have permission to download.
3. Check the release count, platforms, and download routes. If a route needs a client, use **Add Direct HTTP** when offered or open **Download Clients** and add the matching client. Check the source again.
4. Choose **Save source**. You can use **Save anyway** when a valid source has a missing route and you want to finish configuring the client later.
5. Open **Interactive Search**, search for a title listed in the preview, and choose its platform when known. Check the source and platform on the result before grabbing. Follow the transfer and import under **Activity → Queue** and **History**.

## Match links to a download client

| Source link | Client |
| --- | --- |
| Magnet | qBittorrent, Transmission, Deluge, or a configured debrid service that accepts magnets |
| Direct file | Direct HTTP or Mister Download Manager |
| File host page | Real-Debrid or AllDebrid. Direct HTTP also works with supported hosts that offer a public download API, such as pixeldrain. |

A route marked ready has a configured client. The check reads the list and plans the route. A real grab also depends on the client being reachable and the file host accepting that download. Sources may list mirrors, which ROMarr can try when appropriate.

For cartridge and disc files, [DAT verification](FEATURES.md#files-verification-and-names) checks the bytes after download. If you already have the files locally, use the [existing library setup](GETTING-STARTED.md).

## Troubleshooting

| What the check says | What to try |
| --- | --- |
| The host refused to send the source (401 or 403) | Pick a different source from the library. The source URL itself takes no indexer key. |
| The host answered 404 Not Found | Copy the current JSON URL or choose the entry again from the library. |
| The host returned a web page, or JSON has no "downloads" list | Copy the source file's raw JSON URL from its publisher. |
| ROMarr could not look up or connect to the host | Check the URL and DNS or connectivity from the machine running ROMarr. |
| The host did not answer within the timeout | Try again. For a large source, configure **Request timeout** under **Advanced** in the Hydra indexer editor. |
| The certificate could not be verified | Check the URL and the server's configured trust store. |
| The host asked ROMarr to slow down | Wait a few minutes before checking again. |
| Links have nowhere to go, or only some links work | Open **Download Clients** and add the client shown in the preview. **Only** some file hosts have public download APIs. |
| Search is empty | Try a title from the preview, check that the source is enabled, and choose the platform shown in the source. ROMarr caches source contents for up to an hour. |

Developers can exercise check, save, search, grab, and import with a local fixture using `scripts/hydra_proof.py`. Run it against a disposable ROMarr instance because it adds test entries.
