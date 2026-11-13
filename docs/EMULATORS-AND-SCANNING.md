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

## Browse the full catalog

The catalog snapshot contains 1,214 emulator, fork and companion-tool entries. Search by name, system, license or forge, and use Previous / Next to see every result. Free and noncommercial software is included; the license on each project still applies. Entries identify upstream sources and do not imply that every project has been installed or tested.

**Browse forks** retrieves public fork pages directly from GitHub, GitLab or Forgejo. **More forks** continues through the network instead of stopping at a fixed list. Discovery keeps local records so they can be searched afterward. An unavailable or private project cannot be restored by catalog discovery.

Sources include the [Emulation General Wiki](https://emulation.gametechwiki.com/index.php/Main_Page), [DerekTurtleRoe's emulator list](https://github.com/DerekTurtleRoe/awesome-emulators), [alnacle's list](https://github.com/alnacle/awesome-emulators), and upstream forge APIs. Attribution and source links are included in the bundled catalog. Eden and Ryubing use their own Forgejo repositories. AnyPS5 is an executable porting tool; its listing does not promise general PS5 compatibility.

## Sources and references

Choose **Sources and references** in the Emulator Hub for the 58 browser, source and preservation resources from the supplied list. They are kept separate from the emulator count. Thirty-six have identified links; 22 ambiguous or unverified destinations keep their names and notes without a guessed link.

Open **ROM Hub plugins** to find supported acquisition integrations, including MiNERVA and Internet Archive. A reference listing does not install a plugin or bypass a source's access requirements.

Major source repositories are backed up in [ROMarr Emulator Backups](https://gitea.moveweight.com/romarr-emulators). All 55 mirrors passed Git object-connectivity checks and matched their upstream heads at verification. They refresh every 24 hours. These preserve source history and upstream licenses; release binaries and Git LFS objects are not included. The [verification record](proof/emulator-mirrors-2026-11-13.json) lists each source and commit.

## Set up RetroArch and its cores

1. Install RetroArch on the gaming computer using its official download or the Linux / SteamOS Flatpak link.
2. In **Hub → Emulators**, choose **RetroArch cores**. All 350 current upstream core-info records are searchable, including engines and experimental cores.
3. On the same computer as ROMarr, click **Install host core**. ROMarr downloads the official libretro build, checks the extracted library's CRC32 against the buildbot index, runs your enabled scanners, and records a local SHA-256 receipt. The buildbot CRC is an integrity check, not a publisher signature.
4. Click **Prepare RetroArch folders** to create a separate configuration, cores, system, saves and states directories. This preserves your existing RetroArch settings. The result includes the launch command.
5. Click **Check firmware** to see exact required filenames and the system directory. Put your firmware there, then check again. This checks presence and nonzero size; consult the core guide for expected hashes.
6. Start RetroArch with the shown configuration, load the installed core, then load a compatible game. Controllers and graphics settings depend on your gaming device.

Cores vary by host and some have no published binary. Those return a clear message rather than pretending to install. If ROMarr runs on a server and you play on a PC, use RetroArch's Online Updater on the PC.

Open **Hub → Plugins** to install or configure ROM Hub sources such as MiNERVA and Internet Archive, then search and import games through ROMarr's existing library flow. Availability, source access and game compatibility differ. A missing BIOS or console key is not supplied by an emulator installer.

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
