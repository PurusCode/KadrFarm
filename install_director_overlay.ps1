$ErrorActionPreference = "Stop"
$Root = (Get-Location).Path
$Required = @(
  "src\kadr_farm\director_models.py",
  "src\kadr_farm\director_visuals.py",
  "src\kadr_farm\director_sources.py",
  "src\kadr_farm\director_audio.py",
  "src\kadr_farm\director_assembler.py",
  "src\kadr_farm\production_director.py",
  "tests\test_autonomous_director_overlay.py",
  "docs\27_AUTONOMOUS_PRODUCTION_DIRECTOR_RU.md"
)
foreach ($Relative in $Required) {
  if (-not (Test-Path -LiteralPath (Join-Path $Root $Relative))) {
    throw "Missing file: $Relative. Extract the archive into the video-farm project root first."
  }
}
$Python = ".\.venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $Python)) { throw "Missing virtual environment: .venv" }
Write-Host "Checking project dependencies and refreshing editable install..." -ForegroundColor Cyan
& $Python -m pip install -e . --no-deps
if ($LASTEXITCODE -ne 0) { throw "pip install -e . failed" }
Write-Host "Running the complete test suite..." -ForegroundColor Cyan
& $Python -m pytest tests -q
if ($LASTEXITCODE -ne 0) { throw "Tests failed. Do not start production." }
Write-Host "Checking the new CLI..." -ForegroundColor Cyan
& $Python -X utf8 -m kadr_farm.production_director --help
if ($LASTEXITCODE -ne 0) { throw "The production_director CLI failed to start" }
Write-Host "Installation and checks completed. Existing pipeline and .env were not modified." -ForegroundColor Green
