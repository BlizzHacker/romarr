# ROMarr -> Playnite
#
# A Generic script extension: no compilation, installs by copying this folder
# into %APPDATA%\Playnite\Extensions\.
#
# Playnite's Library plugin interface -- the one that makes a source appear
# alongside Steam and GOG -- is C# only. A menu item imports the same games
# without requiring a build toolchain, which is the trade this makes
# deliberately: something you can install and read, over something you must
# compile.

function GetMainMenuItems {
    param($menuArgs)

    $sync = New-Object Playnite.SDK.Plugins.ScriptMainMenuItem
    $sync.Description = "Sync library"
    $sync.FunctionName = "Invoke-ROMarrSync"
    $sync.MenuSection = "@ROMarr"

    $configure = New-Object Playnite.SDK.Plugins.ScriptMainMenuItem
    $configure.Description = "Configure"
    $configure.FunctionName = "Invoke-ROMarrConfigure"
    $configure.MenuSection = "@ROMarr"

    return @($sync, $configure)
}

function Get-ROMarrConfigPath {
    return Join-Path $CurrentExtensionInstallPath "romarr.config.json"
}

function Get-ROMarrConfig {
    $path = Get-ROMarrConfigPath
    if (Test-Path $path) {
        return Get-Content $path -Raw | ConvertFrom-Json
    }
    return $null
}

function Invoke-ROMarrConfigure {
    param($scriptArgs)

    $url = $PlayniteApi.Dialogs.SelectString(
        "ROMarr URL (e.g. http://romarr:6868)", "ROMarr", "http://localhost:6868")
    if (-not $url.Result) { return }

    $key = $PlayniteApi.Dialogs.SelectString(
        "API key (Settings -> General in ROMarr)", "ROMarr", "")
    if (-not $key.Result) { return }

    @{ url = $url.SelectedString.TrimEnd('/'); apiKey = $key.SelectedString } |
        ConvertTo-Json | Set-Content -Path (Get-ROMarrConfigPath) -Encoding UTF8

    $PlayniteApi.Dialogs.ShowMessage("Saved. Extensions -> ROMarr -> Sync library.", "ROMarr")
}

function Invoke-ROMarrSync {
    param($scriptArgs)

    $config = Get-ROMarrConfig
    if ($null -eq $config) {
        $PlayniteApi.Dialogs.ShowErrorMessage(
            "Not configured yet. Extensions -> ROMarr -> Configure.", "ROMarr")
        return
    }

    $uri = "$($config.url)/api/v1/frontend/export?format=playnite"
    try {
        # The key goes in a header, never the query string: Playnite logs the
        # URLs it fetches, and a key in a log is a key on somebody's pastebin.
        $response = Invoke-RestMethod -Uri $uri -Headers @{ "X-Api-Key" = $config.apiKey } `
                                      -TimeoutSec 120 -ErrorAction Stop
    } catch {
        $PlayniteApi.Dialogs.ShowErrorMessage(
            "Could not reach ROMarr: $($_.Exception.Message)", "ROMarr")
        return
    }

    if ($null -eq $response.games) {
        $PlayniteApi.Dialogs.ShowErrorMessage("ROMarr returned no games.", "ROMarr")
        return
    }

    # Index what Playnite already has, so a re-sync updates rather than
    # duplicating. Without this every sync doubles the library, which is the
    # single most common complaint about importers like this one.
    $existing = @{}
    foreach ($game in $PlayniteApi.Database.Games) {
        if ($game.GameId -and $game.PluginId -eq [guid]::Empty) {
            $existing[$game.GameId] = $game
        }
    }

    $added = 0
    $skipped = 0
    foreach ($entry in $response.games) {
        if ([string]::IsNullOrWhiteSpace($entry.romPath)) { $skipped++; continue }
        $gameId = "romarr:$($entry.id)"
        if ($existing.ContainsKey($gameId)) { $skipped++; continue }

        $game = New-Object "Playnite.SDK.Models.Game"
        $game.Name = $entry.name
        $game.GameId = $gameId
        $game.IsInstalled = $true

        $action = New-Object "Playnite.SDK.Models.GameAction"
        $action.Type = [Playnite.SDK.Models.GameActionType]::File
        $action.Path = $entry.romPath
        $action.IsPlayAction = $true
        $game.GameActions = [System.Collections.ObjectModel.ObservableCollection[Playnite.SDK.Models.GameAction]]::new()
        $game.GameActions.Add($action)

        if ($entry.verified -eq "verified") {
            $game.Notes = "ROMarr: verified against DAT"
        }

        $PlayniteApi.Database.Games.Add($game)
        $added++
    }

    $PlayniteApi.Dialogs.ShowMessage(
        "ROMarr: added $added, already present or unplayable $skipped.", "ROMarr")
}
