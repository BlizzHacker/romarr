# Prove contrib/playnite/ROMarr against the REAL Playnite SDK and a LIVE ROMarr.
#
# Loads Playnite.SDK.dll from NuGet (the same assembly Playnite ships), points
# the extension's config at a live ROMarr, and runs Invoke-ROMarrSync with only
# the UI stubbed: dialogs are captured instead of shown, and the game database
# is an in-memory list. Everything else -- the module code, the SDK types, the
# HTTP fetch, the export payload -- is the real thing.
#
#   powershell -File scripts/playnite_proof.ps1 `
#       -SdkDll <path to Playnite.SDK.dll> -RomarrUrl http://host:7878 -ApiKey <key>

param(
    [Parameter(Mandatory)] [string]$SdkDll,
    [Parameter(Mandatory)] [string]$RomarrUrl,
    [Parameter(Mandatory)] [string]$ApiKey
)

$ErrorActionPreference = "Stop"
Add-Type -Path $SdkDll

# The extension reads its config from $CurrentExtensionInstallPath.
$CurrentExtensionInstallPath = Join-Path $env:TEMP "romarr-playnite-proof"
New-Item -ItemType Directory -Force $CurrentExtensionInstallPath | Out-Null
@{ url = $RomarrUrl.TrimEnd('/'); apiKey = $ApiKey } | ConvertTo-Json |
    Set-Content -Path (Join-Path $CurrentExtensionInstallPath "romarr.config.json") -Encoding UTF8

# Only the UI is stubbed: dialogs captured, database an in-memory list.
$script:messages = New-Object System.Collections.ArrayList
$dialogs = [pscustomobject]@{}
$dialogs | Add-Member ScriptMethod ShowMessage { param($m, $t) [void]$script:messages.Add("MSG: $m") }
$dialogs | Add-Member ScriptMethod ShowErrorMessage { param($m, $t) [void]$script:messages.Add("ERR: $m") }
$database = [pscustomobject]@{ Games = New-Object System.Collections.ArrayList }
$PlayniteApi = [pscustomobject]@{ Dialogs = $dialogs; Database = $database }

# Evaluate the module body in THIS scope so its functions resolve our
# $PlayniteApi and $CurrentExtensionInstallPath, exactly as Playnite's
# script host provides them.
$modulePath = Join-Path $PSScriptRoot "..\contrib\playnite\ROMarr\ROMarr.psm1"
Invoke-Expression (Get-Content -Raw $modulePath)

$fail = 0
function Check([string]$step, [bool]$ok, [string]$detail = "") {
    $mark = if ($ok) { "PASS" } else { $script:fail++; "FAIL" }
    Write-Output ("  [{0}] {1}{2}" -f $mark, $step, $(if ($detail) { " -- $detail" } else { "" }))
}

Write-Output ("Playnite extension proof -- {0}" -f (Get-Date -Format s))
Write-Output ("SDK: {0}" -f [Playnite.SDK.Models.Game].Assembly.GetName().Version)

$menu = GetMainMenuItems $null
Check "GetMainMenuItems returns the two real ScriptMainMenuItem entries" `
    ($menu.Count -eq 2 -and $menu[0].GetType().FullName -eq "Playnite.SDK.Plugins.ScriptMainMenuItem")

Invoke-ROMarrSync $null
$added = $database.Games.Count
Check "Invoke-ROMarrSync imported from the LIVE export" ($added -gt 0) `
    ("{0} games via {1}" -f $added, $RomarrUrl)
Check "every imported game is a real Playnite.SDK.Models.Game with a play action" `
    (($database.Games | Where-Object {
        $_.GetType().FullName -eq "Playnite.SDK.Models.Game" -and
        $_.GameActions.Count -eq 1 -and $_.GameActions[0].IsPlayAction
    }).Count -eq $added)

Invoke-ROMarrSync $null
Check "a second sync adds nothing (the dedupe the README promises)" `
    ($database.Games.Count -eq $added) ("still {0} games" -f $database.Games.Count)

Write-Output ""
Write-Output ($script:messages -join "`n")
if ($fail -gt 0) { exit 1 }
Write-Output "All Playnite proofs passed."
