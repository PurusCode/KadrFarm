# KadrFarm — главный план Stage I: Visual Story 2.0

- **Версия документа:** 1.8
- **Дата создания:** 24.11.2026
- **Дата обновления:** 25.11.2026
- **Рабочая ветка:** `feature/autonomous-video-engine-2`
- **Исходная контрольная точка:** `414d08c9a4e8ef7748488e356d266ac62083be5a` (`Harden AutoFit dynamic state integrity`)
- **Статус:** активный навигационный документ Stage I
- **Назначение:** единая точка принятия решений, фиксации прогресса и проверки направления работ.

> **Главное правило:** ни один следующий этап не начинается, пока текущая контрольная точка не завершена, не проверена, не описана здесь и не сохранена отдельным Git-коммитом с push в удалённую ветку.

---

## 1. Для чего нужен этот документ

Этот документ — постоянный маршрутный лист разработки новой визуальной режиссуры KadrFarm. Он предотвращает повторное проектирование уже принятых решений, потерю контекста между сессиями, незаметное расширение объёма работ и повторение циклов исправления несовместимых JSON-схем.

После **каждого завершённого улучшения** здесь фиксируются:

1. какую проблему решали и почему это было необходимо;
2. какое архитектурное решение принято и какие альтернативы отвергнуты;
3. какие файлы добавлены или изменены;
4. какие проверки выполнены и их фактический результат;
5. какие ограничения, риски и незакрытые пункты остались;
6. SHA коммита и подтверждение синхронизации с GitHub;
7. следующий разрешённый шаг.

Если новый чат или сессия начинается без полного контекста, первым делом читается этот документ и проверяется текущий Git checkpoint. Нельзя восстанавливать план по памяти или начинать с предположений.

## 2. Текущее исходное состояние

### 2.1. Git checkpoint

| Параметр | Зафиксированное состояние |
|---|---|
| Репозиторий | `PurusCode/KadrFarm` |
| Ветка | `feature/autonomous-video-engine-2` |
| HEAD | `414d08c9a4e8ef7748488e356d266ac62083be5a` |
| Удалённая ветка | `origin/feature/autonomous-video-engine-2` |
| Синхронизация на момент старта | локальная и удалённая ветки совпадали после `git fetch origin` |
| Staged / modified tracked files | отсутствовали |
| Неотслеживаемые установщики, диагностика и `.bak` | сохранены; не удалять и не добавлять автоматически |

Коммит `414d08c` является базовой точкой восстановления. Не выполнять `git reset --hard`, `git clean`, массовое удаление файлов или `git add .`.

### 2.2. Подтверждённое состояние Professional Renderer V2

По результатам локальных проверок владельца проекта на этой контрольной точке:

- полный набор тестов: **388 passed**;
- реальный recovery smoke MP4 собран успешно;
- профиль результата: 1920×1080, H.264/AAC;
- длительность MP4 совпала с измеренной длительностью master narration: 14,8 секунды;
- Preflight прошёл для всех трёх shots;
- AutoFit v1.2: `unresolved_count = 0`;
- Geometry QC: ошибок и предупреждений не обнаружено;
- общий quality gate: `passed`.

Это подтверждает техническую работоспособность проверенного V2 smoke-сценария, но **не является художественной приёмкой полноценного ролика**. В audit у двух Pixabay материалов указан `rights_status: review_required`; их права необходимо проверять перед публикацией.

### 2.3. Граница восстановления

Неудачный MVP/Visual Production Quality V1 был откачен. Его прежние циклы генерации Global Plan / Scene Director с несовместимыми схемами не возобновлять. Архивы, диагностические артефакты и установщики сохраняются только для истории и расследования, а не становятся основой нового этапа.

## 3. Цель Stage I

Создать отдельную, проверяемую систему **Visual Story 2.0**, которая сначала проектирует визуальную драматургию всего ролика, затем детально описывает композиции сцен, проверяет планы детерминированно и только после этого передаёт исполнимые задания специализированным renderers.

Итоговая цепочка Stage I:

`authoritative storyboard → Global Director → Scene Directors → strict validation → renderer capabilities → Scene Compiler → ExecutionPlan → preview / visual audit`

Подключение к полноценному производству видео — последующий интеграционный этап. До отдельной приёмки новый конвейер не заменяет Professional Renderer V2 и старый production path.

## 4. Неизменяемые проектные принципы

### 4.1. Речь — самостоятельный канал объяснения

Дикторская дорожка должна полностью передавать все существенные факты, числа, причинно-следственные связи и выводы. Визуал усиливает и наглядно иллюстрирует речь, но не несёт единственную важную информацию и не заменяет объяснение диктора.

Нельзя оценивать результат по требованию «понятен без звука». Правильный вопрос: понятна ли вся существенная мысль при прослушивании, и помогает ли изображение понять её лучше.

### 4.2. Смысл важнее таймера и квот

