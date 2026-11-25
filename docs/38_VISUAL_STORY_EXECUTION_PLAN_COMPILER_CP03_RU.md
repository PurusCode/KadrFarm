# KadrFarm — CP-03: детерминированный компилятор ExecutionPlan

- **Контрольная точка:** CP-03, первая реализация deterministic dry-run compiler
- **Версия компилятора:** `1.1.0`
- **Исходный Git checkpoint:** `0d1930dcd3d82b8767e55d94e09536ec90127eae`
- **Рабочая ветка:** `feature/autonomous-video-engine-2`
- **Вход:** frozen authoring contract `visual_story_contract_v2/`, authoritative storyboard
- **Выход:** frozen CP-01 `ExecutionPlan` + deterministic compilation checkpoint
- **Новый модуль:** `visual_story_execution_plan_compiler_v2.py`
- **Старый модуль:** `visual_scene_compiler_v2.py` — не изменяется и не заменяется

## 1. Назначение и разделение двух компиляторов

В проекте уже существует `visual_scene_compiler_v2.py`. Он принимает существующий runtime `visual_story_v2.VisualStoryPlan` и фактически измеренные длительности speech units, после чего рассчитывает временные интервалы shots и events. Этот измерительный timeline compiler не является новым CP-03 компилятором.

CP-03 добавляет отдельный `visual_story_execution_plan_compiler_v2.py`. Он принимает authoritative storyboard, Global Director plan и ровно по одному Scene Director plan на каждую исходную сцену. Результат — детерминированная структура `visual_story_contract_v2.ExecutionPlan`.

Новый модуль не выполняет `model_dump()` authoring-моделей в runtime `visual_story_v2` и не создаёт runtime `Shot`/`VisualStoryPlan`. Адаптер остаётся отдельной задачей CP-06.

## 2. Контракт входа

Обязательные аргументы:

- `storyboard` — исходный JSON-compatible mapping; `scenes[].id` и `scenes[].voice_text` остаются авторитетным источником;
- `global_plan` — `VisualStoryPlan` или соответствующий объект для строгой Pydantic-валидации;
- `scene_plans` — непустая упорядоченная последовательность `SceneCompositionPlan`;
- `run_id` — стабильный идентификатор запуска;
- `renderer_id` — явный target из CP-02; автоматического выбора или fallback нет;
- `output_spec` — явная финальная конфигурация видео;
- `asset_provider_allowlist` — явный список разрешённых источников для image/video objects.

Компилятор использует уже существующие cross-contract валидаторы CP-01: canonical storyboard hash и полный порядок global scenes; точное покрытие source scenes; SHA-256 `voice_text`; точные Unicode character spans speech anchors; continuity references и их направление.

Если структурная проверка всего bundle не пройдена, компилятор возвращает `ExecutionPlan(status="blocked")` без AssetRequests и RenderTasks. Он не пытается частично исполнять неконсистентный source bundle.

## 3. Как строится план

### 3.1. AssetRequest

Для каждого поддержанного image/video object создаётся отдельный AssetRequest с устойчивым ID, `scene_id`, `shot_id`, `object_id`, subject/scene/action description, исходными поисковыми запросами, ориентацией, crop intent и явным provider allowlist. Для video сохраняется qualitative `desired_duration`.

AssetRequest сохраняет `scene_id`, `shot_id`, `object_id`. Компилятор не выбирает provider и не скачивает медиа.

### 3.2. RenderTask — единица исполнения равна целому shot

Создаётся **один RenderTask на весь shot**, а не на каждый VisualObject. Source-level review подтвердил, что оба существующих runtime V2 исполнителя получают все объекты shot как единую композицию: `ProfessionalBrowserRenderer.render_shot(plan, shot, ...)` записывает один HTML-композит, а `AnimationEngine.render_shot(plan, shot, ...)` передаёт весь `shot.objects` в SceneRenderer для каждого кадра. Отдельных MP4/PNG render jobs на каждый объект эти исполнители не создают.

