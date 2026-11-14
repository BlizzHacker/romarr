# ROMarr privacy

Updated November 14, 2026.

ROMarr manages your game library on your device or a server you choose. This page covers the official ROMarr application, its desktop clients and the companion Chrome extension. The websites, sources, plugins, emulators and servers you connect have their own policies.

## What stays on your device or server

The ROMarr backend stores settings, library records, download state and logs in its configured data directory. Connections can require passwords, API keys or tokens. Treat the data directory and backups as private. Browser sessions remain in browser storage or the desktop app’s browser profile.

The Chrome extension stores the server shortcut names and addresses you enter in local extension storage. It does not request access to browsing history, read your server’s cookies, or make background API requests. Opening a shortcut navigates your browser to that server, where you sign in normally.

The Windows client stores desktop preferences locally and isolates browser profiles by server origin. Server addresses do not include embedded credentials or API keys. The Android client’s storage and permissions are documented in the Android guide for that build; selected files may be copied into app storage for local imports or shared with an emulator you choose.

## When information leaves your device

Your chosen servers receive the requests you make to manage them. Search sources, metadata providers, download clients, plugin repositories, release hosts and other integrations receive network requests needed for the features you enable. These services can see your IP address and the queries or identifiers sent to them. Downloading and installing optional tools also contacts their hosts.

Remote ROMarr server operators control their own settings, storage, logging and access. Use HTTPS when connecting over a network you do not trust. A remote connection manages the server’s files; it does not give that server access to arbitrary local files.

ROMarr does not require an advertising account or include an advertising SDK. You can choose to submit diagnostics or screenshots when asking for support. Remove passwords, keys, private URLs and personal filenames before sharing them publicly.

## Removing data

Delete Chrome extension profiles in its popup or remove the extension to clear its local storage. For desktop browser sessions, clear the relevant browser profile. Remove backend data only after backing up anything you wish to retain. Uninstalling an Android app normally removes its private app storage; exported files and remote server data remain where you saved them.

## Questions and reports

Use the [project support channels](https://github.com/BlizzHacker/romarr/issues) for general privacy questions. Do not put credentials or private records in a public issue. Follow [SECURITY.md](../SECURITY.md) for security reporting.

Store data-safety forms must be checked against the exact signed build and enabled SDKs before submission. This page does not claim that third-party integrations share ROMarr’s practices.
