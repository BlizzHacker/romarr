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

    $publish = New-Object Playnite.SDK.Plugins.ScriptMainMenuItem
    $publish.Description = "Report this PC's games to ROMarr"
    $publish.FunctionName = "Publish-ROMarrInventory"
    $publish.MenuSection = "@ROMarr"

    return @($sync, $configure, $publish)
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
    if ($config.pathFrom -and $config.pathTo) {
        $uri += "&path_from=$([uri]::EscapeDataString($config.pathFrom))" +
                "&path_to=$([uri]::EscapeDataString($config.pathTo))"
    }
    try {
        # The key goes in a header, never the query string: Playnite logs the
        # URLs it fetches, and a key in a log is a key on somebody's pastebin.
        $response = Invoke-RestMethod -Uri $uri -Headers @{ "X-Api-Key" = $config.apiKey } `
                                      -TimeoutSec 120 -MaximumRedirection 0 -ErrorAction Stop
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

    $idCounts = @{}
    foreach ($entry in $response.games) {
        $idCounts[[string]$entry.id]++
    }
    $added = 0
    $updated = 0
    $skipped = 0
    foreach ($entry in $response.games) {
        if ([string]::IsNullOrWhiteSpace($entry.romPath)) { $skipped++; continue }
        $legacyId = "romarr:$($entry.id)"
        $gameId = $legacyId
        if ($entry.libraryKey) {
            $encodedLibrary = [uri]::EscapeDataString([string]$entry.libraryKey)
            $gameId = "romarr:library:$encodedLibrary/$($entry.id)"
        }
        $old = $existing[$gameId]
        $migrated = $false
        if (-not $old -and $gameId -ne $legacyId -and $existing.ContainsKey($legacyId)) {
            $legacy = $existing[$legacyId]
            $firstAction = $legacy.GameActions | Select-Object -First 1
            if (($firstAction -and $firstAction.Path -ceq $entry.romPath) -or
                ($idCounts[[string]$entry.id] -eq 1 -and $legacy.Name -ceq $entry.name)) {
                $existing.Remove($legacyId)
                $legacy.GameId = $gameId
                $existing[$gameId] = $legacy
                $old = $legacy
                $migrated = $true
            }
        }
        if ($old) {
            $action = if ($old.GameActions.Count -eq 1) { $old.GameActions[0] }
            $pathChanged = $action -and $action.IsPlayAction -and
                $action.Type -eq [Playnite.SDK.Models.GameActionType]::File -and
                -not $action.Arguments -and $action.Path -cne $entry.romPath
            if ($pathChanged) { $action.Path = $entry.romPath }
            if ($migrated -or $pathChanged) {
                $PlayniteApi.Database.Games.Update($old)
                $updated++
            } else {
                $skipped++
            }
            continue
        }

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
        $existing[$gameId] = $game
        $added++
    }

    $PlayniteApi.Dialogs.ShowMessage(
        "ROMarr: added $added, updated $updated, skipped $skipped.", "ROMarr")
}


function Publish-ROMarrInventory {
    param($scriptArgs)

    $config = Get-ROMarrConfig
    if (-not $config) {
        $PlayniteApi.Dialogs.ShowErrorMessage("Configure ROMarr first.", "ROMarr")
        return
    }
    $storefronts = @{
        "Steam" = "steam"; "Epic" = "epic"; "Epic Games" = "epic"; "GOG" = "gog"
        "Battle.net" = "battlenet"; "Amazon Games" = "amazon"; "EA" = "ea"
        "EA app" = "ea"; "Ubisoft Connect" = "ubisoft"; "itch.io" = "itchio"
        "Humble" = "humble"; "Humble Bundle" = "humble"; "Xbox" = "xbox"
    }
    $providers = @{}
    foreach ($addon in $PlayniteApi.Addons.Plugins) {
        if ($addon -is [Playnite.SDK.Plugins.LibraryPlugin] -and
            $storefronts.ContainsKey($addon.Name)) {
            $providers[[string]$addon.Id] = $storefronts[$addon.Name]
        }
    }
    $games = @(foreach ($game in $PlayniteApi.Database.Games) {
        if (-not $game.Name -or $game.GameId -like 'romarr:*') { continue }
        $store = $providers[[string]$game.PluginId]
        if (-not $store) { $store = "playnite" }
        @{
            name = $game.Name
            provider = $store
            store_id = if ($store -eq "steam") { [string]$game.GameId } else { "" }
            installed = [bool]$game.IsInstalled
        }
    })
    $computer = $env:COMPUTERNAME
    $deviceId = ($computer.ToLowerInvariant() -replace '[^a-z0-9_.-]', '-') + ".playnite"
    $report = @{ device_id = $deviceId; name = $computer; os = "windows"; games = $games }
    try {
        $uri = "$($config.url)/api/v1/lists/device"
        $payload = [Text.Encoding]::UTF8.GetBytes(($report | ConvertTo-Json -Depth 5 -Compress))
        $result = Invoke-RestMethod -Method Post -Uri $uri -Body $payload -ContentType "application/json" `
            -Headers @{"X-Api-Key" = $config.apiKey} -TimeoutSec 60 -MaximumRedirection 0 -ErrorAction Stop
        $PlayniteApi.Dialogs.ShowMessage("Reported $($result.count) games. Open Wanted > Lists in ROMarr.", "ROMarr")
    } catch {
        $PlayniteApi.Dialogs.ShowErrorMessage("Could not publish the library to ROMarr. Check the configured URL and API key.", "ROMarr")
    }
}
