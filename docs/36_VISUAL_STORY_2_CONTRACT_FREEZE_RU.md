# KadrFarm — Visual Story 2.0: замороженный контракт режиссуры

- **Версия контракта:** 2.0
- **Дата фиксации проектного решения:** 24.11.2026
- **Статус:** **принят; CP-01 завершена, commit и push подтверждены**
- **Пакет:** `src/kadr_farm/visual_story_contract_v2/`
- **Тесты:** `tests/test_visual_story_contract_v2.py`
- **Главный маршрутный документ:** `docs/35_KADRFARM_STAGE_I_MASTER_PLAN_RU.md`
- **Исходный draft:** `docs/34_VISUAL_STORY_CONTRACT_V2_RU.md`

> Этот документ описывает новый двухпроходный **authoring contract**. Он не заменяет существующие модели, renderer, CLI или production pipeline. CP-01 считается закрытой только после локального прогона целевых и полных тестов, проверки diff, commit и push.

---

## 1. Разделение существующих контрактов

В репозитории уже есть две реализации, и их нельзя смешивать с новым authoring contract.

| Модуль | Фактическая роль | Решение для Stage I |
|---|---|---|
| `visual_story.py` + `visual_story_prompts.py` | Старый двухпроходный authoring contract 1.x. В старых промптах присутствовали требования recurring motifs / developing share. | Сохранить как legacy 1.x; не подключать к новому пакету и не переносить квоты. |
| `visual_story_v2.py` | Существующий исполнимый runtime contract: `SpeechUnit`, `Shot`, `VisualObject`, `VisualEvent`, `VisualStoryPlan`; события содержат offset/duration, слой содержит нормализованную геометрию. | Сохранить без изменений. На эти модели завязаны компилятор, renderer, animation engine, asset resolver и production renderer. |
| `visual_story_contract_v2/` | Новый двухпроходный authoring contract: глобальная режиссура всего ролика + детальная режиссура каждой сцены, с проверкой ссылок и будущими исполнительными заданиями. | Новая независимая схема. Не импортирует старые runtime-модели и не подключается к CLI на CP-01. |

Совпадение слова `V2` и значения `schema_version = "2.0"` не означает совместимость JSON. Authoring contract и существующий runtime contract имеют разное назначение, состав полей, временную модель и lifecycle. Преобразование между ними должно быть отдельным адаптером на CP-06; не выполнять неявный `model_dump()` одного контракта в другой.

## 2. Авторитетный источник и неизменность речи

Текущий `storyboard.py` выдаёт упорядоченный массив `scenes`. Идентификатором сцены является `scenes[].id` (например, `s_001`), а текст диктора находится в `scenes[].voice_text`. Контракт не предполагает существование `beat_id`.

### 2.1. Global Director

`VisualStoryPlan` создаётся одним планом на весь storyboard и содержит:

- `plan_id`, `title` и SHA-256 полного storyboard;
- центральную визуальную мысль (`visual_thesis`) и общий ход визуальной истории (`arc_summary`);
- единый `style`;
- необязательные крупные `arc`;
- необязательные `motifs`;
- ровно по одному `GlobalSceneDirection` на каждую исходную сцену в исходном порядке.

Каждая `GlobalSceneDirection` указывает исходный `scene_id`, драматургическую роль, визуальную цель, стратегию, основные визуальные режимы, необязательную ориентировочную вилку количества shots и связь с более ранней сценой.

Глобальная режиссура не содержит и не может заменить `voice_text`. Поле `voice_text` неизвестно строгой модели и будет отклонено.

### 2.2. Scene Director

`SceneCompositionPlan` создаётся отдельно для каждой исходной сцены. В нём находятся `scene_id`, SHA-256 исходного `voice_text`, цель сцены и один или несколько `ShotPlan`.

Каждый shot включает цель, конкретное описание видимого результата, композицию, объекты, речевые anchors, события, continuity links и политику отказа/восстановления.

Новый Scene Director не получает право изменять текст, идентификатор или порядок исходной сцены. Перед использованием плана валидатор проверяет SHA-256 `voice_text`, точность каждого excerpt и границы диапазона.

## 3. Хеши и индексы текста

Два отпечатка имеют разные области действия:

1. `storyboard_sha256` — SHA-256 канонического UTF-8 JSON всего storyboard. Канонизация: `json.dumps(storyboard, ensure_ascii=False, sort_keys=True, separators=(",", ":"))`, затем UTF-8 и SHA-256.
2. `source_text_sha256` — SHA-256 исходной строки `voice_text` конкретной сцены в UTF-8, без предварительной нормализации, `strip()` или переписывания пробелов.

`SpeechAnchor.char_start` включителен, `char_end` исключителен. Это индексы Python-строки (Unicode code points), а не UTF-8 byte offsets и не миллисекунды. `text_excerpt` обязан в точности совпадать с `voice_text[char_start:char_end]`; якорь не может выходить за границы сцены или разрезать слово.

SHA нужен для обнаружения изменения исходника между режиссурой и последующими этапами. Он не доказывает фактическую истинность содержания.