В замороженной CP-01 модели `RenderTask.object_id` обязателен. Поэтому CP-03 использует зарезервированный синтетический идентификатор `shot_composition`; он **не является авторским объектом**. `task_id` детерминирован от `(scene_id, shot_id, shot_composition)`. Полный shot, все его объекты и события входят в `input_fingerprint`.

Group membership — внутренняя структура одной композиции, а не межзадачная зависимость. Поэтому CP-03 не создаёт фиктивные дочерние RenderTasks и не заполняет `depends_on` ссылками на объекты. AssetRequests остаются отдельными записями на каждый media object. Компилятор создаёт явный `TaskAssetBinding` в `CompilationResult`: он связывает shot-level task с request IDs по `(scene_id, shot_id)`; эти request IDs также включаются в fingerprint shot task. Поле `depends_on` сохраняется для будущих реальных межзадачных зависимостей.

Task хранит явный `RendererTarget` с версией из CP-02 и выходной MIME, объявленный исполнителем. Professional Browser Renderer V2 объявляет `video/mp4`. Pillow frame executor объявляет `image/png`: в его случае это MIME отдельных кадров в наборе кадров shot, а не обещание одного PNG-файла. `ExecutionOutputSpec` остаётся финальной спецификацией ролика MP4.

### 3.3. Capability gate и события

Каждый object kind/subtype проверяется через CP-02 registry. Renderer target задаётся вызывающей стороной и никогда не меняется автоматически.

Пока разрешаются только точные runtime action names, для которых CP-03 не должен угадывать геометрию или терять обязательные параметры: `appear`, `fade_in`, `fade_out`, `highlight`, `count_up`, `update_text`. Совпадение имени само по себе не считается достаточным для остальных действий.

Дополнительно:

- `move` блокируется: authoring задаёт семантические `destination_region`/`destination_anchor`, а runtime action требует числовые x/y; геометрического lowering в CP-03 нет;
- `scale` блокируется: authoring задаёт semantic `target_size`, а runtime action использует числовой factor; коэффициент нельзя выдумывать;
- `reveal_row` блокируется: authoring адресует стабильный `row_id`, runtime требует целочисленный `row_index`;
- `draw_edge`, `draw_path`, `plot_line`, `reveal_node` и другие graphic actions не заменяются общим `draw`;
- события, направленные на дочерний объект внутри group, блокируются: runtime events могут адресовать только top-level объекты shot;
- `highlight` разрешён только с runtime-совместимыми `tone="accent"` и `intensity="normal"`; остальные параметры нельзя незаметно потерять;
- конфликтующие visibility/move/scale intents на одном anchor блокируются, если автор не указал явный `stagger="sequential"` без overlap; в таком случае порядок событий в исходном массиве считается намеренной последовательностью;
- неподдерживаемые действия блокируются без визуальной подмены.

CP-03 также переносит важные CP-02 guardrails на authoring models, потому что runtime validator CP-02 принимает другую модель данных: multi-series pie/donut (оба renderer) и multi-series Pillow bar блокируются. Для Pillow дополнительно блокируются top-level таблицы и прогрессивные line/arrow, multi-category bar, длинные line charts и node graphs с edges: runtime engine инициализирует их неполным состоянием, а authoring contract не имеет точного действия `draw`/целочисленного `row_index`, которым можно довести композицию до полного состояния. Media глубже прямого child группы блокируется для Pillow asset resolver.

Компилятор сохраняет полные исходные планы в fingerprints. Он не переводит semantic geometry в пиксели, не рассчитывает точное время по длине текста и не материализует значения внешнего provenance registry. Статус `ready` означает только, что dry-run прошёл структурные и capability gates CP-03; он не означает, что media найдены, внешние значения разрешены или renderer payload уже готов к реальному исполнению.

### 3.4. Warnings о внешних значениях

`value_ref` и связанные источники числовых данных пока не разрешаются к внешнему авторитетному реестру. Компилятор явно добавляет warning `external_value_refs_not_resolved`; это не считается доказательством доступности/истинности значения. Аналогично `count_up` и `update_text` оставляют unresolved value/content references как warnings. Поздний этап обязан разрешить их до реального исполнения.

