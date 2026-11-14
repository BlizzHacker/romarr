# ROMarr for Android

The native Android host bundles the ROMarr Python server and UI using Chaquopy.
It starts on an ephemeral **127.0.0.1-only** port and stores state, DATs and ROMs
inside the application's private files directory. Python is included in the APK:
there is no Termux, Docker, browser server, or separately installed Python requirement.
Internet sources and remote servers still require a network connection.

Minimum Android version: Android 7.0 (API 24). Target/compile API: 36.
Packaged architectures: ARM64 and x86_64. This is an installable Android application,
including on compatible Android desktop environments; it is not a Linux desktop binary.

## Use

- **This device** opens the bundled server. The native host preclaims this listener with an ephemeral secret and supplies
  a private HttpOnly session cookie before loading the UI. Other local apps cannot
  claim setup or use protected APIs without that session. Configure library platforms, indexers and download clients through its UI.
- **Servers** saves named HTTPS server origins and switches between them and the local
  library. Credentials are entered into each server's normal sign-in page; profile
  URLs reject embedded credentials, paths, queries and fragments. A trusted TLS
  certificate is required. Plain HTTP remote servers are deliberately unavailable.
- **Import** uses Android's system document picker to copy an explicitly selected ROM
  into private `library/Imported`, or a DAT/XML/ZIP into private `dats` and reload
  the checksum catalogue. Scan the imported library folder in ROMarr. A copied ROM
  is not described as verified until the core actually verifies it against a DAT.
  Existing filenames are preserved by choosing a new destination name.
- **Play file** grants temporary read access to one chosen local ROM through
  FileProvider and Android's ACTION_VIEW chooser. Only installed emulators accepting
  `content:` URIs and this MIME type can handle it. Some emulators need their own
  picker or a different intent contract. BIOS, emulator installation, archive
  extraction and compatibility are separate requirements. No emulator is bundled.

The local server lives in the app process. It may continue briefly after navigating
away, but **Android can suspend or terminate it**. There is no persistent background
service or unattended download guarantee. Use remote server mode for always-on work.
Closing/reopening the UI reuses the in-process server; a new process reloads persisted
state. Android uninstall / Clear storage removes the private library and profiles.

The shared ROMarr UI and HTTP integrations run locally; native desktop executables,
ROM Hub subprocess plugins, Playwright/Chromium downloads, self-updates, native
emulator installers, 7-Zip/unrar/bsdtar and shell tooling are not bundled.
ZIP support uses the Python standard library. Configure external download clients
and remote services only when reachable from the Android device. File permissions,
CA trust and Android process lifetime differ from Windows. Python requests uses
Chaquopy/certifi trust; WebView uses Android TLS trust.

## Build on the server volume

Use JDK 17, Gradle 8.13, Python 3.13, Android SDK platform 36, build tools 36.0.0 and
platform-tools. The build pins AGP 8.13.0, Chaquopy 17.2.0 and Python requirements.
No frontend build is used. Source staging includes Python and JSON catalogues
directly from `romarr/`; it never copies credentials or checkout metadata.

```bash
# Run on root@192.168.0.6; keep toolchains, caches and outputs on /opt/repos.
df -h /opt/repos
export JAVA_HOME=/path/on/opt/repos/to/jdk17
export ANDROID_HOME=/path/on/opt/repos/to/android-sdk
export ROMARR_BUILD_PYTHON=/path/on/opt/repos/to/python3.13
export GRADLE_USER_HOME=/opt/repos/ai-state/wadei/romarr-android/gradle-home
export PIP_CACHE_DIR=/opt/repos/ai-state/wadei/romarr-android/pip
export ANDROID_USER_HOME=/opt/repos/ai-state/wadei/romarr-android/android-home
export PATH="$JAVA_HOME/bin:$PATH"
./scripts/build-android.sh
# Optional connected emulator/device checks:
./scripts/build-android.sh connectedDebugAndroidTest
```

Outputs are under `desktop/android/app/build/outputs/`:
`apk/debug/app-debug.apk` is debug-signed for testing;
`apk/release/app-release-unsigned.apk` and `bundle/release/app-release.aab` are
unsigned by default. To sign a release, set `ROMARR_ANDROID_SIGNING` to an absolute
private Java properties file containing storeFile, storePassword, keyAlias and
keyPassword. Signed APK output is `apk/release/app-release.apk`. Never put that
file, keystore, or password inside the checkout.

