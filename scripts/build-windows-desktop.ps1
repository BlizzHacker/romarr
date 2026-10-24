#Requires -Version 7.2
<#
.SYNOPSIS
Build the self-contained ROMarr Windows desktop ZIP and unsigned MSIX.
.DESCRIPTION
Developer/CI build only. End users extract the ZIP and open ROMarr.exe.
Python, ROM Hub, MinGit, .NET and a fixed WebView2 runtime ship in the package.
#>
[CmdletBinding()]
param(
    [string]$OutputDirectory,
    [string]$Version = '1.8.0',
    [string]$PythonPath = 'python',
    [string]$StoreIdentityName = 'MoveWeight.ROMarr.Desktop',
    [string]$StorePublisher = 'CN=MoveWeight',
    [string]$StorePublisherDisplayName = 'MoveWeight',
    [string]$StoreVersion,
    [switch]$SkipMsix
)

$ErrorActionPreference = 'Stop'
if (-not $IsWindows) { throw 'Build the desktop package on Windows with .NET 10 SDK and Python 3.13.' }
$repo = Split-Path -Parent $PSScriptRoot
if (-not $OutputDirectory) { $OutputDirectory = Join-Path $repo 'artifacts\windows' }
$OutputDirectory = [IO.Path]::GetFullPath($OutputDirectory)
[IO.Directory]::CreateDirectory($OutputDirectory) | Out-Null
if ($Version -notmatch '^\d+\.\d+\.\d+(?:\.\d+)?$') { throw 'Version must contain three or four numeric components.' }
if (-not $StoreVersion) { $StoreVersion = if ($Version.Split('.').Count -eq 3) { "$Version.0" } else { $Version } }
if ($StoreVersion -notmatch '^\d+\.\d+\.\d+\.\d+$') { throw 'StoreVersion must have four numeric components.' }
if ($StoreIdentityName -notmatch '^[A-Za-z0-9.-]+$') { throw 'StoreIdentityName must be the package identity from Partner Center.' }

$bundleName = "ROMarr-$Version-win-x64"
$bundle = Join-Path $OutputDirectory $bundleName
if (Test-Path -LiteralPath $bundle) { throw "Package folder already exists: $bundle. Choose a fresh output directory." }
$work = Join-Path $OutputDirectory ('.work-' + [guid]::NewGuid().ToString('N'))
$downloads = Join-Path $work 'downloads'
[IO.Directory]::CreateDirectory($downloads) | Out-Null
[IO.Directory]::CreateDirectory($bundle) | Out-Null
$backend = Join-Path $bundle 'backend'
$site = Join-Path $backend 'Lib\site-packages'
$backendApp = Join-Path $backend 'app'
foreach ($directory in @($backend, $site, $backendApp)) {
    [IO.Directory]::CreateDirectory($directory) | Out-Null
}

$pythonVersion = '3.13.17'
$pythonUrl = "https://www.python.org/ftp/python/$pythonVersion/python-$pythonVersion-embed-amd64.zip"
$pythonSha = '9e35319bffedab8b36ad44d8f377cb4f1733bcec257a0b6d04d94f01486ddb38'
$hubCommit = '8e46348783546ee03b00e2c933155ba60d29619d'
$hubUrl = "https://github.com/BlizzHacker/rom-hub/archive/$hubCommit.tar.gz"
$gitVersion = '2.56.0.3'
$gitUrl = 'https://github.com/git-for-windows/git/releases/download/v2.56.0.windows.3/MinGit-2.56.0.3-64-bit.zip'
$gitSha = '2e5619966e588fde5f8122c5557cad50c0fbafcaed736689f40ec1dfb1d78ea7'
$webVersion = '156.0.4314.42'
$webUrl = 'https://msedge.sf.dl.delivery.mp.microsoft.com/filestreamingservice/files/074c0930-36ad-4756-9d04-dfe0e503bd94/Microsoft.WebView2.FixedVersionRuntime.156.0.4314.42.x64.cab'
$webSha = 'cb4612ce79a62a0be094301a72f73cf91bb250f5e83dc51886f66a083fc0694f'
$webApiUrl = 'https://developer.microsoft.com/microsoft-edge/api/webview2'

function Get-VerifiedDownload([string]$Url, [string]$Destination, [string]$Sha256) {
    Write-Host "Downloading $Url"
    Invoke-WebRequest -Uri $Url -OutFile $Destination -MaximumRetryCount 3 -RetryIntervalSec 2
    $actual = (Get-FileHash -LiteralPath $Destination -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actual -ne $Sha256) { throw "SHA256 did not match for $Url" }
}

