# KadrFarm — CP-02: Renderer Capability Registry V2

- **Контрольная точка:** CP-02
- **Версия реестра:** `1.0.0`
- **Статус:** source-level review и локальная приёмка пройдены; commit/push CP-02 ожидаются
- **Исходный Git checkpoint:** `5988307b3c4abde43774f979e440bc3ede70adbe`
- **Рабочая ветка:** `feature/autonomous-video-engine-2`
- **Runtime-контракт, описываемый реестром:** `src/kadr_farm/visual_story_v2.py`
- **Модуль:** `src/kadr_farm/visual_capability_registry_v2.py`
- **Тесты:** `tests/test_visual_capability_registry_v2.py`

> Важно: реестр описывает фактические возможности существующих исполнителей runtime V2. Он не означает, что новый authoring contract из `visual_story_contract_v2/` можно напрямую передать в renderer. Между authoring contract и `visual_story_v2.py` по-прежнему нужен отдельный адаптер CP-06.

## 1. Для чего нужен реестр

Реестр — детерминированный ответ на вопрос: какой существующий исполнитель действительно может выполнить конкретный объект и действие, с какими ограничениями и форматом результата.

Неизвестный объект, subtype, action, media motion, недопустимая адресация или несовместимая комбинация блокируются диагностикой. Система не должна незаметно заменять заказанное действие другим визуалом.

В реестр включены только исполнители, напрямую работающие с runtime `VisualStoryPlan`:

1. `professional_browser_v2` — основной профессиональный HTML/CSS/SVG + Chromium + FFmpeg renderer.
2. `pillow_animation_v2` — `SceneRenderer` + `AnimationEngine`, формирующие последовательность PNG-кадров.

Исторические `diagram_renderer.py` и `html_renderer.py` инвентаризированы отдельно: их вход — legacy scene/beat dictionaries, а не runtime `VisualStoryPlan`. Они не регистрируются как прямые runtime V2 исполнители.

## 2. Фактическая поддержка объектов

Оба runtime исполнителя содержат ветви для 12 kinds:

`image`, `video`, `text`, `shape`, `icon`, `callout`, `group`, `node_graph`, `chart`, `table`, `formula`, `counter`.

Это подтверждает поддержку именно типов runtime-объектов. Дополнительная проверка subtype обязательна:

| Поле | Поддерживаемые значения | Ограничение |
|---|---|---|
| `shape.shape_type` | `rectangle`, `rounded_rectangle`, `circle`, `ellipse`, `line`, `arrow`, `polygon` | Другие типы из нового authoring contract, включая `double_arrow`, `arc`, `bracket`, `path`, пока не имеют соответствующего runtime renderer. |
| `icon.icon_id` | `check`, `cross`, `warning`, `play` | Неизвестный ID вызывает ошибку renderer; подмена иконки запрещена. |
| `chart.chart_type` | Professional: `bar`, `line`, `pie`, `donut`, `area`; Pillow: `bar`, `line`, `pie`, `donut` | Pillow не исполняет area fill и фактически рисует только линию; такой subtype блокируется. `pie/donut` у обоих renderer используют ровно первый series, поэтому дополнительные ряды блокируются. Pillow bar также рисует только первый series; multi-series bar блокируется. |
| `node_graph.layout` | `left_to_right`, `top_to_bottom` | Остальные layout значения runtime renderer не исполняет как отдельные режимы и способен молча свести к горизонтальной раскладке; поэтому validator их блокирует. |

Реестр не придумывает лимит количества объектов или рядов: соответствующая явная верхняя граница в этих renderer не обнаружена. `max_objects_per_shot` поэтому остаётся `None`.

### Group и вложенность