- Новая композиция появляется из-за смены мысли, действия, состояния, пространства или визуального аргумента, а не по равномерному таймеру.
- Нет обязательных квот на фото, видео, графику, таблицы, диаграммы, типы анимации или количество shots.
- Нет обязательной доли сцен, развивающих предыдущие композиции.
- Повторяющиеся мотивы необязательны; их нельзя вводить только для прохождения валидатора.
- Контраст, разрешение, переход, продолжение действия и осмысленный callback — полноценные виды визуальной связи.
- Одна сильная композиция может сопровождать длинную законченную мысль.

### 4.3. Граница ответственности

- Режиссёр определяет смысл, видимый результат, объекты, взаимосвязи и намерение событий.
- Детерминированный исполнитель рассчитывает фактическую геометрию, safe areas, размеры, техническую анимацию, frame boundaries и кодирование.
- LLM не создаёт произвольный Python/JavaScript/HTML-код для выполнения.
- LLM не задаёт точные секунды и не выдумывает измерения.

### 4.4. Достоверность и происхождение данных

- `scene_id`, порядок сцен и `voice_text` приходят из авторитетной раскадровки и не переписываются режиссёром.
- Факты/числа берутся из отдельно подготовленного доверенного реестра с provenance; режиссёр может ссылаться на существующие значения, но не создавать verified-факты или URL.
- Иллюстративные данные должны явно обозначаться как иллюстративные и не выдаваться за реальные измерения.
- Реалистичные интерфейсы, документы, имена, цитаты, логотипы, URL и результаты событий нельзя выдумывать.
- Отсутствующий asset или неподдерживаемое действие должны давать явную диагностику, а не молчаливую подмену случайной карточкой или нерелевантным медиа.

### 4.5. Время и речь

- До TTS допустимы только candidate speech segments с точными текстовыми диапазонами; они не являются таймкодами.
- Длительность разрешается только по реально измеренным WAV или проверенному alignment.
- Нельзя делить длительность WAV пропорционально числу символов/слов и объявлять это точной синхронизацией.
- В отчёте должна сохраняться фактическая точность привязки события к речи.

## 5. Документы и порядок приоритета

Чтобы документы не противоречили друг другу, использовать следующий порядок:

1. **Этот документ (`docs/35_KADRFARM_STAGE_I_MASTER_PLAN_RU.md`)** — актуальное направление Stage I, принятые решения, статусы checkpoint и следующий шаг.
2. **`docs/36_VISUAL_STORY_2_CONTRACT_FREEZE_RU.md`** — замороженный authoring contract 2.0: модели, source references, anchors, типизированные объекты/события и границы исполнения. В части Stage I он конкретизирует технические правила этого мастер-документа.
3. **`docs/34_VISUAL_STORY_CONTRACT_V2_RU.md`** — исходный архитектурный draft. Используется как концептуальный источник, но в случае расхождения приоритет имеют документы 35 и 36.
4. **`docs/VISUAL_STORY_V2_PROFESSIONAL_RENDERER_RU.md`** — описание существующего Professional Renderer V2, его возможностей, технических gates и ограничений.
5. **`docs/30_VISUAL_STORYTELLING_PILOT_2026-11-18_RU.md`** — выводы визуального пилота: техническая синхронизация работала, но режиссура была однообразной, часть стоков имела слабое смысловое соответствие, требовались более сильные объясняющие композиции.
6. **`docs/32_VISUAL_STORY_TWO_PASS_CONTRACT_RU.md`** — историческая спецификация прежней двухпроходной режиссуры 1.x. Полезные общие правила можно учитывать, но противоречащие решения не переносить.
7. **`src/kadr_farm/visual_story.py` и `src/kadr_farm/visual_story_prompts.py`** — legacy authoring contract 1.x. Он остаётся нетронутым и не становится автоматически контрактом 2.0.
8. **`src/kadr_farm/visual_story_v2.py` и связанные renderer/execution modules** — существующий исполнимый runtime contract V2. Он не является реализацией нового двухпроходного authoring contract и остаётся рабочей производственной основой.

### 5.1. Различия версий и устаревшие для 2.0 правила

В старой реализации `visual_story.py` / `visual_story_prompts.py` встречались требования повторяющегося мотива и минимальной доли развивающихся сцен. Для нового authoring contract 2.0 эти квоты **не действуют**. Не удалять legacy 1.x: его ограничения остаются только внутри старой версии и её тестов.

Существующий `visual_story_v2.py` — отдельная runtime-схема с `SpeechUnit`, `Shot`, `VisualObject` и `VisualEvent`; она использует measured offsets/durations и связана с реальными renderer/execution модулями. Не считать её старым authoring contract 1.x и не заменять новым пакетом. Новый двухпроходный authoring contract расположен в `visual_story_contract_v2/`; преобразование между ним и runtime V2 допускается только через отдельный проверяемый адаптер на CP-06.

## 6. Архитектура и границы внедрения

### 6.1. Параллельная реализация

Новый authoring contract размещён в отдельном пакете:

