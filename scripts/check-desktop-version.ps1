#Requires -Version 5.1
# Exercise the same version resolver used by real packaging, without downloads.
$ErrorActionPreference = 'Stop'
if (-not (Get-Command Get-ROMarrDesktopPackageVersion -ErrorAction SilentlyContinue)) {
    . (Join-Path $PSScriptRoot 'desktop-version.ps1')
}
$script:desktopVersionChecks = 0
function Assert-DesktopVersionEqual($Expected, $Actual) {
    if ($Expected -cne $Actual) { throw "Version check failed: expected '$Expected', got '$Actual'." }
    $script:desktopVersionChecks++
}
function Assert-DesktopVersionRejects([scriptblock]$Action) {
    $rejected = $false
    try { & $Action | Out-Null } catch { $rejected = $true }
    if (-not $rejected) { throw 'An invalid desktop version or release tag was accepted.' }
    $script:desktopVersionChecks++
}
foreach ($value in @('1.10.1', '1.10.1-alpha.0', '1.10.1-alpha.3', '1.10.1-beta.2', '1.10.1-rc.1')) {
    $parsed = Get-ROMarrDesktopPackageVersion -Version $value
    Assert-DesktopVersionEqual $value $parsed.Version
    Assert-DesktopVersionEqual '1.10.1' $parsed.NumericVersion
    Assert-DesktopVersionEqual '1.10.1.0' $parsed.StoreVersion
    Assert-DesktopVersionEqual $value (Resolve-ROMarrDesktopReleaseVersion -ProjectVersion '1.10.1' -EventName release -ReleaseTag "v$value")
}
$legacy = Get-ROMarrDesktopPackageVersion -Version '1.10.1.7'
Assert-DesktopVersionEqual '1.10.1.7' $legacy.Version
Assert-DesktopVersionEqual '1.10.1.7' $legacy.NumericVersion
Assert-DesktopVersionEqual '1.10.1.7' $legacy.StoreVersion
Assert-DesktopVersionEqual '1.10.1.7' (Resolve-ROMarrDesktopReleaseVersion -ProjectVersion '1.10.1.7' -EventName release -ReleaseTag 'v1.10.1.7')
Assert-DesktopVersionEqual '1.10.1' (Resolve-ROMarrDesktopReleaseVersion -ProjectVersion '1.10.1' -EventName pull_request -ReleaseTag 'untrusted ignored input')
foreach ($value in @('1.10.1.0-alpha.1', '1.10.1.7-beta.2', '1.10.1.7-rc.1', '1.10', '1.10.1-preview', '1.10.1-rc', '1.10.1-RC.1', '1.10.1-rc.01', '01.10.1', 'v1.10.1', '1.10.1+build', "1.10.1`n", '1.10.1-rc.1;Write-Host unsafe')) {
    Assert-DesktopVersionRejects { Get-ROMarrDesktopPackageVersion -Version $value }
}
foreach ($tag in @('', 'v1.10.2-rc.1', 'v2.0.0-alpha.1', 'v1.10.1.1', 'v1.10.1.0-rc.1', '1.10.1-rc.1', 'vv1.10.1', 'vnightly', "v1.10.1`n")) {
    Assert-DesktopVersionRejects { Resolve-ROMarrDesktopReleaseVersion -ProjectVersion '1.10.1' -EventName release -ReleaseTag $tag }
}
# Validate every assembly/MSIX core position, including legacy stable revisions.
foreach ($position in 0..3) {
    $parts = @('1', '10', '1', '7')
    $parts[$position] = '65535'
    $maximum = $parts -join '.'
    Assert-DesktopVersionEqual $maximum (Get-ROMarrDesktopPackageVersion -Version $maximum).NumericVersion
    $parts[$position] = '65536'
    $overflow = $parts -join '.'
    Assert-DesktopVersionRejects { Get-ROMarrDesktopPackageVersion -Version $overflow }
    $parts[$position] = '9' * 1000
    $huge = $parts -join '.'
    Assert-DesktopVersionRejects { Get-ROMarrDesktopPackageVersion -Version $huge }
}
foreach ($position in 0..2) {
    $parts = @('1', '10', '1')
    $parts[$position] = '65535'
    $maximum = ($parts -join '.') + '-rc.1'
    Assert-DesktopVersionEqual $maximum (Get-ROMarrDesktopPackageVersion -Version $maximum).Version
    $parts[$position] = '65536'
    $overflow = ($parts -join '.') + '-beta.1'
    Assert-DesktopVersionRejects { Get-ROMarrDesktopPackageVersion -Version $overflow }
    $parts[$position] = '9' * 1000
    $huge = ($parts -join '.') + '-alpha.1'
    Assert-DesktopVersionRejects { Get-ROMarrDesktopPackageVersion -Version $huge }
}
Assert-DesktopVersionEqual '65535.65535.65535.65535' (Get-ROMarrDesktopPackageVersion -Version '65535.65535.65535.65535').StoreVersion
Write-Host "$script:desktopVersionChecks desktop package and release-version checks passed"
