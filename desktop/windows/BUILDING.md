# Windows desktop packaging

The x64 desktop release has two direct-use formats: a movable portable ZIP and a current-user setup EXE. Both include a self-contained .NET 10 WinForms shell, ROMarr's Python backend, the pinned ROM Hub package, MinGit, and Microsoft's fixed WebView2 runtime. A separate unsigned MSIX remains available for Store preparation.

The ZIP contains `ROMarr.portable`. Its settings, logs, installed plugins, and browser profile live in a `data` folder alongside the app, and internal paths are stored relative to the package so the whole folder can move together. The setup EXE omits that marker, installs under the user's profile without administrator access, creates Start menu and optional desktop shortcuts, and keeps configuration in `%LOCALAPPDATA%\ROMarr` outside the install folder. Both launch ROMarr's local backend with the desktop app; closing the app stops that backend. Neither package installs qBittorrent or another service daemon. The built-in Setup Guide connects ROMarr to the user's existing qBittorrent, SABnzbd, NZBGet, indexer, and library services.

The supported target is x64 Windows 10 build 19041 or later. The portable app should be extracted to a writable folder; do not run it from inside the ZIP.

## Build

Use a Windows builder with .NET 10 SDK, Python 3.13, the Windows SDK, and Inno Setup 6. GitHub Actions uses a Windows runner with Inno Setup and installs the .NET and Python SDKs before running:

```powershell
./scripts/build-windows-desktop.ps1 -OutputDirectory "$env:RUNNER_TEMP/romarr-desktop"
```

The script downloads Python and MinGit from their official release hosts and reads Microsoft's fixed WebView2 API. All three runtime archives have pinned SHA256 hashes. The extracted WebView2 executable must have a valid Microsoft Authenticode signature. The package's `BUILD-INFO.json` records the source commit, runtime versions, hashes, and installed Python package versions.

Artifacts are a portable ZIP, a current-user setup EXE, an unsigned MSIX, and `SHA256SUMS.txt`. The Windows CI smoke starts the actual `ROMarr.exe`, creates its WinForms window and embedded interface, configures a connection-free local instance, and records proof in the chosen smoke folder. The workflow removes external Python and Git from the app's PATH while running that proof and uploads the evidence and logs.

## Microsoft Store preparation

The default MSIX identity is for development. Supply the exact identity and publisher assigned to the app in Partner Center:

```powershell
./scripts/build-windows-desktop.ps1 -Version 1.8.1 -StoreVersion 1.8.1.0 `
  -StoreIdentityName "YourPartnerCenterIdentity" `
  -StorePublisher "CN=YourPartnerCenterPublisher" `
  -StorePublisherDisplayName "Your publisher name"
```

The manifest requests full-trust desktop execution and Internet access. The package contains its runtime dependencies and does not download them at first launch. It is unsigned: sideload testing requires a trusted signing certificate, and Store distribution requires the correct Partner Center identity, upload, review, and Store signing. The release workflow attaches packages to a published, matching GitHub release; it does not submit to Partner Center or sign the installer.
