$ErrorActionPreference = 'Stop'
$python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
$installer = Join-Path $PSScriptRoot 'install_stage4.py'
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw 'Python executable not found: .venv\Scripts\python.exe. Run from the video-farm project root.'
}
if (-not (Test-Path -LiteralPath $installer -PathType Leaf)) {
    throw 'install_stage4.py not found. Extract the complete archive into the project root.'
}
& $python $installer
if ($LASTEXITCODE -ne 0) {
    throw "Stage 4 installation failed (exit code $LASTEXITCODE)."
}
