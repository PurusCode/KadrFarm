$ErrorActionPreference = 'Stop'

$Root = (Resolve-Path -LiteralPath $PSScriptRoot).Path
$PythonVenv = Join-Path $Root '.venv\Scripts\python.exe'
$Installer = Join-Path $Root 'stage3\install_stage3.py'

if (-not (Test-Path -LiteralPath $Installer -PathType Leaf)) {
    throw 'Stage 3 installer payload is missing. Extract the complete ZIP into the project root.'
}

if (Test-Path -LiteralPath $PythonVenv -PathType Leaf) {
    $PythonExe = $PythonVenv
} else {
    $PythonCommand = Get-Command python -ErrorAction SilentlyContinue
    if ($null -eq $PythonCommand) { throw 'Python was not found. Expected .venv\Scripts\python.exe or python in PATH.' }
    $PythonExe = $PythonCommand.Source
}

& $PythonExe $Installer --root $Root
if ($LASTEXITCODE -ne 0) {
    throw "Stage 3 installation failed with exit code $LASTEXITCODE. See the Python installer output above."
}
