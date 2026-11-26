# Windows desktop packaging

The native desktop is a self-contained .NET 10 WinForms application with its own
WebView2 interface. The release ZIP includes Python 3.13.17, ROM Hub pinned to
`2936ffa8861b7768d7888670587ba6e15c8c3929`, MinGit 2.56.0.3, and the fixed
Microsoft WebView2 156.0.4314.42 x64 runtime. Running the ZIP needs no separate
Python, Git, Docker, .NET, PowerShell, or browser installation.

The supported package target is x64 Windows 10 build 19041 or later. Extract the
whole ZIP before opening `ROMarr.exe`. Settings, logs, installed plugins, and the
WebView2 profile live in the data folder selected in the desktop application.
Runtime state is kept outside the program installation.

## Build

Use a Windows builder with .NET 10 SDK, Python 3.13, and the Windows SDK.
GitHub Actions installs the build tools and runs this command:

```powershell
./scripts/build-windows-desktop.ps1 -OutputDirectory "$env:RUNNER_TEMP/romarr-desktop"
```

The script downloads Python and MinGit from their official release hosts and
reads Microsoft's fixed WebView2 API. All three runtime archives have pinned
SHA256 hashes. The extracted WebView2 executable must also have a valid Microsoft
Authenticode signature. The package's `BUILD-INFO.json` records the source commit,
runtime versions, hashes, and installed Python package versions.

Artifacts are a ZIP, unsigned MSIX, and `SHA256SUMS.txt`. The ZIP is ready for
developer testing. The native CI smoke starts the actual `ROMarr.exe`, creates
its WinForms window and embedded interface, and records proof in the chosen
smoke folder. The workflow removes external Python and Git from the app's PATH
while running that test and uploads the proof and logs.

## Microsoft Store preparation

The default MSIX identity is for development. Supply the exact identity and
publisher assigned to the app in Partner Center:

```powershell
./scripts/build-windows-desktop.ps1 -Version 1.10.0 -StoreVersion 1.10.0.0 `
  -StoreIdentityName "YourPartnerCenterIdentity" `
  -StorePublisher "CN=YourPartnerCenterPublisher" `
  -StorePublisherDisplayName "Your publisher name"
```

The manifest requests full-trust desktop execution and Internet access. The
package contains its runtime dependencies and does not download them at first
launch. It is unsigned: sideload testing requires a trusted signing certificate,
and Store distribution requires the correct Partner Center identity, upload,
review, and Store signing. Building the MSIX does not establish certification or
publish the app.

The release workflow attaches the built packages to an already published GitHub
release. It does not submit to Partner Center.

## Connect to an existing server

Enter the server address in the native window and choose **Connect to server**. Use the server origin, for example https://romarr.example.com, without a page path, password or API key. Sign in on that server’s normal login page. **Disconnect** closes the remote view and leaves the server running. Files managed through this view stay on the server.

The desktop remembers the server address locally. Browser profiles are separated by server origin and from the standalone profile. Use HTTPS for remote connections; HTTP does not encrypt traffic. For remote mode, an HTTPS redirect to a different sign-in origin can stay in the embedded browser after a native trust prompt. The prompt shows only its scheme and authority, never OAuth query parameters or tokens. Confirm only your expected identity provider. Approval lasts for the selected server connection and is cleared on disconnect, server selection change, local startup or closing the app. The native status displays the actual sign-in origin while it is visible. Approved sign-in pages can submit their forms and redirect back to the selected ROMarr server in the same isolated browser profile.

Ordinary cross-origin links and popup windows continue to open externally. An unencrypted HTTP identity provider cannot be approved for embedded sign-in. Certificate errors are never bypassed. Redirect-based sign-in support does not guarantee that every identity provider permits embedded browsers, popup-based flows or unusual redirect methods; use a normal browser if your provider requires it. Declining the prompt does not grant access or complete authentication.

The native smoke checks origin-only prompt data and redirect policy with synthetic HTTPS addresses, alongside its real local and remote fixture views. It does not authenticate against a real identity provider or bypass TLS to simulate one.

For a movable standalone setup, extract the complete ZIP into a writable folder and select data and library folders on the same drive before starting. Keep those folders together when moving the setup. The default data location remains your Windows user profile.
