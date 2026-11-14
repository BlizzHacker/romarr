# Your first game with ROMarr

Remember a machine you loved playing? Start with that one. You can build a bigger collection later; the first goal is one game you can open and enjoy.

## What each app does

| App | Its job |
|---|---|
| ROMarr | Find, check and organize game files. You can also import files you already have. |
| An emulator | Run games made for a particular console or computer on your device. |
| RomM (optional) | Present your collection as a browsable library, with browser playback for supported systems. |
| ROM Hub (optional) | Add plugins for supported sources and tools. Each plugin explains its own requirements. |

If you use Sonarr or Radarr, ROMarr fills a similar search-and-import role for games. You can also use it with a plain folder and no search sources.

## 1. Install ROMarr on the computer that will manage your files

Choose your method in the [installation guide](INSTALL.md): Windows app, Docker/NAS, Proxmox or a source installation. For the Windows app, download the Windows ZIP from the release, extract it and open ROMarr.exe. Keep the extracted folder together.

Open ROMarr, set your password, then open **Settings → Setup Guide**. Choose **I already have ROMs** for the shortest route. Pick a folder for your library, or connect your existing RomM library. With Docker, enter the mounted path ROMarr sees, such as `/roms`, rather than your host’s folder name.

## 2. Choose one system and one game

Use a copy of a small game file you are allowed to use. Keep your original collection somewhere safe while you learn the workflow. You do not need to configure downloading, DAT catalogues or plugins first.

C64, Atari 2600 and ColecoVision are included as platforms. Open **Hub → Emulators** and search for your system. Read the emulator’s setup instructions and compatibility notes. A catalogue entry is a starting point, not a guarantee that every game works on your device.

## 3. Import the file

First confirm your destination under **Settings → Libraries**. Then open **Manual Import**, enter the source folder and click **Scan**. Check the file, platform, explanation and DAT result, then import the selected file. Scanning alone does not move anything.

Check **History** and the destination folder. A successful import means the file reached your library; playback is the next check. An **unknown** verification result means no loaded DAT identified the file, which does not by itself mean it is broken. A DAT is a catalogue of known filenames and checksums.

## 4. Open it and set your controls

Open the imported file in your chosen emulator, or let your connected library frontend scan it. Configure keyboard or controller bindings in the emulator or player. Test movement and the main buttons before changing lots of settings.

For a USB controller or joystick, first check that your operating system recognizes it, then select and map it in the emulator. Older original controllers may need a suitable adapter; check the device and emulator documentation before buying one. ROMarr does not configure controller bindings.

Some systems require firmware or BIOS files. Follow the emulator’s official instructions and use files you are entitled to use. A firmware filename check alone does not prove a game is playable.

## 5. Enjoy that first game

Once it opens and your controls work, you have a working starting point. Add another game or system at your own pace. For downloading, verification, plugins and collection comparisons, continue with [Getting started](GETTING-STARTED.md).

If you get stuck, [tell us where you stopped](https://github.com/BlizzHacker/romarr/issues). Your ROMarr version, install method, system and the exact message help us reproduce it. You do not need to diagnose the problem or write code. Remove passwords and API keys from screenshots and logs.

This walkthrough grew out of people sharing their first installations and memories of older systems in [r/ARR](https://www.reddit.com/r/ARR/comments/1xv2ps9/). Thanks to everyone testing, contributing fixes and helping another player get started. A clear report or a friendly answer helps this project just as much as a feature request.
