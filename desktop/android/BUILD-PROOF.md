# Android preview build proof

Verified on 2026-11-14 using the server volume; no Android assets or toolchains
were downloaded to Windows C:.

- Build: JDK 17.0.21, Gradle 8.13, AGP 8.13.0, Chaquopy 17.2.0,
  bundled Python 3.13.16, target/compile API 36, ARM64 + x86_64.
- Actual Gradle `assembleDebug assembleRelease bundleRelease connectedDebugAndroidTest lint`:
  **BUILD SUCCESSFUL**, 2026-11-14 14:49 UTC.
- API 36 / Android 16 KVM x86_64 emulator: **7 tests, 0 failures, 0 errors**.
  They exercise packaged core health/UI/OpenSSL; protected API 401 / setup 409 /
  native session 200; FileProvider state denial; native import byte preservation;
  actual WebView authenticated fetch and HttpOnly secrecy; collision preservation;
  and stale local callback isolation from a remote selection.
- Private library fixture persisted unchanged across force-stop/relaunch
  (separate debug-device proof; synthetic fixture removed afterward).
- Release APK `debuggable=false`; `apksigner verify` passed APK Signature Scheme v2.
  Release AAB passed `jarsigner -verify` with the same preview signer.
- `zipalign -c -P 16 -v 4` passed. All **138 ELF files** in each APK/AAB,
  including both ABIs and nested Chaquopy archives, have PT_LOAD alignment >=16 KB.
- The emulator page size was **4096**. No 16 KB runtime, ARM64 physical-device,
  oldest-supported-Android, production remote-server, or real-gameplay proof is claimed.
- Lint passed with warnings for pinned dependency versions, required SPA JavaScript,
  and untranslated English strings. Predictive back uses AndroidX dispatch.

Public artifact candidates are under
`/opt/repos/ai-state/wadei/romarr-android/release/`:

Exact artifact hashes are published in the release SHA256SUMS after final packaging. Earlier private preview candidates are superseded.

Signer certificate SHA-256:
`76ade6d94d71536eeacc5d87c4db4d05a877d7c0410d3a94b8c307d9fe69868c`.
Private signing material is excluded from Git and public artifacts; see
[Android guide](../../docs/ANDROID.md) for custody requirements and Play preparation.
No store submission was performed.

Private raw receipts, emulator data, SDK/JDK/Gradle caches and screenshot are in
`/opt/repos/ai-state/wadei/romarr-android/`. Source-local Gradle build output is in
`desktop/android/app/build/`. Re-run `scripts/build-android-audit.py APK_OR_AAB`
to audit packaged ELF alignment independently.
