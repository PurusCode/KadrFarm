AI VIDEO FACTORY RU — AUTONOMOUS DIRECTOR OVERLAY 1.1.0

Исправления 1.1.0:
- visual_description больше не выводится на экран; это только внутренняя инструкция renderer;
- добавлены специализированные графические шаблоны: интерфейс-симуляция, схема, типографика,
  сравнение, цитата, визуальная метафора и визуальная шутка;
- on_screen_text фильтруется от описаний сцены и инструкций художнику;
- добавлены visual_asset_audit.json и visual_audit.json: проверка исходных ассетов и кадров,
  извлечённых непосредственно из final.mp4; contact sheet теперь показывает готовое видео;
- 1.0.1: сохранено исправление расхождения на один сэмпл при сведении ambient audio.

Основа: commit 4454e27.

Установка: распаковать архив в корень video-farm, затем в PowerShell:
  powershell -ExecutionPolicy Bypass -File .\install_director_overlay.ps1
Скрипт проверит файлы, выполнит editable-install без обновления зависимостей, запустит полный tests/ и проверит CLI.

Пилот 10 сцен:
  .\.venv\Scripts\python.exe -X utf8 -m kadr_farm.production_director `
    --storyboard ".\output\semantic_runs\semantic-e2e-2026-11-07\storyboard_draft.json" `
    --out ".\output\semantic_runs\autonomous-pilot-10-v1_1" `
    --scene-limit 10 --batch-size 2 --speed 0.9 --revision-rounds 2

Команда выполняет AI-режиссуру, критику, исправления, аудит источников,
сценовой Kokoro TTS по точным фразам, Pixabay/локальный renderer, sound design,
FFmpeg assembly и postproduction QC. Теперь дополнительно создаются visual_asset_audit.json,
visual_audit.json и metadata sidecar-файлы для локальной графики. `visual_contact_sheet.jpg` строится из кадров
самого закодированного MP4, а не из отдельных исходных PNG. Подробности — docs/27_AUTONOMOUS_PRODUCTION_DIRECTOR_RU.md.

Overlay добавляет новые модули и обновляет только автономные модули overlay; он не меняет
старый production pipeline, `.env`, модели, data/ и старые файлы output/. Все факты остаются
на human review; доступность URL не считается проверкой. Автоматический visual audit не
подтверждает смысловое соответствие кадра речи: контактный лист и MP4 всё ещё нужно смотреть.
