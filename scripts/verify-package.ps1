[CmdletBinding()]
param(
    [string]$RepositoryRoot = (Split-Path -Parent $PSScriptRoot)
)

$ErrorActionPreference = 'Stop'
$resolvedRoot = (Resolve-Path -LiteralPath $RepositoryRoot).Path
$pythonCommand = Get-Command python -ErrorAction Stop

& $pythonCommand.Source (Join-Path $resolvedRoot 'scripts\verify_package.py') $resolvedRoot
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}

$bytecodeRoot = Join-Path ([IO.Path]::GetTempPath()) ("scholar-slides-compile-" + [Guid]::NewGuid().ToString('N'))
$previousBytecodeRoot = $env:PYTHONPYCACHEPREFIX
try {
    New-Item -ItemType Directory -Path $bytecodeRoot -Force | Out-Null
    $env:PYTHONPYCACHEPREFIX = $bytecodeRoot
    & $pythonCommand.Source -m compileall -q -f (Join-Path $resolvedRoot 'skills\scholar-slides\runtime\scripts')
    if ($LASTEXITCODE -ne 0) {
        throw "Python compileall failed with exit code $LASTEXITCODE"
    }
}
finally {
    $env:PYTHONPYCACHEPREFIX = $previousBytecodeRoot
    if (Test-Path -LiteralPath $bytecodeRoot) {
        Remove-Item -LiteralPath $bytecodeRoot -Recurse -Force
    }
}

$parseFailures = [System.Collections.Generic.List[string]]::new()
Get-ChildItem -LiteralPath $resolvedRoot -Filter '*.ps1' -File -Recurse | ForEach-Object {
    $tokens = $null
    $parseErrors = $null
    [void][System.Management.Automation.Language.Parser]::ParseFile(
        $_.FullName,
        [ref]$tokens,
        [ref]$parseErrors
    )
    foreach ($parseError in $parseErrors) {
        $parseFailures.Add("$($_.FullName): $($parseError.Message)")
    }
}

if ($parseFailures.Count -gt 0) {
    Write-Error ("PowerShell parsing failed:`n- " + ($parseFailures -join "`n- "))
}

Write-Output 'Package, Python, and PowerShell validation passed.'
