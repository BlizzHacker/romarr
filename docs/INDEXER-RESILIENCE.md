# Indexer request timeouts and cooldowns

Every indexer form has a **Request timeout (seconds)** field, from 1 to 300
seconds (default 60). Saved searches and the Test button use that value,
including an unsaved form's Test. Invalid values are rejected before saving.
This is the HTTP request timeout; it does not change the service's existing
search concurrency limits or act as a total deadline across several calls.

When Prowlarr, Torznab/Newznab, Torrent RSS or TorrentPotato answers HTTP 429,
ROMarr pauses further search and capability requests through that live client.
Requests during the pause fail immediately with a safe retry-delay message;
workers do not sleep and other configured indexers can continue responding.

ROMarr understands both seconds and HTTP dates in `Retry-After`, bounded to
24 hours. Missing, malformed, zero or expired advice uses 30 seconds, doubling
for consecutive 429 responses to a maximum of 15 minutes. A successful request
after the pause resets that fallback. Concurrent responses preserve the longest
pause; an older successful response cannot cancel a newer 429. Torrent grabbing
keeps its existing streaming, size and origin protections independently.

Cooldowns are held in memory on each client. Restarting ROMarr or rebuilding
clients after a configuration change resets them. A new unsaved Test creates a
separate client. No credential or cooldown database is written to disk.

## Feature inspiration and licensing

The behavior was independently implemented for ROMarr after reviewing the
feature description of [Questarr PR #1337](https://github.com/Doezer/Questarr/pull/1337),
commit `b2de36caa1b81a5dbbee36b491a423ad5701252d`, and its indexer documentation.
Questarr distributes its source under GPL-3.0; no Questarr implementation code
was copied or translated. ROMarr retains its MIT license and existing
standard-library plus requests runtime.