## 4. Речевые якоря и время

Каждый `SpeechAnchor` содержит:

- `anchor_id`;
- `source_scene_id`;
- `char_start` и `char_end`;
- дословный `text_excerpt`;
- семантическую роль якоря (`opening`, `subject`, `action`, `contrast`, `emphasis`, `conclusion`, `pause`, `illustration`, `transition`).

Ни `SpeechAnchor`, ни `VisualEvent`, ни `TimingIntent` не содержат секунд, миллисекунд, frame index или длительности в числовой форме. Event trigger ссылается на `anchor_id`; `TimingIntent` содержит только качественное намерение (`instant/brief/medium/long/hold`, easing, stagger, overlap, hold-until anchor).

Точные секунды, frame boundaries и длительность событий появляются только в более позднем compiler/execution слое после анализа реально созданных WAV или проверенного alignment. Делить время по длине текста нельзя. При недостаточной точности система обязана сообщить о достигнутой точности, а не выдавать оценку за точную синхронизацию.

## 5. Пространство и содержимое объектов

Все модели строгие: `extra="forbid"`, проверка присваивания включена, NaN и Infinity запрещены. Объекты используют discriminated union по `kind`.

Поддерживаемые семейства:

| Kind | Типизированное содержимое |
|---|---|
| `image`, `video` | Предмет изображения/сцена/действие, поиск, ракурс, ориентация, crop intent; для video — desired action, motion intensity, qualitative duration и loop intent. |
| `text` | Точный текст, роль, emphasis spans, hierarchy, source refs. |
| `shape` | Тип геометрии, смысловые fill/stroke роли, опциональные привязки connectors к объектам. |
| `icon` | Стабильный `icon_id` и семантическое описание. |
| `callout` | Текст, тон, форма, target и source refs. |
| `group` | Список ID дочерних объектов, локальная компоновка и clip policy; ссылки и циклы проверяются. |
| `node_graph` | Идентифицированные nodes/edges, тип связи и layout intent; endpoints должны существовать. |
| `chart` | Тип диаграммы, категории и series; approved data идут через `value_refs`, иллюстративные числа — через `illustrative_values` и обязательную видимую метку. |
| `table` | Типизированные columns/rows/cells; value refs отделены от иллюстративных ячеек. |
| `formula` | Ограниченное арифметическое выражение; все числовые операнды являются именованными refs. Произвольный Python-код и незарегистрированные числовые константы запрещены. |
| `counter` | Approved/derived `value_ref` либо явное illustrative value с меткой. |

### 5.1. Provenance значений

Числовой текст, числовые подписи callout и numeric labels графа требуют `source_ref`/`source_refs`. `chart`, `table`, `formula` и `counter` отделяют ссылки на проверенные значения от иллюстративных данных. Иллюстративные данные должны иметь явную маркировку и не выдаваться за измерения.

На CP-01 проверяется форма и наличие ссылок, а не истинность или доступность внешнего реестра значений. Проверка разрешения `value_ref` к авторитетному источнику и provenance registry — ответственность отдельного последующего этапа; простой текстовый ID не считается подтверждением факта.

### 5.2. Семантическая геометрия

`LayerSpec` допускает `region`, `anchor`, `size`, `overlap_policy` и необязательный `z_index`. В ней намеренно нет x/y координат, pixel width/height, CSS, HTML, SVG path code, абсолютного времени или кода для выполнения. Renderer рассчитывает реальные пиксели, размеры, safe areas и техническую геометрию.

Смысловые пространственные связи между объектами задаются отдельно. Это указания режиссёра, а не гарантия геометрической выполнимости; позже их проверяет capability/compiler/visual QC.

## 6. События и адресация

Каждое `VisualEvent` имеет уникальный ID, `EventTarget`, trigger на существующий `SpeechAnchor` и качественный `TimingIntent`.

Типизированные группы действий:

- visibility: `appear`, `reveal`, `fade_in`, `fade_out`, `hide`, `remove`;
- emphasis: `highlight`, `dim`, `pulse`, `outline`, `shake`;
- transform: `move`, `scale`, `rotate`, `transform`, `morph`, `crop_reframe`;
- graphic: `draw_path`, `draw_edge`, `reveal_node`, `reveal_row`, `reveal_column`, `fill_progress`, `grow_bar`, `plot_line`;
- content: `count_up`, `update_text`, `update_value`, `swap_content`;
- media: `pan`, `zoom`, `ken_burns`, `play`, `freeze_frame`, `crossfade`.

Поля действия и параметры типизированы, обязательные параметры проверяются, неизвестные параметры отклоняются. Event target должен ссылаться на существующий объект; subtarget — на реально объявленный node/edge/row/column/series/group child. Проверяется совместимость действия с kind объекта, включая специальные действия графов/таблиц/диаграмм.

Этот перечень — **семантический язык намерений**, а не декларация, что каждый renderer уже умеет его исполнять. Реальную поддержку проверит CP-02.

## 7. Continuity без квот