```text
src/kadr_farm/visual_story_contract_v2/
    __init__.py
    common.py
    global_plan.py
    objects.py
    events.py
    scene_plan.py
    execution.py
    validation.py
```

Пакет не затеняет существующий файл `src/kadr_farm/visual_story_v2.py`. Последний остаётся отдельным runtime contract. Новый пакет не подключён к старым renderer/CLI и не меняет их поведение.

Тесты и fixtures изолированы:

```text
tests/test_visual_story_contract_v2.py
tests/fixtures/visual_story_contract_v2/
```

### 6.2. Запрещено до отдельной приёмки

- заменять или переименовывать существующие `visual_story.py`, `visual_story_prompts.py`, `visual_story_v2.py`;
- подключать новый Stage I к `production_director.py` или к CLI;
- менять работающий Professional Renderer V2, AutoFit, Preflight, Geometry QC или FFmpeg pipeline;
- добавлять новый путь производства MP4 до получения валидного `ExecutionPlan` и принятия dry-run;
- менять provider routing или создавать автоматический fallback.

### 6.3. LLM и provider policy

Для интеграционного этапа использовать принятую в проекте политику: закреплённый MWS-провайдер на весь run, без автоматического переключения на локальную Ollama/другую модель. Повторное обсуждение или скрытая смена маршрута не входят в Stage I. Перед реальным вызовом проверяются endpoint/model identity, request fingerprint и журнал; секреты и содержимое `.env` в артефакты Git не включать.

## 7. План этапов и контрольные точки

Каждый пункт ниже — отдельный checkpoint, отдельная проверка, отдельная запись в этот документ, отдельный commit и push.

### CP-00 — Навигация и правила ведения работ

**Задача:** создать этот мастер-документ и договориться о дисциплине checkpoint.

**Почему:** предыдущая работа теряла направление из-за параллельных патчей, несовпадающих контрактов и перехода к интеграции до стабилизации схем.

**Состав:** только этот документ. Никаких runtime-изменений.

**Проверка:** документ существует, `git diff --check` чист, в stage находится только этот файл.

**Выход:** коммит документа и подтверждённый push. После этого — CP-01.

### CP-01 — Freeze технического контракта 2.0 и fixtures

**Задача:** закрепить строгий двухпроходный authoring contract 2.0, не смешивая его с существующим runtime V2.

**Почему:** существующие `visual_story.py` (legacy authoring 1.x) и `visual_story_v2.py` (исполняемый runtime V2) имеют разные назначения. Новому Global Director / Scene Director нужен отдельный контракт с точными речевыми диапазонами и без LLM-таймкодов.

**Состав CP-01:**

- `src/kadr_farm/visual_story_contract_v2/` — 8 файлов строгих Pydantic v2 моделей и cross-validation;
- `tests/test_visual_story_contract_v2.py` — отдельный набор из 49 тестов;
- `tests/fixtures/visual_story_contract_v2/` — 8 JSON fixtures;
- `docs/36_VISUAL_STORY_2_CONTRACT_FREEZE_RU.md` — полная замороженная спецификация;
- обновление этого мастер-документа.

**Принятые решения:**

- Global Plan покрывает все исходные `scene_id` строго в исходном порядке; Scene Plan существует ровно один на каждую исходную сцену.
- Источник речи — `storyboard.scenes[].voice_text`; новый контракт не предполагает `beat_id` и не возвращает/переписывает `voice_text`.
- Speech anchors адресуют точные Unicode character spans исходного текста и проверяются по исходному excerpt и SHA-256.
- В authoring-моделях нет секунд, frame indices, pixel geometry или произвольного исполняемого кода; renderer рассчитывает техническое исполнение.
- Объекты и действия типизированы; неизвестные поля и enum отклоняются; проверяются targets/subtargets, групповые ссылки и циклы.
- Numeric claims требуют provenance references; illustrative values требуют явной маркировки. Формулы допускают только ограниченное арифметическое выражение на зарегистрированных refs.
- Контраст, разрешение, callback и переход допустимы без переноса одного объекта. Пустые motifs и отсутствие continuity carry-over полностью допустимы; квот нет.
- ExecutionPlan, AssetRequest, RendererCapabilities и RenderTask — только структуры данных CP-01. Реальный registry/compiler относятся к следующим этапам.
- Неподдерживаемое действие должно блокировать исполнение; silent substitution запрещена.

**Локальные проверки 24.11.2026:**

- `python -m compileall -q src/kadr_farm/visual_story_contract_v2` — успешно.
- `python -m pytest -q tests/test_visual_story_contract_v2.py` — **49 passed in 0.28s**.
- `python -m pytest -q` — **437 passed in 11.24s**.
- `git diff --check` — ошибок whitespace не обнаружено. Git вывел только стандартное предупреждение Windows LF/CRLF.
- Runtime/renderer/CLI файлы не изменены; единственное изменение отслеживаемого файла до этой записи — данный мастер-документ.
- Ранее существовавшая резервная копия `docs/35...before_cp01...bak` сохранена; установщики и иные untracked-файлы не включаются.

