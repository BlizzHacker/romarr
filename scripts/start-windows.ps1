#Requires -Version 5.1
<#
.SYNOPSIS
Run ROMarr in the foreground on Windows with Python 3.11 or newer.
.DESCRIPTION
Run from a downloaded or cloned ROMarr checkout. Dependencies are installed in
an isolated environment on first launch, when requirements change, or when
-UpdateDependencies is requested. No administrator privileges are required.
#>
[CmdletBinding()]
param(
    [string]$DataDirectory,
    [string]$LibraryPath,
    [string]$VenvDirectory,
    [string]$PythonPath,
    [ValidateRange(1024, 65535)][int]$Port = 6868,
    [switch]$UpdateDependencies,
    [switch]$CheckOnly,
    [switch]$OpenBrowser
)

$ErrorActionPreference = 'Stop'
if ($env:OS -ne 'Windows_NT') {
    throw 'This launcher is for Windows. On Linux use python -m romarr or Docker.'
}
$repo = Split-Path -Parent $PSScriptRoot
$requirements = Join-Path $repo 'requirements.txt'
if (-not (Test-Path -LiteralPath (Join-Path $repo 'romarr\__main__.py'))) {
    throw 'Keep this script inside the scripts folder of a complete ROMarr checkout.'
}

function Get-FullDirectory([string]$Path) {
    $directory = [System.IO.Path]::GetFullPath($Path)
    [System.IO.Directory]::CreateDirectory($directory) | Out-Null
    $probe = Join-Path $directory ('.romarr-write-' + [guid]::NewGuid().ToString('N'))
    try { [System.IO.File]::WriteAllText($probe, '') }
    finally { if (Test-Path -LiteralPath $probe) { Remove-Item -LiteralPath $probe } }
    return $directory
}

if (-not $DataDirectory) {
    if (-not $env:LOCALAPPDATA) { throw 'Specify -DataDirectory for ROMarr configuration.' }
    $DataDirectory = Join-Path $env:LOCALAPPDATA 'ROMarr'
}
$DataDirectory = Get-FullDirectory $DataDirectory
$statePath = Join-Path $DataDirectory 'romarr.json'
# Saved library settings remain authoritative on subsequent launches.
$savedLibrary = $null
if (Test-Path -LiteralPath $statePath) {
    try { $savedLibrary = (Get-Content -LiteralPath $statePath -Raw | ConvertFrom-Json).settings.library_path }
    catch { throw "Cannot read $statePath. Preserve the file and repair it before launching: $_" }
}
if ($LibraryPath -and $savedLibrary -and
        [System.IO.Path]::GetFullPath($LibraryPath) -ne [System.IO.Path]::GetFullPath($savedLibrary)) {
    throw 'This install already has a saved library path. Change it in Settings > Media Management, or use its current path.'
}
if (-not $LibraryPath) {
    $LibraryPath = if ($savedLibrary) { $savedLibrary } else { Join-Path $DataDirectory 'roms' }
}
$LibraryPath = Get-FullDirectory $LibraryPath
if (-not $VenvDirectory) { $VenvDirectory = Join-Path $repo '.venv-windows' }
$VenvDirectory = [System.IO.Path]::GetFullPath($VenvDirectory)
$venvPython = Join-Path $VenvDirectory 'Scripts\python.exe'

if (-not (Test-Path -LiteralPath $venvPython)) {
    $pythonArgs = @()
    if ($PythonPath) {
        $pythonCommand = (Get-Command $PythonPath -ErrorAction Stop).Source
    } elseif (Get-Command py -ErrorAction SilentlyContinue) {
        $pythonCommand = (Get-Command py).Source
        $pythonArgs = @('-3')
    } elseif (Get-Command python -ErrorAction SilentlyContinue) {
        $pythonCommand = (Get-Command python).Source
    } else {
        throw 'Install Python 3.11 or newer from python.org, then rerun this command. You can select it with -PythonPath.'
    }
    & $pythonCommand @pythonArgs -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)'
    if ($LASTEXITCODE -ne 0) { throw 'ROMarr needs Python 3.11 or newer. Select it with -PythonPath.' }
    Write-Host "Creating Python environment: $VenvDirectory"
    & $pythonCommand @pythonArgs -m venv $VenvDirectory
    if ($LASTEXITCODE -ne 0) { throw 'Could not create the Python environment.' }
}
& $venvPython -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)'
if ($LASTEXITCODE -ne 0) { throw 'This environment needs Python 3.11 or newer. Select another -VenvDirectory.' }

$requirementsHash = (Get-FileHash -LiteralPath $requirements -Algorithm SHA256).Hash
$marker = Join-Path $VenvDirectory '.romarr-requirements.sha256'
$installedHash = if (Test-Path -LiteralPath $marker) { (Get-Content -LiteralPath $marker -Raw).Trim() } else { '' }
if ($UpdateDependencies -or $installedHash -ne $requirementsHash) {
    & $venvPython -m pip install --disable-pip-version-check -r $requirements
    if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed. Check the pip message above and rerun.' }
    [System.IO.File]::WriteAllText($marker, $requirementsHash)
}

$previous = @{}
$settings = @{
    ROMARR_DATA = $statePath
    LIBRARY_PATH = $LibraryPath
    ROMARR_PORT = [string]$Port
    ROMARR_HOST = '127.0.0.1'
    PYTHONUTF8 = '1'
}
if (-not $env:LIBRARY_KIND) { $settings['LIBRARY_KIND'] = 'folder' }
$browserJob = $null
Push-Location -LiteralPath $repo
try {
    foreach ($name in $settings.Keys) {
        $previous[$name] = [Environment]::GetEnvironmentVariable($name, 'Process')
        [Environment]::SetEnvironmentVariable($name, $settings[$name], 'Process')
    }
    & $venvPython -c 'import requests; import romarr.app'
    if ($LASTEXITCODE -ne 0) { throw 'ROMarr could not import. Rerun with -UpdateDependencies after checking the error above.' }
    $url = "http://127.0.0.1:$Port"
    Write-Host "Configuration: $statePath"
    Write-Host "Library: $LibraryPath"
    if ($CheckOnly) { Write-Host 'Python and ROMarr imports are ready.'; return }
    Write-Host "Starting ROMarr at $url. Keep this window open; Ctrl+C stops it."
    if ($OpenBrowser) {
        $browserJob = Start-Job -ArgumentList $url -ScriptBlock {
            param($address)
            for ($attempt = 0; $attempt -lt 60; $attempt++) {
                try {
                    $response = Invoke-WebRequest -UseBasicParsing -Uri ($address + '/api/health') -TimeoutSec 1
                    if ($response.StatusCode -eq 200) { Start-Process $address; return }
                } catch { }
                Start-Sleep -Milliseconds 500
            }
        }
    }
    & $venvPython -m romarr
    if ($LASTEXITCODE -ne 0) { throw "ROMarr exited with code $LASTEXITCODE. Check the message above." }
} finally {
    if ($browserJob) { Stop-Job -Job $browserJob -ErrorAction SilentlyContinue; Remove-Job -Job $browserJob -Force -ErrorAction SilentlyContinue }
    foreach ($name in $previous.Keys) {
        [Environment]::SetEnvironmentVariable($name, $previous[$name], 'Process')
    }
    Pop-Location
}