& $PythonPath -c 'import sys; assert sys.version_info[:2] == (3,13), "Build with Python 3.13 to match the bundled interpreter"'
if ($LASTEXITCODE -ne 0) { throw 'Python 3.13 build environment is required.' }
$dotnetVersion = & dotnet --version
if ($LASTEXITCODE -ne 0 -or $dotnetVersion -notmatch '^10\.') { throw '.NET 10 SDK is required.' }

# Read the official fixed-runtime API, then retain the verified pinned runtime.
# Older fixed builds may age out of the API while their official URL remains.
$webApi = Invoke-RestMethod -Uri $webApiUrl
$webEntry = @($webApi | Where-Object version -eq $webVersion)
if ($webEntry.Count) {
    $advertised = @($webEntry[0].builds | Where-Object architecture -eq 'x64')[0].url
    if ($advertised -ne $webUrl) { throw 'The official WebView2 API URL changed. Review and update the pinned runtime deliberately.' }
} else {
    Write-Host "Using archived pinned WebView2 $webVersion from its verified Microsoft download URL."
}
[IO.File]::WriteAllText((Join-Path $work 'webview2-api.json'), ($webApi | ConvertTo-Json -Depth 8))

$pythonArchive = Join-Path $downloads 'python.zip'
$gitArchive = Join-Path $downloads 'mingit.zip'
$webArchive = Join-Path $downloads 'webview2.cab'
Get-VerifiedDownload $pythonUrl $pythonArchive $pythonSha
Get-VerifiedDownload $gitUrl $gitArchive $gitSha
Get-VerifiedDownload $webUrl $webArchive $webSha
Expand-Archive -LiteralPath $pythonArchive -DestinationPath $backend
Expand-Archive -LiteralPath $gitArchive -DestinationPath (Join-Path $bundle 'git')

$webExpanded = Join-Path $work 'webview2'
[IO.Directory]::CreateDirectory($webExpanded) | Out-Null
& "$env:SystemRoot\System32\expand.exe" '-F:*' $webArchive $webExpanded
if ($LASTEXITCODE -ne 0) { throw 'Could not extract the fixed WebView2 runtime.' }
$webExe = @(Get-ChildItem -LiteralPath $webExpanded -Filter 'msedgewebview2.exe' -File -Recurse)
if ($webExe.Count -ne 1) { throw 'The fixed WebView2 archive did not contain exactly one runtime.' }
$signature = Get-AuthenticodeSignature -LiteralPath $webExe[0].FullName
if ($signature.Status -ne 'Valid' -or $signature.SignerCertificate.Subject -notlike '*Microsoft Corporation*') {
    throw 'The fixed WebView2 executable does not have a valid Microsoft signature.'
}
Copy-Item -LiteralPath $webExe[0].Directory.FullName -Destination (Join-Path $bundle 'browser') -Recurse

# Build-only pip installs all Windows wheels into the embedded interpreter.
# The final package does not need pip, an external Python, or an external Git.
& $PythonPath -m pip install --disable-pip-version-check --no-cache-dir --target $site -r (Join-Path $repo 'requirements.txt') "rom-hub @ $hubUrl"
if ($LASTEXITCODE -ne 0) { throw 'Could not bundle the Python dependencies and pinned ROM Hub.' }
Copy-Item -LiteralPath (Join-Path $repo 'romarr') -Destination (Join-Path $backendApp 'romarr') -Recurse
# _pth is relative to python.exe and is also honored by ROM Hub child processes.
$pythonPaths = @('python313.zip', '.', 'Lib/site-packages', 'app', 'import site')
[IO.File]::WriteAllLines((Join-Path $backend 'python313._pth'), $pythonPaths)
$packagedPython = Join-Path $backend 'python.exe'
& $packagedPython -c 'import sys, requests, romarr.app, rom_hub; from rom_hub.cli import main; assert sys.version_info[:3] == (3,13,17); print("Bundled Python, ROMarr and ROM Hub imports passed")'
if ($LASTEXITCODE -ne 0) { throw 'The embedded Python package is incomplete.' }
& (Join-Path $bundle 'git\cmd\git.exe') --version
if ($LASTEXITCODE -ne 0) { throw 'Bundled MinGit did not run.' }