## 4. Детерминизм и fingerprints

JSON канонизируется так: UTF-8, `ensure_ascii=False`, ключи сортируются, разделители `(',', ':')`, NaN/Infinity запрещены.

- `global_plan_sha256` — SHA-256 канонического сериализованного GlobalPlan;
- `scene_plan_sha256[scene_id]` — SHA-256 каждого SceneCompositionPlan;
- task ID — детерминированный digest от `(scene_id, shot_id, object_id)`;
- `input_fingerprint` — SHA-256 от версии компилятора и реестра, renderer ID/version, source hashes, Global Scene Direction, полного shot plan, sentinel `shot_composition`, списка `asset_request_ids`, provider inputs, output spec и dependencies.

`run_id` не входит в input fingerprint: он разделяет запуски, но не меняет тождество входного задания.

### 4.1. CompilationCheckpoint

Компилятор создаёт baseline checkpoint с hash всего ExecutionPlan, source hashes и соответствием `task_id → input_fingerprint`. Проверка совместимости запрещает использовать checkpoint при изменении plan или любого task fingerprint; неизвестные completed task IDs отклоняются.

CP-03 только формирует identity checkpoint. Он не запускает tasks и не утверждает, что какие-либо рендеры завершены: `completed_task_ids` в новом checkpoint пуст. Реальный журнал выполненных этапов, атомарная запись прогресса и resume во время рендера относятся к orchestration/execution этапам.

## 5. Dry-run: запрещённые побочные эффекты

Компилятор является pure in-memory функцией. В нём нет:

- сетевых запросов, MWS/LLM или provider routing;
- скачивания, открытия или разрешения image/video assets;
- чтения WAV, расчёта секунд/кадров или выдуманной синхронизации;
- вызовов Pillow/Chromium/FFmpeg и создания MP4/PNG;
- записи файлов, изменения `.env`, CLI или production pipeline.

`serialize_compilation_result()` только возвращает canonical JSON string. Запись dry-run отчёта на диск может быть добавлена отдельным безопасным интерфейсом, но не является скрытым side effect компилятора.

## 6. Ограничения первой реализации

- Один output task — детерминированная dispatch-запись целого shot. `object_id="shot_composition"` — зарезервированный sentinel из-за обязательного поля в frozen CP-01 `RenderTask`, а не реальный VisualObject.
- Dispatch records не являются готовыми runtime `Shot` и не содержат полноценный renderer payload. Для CP-06 исходный immutable storyboard/GlobalPlan/ScenePlans должны быть повторно предоставлены и проверены по SHA-256, прежде чем адаптер сможет построить runtime payload.
- `CompilationResult.asset_bindings` явно соединяет каждый shot task с IDs соответствующих AssetRequests. Binding manifest — детерминированная проекция ExecutionPlan и входит в canonical serialization результата; он не является дополнительным CP-01 RenderTask.
- Реальные asset-resolution tasks и их orchestration dependencies появятся позже; CP-03 ничего не загружает.
- Стабильные subtarget IDs остаются заблокированными по CP-02.
- Внешние `value_ref`/`content_ref` не разрешаются к реальным значениям.
- Точное медиа-тайминг вытекает только из WAV/alignment на отдельном этапе; длина текста не используется.
- Renderer adaptation, geometry/layout lowering, media resolution, measured audio schedule, preview и real render не входят в этот модуль.

## 7. Проверки CP-03

Текущая целевая матрица: **44 теста**. Они подтверждают детерминированную сериализацию и fingerprints, coverage/hash/anchor/continuity gates, exact capability/action checks, отсутствие silent aliasing/fallback, AssetRequest generation и explicit TaskAssetBinding, один shot-level RenderTask, групповые и вложенные ограничения, renderer-specific progressive-state guards, конфликтующие и явно последовательные события, blocked plan behavior, checkpoint compatibility и отсутствие I/O side effects.

CP-03 не принимается только по числу тестов: source-level review обязан подтвердить, что старый `visual_scene_compiler_v2.py`, `visual_execution_v2.py`, runtime models, renderers, asset resolver, CLI и production path остались без изменений.
