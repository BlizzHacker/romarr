# Emulators and import scanning

Open **Hub → Emulators** to find the emulator for your system. Each entry links to the project's downloads and setup guide. The catalog includes RetroArch, the standalone console emulators, ScummVM, DOSBox, and the experimental Lichii and Eglantine Switch 2 projects.

## Install a portable build

1. Check the host shown at the top of the page. An install goes onto the computer running ROMarr.
2. Click **Check release** beside a project.
3. Choose a matching portable build. ROMarr checks the release's SHA-256 digest, downloads it to a staging folder, applies enabled scanners, and publishes the installation after verification.
4. Open the installed emulator on that computer. Use its setup guide to choose the game folders and configure controllers, graphics and firmware.

Installations are kept under tools/emulators beside ROMarr's state file. Supporting files and license notices stay with the package. A failed download, checksum, extraction or scan leaves the previous installation available.

If GitHub rate-limits release checks, the page offers the catalog's pinned, verified release and labels it clearly. Set ROMARR_GITHUB_TOKEN in the host environment to raise the API limit if you want fresh release checks. It is used only to query GitHub release metadata.

Automatic installation handles verified ZIP, tar.gz, tar.xz and AppImage packages for the host's operating system and architecture. Use **Downloads** for other packages or a different device. The Flatpak links for RetroArch, PCSX2, Dolphin and ScummVM open Flathub manifests on your Linux or SteamOS device. The device's Flatpak installer handles those packages.

Lime3DS users should use Azahar. Eden and Ryubing host their own distribution sites. Citron's upstream availability needs checking. Lichii and Eglantine are experimental, and a game appearing in ROMarr's catalog does not establish emulator compatibility.

Firmware, BIOS and console keys belong in the locations specified by the emulator's setup guide. Supply your own dumps. Adding a directory to ROMarr's library does not add that directory to an emulator automatically.

## Scan imports

Open **Settings → Media Management** and enable the scanners you want to require.

| Scanner | Setup |
| --- | --- |
| ClamAV | Enter the clamd socket or TCP address. The daemon can inspect archives. Set its size limits high enough for your largest packages. |
| Microsoft Defender | Run ROMarr on Windows with Defender available. ROMarr finds the current MpCmdRun.exe and requests a custom scan with remediation disabled. |
| MWM antivirus | Enter your MWM HTTPS URL and set ROMARR_MWM_TOKEN in ROMarr's environment. MWM must see the file at the same absolute path. It uses Defender on Windows or ClamAV on Linux. |
| VirusTotal | Set ROMARR_VIRUSTOTAL_API_KEY in ROMarr's environment. ROMarr sends SHA-256 hashes to look up existing reports. A missing report, rate limit or failed request holds the import. |

Secrets stay in the server environment. Restart ROMarr after changing environment variables. The same checks apply to manual imports, completed downloads and managed emulator installations. If an enabled scanner is unavailable or ClamAV refuses the file size, the import waits. A reported infection blocks the import and appears in History.

Windows Security can also act independently of ROMarr. If it quarantines a source file, check its Protection History before retrying. ROMarr cannot import a file that is no longer present.

## MWM and Arkana inspection

The Emulator Hub also has an inspection form for files you want to examine more closely.

- **Inspect with MWM** requests its static malware report.
- **Send to Arkana** asks MWM to copy the chosen file into your configured Arkana lab and starts an analysis job. Follow the job in MWM's Malware Lab.

Connect Arkana in MWM first and configure its samples directory. The file path must be visible to both ROMarr and MWM. These tools help you investigate a suspicious file. Antivirus import checks use the scanner settings above.

Upstream references include [MWM](https://github.com/BlizzHacker/mwm), [Arkana](https://github.com/BlizzHacker/Arkana), [Microsoft Defender commands](https://learn.microsoft.com/en-us/defender-endpoint/command-line-arguments-microsoft-defender-antivirus), and the [VirusTotal file-report API](https://docs.virustotal.com/reference/file-info).