The public 1.10.0 preview uses a persistent owner-controlled preview certificate
with SHA-256 fingerprint
`76ade6d94d71536eeacc5d87c4db4d05a877d7c0410d3a94b8c307d9fe69868c`.
Its PKCS12 key, password and signing properties are private mode-0600 files under
`/opt/repos/ai-state/wadei/romarr-android`, outside Git and release artifacts.
The owner must arrange an encrypted private backup of the key and password before
relying on update continuity; losing it prevents updates to that signed install.
This is a preview distribution identity, not proof of a verified Play publisher.
A future different Play signing identity may require uninstall/reinstall.

The instrumentation tests run the packaged Python interpreter, start the actual
loopback server, fetch health and UI responses, reject anonymous protected API/setup requests,
authenticate the actual WebView without a JavaScript credential bridge, copy a
user-selected content URI through the native import handler, exercise OpenSSL scrypt/TLS,
and verify FileProvider exposes library files while refusing state files.
A device test result does not prove emulator compatibility or every downloader.
ARM64 physical-device and 16 KB runtime checks remain separate release gates.

## Google Play preparation

Candidate package ID: `org.romarr.android`; confirm ownership before first upload.
Candidate store title: **ROMarr**.
Short description: **Manage your game library on this device and your own servers.**

Draft full description:

> Organize your own game library with ROMarr. Run its included library manager on
> your Android device or connect to your own HTTPS ROMarr servers. Import files
> through Android's document picker, configure checksum DATs, review your library,
> and open local files in compatible installed emulators. No games, BIOS files or
> emulators are included. Source integrations require your own services and access.
> Local work runs while Android keeps the application process alive.

Before submission, the publisher must:

1. Choose the permanent package ID and version, enable Play App Signing and sign
   the release AAB with the owner-held upload key. Do not publish a debug APK.
2. Provide a public privacy policy/support URL, store icon (512x512), feature graphic
   (1024x500), and genuine phone/tablet screenshots from the release build.
3. Complete content rating, app access instructions, target audience, advertising
   declaration, Data safety, and required account/developer verification/testing.
   If login blocks review, provide reviewer access to a nonproduction test server.
4. Review native library ELF and APK 16 KB alignment, test a 16 KB device/emulator,
   test ARM64 physical devices and accessibility/rotation/import/error flows, and
   run Play's pre-launch report. Building an AAB alone is not Play approval.
5. Review integration/content access and intellectual property policies. Distribute
   only application code and authorized assets; the package includes no game files.
6. Increase versionCode for every subsequent upload and recheck current requirements.

Draft data inventory for publisher verification (not a completed legal declaration):

| Data | Handling |
| --- | --- |
| Local ROMs, DATs, library state | Private device storage; removed by Clear storage/uninstall |
| Remote profile names/origins | Private SharedPreferences; backups disabled |
| Server login/session and configured integration credentials | WebView cookies and ROMarr private state; transmitted to the server/services the user configures |
| Searches, source requests, metadata and download client commands | Sent to explicitly configured integrations; their operators have their own retention policies |
| Emulator handoff | One user-selected local file receives a temporary read URI grant |
| Telemetry/ads SDKs | None added by the native host; audit bundled core integrations before disclosure |

Do not claim "no data leaves the device": configured source/metadata requests and
remote management transmit data. Whether a particular transfer is developer
collection, sharing, or an exempt user-directed transfer must be assessed against
the final released integrations and Google's definitions.

Official references:
[Chaquopy build/runtime support](https://chaquo.com/chaquopy/doc/current/android.html),
[Chaquopy version compatibility](https://chaquo.com/chaquopy/doc/current/versions.html),
[Play target API requirements](https://support.google.com/googleplay/android-developer/answer/11926878),
[16 KB support](https://developer.android.com/guide/practices/page-sizes),
[Storage Access Framework](https://developer.android.com/training/data-storage/shared/documents-files),
[FileProvider](https://developer.android.com/reference/androidx/core/content/FileProvider),
[Play App Signing](https://developer.android.com/studio/publish/app-signing),
[Data safety](https://support.google.com/googleplay/android-developer/answer/10787469).
