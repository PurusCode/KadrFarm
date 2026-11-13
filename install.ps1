param(
  [switch]$SkipTests
)
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

Write-Host '=== video-farm setup (Kokoro-ru v2 / CPU) ===' -ForegroundColor Cyan
$py = $null
foreach ($candidate in @('3.13', '3.12')) {
  try {
    & py -$candidate --version | Out-Null
    if ($LASTEXITCODE -eq 0) { $py = $candidate; break }
  } catch {}
}
if (-not $py) {
  throw 'Python 3.12 или 3.13 x64 не найден. Установите Python и включите Python Launcher.'
}
Write-Host "Используется Python $py"
if (-not (Test-Path '.venv\Scripts\python.exe')) {
  & py -$py -m venv .venv
}
$venvPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
& $venvPython -m pip install --upgrade pip setuptools wheel
& $venvPython -m pip install -e '.[tts,test]'
if ($LASTEXITCODE -ne 0) { throw 'Не удалось установить зависимости Kokoro. См. сообщение pip выше.' }
if (-not (Test-Path '.env')) {
  Copy-Item '.env.example' '.env'
  Write-Host 'Создан .env из .env.example — заполните ключи.' -ForegroundColor Yellow
}
if (-not $SkipTests) {
  & $venvPython -m pytest -q
  if ($LASTEXITCODE -ne 0) { throw 'Автоматические тесты завершились с ошибкой.' }
}
Write-Host "Готово. Проверьте: .\.venv\Scripts\python.exe -m kadr_farm.cli doctor" -ForegroundColor Green