**Не входит:** LLM/MWS-вызовы, Pixabay, реальный capability registry, compiler implementation, actual render, FFmpeg, production CLI, provider routing и изменение existing runtime V2.

**Статус:** **завершена**; локальная приёмка, отдельный commit и push подтверждены.

**Commit:** `8884c9509a1310ae687e4f9d8e87d41394de3df9` (`Freeze Visual Story 2.0 authoring contract`).
**Push:** `origin/feature/autonomous-video-engine-2` подтверждён; после `git fetch origin` локальный `HEAD` и удалённая ветка совпали.

**Следующий разрешённый шаг:** CP-02 — Renderer Capability Registry.

### CP-02 — Renderer Capability Registry

**Commit:** `0d1930dcd3d82b8767e55d94e09536ec90127eae` (`Implement renderer capability registry for CP-02`).
**Push:** `origin/feature/autonomous-video-engine-2` подтверждён; удалённая ветка указывает на этот commit.

**Задача:** описать реальные возможности существующих runtime V2 исполнителей и добавить детерминированный gate, который запрещает неподдерживаемые комбинации без молчаливой подмены.

**Состав CP-02:**

- `src/kadr_farm/visual_capability_registry_v2.py` — версия реестра `1.0.0`, два runtime исполнителя (`professional_browser_v2`, `pillow_animation_v2`), kinds/actions/subtypes, форматы, источники медиа, ограничения, validator и структурные guardrails;
- `tests/test_visual_capability_registry_v2.py` — 67 целевых тестов;
- `docs/37_RENDERER_CAPABILITY_REGISTRY_V2_RU.md` — инвентаризация, ограничения и API;
- никаких изменений в production renderer, AnimationEngine, asset resolvers, runtime-модели, CLI или pipeline.

**Фактические исполнители:** Professional Browser Renderer V2 — основной runtime executor; Pillow SceneRenderer + AnimationEngine V2 — альтернативный frame executor. Исторические `diagram_renderer.py` и `html_renderer.py` оставлены в отдельном legacy inventory, поскольку принимают не runtime `VisualStoryPlan`, а legacy scene/beat dictionaries.

**Результаты source-level review и hardening:**

- Pillow `area` исключён из поддерживаемых chart subtypes: существующая ветка фактически рисовала линию без area fill.
- Multi-series bar в Pillow и multi-series pie/donut в обоих исполнителях блокируются: текущие ветви renderer используют только первый series.
- `appear` для Pillow group зарегистрирован как поддержанный: `visible` проверяется до рекурсивной отрисовки детей. Остальные group actions Pillow не поддерживает.
- Проверяются события `draw` для top-level Pillow line/arrow, bar/line charts и node graph, если начальное состояние иначе оставляет прогрессивные элементы неполными; для top-level Pillow table требуется раскрытие последней строки.
- Добавлены структурные проверки node graph (непустые уникальные строковые IDs и существующие ссылки рёбер), polygon points, chart series/values, table columns/rows, row subtargets, media motion и несовместимой геометрии Pillow line/arrow.
- Валидация явно различает runtime contract `visual_story_v2.py` и новый authoring contract `visual_story_contract_v2/`; прямого подключения или адаптера пока нет.

**Проверки локальной приёмки:**

- `py_compile` для реестра и его тестов — успешно;
- `python -m pytest -q tests/test_visual_capability_registry_v2.py` — **67 passed in 0.27s**;
- `python -m pytest -q` — **504 passed in 8.41s**;
- `git diff --check` — без замечаний.

**Ограничения и non-goals:** реестр описывает существующие возможности, а не добавляет renderer features. Нет runtime/CLI/provider изменений, LLM/MWS вызовов, загрузки медиа, MP4 рендера или адаптера authoring → runtime. Стабильные subtarget IDs для внутренних элементов графика/таблицы/группы не поддерживаются. Полноценная компиляция authoring plans остаётся задачей CP-03 и следующих этапов.

**Статус:** CP-02 завершена; source-level review, локальная приёмка, commit и push подтверждены.

### CP-03 — Детерминированный Scene Compiler и dry-run

**Задача:** компилировать Global/Scene plans в ExecutionPlan без LLM и без рендера.

**Состав:** проверка ссылок и coverage, asset requests, render tasks, зависимости, речевые anchors, проверка capabilities и конфликтов, стабильный сериализуемый ExecutionPlan, fingerprints и resume-safe checkpoints.

**Критерий выхода:** одинаковые входы и версии дают одинаковый план; ошибочные связи/действия блокируются; dry-run не скачивает медиа и не создаёт MP4.

**Состав CP-03:**

- `src/kadr_farm/visual_story_execution_plan_compiler_v2.py` — чистый детерминированный компилятор, версия `1.1.0`;
- `tests/test_visual_story_execution_plan_compiler_v2.py` — 44 целевых теста;
- `docs/38_VISUAL_STORY_EXECUTION_PLAN_COMPILER_CP03_RU.md` — архитектура, capability gates, fingerprints, checkpoint и ограничения.

