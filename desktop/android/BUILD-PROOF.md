# Android preview build proof

Verified on 2026-11-14 using the server volume; no Android assets or toolchains
were downloaded to Windows C:.

- Build: JDK 17.0.21, Gradle 8.13, AGP 8.13.0, Chaquopy 17.2.0,
  bundled Python 3.13.16, target/compile API 36, ARM64 + x86_64.
- Latest completed instrumentation: 2026-11-14, with opt-in live SSO enabled.
  Final signed packages are rebuilt from the committed source with a private source/artifact receipt.
- API 36 / Android 16 KVM x86_64 emulator: **11 tests, 0 failures, 0 errors, 0 skipped**.
  They exercise packaged core health/UI/OpenSSL; protected API 401 / setup 409 /
  native session 200; FileProvider state denial; native import byte preservation;
  actual WebView authenticated fetch and HttpOnly secrecy; collision preservation;
  stale local callback isolation from a remote selection; baked core/package version
  equality; quarantine/internal file handoff denial; HTTPS sign-in consent, normal
  external links and stale redirect isolation; real auto-submitting cross-authority form consent; report scan/quarantine/restore
  in private Android storage; running scheduled jobs; and the active Authentik login page
  rendered in the same WebView without credentials entered.
- Private library fixture persisted unchanged across force-stop/relaunch
  (separate debug-device proof; synthetic fixture removed afterward).
- Release APK `debuggable=false`; `apksigner verify` passed APK Signature Scheme v2.
  Release AAB passed `jarsigner -verify` with the same preview signer.
- `zipalign -c -P 16 -v 4` passed. All **138 ELF files** in each APK/AAB,
  including both ABIs and nested Chaquopy archives, have PT_LOAD alignment >=16 KB.
- The emulator page size was **4096**. No 16 KB runtime, ARM64 physical-device,
  oldest-supported-Android, authenticated production-server management, or real-gameplay proof is claimed.
- Lint passed with warnings for pinned dependency versions, required SPA JavaScript,
  and untranslated English strings. Predictive back uses AndroidX dispatch.

Final signed artifacts/hashes are refreshed after latest core integration. Earlier
APK/AAB candidates are superseded and must not be distributed.

Signer certificate SHA-256:
`76ade6d94d71536eeacc5d87c4db4d05a877d7c0410d3a94b8c307d9fe69868c`.
Private signing material is excluded from Git and public artifacts; see
[Android guide](../../docs/ANDROID.md) for custody requirements and Play preparation.
No store submission was performed.

Private raw receipts, emulator data, SDK/JDK/Gradle caches and screenshot are in
`/opt/repos/ai-state/wadei/romarr-android/`. Source-local Gradle build output is in
`desktop/android/app/build/`. Re-run `scripts/build-android-audit.py APK_OR_AAB`
to audit packaged ELF alignment independently.