# A small vector-drawn monogram supplies every Windows icon size.
Add-Type -AssemblyName System.Drawing
$iconPath = Join-Path $work 'ROMarr.ico'
$iconImages = [Collections.Generic.List[byte[]]]::new()
$iconSizes = @(16,24,32,48,64,128,256)
foreach ($size in $iconSizes) {
    $bitmap = [Drawing.Bitmap]::new($size,$size)
    $graphics = [Drawing.Graphics]::FromImage($bitmap)
    $stream = [IO.MemoryStream]::new()
    $brush = [Drawing.SolidBrush]::new([Drawing.ColorTranslator]::FromHtml('#f2a33c'))
    $font = [Drawing.Font]::new('Segoe UI',[single]($size*0.68),[Drawing.FontStyle]::Bold,[Drawing.GraphicsUnit]::Pixel)
    $format = [Drawing.StringFormat]::new()
    try {
        $graphics.Clear([Drawing.ColorTranslator]::FromHtml('#12141a'))
        $graphics.TextRenderingHint = [Drawing.Text.TextRenderingHint]::AntiAliasGridFit
        $format.Alignment = [Drawing.StringAlignment]::Center
        $format.LineAlignment = [Drawing.StringAlignment]::Center
        $graphics.DrawString('R',$font,$brush,[Drawing.RectangleF]::new(0,0,$size,$size),$format)
        $bitmap.Save($stream,[Drawing.Imaging.ImageFormat]::Png)
        $iconImages.Add($stream.ToArray())
    } finally {
        $format.Dispose(); $font.Dispose(); $brush.Dispose()
        $stream.Dispose(); $graphics.Dispose(); $bitmap.Dispose()
    }
}
$iconStream = [IO.File]::Create($iconPath)
$writer = [IO.BinaryWriter]::new($iconStream)
try {
    $writer.Write([uint16]0); $writer.Write([uint16]1); $writer.Write([uint16]$iconSizes.Count)
    [uint32]$offset = 6 + 16 * $iconSizes.Count
    for ($n=0; $n -lt $iconSizes.Count; $n++) {
        $dimension = if ($iconSizes[$n] -eq 256) { 0 } else { $iconSizes[$n] }
        $writer.Write([byte]$dimension); $writer.Write([byte]$dimension)
        $writer.Write([byte]0); $writer.Write([byte]0)
        $writer.Write([uint16]1); $writer.Write([uint16]32)
        $writer.Write([uint32]$iconImages[$n].Length); $writer.Write($offset)
        $offset += $iconImages[$n].Length
    }
    foreach ($image in $iconImages) { $writer.Write($image) }
} finally { $writer.Dispose(); $iconStream.Dispose() }

& dotnet publish (Join-Path $repo 'desktop\windows\ROMarr.Desktop.csproj') --configuration Release --runtime win-x64 --self-contained true --output $bundle "-p:Version=$Version" '-p:DebugType=None' '-p:DebugSymbols=false' "-p:ApplicationIcon=$iconPath"
if ($LASTEXITCODE -ne 0) { throw 'The native desktop application did not publish.' }
if (-not (Test-Path -LiteralPath (Join-Path $bundle 'ROMarr.exe'))) { throw 'ROMarr.exe is missing.' }
Copy-Item -LiteralPath (Join-Path $repo 'LICENSE') -Destination (Join-Path $bundle 'LICENSE.txt')
Copy-Item -LiteralPath (Join-Path $repo 'README.md') -Destination (Join-Path $bundle 'README.md')
$instructions = @'
ROMarr for Windows

Extract this entire folder to a location you choose, then open ROMarr.exe.
Keep backend, browser and git next to the EXE. No Python, Docker, PowerShell
or separate browser installation is needed to run the desktop app.

Choose your data folder and ROM library folder on first launch. Your settings
and logs stay in your data folder, outside the program folder. ROMarr listens
only on this PC. Stop or close the app to stop its own backend.

The ZIP works without a signing certificate. The separate unsigned MSIX is a
development/Partner Center submission package; it is not a certified Store
release and Windows requires signing before it can be installed directly.