**Ключевые решения и guards:**

- Один `RenderTask` представляет целую shot-композицию: `object_id="shot_composition"`. Это соответствует фактическому runtime API обоих исполнителей; фиктивные задачи на отдельные объекты не создаются.
- Image/video получают отдельные `AssetRequest`; `CompilationResult.asset_bindings` явно связывает их с shot-level task.
- Renderer выбирается только явно. Нет автоматического fallback, сетевых вызовов, LLM/MWS, скачивания медиа или рендера.
- Проверяются CP-01 storyboard hash, покрытие сцен, source text hashes, speech anchors, continuity references, renderer capabilities и конфликтующие события.
- Неподдерживаемые действия и параметры блокируются без aliasing или визуальной подмены. Pillow-specific guards учитывают начальные draw/table states, chart series, graph edges и глубину вложенных media.
- Canonical JSON, task fingerprints и baseline checkpoint детерминированы; новый checkpoint не объявляет задачи завершёнными.

**Проверки локальной приёмки:**

- `py_compile` для CP-03 compiler и test — успешно;
- целевые тесты — **44 passed**;
- полный набор проекта — **548 passed**;
- `git diff --check` — без замечаний.

**Source-level review:** компилятор сверён с frozen CP-01 authoring contract, CP-02 capability registry и фактическими границами runtime V2 renderers/asset resolver. Старый `visual_scene_compiler_v2.py`, `visual_execution_v2.py`, runtime-модели, renderers, asset resolver, CLI и production pipeline не изменялись.

**Статус:** **завершена; source-level review, локальная приёмка, отдельный commit и push подтверждены.**

**Ограничения и non-goals:** CP-03 создаёт только dry-run dispatch plan. Он не является runtime adapter, не разрешает реальные assets/value refs, не вычисляет аудио-тайминг и не создаёт PNG/MP4. Authoring → runtime adapter остаётся CP-06; production path не переключается.



### CP-04 — Preview и проверка исполнимости

**Задача:** создать ограниченный asset-free preview, который реально исполняет выбранные композиции существующего Professional Browser Renderer V2 и показывает начальные, промежуточные и итоговые состояния.

**Входной контракт:** только существующий runtime `kadr_farm.visual_story_v2.VisualStoryPlan`. CP-01 authoring models и CP-03 `ExecutionPlan` не принимаются и не преобразуются; authoring → runtime adapter остаётся CP-06.

**Исполнитель и проверки:** `professional_browser_v2`; существующий `ProfessionalBrowserRenderer.preflight_shot()` выполняет Chromium preflight, AutoFit и geometry QC без записи видео. Отдельный preview harness снимает PNG из того же подготовленного HTML, сверяет полный DOM object inventory (включая nested group children) и собирает contact sheet + JSON audit.

**Ограниченный профиль:** kinds `text`, `shape`, `icon`, `callout`, `group`, `node_graph`, `chart`, `table`, `formula`, `counter`; chart subtypes только `bar`/`line`; разрешённые actions `appear`, `fade_in`, `fade_out`, `highlight`, `count_up`, `update_text`. Preview policy: не более 6 shots, 24 объектов и 8 событий на shot, расписание вместе с preview tail не длиннее 12 секунд.

**Состав CP-04 candidate:** `src/kadr_farm/visual_story_preview_v2.py`, `tests/test_visual_story_preview_v2.py`, `examples/visual_story_v2_preview_cp04.json`, `docs/39_VISUAL_STORY_PREVIEW_CP04_RU.md` и этот мастер-документ.

**Non-goals:** image/video и poster assets, asset resolver, сеть/Pixabay, MWS/LLM, WAV/TTS и measured audio timing, FFmpeg/MP4, существующий renderer/pipeline/CLI изменения, Pillow preview и production switch. Runtime preview offsets — только ручная шкала демонстрационных состояний, не audio sync.

**Статус:** **локальная реализация, source-level review, целевая и полная регрессионная приёмка, реальный Chromium preview и визуальная проверка завершены; отдельные commit и push ожидаются.**

**Критерий выхода:** целевые и полные тесты проходят; реальный sample preview создаёт PNG snapshots, контактный лист и аудит; capability gate не допускает неподдерживаемые комбинации; AutoFit `unresolved_count = 0`, geometry errors отсутствуют, DOM object IDs совпадают с runtime inventory, renderer не создаёт `.engine-error`; контактный лист визуально принят. Только после этого CP-04 может получить отдельный acceptance commit/push.

### CP-05 — Двухпроходный Global Director / Scene Director

**Задача:** подключить модель к уже замороженным схемам и валидированным orchestration contracts.

**Global Director:** один запрос на весь ролик, полный список исходных сцен, source hash, одна версия схемы; никаких независимых batch-запросов по сценам.

**Scene Director:** один запрос на сцену последовательно; получает полный Global Plan, точную текущую сцену, направления соседних сцен и проверенную историю предыдущих сцен.

**Обязательные guards:**