`ContinuityLink` может описывать `object`, `composition`, `action`, `space`, `visual_metaphor`, `contrast`, `callback`, `resolution` и `transition`.

- Входящая связь указывает на ранее существовавшую сцену/shot.
- Исходящая связь может указать на будущую сцену/shot.
- Для явной связи `object` обязательно задаются object reference и `continuity_id`.
- Если `continuity_id` указан для другой связи, он должен совпадать с ID источника и существовать у объекта текущего shot.
- Для `contrast`, `resolution`, `transition` и других не-object связей разрешено не указывать `continuity_id` и не переносить объект.
- Локальный объект по умолчанию завершается на границе shot; только явно объявленная идентичность продолжается дальше.

Не существует минимальной доли developing scenes, обязательного recurring motif, обязательного object carry-over или процента continuity. Пустой `motifs` и пустые `motif_refs` корректны.

## 8. Исполнительные модели CP-01

`execution.py` содержит только структуру будущих исполнительных данных:

- `RendererCapabilities` — перечень поддерживаемых kinds/actions/chart types/formats и лимиты;
- `RendererTarget` — ID и версия выбранного renderer;
- `AssetRequest` — тип медиа, описание, поисковые запросы, crop intent, provider allowlist и качественная длительность видео;
- `RenderTask` — объект/shot/renderer, входной fingerprint и зависимости;
- `ExecutionPlan` — fingerprints исходных планов, версия registry, requests/tasks, output spec, diagnostics и `ready/blocked` статус.

ExecutionPlan проверяет уникальность ID, разрешённость task dependencies и отсутствие циклов. Если есть error diagnostic, `status=ready` запрещён; `status=blocked` требует хотя бы одну error diagnostic.

Это пока **модели данных**, не capability registry и не компилятор. CP-01 не создаёт реальные render tasks, не скачивает assets, не рассчитывает время и не производит изображения/видео.

## 9. Отказ и fallback

`FallbackPolicy.unsupported_renderer_action` имеет только значение `block_execution`. Неподдерживаемый action нельзя молча заменить другой анимацией, карточкой, изображением или случайным stock asset.

Для отсутствующего asset разрешены только явно описанные варианты: retry search, alternative asset в пределах согласованного назначения, revise director plan или manual review. Любая смысловая замена должна быть разрешена явно и записана в audit. В противном случае run останавливается с диагностикой.

## 10. Валидация и fixtures

Публичные функции cross-validation:

- `validate_global_plan_against_storyboard` — canonical storyboard hash и точное совпадение scene ID/order;
- `validate_scene_composition_plan` — source scene ID, точный raw `voice_text` hash и точность каждого speech span;
- `validate_scene_plan_coverage` — ровно один SceneCompositionPlan на каждую исходную сцену в исходном порядке;
- `validate_continuity_references` — ссылки, направление, существование shot/object и continuity IDs;
- `validate_visual_story_bundle` — совместная проверка полного глобального и сценового проходов.

Fixture каталог содержит валидные примеры глобального плана, сложной многослойной сцены, контрастной сцены без carry-over, а также отдельные заведомо невалидные JSON для unknown field/enum, missing object, incompatible action и неверного диапазона anchor.

## 11. Границы CP-01 и следующий шаг

CP-01 добавляет только новый пакет, fixture/tests и документацию. Не изменяются:

- `visual_story.py`, `visual_story_prompts.py`, `visual_story_v2.py`;
- `visual_scene_compiler_v2.py`, `visual_execution_v2.py`;
- `visual_renderer_v2.py`, `visual_animation_v2.py`, `visual_asset_resolver_v2.py`;
- `visual_professional_renderer_v2.py`, AutoFit, Preflight, Geometry QC;
- production director, CLI, provider routing и `.env`.

Дальше разрешён только **CP-02: инвентаризация и формализация реальных renderer capabilities**. Для этого сначала сопоставляются kinds/actions/subtargets с `visual_renderer_v2.py`, `visual_animation_v2.py`, `visual_professional_renderer_v2.py`, `visual_asset_resolver_v2.py` и остальными реальными исполнителями. Нельзя зарегистрировать возможность, которую код и тест не подтверждают.

---

## Протокол проверки CP-01

Изолированная предварительная проверка автора пакета: **49 тестов прошли**; `compileall` прошёл; Pydantic JSON Schema generation прошла для `VisualStoryPlan`, `SceneCompositionPlan` и `ExecutionPlan`. Это не заменяет повторный запуск тестов в локальном клоне пользователя и полный regression suite.

Локальные acceptance checks владельца проекта 24.11.2026: `compileall` успешно; целевые тесты **49 passed in 0.28s**; полный набор **437 passed in 11.24s**; `git diff --check` не обнаружил whitespace ошибок (только уведомление Git о Windows LF/CRLF).

CP-01 окончательно закрыта: точный состав из 19 файлов проверен, commit `8884c9509a1310ae687e4f9d8e87d41394de3df9` создан, push подтверждён и локальная ветка синхронизирована с `origin/feature/autonomous-video-engine-2`. Новый пакет остаётся изолированным authoring contract и не подключается к production path.
