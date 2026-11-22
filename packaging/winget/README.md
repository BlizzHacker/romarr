# Windows Package Manager (winget)

`manifests/m/MoveWeight/ROMarr/1.12.0/` is a multi-file manifest (schema 1.28.0)
for [microsoft/winget-pkgs](https://github.com/microsoft/winget-pkgs), at the
exact path that repository uses. It installs the release's **portable ZIP**
(`ROMarr-1.12.0-win-x64.zip`, SHA-256 from the release's `SHA256SUMS.txt`) and
puts a `romarr` command on the PATH that opens the desktop app:

```powershell
winget install MoveWeight.ROMarr
```

The ZIP carries `ROMarr.exe`, its bundled Python backend and a fixed WebView2
runtime, so the manifest declares no dependencies.

## Validation (2026-11-15)

This record is for the 1.10.0 manifest. The manifest moved to 1.12.0 on 2026-11-22
(new ZIP and SHA-256 from that release) and has not been revalidated locally;
the winget-pkgs bot validates and installs it on the PR.

```powershell
winget validate --manifest packaging\winget\manifests\m\MoveWeight\ROMarr\1.10.0
# Manifest validation succeeded.
```

Not run locally: `winget install --manifest ...`, which would put a 433 MB
download and about 1.2 GB of extracted files on the system drive. winget-pkgs'
own pipeline installs every submission in a clean VM, which is the real
install test. The package launches through winget's symlink alias, and
`ROMarr.exe` finds `backend\` and `browser\` relative to `AppContext.BaseDirectory`;
check that the pipeline's run starts the app.

## Submit

Either with Microsoft's tool (it forks, branches and opens the PR for you):

```powershell
wingetcreate submit --prtitle "New package: MoveWeight.ROMarr version 1.12.0" packaging\winget\manifests\m\MoveWeight\ROMarr\1.12.0
```

or by hand: fork `microsoft/winget-pkgs`, copy the `1.12.0` folder to
`manifests/m/MoveWeight/ROMarr/1.12.0/`, and open a PR. A bot validates, installs
and scans it; a moderator merges.

Updates: `wingetcreate update MoveWeight.ROMarr --version <v> --urls <zip-url> --submit`.

Once the Microsoft Store listing (9PN1VX549NSW) is live, `winget install ROMarr`
will also offer it from the `msstore` source; the two packages coexist.
