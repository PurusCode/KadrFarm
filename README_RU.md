# KadrFarm — пакет обновления Visual Story V2 Professional Renderer

Этот пакет добавляет отдельный браузерный композитор HTML/CSS/SVG и новый CLI. Старый `visual_renderer_v2.py`, старые команды и основной production pipeline не заменяются.

## Состав пакета

```text
src/kadr_farm/
  visual_professional_assets_v2.py
  visual_professional_renderer_v2.py
  visual_professional_pipeline_v2.py
  visual_professional_cli.py

tests/
  test_visual_professional_v2.py

examples/
  visual_story_v2_professional_demo.json

docs/
  VISUAL_STORY_V2_PROFESSIONAL_RENDERER_RU.md

pyproject.visual.patch
```

## Установка в Windows

1. Распакуй содержимое архива в корень `D:\my_projects\KadrFarm`, сохрани структуру папок.
2. Из корня проекта примени патч зависимостей и CLI-команды:

```powershell
git apply .\pyproject.visual.patch
```

3. Установи Playwright и браузер Chromium:

```powershell
python -m pip install -e ".[visual]"
python -m playwright install chromium
```

Если проект уже установлен в editable-режиме, команда выше обновит установку с новым extra.

## Проверка

```powershell
python -m py_compile .\src\kadr_farm\visual_professional_assets_v2.py .\src\kadr_farm\visual_professional_renderer_v2.py .\src\kadr_farm\visual_professional_pipeline_v2.py .\src\kadr_farm\visual_professional_cli.py

python -m pytest .\tests\test_visual_story_v2.py .\tests\test_visual_professional_v2.py -q
```

## Создание демонстрационного плана и видео

Демо использует уже установленный Kokoro и Pixabay.

### 1. Синтез речи

```powershell
python -m kadr_farm.visual_v2_cli tts `
  .\examples\visual_story_v2_professional_demo.json `
  --out-dir .\output\visual_v2_professional\audio_units `
  --out-plan .\output\visual_v2_professional\plan_measured.json `
  --speed 0.9
```

### 2. Проверка нового исполнителя

```powershell
python -m kadr_farm.visual_professional_cli validate `
  .\output\visual_v2_professional\plan_measured.json
```

### 3. Рендеринг MP4

```powershell
python -m kadr_farm.visual_professional_cli render `
  .\output\visual_v2_professional\plan_measured.json `
  --out .\output\visual_v2_professional\demo.mp4 `
  --clean-work
```

В демо предусмотрены три разных визуальных решения: фотография с типографической композицией, анимированная SVG-схема и фоновое видео с итоговым титром и счётчиком.

## Что должно появиться

- `demo.mp4` — H.264-видео с AAC-дорожкой речи;
- `demo.contact.jpg` — контактный лист кадров;
- `demo.audit.json` — список объектов, событий, источников медиа и результат ffprobe;
- `demo_professional_work/` — HTML-композиции, промежуточные browser recordings, сегменты и master WAV.

## Важно

- Для демо нужен `PIXABAY_API_KEY` в существующем `.env`.
- Для локальных материалов в пользовательских JSON-планах указывай абсолютные пути либо пути относительно расположения `plan_measured.json`.
- Фактическая длительность речевых WAV измеряется; временная шкала shots округляется по общим границам кадров, чтобы не накапливать дрейф.
- Браузер загружает медиа через контролируемые Playwright-маршруты. Видео не разворачивается в отдельные PNG-кадры.
- При отсутствии или невозможности декодировать медиа рендер останавливается с ошибкой, а не подставляет случайную заглушку.
- На первом этапе стыки shots остаются монтажными склейками. Смысловые переходы, 3D-сцены, частицы и forced alignment по словам ещё не реализованы.
- Это отдельный исполнительный слой, а не уже готовая полностью автономная студия. Global Director пока не генерирует этот новый контракт автоматически.
- Старый production pipeline остаётся нетронутым. Не переключай его на новый renderer до отдельной проверки.
