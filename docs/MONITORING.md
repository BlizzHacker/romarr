# Find games, build a wishlist and monitor releases

Open **Games → Add New**. Enter a title and choose **Find games** to search your configured IGDB catalogue. Select a platform on a result to see its game page. If IGDB is not configured, you can still enter an exact title and choose a platform yourself.

- **Save to wishlist** remembers the game without searching or downloading it. Scheduled searches and RSS checks skip wishlist games.
- **Monitor and search now** starts a search using your existing sources and download clients. If no suitable release is available, the game remains monitored for future checks.
- **Pause monitoring** keeps the entry and its history while stopping automatic searches. **Resume monitoring** includes it in future checks again.

In **Wanted → Missing**, use All, Monitored or Wishlist to find your entries. The monitoring button changes one game at a time. Search All searches monitored entries; it leaves wishlist entries alone.

Select a game in your library to see its downloads and history, edit your personal status, rating and notes, or run an interactive search. The existing interactive search page still shows release scores and explanations before you choose a grab.

**Queue** also shows qBittorrent progress, speed and an estimated time when the client supplies them. Missing estimates stay unknown. ROMarr matches progress to the recorded client and torrent identity; similarly named downloads are not treated as the same job. Older queue entries without that identity may have no progress until a new download is started.

For an externally accessible RomM address, set `romm_public_url` in your ROMarr configuration. Only HTTP and HTTPS links are offered by the app.
