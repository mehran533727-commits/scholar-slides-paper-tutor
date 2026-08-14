$ErrorActionPreference = 'Stop'
$skillRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $skillRoot 'runtime\.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) { throw "scholar-slides Python runtime is missing: $python" }
$env:SCHOLAR_SLIDES_ROOT = $skillRoot
$env:SCHOLAR_SLIDES_PYTHON = $python
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONDONTWRITEBYTECODE = '1'
& $python (Join-Path $skillRoot 'runtime\scripts\scholar_slides.py') @args
exit $LASTEXITCODE