- Professional Asset Resolver рекурсивно обходит группы, но блокирует глубину больше 8.
- Professional Renderer также имеет защиту глубины вложенной композиции.
- Pillow SceneRenderer рекурсивно рисует содержимое групп, но его asset resolver подготавливает media только для top-level объектов и непосредственных детей группы. Вложенное media глубже одного group-child уровня блокируется реестром.
- AnimationEngine строит состояния только для top-level объектов. SceneRenderer проверяет `visible` до рекурсии в group, поэтому `appear` для group поддерживается. Однако SceneRenderer возвращается из ветви group до применения opacity, highlight, move, scale и draw; эти действия для group блокируются.
- Реестр вводит общий безопасный предел вложенности 8 уровней. Для Pillow это защитное правило capability validator, а не собственная проверка в SceneRenderer.
- Повторные object IDs внутри вложенной композиции блокируются до рендера.
- Для Pillow top-level `table` требует `reveal_row` на последнюю строку: AnimationEngine инициализирует `revealed_rows=0`, иначе тело таблицы остаётся скрытым. Событие раскрывает префикс строк до указанного индекса.
- Для Pillow top-level `shape.line/arrow`, `chart.bar` с более чем одной категорией, `chart.line` с более чем двумя точками в серии и `node_graph` с рёбрами требуется `draw`: AnimationEngine инициализирует `draw_progress=0`, иначе графический элемент остаётся частично или полностью неотрисованным. Для объектов внутри group это требование не применяется: их состояния не создаются отдельно и SceneRenderer использует собственное полное значение по умолчанию.
- `appear` поддерживается для Pillow group, поскольку `visible` проверяется до рекурсивной отрисовки детей. Другие group actions не применяют состояние к оболочке группы и не регистрируются.

Дополнительные структурные guards CP-02: для `node_graph` каждая связь обязана ссылаться на существующие `node_id`; runtime renderer иначе может молча пропустить ребро. Для Pillow действие `draw` на `node_graph` допустимо только при наличии хотя бы одного ребра: узлы этого исполнителя не раскрываются постепенно. Неизвестное имя subtype-поля также даёт отдельную блокирующую диагностику `unknown_subtype_field`, а не принимается молча.

## 3. Действия и адресация

Runtime V2 знает 10 actions. Их наличие в общей схеме не означает, что каждый исполнитель поддерживает каждое действие для каждого kind.

| Action | Professional Browser V2 | Pillow + AnimationEngine |
|---|---|---|
| `appear` | Все kinds; CSS visibility/opacity для верхнеуровневого объекта | Все kinds, включая group; visibility проверяется до рекурсии детей |
| `fade_in` | Все kinds; CSS opacity | Все kinds, кроме group |
| `fade_out` | Все kinds; CSS opacity | Все kinds, кроме group |
| `highlight` | Все kinds; CSS drop-shadow | Все kinds, кроме group; рамка вокруг объекта |
| `move` | Все kinds; top-level left/top | Все kinds, кроме group; смещение слоя |
| `scale` | Все kinds; top-level scale | Все kinds, кроме group; масштаб слоя |
| `draw` | Все kinds получают wipe/reveal маску; дополнительное path/bar поведение зависит от внутренней разметки | Только `shape.line/arrow`, `chart.bar/line`, `node_graph`; узлы графа остаются видимыми, постепенно проявляются рёбра. `area` не поддержан, поскольку заливка области отсутствует |
| `reveal_row` | Только table; адресация целочисленным `params.row_index` | Только table; раскрывает префикс строк до указанного индекса |
| `count_up` | Только counter | Только counter |
| `update_text` | `text`, `callout`, `counter` | Только `text` |

### Subtargets

Текущий runtime event API адресует `target_id` верхнеуровневого объекта. У него нет универсального стабильного `subtarget_id`.

В частности, сейчас нельзя адресовать отдельные graph nodes/edges, chart series/bars, table columns или дочерние объекты группы как самостоятельные event targets. `reveal_row` использует `row_index`, а не стабильный `row_id`. Pillow раскрывает строки как префикс от начала таблицы до индекса, Professional Renderer анимирует строку по индексу.

Это важное ограничение для будущего compiler/adapter: семантические действия authoring contract `draw_edge`, `reveal_node`, `reveal_column`, `grow_bar` и адресация по стабильному ID не должны объявляться исполнимыми до отдельной реализации преобразования и проверки.

## 4. Media, форматы и пределы

