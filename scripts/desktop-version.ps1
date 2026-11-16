#Requires -Version 5.1
# Pure version validation shared by desktop packaging and release workflows.
function Get-ROMarrDesktopPackageVersion {
    <# .SYNOPSIS Validate package identity while retaining its exact prerelease suffix. #>
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$Version)

    $part = '(?:0|[1-9][0-9]*)'
    $pattern = "\A(?<core>$part\.$part\.$part(?:\.$part)?)(?:-(?<stage>alpha|beta|rc)\.(?<preview>$part))?\z"
    if ($Version -cnotmatch $pattern) {
        throw 'Version must be numeric or use -alpha.N, -beta.N or -rc.N without leading zeros.'
    }
    $core = $Matches['core']
    $prerelease = -not [string]::IsNullOrEmpty($Matches['stage'])
    if ($prerelease -and $core.Split('.').Count -ne 3) {
        throw 'Prerelease versions must have exactly three numeric components.'
    }
    [pscustomobject]@{
        Version = $Version
        NumericVersion = $core
        StoreVersion = $(if ($core.Split('.').Count -eq 3) { "$core.0" } else { $core })
        IsPrerelease = $prerelease
    }
}

function Resolve-ROMarrDesktopReleaseVersion {
    <# .SYNOPSIS Match the release numeric core and retain the complete published identity. #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$ProjectVersion,
        [string]$EventName,
        [string]$ReleaseTag
    )

    $project = Get-ROMarrDesktopPackageVersion -Version $ProjectVersion
    if ($EventName -ne 'release') { return $project.Version }
    if ([string]::IsNullOrEmpty($ReleaseTag) -or -not $ReleaseTag.StartsWith('v', [StringComparison]::Ordinal)) {
        throw 'Desktop release tags must start with v and contain a supported version.'
    }
    $release = Get-ROMarrDesktopPackageVersion -Version $ReleaseTag.Substring(1)
    if ($release.NumericVersion -cne $project.NumericVersion) {
        throw 'Release tag numeric core must match the desktop project version.'
    }
    return $release.Version
}
