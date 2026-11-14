# Android emulator apps

ROMarr lists Android apps alongside desktop emulators. Choose Android in the host filter or search by app name or package ID. Verification checked official distribution pages and build metadata on 2026-11-14. No apps were run on Android.

Install manually from the official source. The embedded Android host refuses managed desktop emulator installation. Package IDs identify apps; accepting a content URI and supporting a particular game are separate requirements.

| App | Package | Official Android source | Source-build ABIs |
|---|---|---|---|
| DuckStation | com.github.stenzek.duckstation | [Official source](https://downloads.duckstation.org/android-testing/) | Not independently established; check selected APK |
| Flycast | com.flycast.emulator | [Official source](https://github.com/flyinghead/flycast/releases) | Not independently established; check selected APK |
| melonDS-android | me.magnum.melonds | [Official source](https://github.com/rafaelvcaetano/melonDS-android/releases) | armeabi-v7a, arm64-v8a, x86_64 |
| Azahar | org.azahar_emu.azahar | [Official source](https://azahar-emu.org/) | arm64-v8a, x86_64 |
| Dolphin | org.dolphinemu.dolphinemu | [Official source](https://dolphin-emu.org/download/) | arm64-v8a, x86_64 |
| PPSSPP | org.ppsspp.ppsspp | [Official source](https://www.ppsspp.org/download/) | armeabi-v7a, arm64-v8a, x86_64 |
| Vita3K | org.vita3k.emulator | [Official source](https://github.com/Vita3K/Vita3K/releases/tag/continuous) | arm64-v8a, x86_64 |
| RetroArch / libretro | com.retroarch | [Official source](https://www.retroarch.com/platforms.html) | armeabi-v7a, arm64-v8a, x86_64 |
| ScummVM | org.scummvm.scummvm | [Official source](https://docs.scummvm.org/en/latest/other_platforms/android.html) | Not independently established; check selected APK |
| Eden | dev.eden.eden_emulator | [Official source](https://git.eden-emu.dev/eden-emu/eden/releases) | arm64-v8a, x86_64 |
| Lemuroid | com.swordfish.lemuroid | [Official source](https://github.com/Swordfish90/Lemuroid/releases) | Not independently established; check selected APK |
| redream | io.recompiled.redream | [Official source](https://play.google.com/store/apps/details?id=io.recompiled.redream) | Not independently established; check selected APK |
| M64Plus FZ Emulator | org.mupen64plusae.v3.fzurita | [Official source](https://play.google.com/store/apps/details?id=org.mupen64plusae.v3.fzurita) | Not independently established; check selected APK |
| Pizza Boy A: GBA Emulator (Basic) | it.dbtecno.pizzaboygba | [Official source](https://play.google.com/store/apps/details?id=it.dbtecno.pizzaboygba) | Not independently established; check selected APK |
| Snes9x EX+ | com.explusalpha.Snes9xPlus | [Official source](https://www.explusalpha.com/contents/snes9x-ex/downloads) | armeabi-v7a, arm64-v8a, x86 |
| MAME4droid (Current) | com.seleuco.mame4d2024 | [Official source](https://github.com/seleuco/MAME4droid-Current/releases) | arm64-v8a |
| EKA2L1 (Android) | com.github.eka2l1 | [Official source](https://github.com/EKA2L1/EKA2L1/releases) | armeabi-v7a, arm64-v8a, x86_64 |

ABI lists are source targets, not proof that every distribution includes each ABI. Empty lists mean unknown, not universal. OS, GPU, firmware and individual game requirements vary.

Pizza Boy A Basic is proprietary freeware with a separate paid Pro app. Redream has a free tier and paid upgrades. M64Plus FZ has ads and a separate Pro app; its store listing was last updated April 2024. Snes9x EX+ includes Snes9x noncommercial terms and GPL components. DuckStation Android beta redistribution is prohibited by its official page; link there instead.

Azahar succeeds Lime3DS and PabloMK7 Citra. DuckStation has a current November 2026 Android beta rewrite. Vita3K and EKA2L1 are experimental. Eden standard, legacy and ChromeOS variants differ. melonDS Android is separate from desktop melonDS. DOSBox Pure runs as a RetroArch core and is not a separate Android app.
