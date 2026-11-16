# ROMarr in Chrome and ChromeOS

ROMarr provides an installable web app and a separate Manifest V3 server launcher extension. Both require your existing running ROMarr server. They do not include a server or an emulator, and neither has been submitted to or approved by the Chrome Web Store.

## Install the app

Open your server in Chrome and sign in. Serve ROMarr at the origin root through HTTPS with a valid certificate (localhost is suitable for development). Plain HTTP LAN addresses can use the web UI, but do not qualify as secure contexts for the service worker or normal app install promotion. Reverse proxies must preserve root paths; subpath deployments are not supported by the server UI.

Use Chrome's install icon/menu, or the **Install app** button when Chrome supplies an install prompt. Chrome controls prompt availability and may require engagement before displaying it. ChromeOS installs the same web app in a separate window. Uninstall from Chrome/ChromeOS app management.

The manifest has a stable identity, standalone display and 192/512 pixel icons. The service worker supports registration only: it has no fetch handler, Cache Storage, precache, offline response or background synchronization. Network requests continue directly to your server and existing authentication applies. The app needs a network connection, including after installation. Signing out ends access through the installed app as it does in a normal tab.

See [Chrome installation guidance](https://developer.chrome.com/docs/modern-web-guidance/guides/pwa/install-web-app).

## Server launcher extension

`desktop/chrome` is an independent extension for desktop Chrome and ChromeOS (Chrome 102 or newer). Open its toolbar popup to save up to 20 server profiles, remove profiles and open Library, Queue, Emulators or Settings. Choose **Open / Connect** and grant access to that server when Chrome asks. Sign in on the server once. Library, Queue, Emulators and Settings then reuse its tab. If it is on a sign-in provider, the extension focuses that tab so you can finish signing in instead of interrupting it. Closing the popup does not cancel the connection. Names and URLs remain in `chrome.storage.local` on that Chrome profile; they are not synchronized. HTTP/HTTPS URLs may include a deployment path, but ROMarr itself currently serves at the root. Do not put keys or other secrets in paths. Credentials, query parameters and fragments are rejected.

For local testing, open `chrome://extensions`, enable Developer mode, choose **Load unpacked**, and select `desktop/chrome`. The Chrome Web Store release ZIP contains only runtime files, with the manifest at the ZIP root:

```sh
python3 desktop/chrome/package.py romarr-chrome-1.0.1.zip
```

Building the same source twice produces identical ZIP bytes. The extension uses a local service worker only to finish user-requested tab actions after the popup closes. It has no remote scripts, content scripts, injected code, cookie access, telemetry or background server API calls.

The installed permission is `storage`. Optional HTTP/HTTPS host access is requested only when you choose a saved server. Chrome grants access by hostname; the launcher additionally matches the exact saved origin and path before reusing a tab. No global `tabs`, `cookies`, `scripting` or `webRequest` permission is requested. Server tab IDs live only in Chrome session storage and are checked before reuse. You can revoke a hostname grant in Chrome’s extension site-access settings. See [Chrome host permissions and cookies](https://developer.chrome.com/docs/extensions/develop/concepts/storage-and-cookies) and the [Tabs API](https://developer.chrome.com/docs/extensions/reference/api/tabs).

## Store listing draft and privacy disclosure

**Name:** ROMarr Server Launcher
**Summary:** Save your ROMarr server addresses and open library, queue and settings in Chrome.
**Single purpose:** Locally organize and open the user's ROMarr server addresses.
**Permission justification:** Storage persists user-entered names and URLs; session storage remembers launcher tab IDs. Optional host access lets the extension identify and focus an existing tab for the server you select, preserving sign-in context. It is requested when you connect, rather than granted to every site during installation.

**Privacy policy text:** The ROMarr Server Launcher stores server names and URLs entered by you in Chrome's local extension storage. The extension does not collect, transmit, sell or synchronize this information. It does not read browsing history, page contents, cookies, passwords, API tokens or server responses. With a hostname grant, it inspects matching tab URLs and remembers the tab ID for your saved server; an off-origin sign-in tab is focused without reading its contents. Opening a saved link sends a normal browser navigation to that server, whose privacy policy and authentication apply. Remove a profile in the popup to delete it, or uninstall the extension to remove its local extension data. Do not enter secrets in server names or URL paths.

Before submission the publisher must host this privacy policy at a public URL, provide support/contact details, select accurate dashboard privacy declarations, add actual screenshots and required listing images, verify the ZIP in the target Chrome/ChromeOS versions, and complete store review. No store approval or listing URL is claimed. The PWA is installed from your server and does not require a Chrome Web Store listing.

## Verification evidence

The implementation was exercised in Linux Chromium on the build server. At viewport widths 360, 390 and 768 pixels, Library, Emulator Hub, General settings, Queue and History stayed within the viewport; populated long-name Queue/History fixtures scrolled inside their tables. The mobile navigation opened and closed. Chromium reported no page errors, no manifest errors and no installability errors in a persistent browser profile on localhost. The active service worker left Cache Storage empty. This verifies browser install eligibility; it does not claim a completed interactive OS installation.

The unpacked extension loaded in Chromium. Saving a profile persisted across reload, all four links used the expected URLs, a URL containing a username/password was rejected, and deleting the profile persisted across reload. It produced no page errors. Physical ChromeOS, Android and iOS devices and Chrome Web Store review remain separate release checks.

Automated endpoint, manifest, worker execution, URL validation, permission scope and reproducible-package checks live in `tests/test_chrome_app.py`.

### Sign-in regression proof (1.0.1)

A real headed Chromium extension popup was exercised with a native permission prompt and a server using HttpOnly/SameSite=Strict sessions. One sign-in survived all four route buttons, reuse of an existing tab, creation after closing that tab, and an off-origin sign-in flow. The service worker completed opening/focusing the server after the action popup closed. Cookies and credentials were not inspected, copied or changed by the extension.
