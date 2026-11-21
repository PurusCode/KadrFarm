# KadrFarm Visual Story V2 — Professional HTML/SVG Renderer

## Назначение

Добавочный исполнитель сложных многослойных композиций. Старые рендереры и основной production pipeline не заменяет.

Экспериментальный рендерер на Pillow создавал полноэкранный прозрачный PNG для каждого объекта и накладывал эти PNG друг на друга. Для богатых композиций это неудачная основа. Новый исполнитель использует один HTML-документ на shot:

- HTML/CSS — геометрия, типографика, карточки, таблицы и компоновка;
- SVG — фигуры, стрелки, графики, узлы и соединения;
- нативный `<img>` и `<video>` — фото и видеослои;
- Playwright/Chromium — запись сцены как видеосегмента;
- FFmpeg — нормализация, монтаж, аудио и MP4.

## Новые файлы

- `visual_professional_assets_v2.py` — разрешение локальных, HTTP(S) и Pixabay-материалов. Видеофайлы сохраняются как видео.
- `visual_professional_renderer_v2.py` — композитор HTML/CSS/SVG и браузерная запись.
- `visual_professional_pipeline_v2.py` — синхронизация с WAV, монтаж shots, аудио, контактный лист и аудит.
- `visual_professional_cli.py` — отдельный CLI `validate` / `render`.
- `test_visual_professional_v2.py` — проверки HTML-композиции, экранирования, видеослоёв, событий и графики.
- `visual_story_v2_professional_demo.json` — демонстрация с фотографией, узловой схемой, видеослоем, титрами и счётчиком.

## Установка

```powershell
python -m pip install -e ".[visual]"
python -m playwright install chromium
```

В `pyproject.toml` добавляются extra `visual` и entry point через патч `pyproject.visual.patch`.

## Запуск

Синтезировать отдельные речевые WAV существующей командой Kokoro:

```powershell
python -m kadr_farm.visual_v2_cli tts `
  .\examples\visual_story_v2_professional_demo.json `
  --out-dir .\output\visual_v2_professional\audio_units `
  --out-plan .\output\visual_v2_professional\plan_measured.json `
  --speed 0.9
```

Проверить план:

```powershell
python -m kadr_farm.visual_professional_cli validate `
  .\output\visual_v2_professional\plan_measured.json
```

Собрать видео:

```powershell
python -m kadr_farm.visual_professional_cli render `
  .\output\visual_v2_professional\plan_measured.json `
  --out .\output\visual_v2_professional\demo.mp4 `
  --clean-work
```

Для демонстрации нужен существующий `PIXABAY_API_KEY`.

## Временная шкала и запись

1. Длительности речевых WAV измеряются из файлов.
2. Компилятор переводит их в общую временную шкалу и рассчитывает глобальные границы кадров.
3. Внутри каждого shot Playwright сначала загружает шрифты, изображения и метаданные/первые кадры видео.
4. После готовности страницы одним сигналом запускаются CSS-анимации, timed JS-события и воспроизведение видео.
5. Технический преролл браузерной записи отрезается FFmpeg. Это не даёт событиям начинаться во время загрузки материалов.
6. Длительность каждого сегмента привязывается к общим границам кадров, а не округляется независимо от остальных сцен.
7. Речь объединяется без перекодирования PCM WAV и добавляется к H.264-видео.

Браузерные запросы на локальные материалы выполняются через Playwright route handler и ограниченный список разрешённых файлов. Это избавляет от проблем `file://` в Chromium и поддерживает byte-range-запросы для `<video>`. Медиа читаются в память текущего shot при первом запросе; размер скачиваемых по URL/Pixabay файлов ограничен resolver-ом.

## Поддерживаемые объекты

- `image` / `video`: native media layer, cover/contain/fill, focal point, медленное приближение и панорамирование;
- `text`: eyebrow, headline, subtitle, font size/weight, letter spacing, alignment и line breaks;
- `shape`: rectangle, rounded rectangle, circle/ellipse, line, arrow, polygon;
- `icon`: check, cross, play, warning;
- `callout`, `counter`, `formula`;
- `table`;
- `chart`: bar, line, area, pie, donut; оси, сетка, категории, серии, легенда и пометка illustrative data;
- `node_graph`: подписанные узлы, рёбра и стрелки;
- `group`: вложенные объекты с локальной геометрией.

## Поддерживаемые события

Текущий контракт V2:

- `appear`;
- `fade_in` / `fade_out`;
- `highlight`;
- `move`;
- `scale`;
- `draw`;
- `reveal_row`;
- `count_up`;
- `update_text`.

Easing: `linear`, `ease`, `ease-in`, `ease-out`, `ease-in-out`, `snappy`, `cinematic`, `spring`.

События получают время от compiler timeline и фактической длительности WAV. `offset_sec` остаётся позицией внутри речевой единицы, заданной планом; это не forced alignment по словам. Для синхронизации по отдельным словам потребуется отдельный aligner или более мелкие TTS-единицы.

## Ограничения текущей итерации

Это новая основа компоновки и исполнения, но не аналог After Effects и не гарантия студийного качества на любом автоматически сгенерированном плане.

- Chromium рендерит композиции в реальном времени или медленнее; это не GPU-ускоренный 3D-движок.
- Сложная 3D-сцена, частицы, физическая симуляция, ручное ротоскопирование и генеративный video-to-video пока не входят в исполнитель.
- Переходы между shots пока являются монтажными склейками. Набор смысловых переходов следует добавить после стабилизации композитора.
- Качество стокового результата зависит от поискового запроса и фактически выбранного материала.
- Для коммерческой публикации проверьте права, лицензии и требования атрибуции каждого медиаматериала.
- Global Director ещё не подключён к генерации нового плана автоматически. На первом этапе план должен существовать как JSON Visual Story V2.
- Старый `visual_v2_cli render` остаётся прежним. Новый путь запускается через `visual_professional_cli`.

## Критерии приёмки

1. На контактном листе видны все смысловые состояния, а не только наличие объектов в JSON.
2. Текст находится внутри своих областей, не обрезается и имеет чёткую иерархию.
3. Фото и видео занимают предусмотренные режиссёром области и сохраняют корректный aspect ratio.
4. Графики, связи и ключевые анимации действительно исполняются в браузере.
5. Одновременные события на одном объекте не перезаписывают друг друга.
6. Длительность MP4 согласована с master WAV в пределах кадра/кодировочного допуска.
7. Отчёт фиксирует каждый использованный медиаматериал и его источник.
8. Старый production pipeline остаётся работоспособным.