- exact scene coverage/order;
- неизменность `scene_id` и `voice_text`;
- validation сразу после каждого ответа;
- fingerprint включает model/provider/endpoint, schema и prompt versions;
- ProductionJournal сохраняет raw responses и checkpoints;
- семантически отклонённые задачи не переиспользуются из кэша;
- никакого provider fallback;
- никаких автоматических ослаблений редакторских правил.

**Критерий выхода:** тестовые ответы проходят все контракты, реальные ответы MWS проходят без ручного редактирования схемы, сбой одной сцены не повреждает проверенный префикс, run можно безопасно возобновить.

### CP-06 — Интеграция с Professional Renderer V2 через адаптер

**Задача:** адаптировать утверждённый ExecutionPlan к существующему renderer, не заменяя его.

**Критерий выхода:** ограниченная тестовая композиция проходит полный путь plan → compiler → adapter → V2 render → Preflight/AutoFit/Geometry QC; старый CLI и старые тесты остаются рабочими.

### CP-07 — Сквозная приёмка нового визуального пути

**Задача:** выполнить ограниченный, затем многосценовый E2E с настоящими WAV и реальными media assets там, где они нужны.

**Критерии:** соответствие речи, самостоятельная понятность дикторской дорожки, разнообразие и развитие визуального аргумента без искусственных квот, права на used media, стабильность resume, качество изображения, frame/audio sync, audit и контактный лист.

Только после этого можно отдельно рассматривать подключение нового пути к production CLI. Автоматического переключения до отдельного решения нет.

## 8. Обязательный протокол каждого checkpoint

1. **Исходное состояние:** проверить branch, HEAD, `git status`, staged files и отсутствие неожиданных tracked modifications.
2. **План:** определить узкую цель checkpoint, список допустимых файлов, причины каждого изменения и явные non-goals.
3. **Реализация:** создать install/apply script или конкретные файлы; не использовать массовые правки и не перезаписывать неизвестные пользовательские изменения.
4. **Проверка:** сначала compile/targeted tests, затем соответствующий полный набор tests; для рендера — отдельный реальный smoke только когда это предусмотрено checkpoint.
5. **Ревью:** изучить diff, `git diff --check`, staged name-status и stat; не применять `git add .`.
6. **Документирование:** дополнить раздел 9 этого файла — что, зачем, файлы, тесты, результат, остаточные риски и следующий checkpoint.
7. **Коммит:** один логически законченный checkpoint — один commit с ясным сообщением.
8. **Push:** отправить в `origin/feature/autonomous-video-engine-2`.
9. **Подтверждение:** `git fetch origin`, сравнить локальный HEAD и origin HEAD, проверить `git status -sb`.
10. **Только затем** разрешается следующий checkpoint.

Если тест или проверка не прошли, checkpoint остаётся открытым. Нельзя документировать его как завершённый, переходить дальше или коммитить заведомо неготовое улучшение.

## 9. Журнал выполненных checkpoint

### CP-00 — Навигационный документ

- **Статус:** выполнен; commit и push подтверждены.
- **Почему:** нужен единый источник актуального направления и обязательная дисциплина commit/push после каждого этапа.
- **Файл:** `docs/35_KADRFARM_STAGE_I_MASTER_PLAN_RU.md`.
- **Runtime изменения:** нет.
- **Проверки:** UTF-8 валиден, 317 строк; SHA-256 исходного файла перед stage совпал с ожидаемым; `git diff --cached --check` без ошибок; в stage был только этот файл.
- **Commit:** `12fffcf4de4c4978ebfe66e7a549ab521fcf8105` (`Add Stage I Visual Story 2.0 master plan`).
- **Push:** `origin/feature/autonomous-video-engine-2` подтверждён; после `git fetch origin` локальный `HEAD` и `origin/feature/autonomous-video-engine-2` совпали. Прежние untracked installers/backups остались нетронутыми.

### CP-01 — Заморозка authoring contract 2.0

- **Статус:** **завершён; commit и push подтверждены**.
- **Почему:** строго разделить новый двухпроходный authoring contract, legacy 1.x и действующий runtime V2.
- **Файлы:** 8 модулей нового пакета, 8 JSON fixtures, 1 тестовый модуль, `docs/36_VISUAL_STORY_2_CONTRACT_FREEZE_RU.md`, этот мастер-документ.
- **Runtime изменения:** нет; существующие renderers, CLI, production path и provider routing не менялись.
- **Проверки:** `compileall` успешно; целевые тесты **49 passed in 0.28s**; полный набор **437 passed in 11.24s**; `git diff --check` без whitespace ошибок (Git показал только уведомление Windows LF/CRLF).
- **Commit:** `8884c9509a1310ae687e4f9d8e87d41394de3df9` (`Freeze Visual Story 2.0 authoring contract`).
- **Push:** подтверждён в `origin/feature/autonomous-video-engine-2`; после `git fetch origin` локальный `HEAD` и remote HEAD совпали.
- **Остаток:** прежние untracked installers, diagnostics и `.bak` сохранены и не включены в commit.
- **Следующий этап:** CP-02 — Renderer Capability Registry.

