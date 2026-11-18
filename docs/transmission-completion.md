# Transmission completion and ownership

Transmission clients accept a download directory, expose the returned torrent
hash as the job identity and poll fully completed, nonempty torrents. Polling
uses the configured label before returning import paths. An unlabelled torrent
can report progress or complete only when exactly one active queue row matches
its configured client and hash. This covers older RPC 16 grabs and clients
configured without a label. Soundtrack torrents use a separate label and never
enter normal ROM completion polling.
Paths come from actual torrent files, which may differ from the display name.
Multiple files must share a safe root; metadata and every file must be complete
before import. A finished wanted subset is not a completed payload.

An unowned duplicate magnet can be attached in its existing absolute download
directory. ROMarr verifies its hash, preserves unrelated labels, applies the
configured label, and verifies the result. Failed verification removes only the
attempted label. Torrents already labelled for ROMarr, another ROMarr request or
a retained collection are refused, including unlabelled torrents with an active
queue owner. Metainfo duplicates are refused; attachment
requires the magnet identity. Files are never moved or deleted during attachment.
Configure remote path mappings when the existing directory differs on the ROMarr
host. RPC failures remain visible to the importer.

Queue percentage, speed and ETA use read-only RPC calls within the shared queue
deadline. Soundtrack grabs use a separate label and client-scoped hash watch.
The watch waits for metadata, file verification and every file's bytes before
attempting to import from the validated payload path.

Labelled submissions discover and cache the RPC version through `session-get`.
A successful connection test supplies that cache. RPC 17 and later receive labels
in `torrent-add`. RPC 16 receives labels through `torrent-set` only after a new
torrent is created, then its hash, label and destination are verified. Failed
capability discovery or RPC versions below 16 refuse labelled submissions before
adding anything. Post-add labelling or verification failures report failure and
leave the torrent intact for inspection. Vuze and BiglyBT use the same capability
checks through their Transmission RPC plugins.

Tests simulate RPC responses and exercise the existing 409 handshake; they do not
claim compatibility with a running Transmission daemon or all RPC-compatible forks.

Existing DAT source torrents use the Collections workflow, separately from
selective grabs through search results. ROMarr saves file identities, wanted
flags, native priorities and the original paused state before changing a
selection. It restores them on completion or cancellation, and keeps recovery
records across restarts and interrupted requests. Unexpected external file or
selection changes require review rather than being overwritten. Active sources
are excluded from ordinary completion and cannot be attached to another grab.
The staging cap covers ROMarr copies, not files downloaded by Transmission.
