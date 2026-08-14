[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [string]$DestinationRoot = (Join-Path ([Environment]::GetFolderPath('UserProfile')) '.agents\skills'),
    [switch]$ConfirmRemoval,
    [switch]$RemoveFromPath
)

$ErrorActionPreference = 'Stop'
$skillNames = @('scholar-slides', 'paper-tutor')

if (-not $ConfirmRemoval) {
    throw 'Uninstallation requires the explicit -ConfirmRemoval switch.'
}

if (-not (Test-Path -LiteralPath $DestinationRoot -PathType Container)) {
    Write-Output "Destination root does not exist; nothing to remove: $DestinationRoot"
    return
}

$destinationFull = (Resolve-Path -LiteralPath $DestinationRoot).Path.TrimEnd('\', '/')
$expectedPrefix = $destinationFull + [IO.Path]::DirectorySeparatorChar

foreach ($skillName in $skillNames) {
    $target = [IO.Path]::GetFullPath((Join-Path $destinationFull $skillName))
    if (-not $target.StartsWith($expectedPrefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Resolved target escapes destination root: $target"
    }
    if ((Split-Path -Parent $target).TrimEnd('\', '/') -ne $destinationFull) {
        throw "Resolved target is not a direct child of destination root: $target"
    }
    if ((Test-Path -LiteralPath $target) -and $PSCmdlet.ShouldProcess($target, 'Remove installed skill')) {
        Remove-Item -LiteralPath $target -Recurse -Force
        Write-Output "Removed $target"
    }
}

if ($RemoveFromPath) {
    $binPath = [IO.Path]::GetFullPath((Join-Path $destinationFull 'scholar-slides\bin')).TrimEnd('\', '/')
    $userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
    $entries = @($userPath -split ';' | Where-Object { $_ })
    $updatedEntries = @(
        $entries | Where-Object { $_.TrimEnd('\', '/') -ne $binPath }
    )
    if ($updatedEntries.Count -ne $entries.Count) {
        [Environment]::SetEnvironmentVariable('Path', ($updatedEntries -join ';'), 'User')
        Write-Output "Removed Scholar-Slides launcher directory from the user PATH: $binPath"
    }
}

Write-Output 'Uninstallation finished. Backups under .skill-backups were preserved.'