### CP-02 — Renderer Capability Registry

- **Статус:** **завершена; source-level review, локальная приёмка, отдельный commit и push подтверждены.**
- **Почему:** CP-03 должен опираться на фактическую матрицу исполнителей, а не на предположения о поддержке объектов и анимаций.
- **Файлы:** `src/kadr_farm/visual_capability_registry_v2.py`, `tests/test_visual_capability_registry_v2.py`, `docs/37_RENDERER_CAPABILITY_REGISTRY_V2_RU.md` и этот мастер-документ.
- **Исполнители:** `professional_browser_v2` и `pillow_animation_v2`; legacy `diagram_renderer.py` / `html_renderer.py` документированы отдельно и не объявлены runtime V2 targets.
- **Source-level review:** устранены ложноположительные capabilities Pillow area/multi-series charts; уточнена поддержка group `appear`; добавлены guards для неполного начального draw/table state и структуры graph/polygon/chart/table payloads.
- **Runtime изменения:** отсутствуют. Production renderer, AnimationEngine, asset resolver, runtime schema, CLI, provider routing и старый production path не изменялись.
- **Проверки:** `py_compile` успешно; целевые тесты **67 passed in 0.27s**; полный набор **504 passed in 8.41s**; `git diff --check` без замечаний.
- **Commit:** `0d1930dcd3d82b8767e55d94e09536ec90127eae` (`Implement renderer capability registry for CP-02`).
- **Push:** `origin/feature/autonomous-video-engine-2` подтверждён; удалённая ветка указывает на этот commit.
- **Остатки:** нет интеграции authoring → runtime, нет compiler/dry-run и нет production переключения. Неподдерживаемые subtargets и отличия исполнителей должны учитываться CP-03.
- **Следующий этап:** CP-03 — детерминированный Scene Compiler и dry-run, только после подтверждённого push CP-02.


### CP-03 — Детерминированный Scene Compiler и dry-run

- **Статус:** **завершён; source-level review, локальная регрессионная приёмка, отдельный commit и push подтверждены**.
- **Почему:** создать детерминированную компиляцию Global/Scene Plans в dry-run ExecutionPlan, опирающуюся на frozen CP-01 contract и фактические CP-02 renderer capabilities.
- **Файлы:** `src/kadr_farm/visual_story_execution_plan_compiler_v2.py`, `tests/test_visual_story_execution_plan_compiler_v2.py`, `docs/38_VISUAL_STORY_EXECUTION_PLAN_COMPILER_CP03_RU.md` и этот мастер-документ.
- **Ключевые решения:** один `RenderTask` представляет целую shot-композицию (`object_id="shot_composition"`); Image/Video `AssetRequest` связываются с shot-level task через `CompilationResult.asset_bindings`; renderer выбирается явно, без fallback.
- **Guards:** проверяются storyboard/source hashes, scene coverage, speech anchors, continuity references, capabilities и конфликты; неподдерживаемые операции блокируются без silent substitution. Canonical JSON, task fingerprints и baseline checkpoints детерминированы.
- **Runtime / production изменения:** отсутствуют. Нет LLM/MWS, сети, скачивания медиа, разрешения реальных assets, WAV timing, PNG/MP4 render, CLI или переключения production path. Старые compiler/execution и renderer modules не изменялись.
- **Проверки:** `py_compile` успешно; целевые тесты **44 passed**; полный набор проекта **548 passed**; `git diff --check` без замечаний; source-level review относительно CP-01, CP-02 и фактического runtime API завершён.
- **Commit:** `37feccdd6353a80b64b28fcfc9975abace94cd42` (`Implement deterministic CP-03 execution plan compiler`).
- **Push:** `origin/feature/autonomous-video-engine-2` подтверждён; после push локальный и удалённый HEAD совпали.
- **Остатки:** authoring → runtime adapter остаётся CP-06; CP-03 создаёт только dry-run dispatch plan и не подключается к production path.
- **Следующий этап:** CP-04 — Preview и проверка исполнимости.

### CP-04 — Preview и проверка исполнимости

