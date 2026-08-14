[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [string]$DestinationRoot = (Join-Path ([Environment]::GetFolderPath('UserProfile')) '.agents\skills'),
    [switch]$Force,
    [switch]$SkipDependencies,
    [switch]$SkipBrowser,
    [switch]$AddToPath
)

$ErrorActionPreference = 'Stop'
$skillNames = @('scholar-slides', 'paper-tutor')
$repositoryRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$sourceSkillsRoot = Join-Path $repositoryRoot 'skills'

function Get-CheckedChildPath {
    param(
        [Parameter(Mandatory = $true)][string]$Parent,
        [Parameter(Mandatory = $true)][string]$ChildName
    )

    $parentFull = [IO.Path]::GetFullPath($Parent).TrimEnd('\', '/')
    $candidate = [IO.Path]::GetFullPath((Join-Path $parentFull $ChildName))
    $expectedPrefix = $parentFull + [IO.Path]::DirectorySeparatorChar
    if (-not $candidate.StartsWith($expectedPrefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Resolved child path escapes destination root: $candidate"
    }
    return $candidate
}

function Invoke-CheckedCommand {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [Parameter(Mandatory = $true)][string[]]$Arguments,
        [string]$WorkingDirectory
    )

    $previousLocation = $null
    try {
        if ($WorkingDirectory) {
            $previousLocation = Get-Location
            Set-Location -LiteralPath $WorkingDirectory
        }
        & $FilePath @Arguments
        if ($LASTEXITCODE -ne 0) {
            throw "$FilePath failed with exit code $LASTEXITCODE"
        }
    }
    finally {
        if ($null -ne $previousLocation) {
            Set-Location -LiteralPath $previousLocation.Path
        }
    }
}

foreach ($skillName in $skillNames) {
    $source = Join-Path $sourceSkillsRoot $skillName
    if (-not (Test-Path -LiteralPath (Join-Path $source 'SKILL.md') -PathType Leaf)) {
        throw "Skill source is incomplete: $source"
    }
}

$destinationFull = [IO.Path]::GetFullPath($DestinationRoot).TrimEnd('\', '/')
if (-not (Test-Path -LiteralPath $destinationFull)) {
    New-Item -ItemType Directory -Path $destinationFull -Force | Out-Null
}
$destinationFull = (Resolve-Path -LiteralPath $destinationFull).Path.TrimEnd('\', '/')

foreach ($skillName in $skillNames) {
    $target = Get-CheckedChildPath -Parent $destinationFull -ChildName $skillName
    if ((Test-Path -LiteralPath $target) -and -not $Force) {
        throw "Target already exists: $target. Re-run with -Force to back it up and replace it."
    }
}

if (-not $PSCmdlet.ShouldProcess($destinationFull, 'Install scholar-slides and paper-tutor')) {
    return
}

$stageName = '.skill-install-staging-' + [Guid]::NewGuid().ToString('N')
$stageRoot = Get-CheckedChildPath -Parent $destinationFull -ChildName $stageName
$backupStamp = (Get-Date -Format 'yyyyMMdd-HHmmss-fff') + '-' + [Guid]::NewGuid().ToString('N').Substring(0, 8)
$backupRoot = Get-CheckedChildPath -Parent $destinationFull -ChildName (Join-Path '.skill-backups' $backupStamp)

try {
    New-Item -ItemType Directory -Path $stageRoot -Force | Out-Null
    foreach ($skillName in $skillNames) {
        $source = Join-Path $sourceSkillsRoot $skillName
        $staged = Get-CheckedChildPath -Parent $stageRoot -ChildName $skillName
        Copy-Item -LiteralPath $source -Destination $staged -Recurse -Force
    }

    $stagedScholar = Get-CheckedChildPath -Parent $stageRoot -ChildName 'scholar-slides'
    if (-not $SkipDependencies) {
        $pythonCommand = Get-Command python -ErrorAction Stop
        $nodeCommand = Get-Command node -ErrorAction Stop
        $npmCommand = Get-Command npm.cmd -ErrorAction SilentlyContinue
        if ($null -eq $npmCommand) {
            $npmCommand = Get-Command npm -ErrorAction Stop
        }
        $npxCommand = Get-Command npx.cmd -ErrorAction SilentlyContinue
        if ($null -eq $npxCommand) {
            $npxCommand = Get-Command npx -ErrorAction Stop
        }

        $pythonVersionText = & $pythonCommand.Source -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")'
        if ($LASTEXITCODE -ne 0) {
            throw 'Unable to determine Python version.'
        }
        $pythonVersion = [Version]($pythonVersionText | Select-Object -Last 1)
        if ($pythonVersion -lt [Version]'3.11') {
            throw "Python 3.11 or newer is required; found $pythonVersion."
        }

        $nodeVersionText = (& $nodeCommand.Source --version | Select-Object -Last 1).TrimStart('v')
        if ($LASTEXITCODE -ne 0) {
            throw 'Unable to determine Node.js version.'
        }
        if ([Version]$nodeVersionText -lt [Version]'18.0') {
            throw "Node.js 18 or newer is required; found $nodeVersionText."
        }

        $runtime = Join-Path $stagedScholar 'runtime'
        $venv = Join-Path $runtime '.venv'
        Invoke-CheckedCommand -FilePath $pythonCommand.Source -Arguments @('-m', 'venv', $venv)
        $venvPython = Join-Path $venv 'Scripts\python.exe'
        Invoke-CheckedCommand -FilePath $venvPython -Arguments @(
            '-m', 'pip', 'install', '--disable-pip-version-check',
            '-r', (Join-Path $runtime 'requirements-runtime.txt')
        )
        Invoke-CheckedCommand -FilePath $npmCommand.Source -Arguments @('ci') -WorkingDirectory $runtime
        if (-not $SkipBrowser) {
            Invoke-CheckedCommand -FilePath $npxCommand.Source -Arguments @('playwright', 'install', 'chromium') -WorkingDirectory $runtime
        }

        $launcher = Join-Path $stagedScholar 'bin\scholar-slides.ps1'
        $versionOutput = & $launcher --version
        if ($LASTEXITCODE -ne 0) {
            throw "Scholar-Slides version check failed with exit code $LASTEXITCODE"
        }
        if (($versionOutput | Out-String).Trim() -ne '0.3.0') {
            throw "Scholar-Slides version check returned an unexpected value: $versionOutput"
        }
        $doctorJson = & $launcher doctor --json
        if ($LASTEXITCODE -ne 0) {
            throw "Scholar-Slides doctor failed with exit code $LASTEXITCODE"
        }
        $doctor = ($doctorJson -join "`n") | ConvertFrom-Json
        if ($doctor.ok -ne $true) {
            throw 'Scholar-Slides doctor returned ok=false.'
        }
    }

    foreach ($skillName in $skillNames) {
        $target = Get-CheckedChildPath -Parent $destinationFull -ChildName $skillName
        if (Test-Path -LiteralPath $target) {
            New-Item -ItemType Directory -Path $backupRoot -Force | Out-Null
            $backupTarget = Get-CheckedChildPath -Parent $backupRoot -ChildName $skillName
            Move-Item -LiteralPath $target -Destination $backupTarget
        }
        $staged = Get-CheckedChildPath -Parent $stageRoot -ChildName $skillName
        Move-Item -LiteralPath $staged -Destination $target
    }

    if ($AddToPath) {
        $binPath = Get-CheckedChildPath -Parent (Get-CheckedChildPath -Parent $destinationFull -ChildName 'scholar-slides') -ChildName 'bin'
        $userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
        $entries = @($userPath -split ';' | Where-Object { $_ })
        if (-not ($entries | Where-Object { $_.TrimEnd('\', '/') -eq $binPath.TrimEnd('\', '/') })) {
            $updatedPath = (@($entries) + $binPath) -join ';'
            [Environment]::SetEnvironmentVariable('Path', $updatedPath, 'User')
            Write-Output "Added Scholar-Slides launcher directory to the user PATH: $binPath"
        }
    }

    Write-Output "Installed scholar-slides and paper-tutor to $destinationFull"
    if ($Force -and (Test-Path -LiteralPath $backupRoot)) {
        Write-Output "Previous installations were backed up to $backupRoot"
    }
    Write-Output 'Restart Codex so it can discover the installed skills.'
}
finally {
    if (Test-Path -LiteralPath $stageRoot) {
        Remove-Item -LiteralPath $stageRoot -Recurse -Force
    }
}
