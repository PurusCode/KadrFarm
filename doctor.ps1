$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
$py = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path $py)) { throw 'Сначала выполните .\install.ps1' }
& $py -m kadr_farm.cli doctor
exit $LASTEXITCODE