- **Статус:** **завершена; source-level review, локальная регрессионная и визуальная приёмка, отдельные commit и push подтверждены.**
- **Commit:** `bf7a9ffae75c638a5d38b72294e8fca95ddf14b2` (`Implement bounded Visual Story V2 preview for CP-04`).
- **Push:** `origin/feature/autonomous-video-engine-2` подтверждён; после push локальный и удалённый HEAD совпадают.
- **Задача и границы:** ограниченный asset-free preview для существующего runtime `kadr_farm.visual_story_v2.VisualStoryPlan`; CP-01 authoring models и CP-03 `ExecutionPlan` не преобразуются, authoring → runtime adapter остаётся CP-06.
- **Состав:** `src/kadr_farm/visual_story_preview_v2.py`, `tests/test_visual_story_preview_v2.py`, `examples/visual_story_v2_preview_cp04.json`, `docs/39_VISUAL_STORY_PREVIEW_CP04_RU.md` и этот мастер-документ.
- **Результат Chromium:** 3 shots со статусом `passed`, создано 9 PNG snapshots, контактный лист и `preview_audit.json`; renderer — `professional_browser_v2`.
- **Анимация:** `count_up` снимает отдельное состояние `progress` на 25% временного интервала; в проверочном примере начальное, промежуточное и финальное значения счётчика — `0`, `2`, `3`, а SHA-256 промежуточного и финального PNG различаются.
- **Audit и визуальная проверка:** `policy_diagnostics` пуст; capability reports — `supported`; AutoFit `unresolved_count = 0`; geometry errors и warnings отсутствуют; DOM object IDs совпали с runtime inventory; `.engine-error` не обнаружен; контактный лист визуально принят.
- **Проверки:** `py_compile` успешно; целевые тесты **15 passed**; полный набор проекта **563 passed**; реальный Chromium preview — `passed`; `git diff --check` без замечаний.
- **Runtime / production изменения:** отсутствуют. Не использовались WAV/audio timing, сеть и внешние assets, MWS/LLM, FFmpeg/MP4; существующие renderer, AnimationEngine, runtime schema, CLI и production pipeline не переключались.
- **Остатки и non-goals:** CP-04 проверяет ограниченный preview существующего runtime plan; она не реализует authoring → runtime adapter (CP-06), Global/Scene Directors (CP-05) или сквозной E2E production workflow (CP-07).
- **Следующий этап:** CP-05 — Global/Scene Directors; начать его после отдельного commit/push CP-04 и подтверждения совпадения локального и удалённого HEAD.

## 10. Текущий рабочий статус

| Контрольная точка | Статус |
|---|---|
| Исходный Professional Renderer V2 baseline `414d08c` | Подтверждён локально и на GitHub |
| CP-00: мастер-документ | **Завершён**; commit `12fffcf4de4c4978ebfe66e7a549ab521fcf8105`, push подтверждён |
| CP-01: строгие authoring-модели и fixtures | **Завершён**; commit `8884c9509a1310ae687e4f9d8e87d41394de3df9`, push подтверждён |
| CP-02: capability registry | **Завершён**; commit `0d1930dcd3d82b8767e55d94e09536ec90127eae`, push подтверждён |
| CP-03: compiler dry-run | **Завершён**; commit `37feccdd6353a80b64b28fcfc9975abace94cd42`, push подтверждён |
| CP-04: preview | **Завершён**; commit `bf7a9ffae75c638a5d38b72294e8fca95ddf14b2`, push подтверждён |
| CP-05: Global/Scene Directors | Не начат |
| CP-06: V2 renderer adapter | Не начат |
| CP-07: E2E приёмка | Не начат |

**Следующее разрешённое действие:** начать CP-05 — Global/Scene Directors. CP-04 завершена и опубликована отдельным commit `bf7a9ffae75c638a5d38b72294e8fca95ddf14b2`; локальная и удалённая ветки совпадают. Authoring → runtime adapter остаётся задачей CP-06; production path не переключается до прохождения соответствующего adapter gate.

## Appendix A. Ссылки на ключевые материалы

- `docs/35_KADRFARM_STAGE_I_MASTER_PLAN_RU.md` — этот мастер-план; главный приоритет по направлению и checkpoint status.
- `docs/36_VISUAL_STORY_2_CONTRACT_FREEZE_RU.md` — frozen authoring contract 2.0 и точные границы моделей.
- `docs/34_VISUAL_STORY_CONTRACT_V2_RU.md` — исходная архитектурная спецификация/draft.
- `docs/32_VISUAL_STORY_TWO_PASS_CONTRACT_RU.md` — прежняя двухпроходная спецификация; использовать только совместимые идеи, без квот 1.x.
- `docs/30_VISUAL_STORYTELLING_PILOT_2026-11-18_RU.md` — выводы пилота.
- `docs/VISUAL_STORY_V2_PROFESSIONAL_RENDERER_RU.md` — существующий Professional Renderer V2.
- `src/kadr_farm/visual_story.py` и `visual_story_prompts.py` — legacy authoring contract 1.x.
- `src/kadr_farm/visual_story_v2.py` — отдельный действующий runtime contract V2; не смешивать с новым authoring package.
- `src/kadr_farm/visual_story_contract_v2/` — новый двухпроходный authoring contract Stage I.

## Appendix B. Зафиксированное сообщение проекта

KadrFarm должен сначала проектировать цельную визуальную историю ролика, затем описывать конкретные композиции и действия, после чего детерминированно компилировать их в задания реальным исполнителям. Качество режиссуры определяется ясностью и выразительностью того, что увидит зритель, а не количеством эффектов, кадров или сложностью JSON. Работающий Professional Renderer V2 сохраняется как производственная основа и не подвергается риску до тех пор, пока новый путь не докажет свою корректность отдельными тестами и сквозной приёмкой.
