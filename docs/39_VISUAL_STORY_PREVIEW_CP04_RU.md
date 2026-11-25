# KadrFarm — CP-04: Preview и проверка исполнимости

- **Статус:** локальный кандидат CP-04; приёмка ожидает запуска на рабочей машине и визуального просмотра контактного листа.
- **Контрольная точка-основание:** `6547a6c7ef89c7a706492256173770a90d4dff46` (`Record CP-02 and CP-03 acceptance in master plan`).
- **Ветка:** `feature/autonomous-video-engine-2`.

## 1. Назначение

CP-04 проверяет, что ограниченный набор композиций действительно строится существующим **Professional Browser Renderer V2**: браузер получает точные runtime-объекты, события изменяют адресованные состояния, текст проходит AutoFit, а геометрия проходит browser QC. Результат — PNG-снимки отдельных состояний, контактный лист и машиночитаемый аудит.

CP-04 — инструмент визуальной инспекции, а не новый production renderer и не интеграция authoring-конвейера.

## 2. Жёсткая граница входного контракта

Инструмент принимает только `kadr_farm.visual_story_v2.VisualStoryPlan` — существующий runtime contract V2.

Он **не принимает и не преобразует**:

- CP-01 `visual_story_contract_v2.VisualStoryPlan` / `SceneCompositionPlan`;
- CP-03 `ExecutionPlan` / `CompilationResult`;
- произвольные словари, которые можно было бы молча трактовать как runtime plan.

Преобразование authoring → runtime остаётся отдельной задачей CP-06. CP-04 не создаёт временный, неполный или скрытый adapter.

## 3. Разрешённый preview-профиль

Используется один исполнитель: `professional_browser_v2` из CP-02 registry.

Разрешённые kinds:

- `text`, `shape`, `icon`, `callout`, `group`;
- `node_graph`, `chart`, `table`, `formula`, `counter`.

Ограниченные subtype для preview:

- shape: `rectangle`, `rounded_rectangle`, `circle`, `ellipse`, `line`, `arrow`, `polygon`;
- icon: `check`, `cross`, `warning`, `play`;
- chart: `bar`, `line`;
- node graph: `left_to_right`, `top_to_bottom`.

Разрешённые actions: `appear`, `fade_in`, `fade_out`, `highlight`, `count_up`, `update_text`. Это узкий прямой набор runtime actions, пересекающийся с зафиксированной CP-03 direct-action policy. `move`, `scale`, `draw`, `reveal_row`, stable subtargets и любые неописанные подмены в CP-04 не добавляются.

Ограничения запуска CP-04: до 6 shots в плане, до 24 объектов на shot с учётом вложенных group children, до 8 событий на shot, конец расписания вместе с preview tail — не позже 12 секунд. Эти пределы относятся к локальному preview-инструменту, а не к максимальной ёмкости renderer.

## 4. Media и внешние побочные эффекты

`image` и `video` намеренно запрещены, включая вложенные объекты группы и poster assets. Для CP-04 применяются только самодостаточные графические/типографические объекты.

Инструмент:

- не вызывает `VisualAssetResolver` / `ProfessionalAssetResolver`;
- не обращается к Pixabay, HTTP(S), MWS или LLM;
- не читает WAV, не измеряет длительности речи и не утверждает audio sync;
- не вызывает FFmpeg и не записывает MP4;
- не меняет `visual_professional_renderer_v2.py`, pipeline, runtime-модели, существующий CLI или production path.

`SpeechAnchor.offset_sec` в демонстрационном runtime plan используется только как **ручная шкала preview-состояний**. Это не measured WAV timing и не доказательство речевой синхронизации.

## 5. Исполнение и выходы

Для каждого shot:

1. CP-02 `validate_shot_capabilities(..., professional_browser_v2)` блокирует неподдерживаемые сочетания.
2. Существующий `ProfessionalBrowserRenderer.preflight_shot()` выполняет Chromium preflight, AutoFit и browser geometry QC без записи видео.
3. Проверяется, что preflight не зарегистрировал медиа; unresolved AutoFit и geometry errors блокируют shot.
4. Из того же подготовленного HTML в Chromium снимаются: initial state, середина каждого preview event и итоговое состояние.
5. Проверяется соответствие DOM object IDs полному runtime object inventory, включая nested group children; `.engine-error` узлы запрещены.
6. Из PNG создаётся контактный лист; рядом сохраняется JSON-аудит.

Структура выходов в каталоге `preview_<plan_sha256_prefix>/`:

```text
preview_<hash>/
    preflight/<shot_id>/<shot_id>.preflight.html
    frames/*.png
    preview_contact_sheet.jpg
    preview_audit.json
```

Аудит фиксирует SHA-256 исходного runtime plan, renderer и версию capability registry, inventory, capability report, AutoFit manifest, geometry report, расписание sample states, пути/хеши PNG и явные флаги отсутствия WAV/network/FFmpeg/MP4/adapter.

Статус `passed_with_warnings` допустим только при отсутствии ошибок geometry и unresolved AutoFit; все предупреждения сохраняются в аудите и требуют визуального просмотра.

## 6. Демонстрационный пример

`examples/visual_story_v2_preview_cp04.json` содержит три искусственных runtime shots: композиционная иерархия, bar chart + table, node graph + counter + formula. Табличные и графические значения — демонстрационные, не реальные измерения. В примере нет WAV и внешних медиа.

Запуск из корня проекта после установки CP-04 candidate:

```powershell
python -m kadr_farm.visual_story_preview_v2 `
  .\examples\visual_story_v2_preview_cp04.json `
  --output-dir .\output\cp04_preview
```

Для Playwright/Chromium используется уже предусмотренная optional dependency проекта:

```powershell
python -m pip install -e ".[visual]"
python -m playwright install chromium
```

## 7. Проверки CP-04

Целевой набор:

```powershell
python -m py_compile `
  .\src\kadr_farm\visual_story_preview_v2.py `
  .\tests\test_visual_story_preview_v2.py

python -m pytest -q .\tests\test_visual_story_preview_v2.py
```

Регрессия всего проекта:

```powershell
python -m pytest -q
git diff --check
```

Browser smoke в тесте автоматически пропускается, только если Playwright/Chromium недоступен. Для приёмки CP-04 одного пропуска недостаточно: нужно вручную запустить пример на рабочей машине, открыть `preview_contact_sheet.jpg` и проверить `preview_audit.json`. Визуальный просмотр должен подтвердить отсутствие подмены объектов/текста, ошибок рендера, обрезания и неприемлемой читаемости.

## 8. Не входит

CP-04 не доказывает совместимость CP-01 authoring plans с runtime, не исполняет CP-03 `RenderTask`, не загружает реальные assets, не производит WAV/MP4, не является E2E и не разрешает production switch. Эти границы сохраняются до CP-05/CP-06/CP-07.