Third-party licenses ship with the bundled Python, MinGit, WebView2 and Python
packages. ROMarr is MIT licensed. Support: https://github.com/BlizzHacker/romarr
'@
[IO.File]::WriteAllText((Join-Path $bundle 'START-HERE.txt'), $instructions)
$installedPackages = (& $PythonPath -m pip list --path $site --format=json) -join [Environment]::NewLine
if ($LASTEXITCODE -ne 0) { throw 'Could not record bundled Python packages.' }
$sourceRevision = (& git -C $repo rev-parse HEAD) -join ''
$buildInfo = [ordered]@{
    applicationVersion = $Version; sourceRevision = $sourceRevision; architecture = 'x64'
    minimumWindows = '10.0.19041.0'; dotnetSdk = $dotnetVersion
    python = @{ version = $pythonVersion; url = $pythonUrl; sha256 = $pythonSha }
    romHub = @{ commit = $hubCommit; source = $hubUrl }
    minGit = @{ version = $gitVersion; url = $gitUrl; sha256 = $gitSha }
    webView2 = @{ version = $webVersion; url = $webUrl; sha256 = $webSha; api = $webApiUrl }
    pythonPackages = ($installedPackages | ConvertFrom-Json)
}
[IO.File]::WriteAllText((Join-Path $bundle 'BUILD-INFO.json'), ($buildInfo | ConvertTo-Json -Depth 8))

if (-not $SkipMsix) {
    Add-Type -AssemblyName System.Drawing
    $assets = Join-Path $bundle 'Assets'
    [IO.Directory]::CreateDirectory($assets) | Out-Null
    foreach ($asset in @(@('Square44x44Logo.png',44),@('Square150x150Logo.png',150),@('StoreLogo.png',50))) {
        $size = [int]$asset[1]
        $bitmap = [Drawing.Bitmap]::new($size,$size)
        $graphics = [Drawing.Graphics]::FromImage($bitmap)
        try {
            $graphics.Clear([Drawing.ColorTranslator]::FromHtml('#12141a'))
            $brush = [Drawing.SolidBrush]::new([Drawing.ColorTranslator]::FromHtml('#f2a33c'))
            $font = [Drawing.Font]::new('Segoe UI',[single]($size*0.46),[Drawing.FontStyle]::Bold,[Drawing.GraphicsUnit]::Pixel)
            $format = [Drawing.StringFormat]::new()
            try {
                $format.Alignment = [Drawing.StringAlignment]::Center
                $format.LineAlignment = [Drawing.StringAlignment]::Center
                $graphics.TextRenderingHint = [Drawing.Text.TextRenderingHint]::AntiAliasGridFit
                $graphics.DrawString('R',$font,$brush,[Drawing.RectangleF]::new(0,0,$size,$size),$format)
                $bitmap.Save((Join-Path $assets $asset[0]),[Drawing.Imaging.ImageFormat]::Png)
            } finally { $brush.Dispose(); $font.Dispose(); $format.Dispose() }
        } finally { $graphics.Dispose(); $bitmap.Dispose() }
    }
    $template = Get-Content -LiteralPath (Join-Path $repo 'desktop\windows\Package.appxmanifest') -Raw
    foreach ($entry in @{
        '@IDENTITY_NAME@' = $StoreIdentityName
        '@PUBLISHER@' = $StorePublisher
        '@PUBLISHER_DISPLAY_NAME@' = $StorePublisherDisplayName
        '@VERSION@' = $StoreVersion
    }.GetEnumerator()) {
        $template = $template.Replace($entry.Key,[Security.SecurityElement]::Escape([string]$entry.Value))
    }
    [IO.File]::WriteAllText((Join-Path $bundle 'AppxManifest.xml'),$template)
    $kits = Join-Path ([Environment]::GetFolderPath('ProgramFilesX86')) 'Windows Kits\10\bin'
    $makeAppx = @(Get-ChildItem -Path (Join-Path $kits '*\x64\makeappx.exe') -File |
        Sort-Object { [version]$_.Directory.Parent.Name } -Descending)[0].FullName
    if (-not $makeAppx) { throw 'Windows SDK MakeAppx.exe is required for MSIX. Install the SDK or use -SkipMsix for ZIP-only development.' }
    & $makeAppx pack /o /d $bundle /p (Join-Path $OutputDirectory "$bundleName-unsigned.msix")
    if ($LASTEXITCODE -ne 0) { throw 'MSIX packaging failed.' }
}
$zip = Join-Path $OutputDirectory "$bundleName.zip"
Compress-Archive -LiteralPath $bundle -DestinationPath $zip -CompressionLevel Optimal
$packages = @(Get-ChildItem -LiteralPath $OutputDirectory -File | Where-Object Extension -in '.zip','.msix')
$checksums = $packages | ForEach-Object {
    (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash.ToLowerInvariant() + '  ' + $_.Name
}
[IO.File]::WriteAllLines((Join-Path $OutputDirectory 'SHA256SUMS.txt'),$checksums)
Write-Host "Built native desktop package: $zip"
Write-Host 'The MSIX is unsigned and requires the real Partner Center identity and Store signing for public Store distribution.'
