param(
  [Parameter(Mandatory=$true, Position=0)] [string]$Topic,
  [string]$Name = 'video',
  [switch]$Draft,
  [double]$Speed = 0.9
)
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
$py = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path $py)) { throw 'Сначала выполните .\install.ps1' }
$cliArgs = @('-m', 'kadr_farm.cli', 'run', '--topic', $Topic, '--name', $Name, '--speed', "$Speed")
if ($Draft) { $cliArgs += '--draft' }
& $py @cliArgs
exit $LASTEXITCODE