| Характеристика | Professional Browser V2 | Pillow pipeline V2 |
|---|---|---|
| Sources | local path, HTTP(S), Pixabay search | local path, URL, Pixabay search |
| Image handling | Native browser `img` | Pillow `Image.open` |
| Video handling | Native HTML `video` inside recorded scene | FFmpeg extracts frame sequence to PNG |
| Input format allowlist | Не обнаружен в коде resolver | Не обнаружен в коде resolver |
| Download cap | По умолчанию 100 MiB на remote media (настройка конструктора изменяема) | Для Pixabay CDN действует лимит 100 MiB в `pixabay_media.py`; прямой HTTP(S) URL в `visual_asset_resolver_v2.py` скачивается без byte cap |
| Renderer output | `video/mp4` (H.264, yuv420p; frame count verified) | `image/png` numbered frame sequence |

Реестр намеренно не перечисляет расширения `.jpg`, `.webp`, `.mp4` и т.п. как гарантированно поддержанные. Код не содержит единого allowlist, который это подтверждал бы. Professional renderer передаёт декодирование Chromium; Pillow/FFmpeg — соответствующим установленным библиотекам. Формат должен проверяться на фактическом этапе preflight/decode, а не выводиться только из расширения файла.

Professional Renderer распознаёт `camera_motion`: `static`, `slow_zoom`, `zoom_in`, `zoom_out`, `pan_left`, `pan_right`. Pillow renderer игнорирует `camera_motion`, поэтому реестр допускает для него только `static`. Pillow line/arrow использует диагональ bounding box и не читает `content.start`/`content.end`; при заданных endpoints такой запрос блокируется. Если endpoints не заданы, нужно учитывать различие геометрии: Professional по умолчанию строит горизонтальный сегмент между нормализованными точками, Pillow — диагональ object box.

## 5. Версии и интеграционный статус

В исходных модулях renderer отдельные SemVer константы не объявлены. В реестре это не маскируется выдуманным номером релиза: поле версии фиксирует module identity и runtime schema `2.0`, а отсутствие исходной версии обозначено явно.

`professional_browser_v2` — основной runtime executor. `pillow_animation_v2` — альтернативный frame executor. Оба реализуют runtime V2, но различаются поведением actions, вложенными media и выходными форматами.

`diagram_renderer_legacy` (`diagram_renderer.py`) принимает scene dictionary и выдаёт PNG. `html_renderer_legacy` (`html_renderer.py`) принимает beat dictionary и выдаёт MP4. Это не прямые runtime V2 исполнители и не targets нового registry.

## 6. API реестра

Модуль `visual_capability_registry_v2.py` предоставляет:

- `list_renderer_capabilities()` и `get_renderer_capability(renderer_id)`;
- `validate_capability_request(...)` — проверка отдельной комбинации kind/subtype/action/subtarget;
- `validate_shot_capabilities(shot, renderer_id)` — проверка runtime Shot, групп, media motion, событий и индексов строк;
- `require_shot_capabilities(...)` — тот же gate с блокирующим `CapabilityValidationError`;
- `RendererCapability.as_contract_model()` — компактная проекция в CP-01 `RendererCapabilities`. Проекция не заменяет расширенную матрицу реестра.

Диагностики имеют стабильные `code`, `severity`, `renderer_id`, `target_id`, `message`; порядок сортируется для воспроизводимых dry-run отчётов. Validator также проверяет структуру графов, polygon points, таблиц и chart series/values, а для Pillow блокирует сценарии, в которых инициализированное состояние AnimationEngine привело бы к неполному визуальному содержимому.

## 7. Границы CP-02

**Входит:** каталог существующих runtime исполнителей, kinds/actions/subtypes, реальные ограничения, output formats, media source/format policy, validator, диагностические коды и тесты.

**Не входит:** изменение старых renderers/animation engine/asset resolver, новые эффекты, новый CLI, MWS/LLM, compiler CP-03, преобразование authoring → runtime, загрузка media, генерация MP4 или производственное переключение.

Локальная приёмка владельцем проекта после source-level review hardening: `py_compile` успешно; целевые тесты **67 passed in 0.27s**; полный набор **504 passed in 8.41s**; `git diff --check` без замечаний. Source-level review подтвердил соответствие реестра двум runtime V2 исполнителям и выявил/закрыл ложноположительные записи Pillow area/multi-series charts, ошибочную блокировку group `appear`, а также отсутствие guardrails для неполного progressive draw/table state и ряда некорректных структурных payloads. Production renderer/runtime/CLI/pipeline не менялись. Commit и push ещё ожидаются; CP-03 не начинается до проверки синхронизации с GitHub.
