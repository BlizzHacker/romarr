# Android preview build proof

Verified on 2026-11-15 using the server volume. No Android assets, browser profiles,
toolchains or build outputs were written to Windows C:.

- Version: **1.10.1-preview**, Android versionCode **2**.
- Toolchain: JDK 17.0.21, Gradle 8.13, AGP 8.13.0, Chaquopy 17.2.0,
  bundled Python 3.13.16, target/compile API 36, ARM64 + x86_64.
- API 36 / Android 16 KVM x86_64 emulator: **12 tests, 0 failures,
  0 errors, 0 skipped**, including the opt-in live SSO check.
- Coverage includes actual bundled server health/UI/OpenSSL, anonymous API/setup
  rejection and native session access, HttpOnly secrecy in the actual WebView,
  FileProvider state denial, Unicode import names and byte preservation,
  collision preservation, deterministic stale-callback isolation, package/core
  version equality, quarantine/internal file handoff denial, private report
  scan/quarantine/restore, and a live process-local scheduler.
- Sign-in checks cover HTTPS authority consent, ordinary external links, stale
  redirect isolation, an actual auto-submitting cross-authority POST, and the
  active Authentik login form without credentials entered. The known public privacy
  URL is opened through an external ACTION_VIEW intent without switching servers.
- The native toolbar and actual WebView/IME bounds check also passed at **360 ×
  640 dp**, font scales **1.0 and 1.3**, and actual **640 × 360 dp landscape** at
  font scale **1.3**. It verifies the 48 dp menu target, a long server label,
  non-overlapping toolbar/WebView bounds, and resize above the real keyboard.
  Emulator rotation was checked against actual display frames and screenshot
  dimensions. Native local/remote/keyboard screenshots are kept with the receipts.
- The earlier private library fixture persisted unchanged across force-stop and
  relaunch; that synthetic fixture was removed afterward.
- Debug lint passed. Final packaging runs release lint, checks the release APK is
  non-debuggable, verifies APK/AAB signatures and APK ZIP alignment, and audits
  all ELF PT_LOAD alignments recursively, including nested Chaquopy archives.
- Final packaging records a clean Git revision and hashes all build inputs before
  and after building. Every packaged core Python/JSON source is compared with
  that checkout; compiled core modules and the native Python backend are also
  compared with recompilation of the exact source. Artifact SHA-256 values and
  reconciled device-test counts are recorded outside Git without signing secrets.

A first-raster issue also reproduces in unchanged 1.10.0 on this software-rendered
emulator: ordinary SPA content can stay blank until a normal scroll repaints it.
No production rendering workaround was added. Product screenshots labeled
`normal-scroll` show real pages after that ordinary gesture; layout/keyboard
screenshots and the [native layout receipt](layout-proof.json) record the measured checks.

The emulator uses **4096-byte pages**. No 16 KB runtime, ARM64 physical device,
oldest-supported-Android run, production authenticated management, real gameplay,
or Play submission is claimed. A signed AAB is not Play approval. Lint warnings
include pinned dependencies, required SPA JavaScript and untranslated English
strings; predictive back uses AndroidX dispatch.

Preview signer certificate SHA-256:
`76ade6d94d71536eeacc5d87c4db4d05a877d7c0410d3a94b8c307d9fe69868c`.
Private signing material stays outside Git; see the [Android guide](../../docs/ANDROID.md)
for custody and Play preparation. Earlier 1.10.0 candidates are superseded.

Private logs, SDK/JDK/Gradle caches and emulator data remain under
`/opt/repos/ai-state/wadei/romarr-android/`. The 1.10.1 signed package and exact-source
receipt directory is `final-1.10.1-preview-20261115/`; layout evidence is under
`toolbar-proof/`. Source-local Gradle output is in `desktop/android/app/build/`.
Re-run `scripts/build-android-audit.py APK_OR_AAB` to inspect packaged ELF alignment.
