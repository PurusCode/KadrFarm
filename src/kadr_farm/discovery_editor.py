from __future__ import annotations

"""Generate an offline editorial HTML panel from a saved Discovery v2 report.

The page embeds raw YouTube API fields and therefore inherits the source report's
refresh/delete deadline. It makes no network requests and computes no derived
YouTube analytics, semantic topics, or language classifications.
"""

import datetime as dt
import json
from pathlib import Path
from typing import Any

from .common import read_json

SCHEMA_VERSION = "1.1"


def _parse_utc(value: str | None, field: str) -> dt.datetime:
    if not value:
        raise ValueError(f"В discovery.json отсутствует обязательное поле {field}")
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"Некорректная дата {field}: {value}") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"Дата {field} должна содержать часовой пояс UTC")
    return parsed.astimezone(dt.timezone.utc)


def _compact_video(item: dict[str, Any]) -> dict[str, Any]:
    video_id = str(item.get("video_id") or "").strip()
    if not video_id:
        raise ValueError("В unique_candidates найдено видео без video_id")
    contexts = []
    for context in item.get("found_in") or []:
        if not isinstance(context, dict):
            continue
        contexts.append({
            "market": str(context.get("market") or "unknown"),
            "stream_id": str(context.get("stream_id") or "unknown"),
            "stream_title": str(context.get("stream_title") or context.get("stream_id") or ""),
            "window_days": context.get("window_days"),
            # Stage 5.1 source provenance; older Stage 4 reports safely map to unknown.
            "order": str(context.get("order") or "unknown"),
            "query": str(context.get("query") or ""),
        })
    return {
        "video_id": video_id,
        "title": str(item.get("title") or ""),
        "channel_title": str(item.get("channel_title") or ""),
        "published_at": item.get("published_at"),
        "view_count": item.get("view_count"),
        "like_count": item.get("like_count"),
        "comment_count": item.get("comment_count"),
        "duration": item.get("duration"),
        "duration_seconds": item.get("duration_seconds"),
        "category_id": item.get("category_id"),
        "found_in": contexts,
    }


def _load_production_catalog(profiles_path: Path | None = None) -> dict[str, Any]:
    """Load the read-only Stage 5 production taxonomy installed by Stage 5.1."""
    candidates: list[Path] = []
    if profiles_path is not None:
        candidates.append(Path(profiles_path))
    else:
        candidates.extend([
            Path.cwd() / "config" / "production_profiles.json",
            Path(__file__).resolve().parents[2] / "config" / "production_profiles.json",
        ])
    path = next((candidate for candidate in candidates if candidate.is_file()), None)
    if path is None:
        raise FileNotFoundError(
            "Не найден config/production_profiles.json. Сначала установите Stage 5.1 v1.0.1."
        )
    catalog = read_json(path)
    if not isinstance(catalog, dict) or str(catalog.get("schema_version")) != "1.0":
        raise ValueError("production_profiles.json должен иметь schema_version 1.0")
    required_lists = (
        "profiles", "reference_formats", "production_fit", "blockers",
        "rights_status", "fact_check_status", "budget_status", "asset_strategies",
    )
    for key in required_lists:
        if not isinstance(catalog.get(key), list):
            raise ValueError(f"В production_profiles.json отсутствует корректный список {key}")
    profile_ids = [item.get("id") for item in catalog["profiles"] if isinstance(item, dict)]
    if len(profile_ids) != len(catalog["profiles"]) or len(profile_ids) != len(set(profile_ids)):
        raise ValueError("В каталоге производственных профилей найдены некорректные или повторяющиеся ID")
    # The offline page only needs the editorial taxonomy, not quota/runtime settings.
    return {key: catalog[key] for key in (
        "schema_version", "profile_statuses", "profiles", "reference_formats",
        "production_fit", "blockers", "rights_status", "fact_check_status",
        "budget_status", "asset_strategies",
    )}


def build_review_page(
    source: Path,
    output: Path,
    *,
    now: dt.datetime | None = None,
    profiles_path: Path | None = None,
) -> dict[str, Any]:
    """Create a self-contained review.html page from all unique candidates."""
    source = Path(source)
    output = Path(output)
    if not source.is_file():
        raise FileNotFoundError(f"Не найден файл Discovery: {source}")
    report = read_json(source)
    if not isinstance(report, dict) or str(report.get("schema_version")) != "2.0":
        raise ValueError("Поддерживается только discovery.json со schema_version 2.0")
    production_catalog = _load_production_catalog(profiles_path)
    raw_candidates = report.get("unique_candidates")
    if not isinstance(raw_candidates, list):
        raise ValueError("В discovery.json отсутствует список unique_candidates")

    generated = _parse_utc(report.get("generated_at_utc"), "generated_at_utc")
    deadline = _parse_utc(report.get("refresh_or_delete_by_utc"), "refresh_or_delete_by_utc")
    current = now or dt.datetime.now(dt.timezone.utc)
    if current.tzinfo is None:
        raise ValueError("now должен содержать часовой пояс")
    if current.astimezone(dt.timezone.utc) >= deadline:
        raise ValueError(
            "Срок обновления или удаления исходных YouTube API Data уже наступил. "
            "Сначала выполните новый discover или discover-cleanup."
        )

    seen: set[str] = set()
    videos: list[dict[str, Any]] = []
    for item in raw_candidates:
        if not isinstance(item, dict):
            continue
        video = _compact_video(item)
        if video["video_id"] in seen:
            continue
        seen.add(video["video_id"])
        videos.append(video)
    if not videos:
        raise ValueError("В discovery.json нет пригодных видео-кандидатов")

    payload = {
        "schema_version": SCHEMA_VERSION,
        "source_generated_at_utc": generated.isoformat().replace("+00:00", "Z"),
        "source_refresh_or_delete_by_utc": deadline.isoformat().replace("+00:00", "Z"),
        "candidate_count": len(videos),
        "videos": videos,
        "production_catalog": production_catalog,
        "editorial_schema_version": "1.1",
    }
    # Escape HTML-sensitive characters so API text cannot terminate the data script.
    embedded_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    embedded_json = (embedded_json.replace("&", "\\u0026")
                                 .replace("<", "\\u003c")
                                 .replace(">", "\\u003e")
                                 .replace("\u2028", "\\u2028")
                                 .replace("\u2029", "\\u2029"))
    page = _HTML_TEMPLATE.replace("__SOURCE_JSON__", embedded_json)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    temporary.write_text(page, encoding="utf-8")
    temporary.replace(output)
    return {
        "candidate_count": len(videos),
        "source_generated_at_utc": payload["source_generated_at_utc"],
        "source_refresh_or_delete_by_utc": payload["source_refresh_or_delete_by_utc"],
        "output": output,
    }


_HTML_TEMPLATE = r'''<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="referrer" content="no-referrer">
<title>AI Video Factory RU — редакторский отбор</title>
<style>
:root{color-scheme:light;--bg:#f3f5f8;--panel:#fff;--text:#172033;--muted:#64748b;--line:#dce2eb;--accent:#255bc7;--accent-soft:#e9f0ff;--green:#18794e;--red:#b42318;--amber:#9a6700;--shadow:0 2px 10px #1720330d}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:14px/1.45 "Segoe UI",Arial,sans-serif}button,input,select,textarea{font:inherit}button{cursor:pointer;border:1px solid var(--line);background:#fff;color:var(--text);border-radius:8px;padding:8px 11px}button:hover{border-color:#9aabc5;background:#f8faff}button.primary{background:var(--accent);border-color:var(--accent);color:#fff}button.danger{color:var(--red)}button.small{padding:5px 8px;font-size:12px}.wrap{max-width:1500px;margin:0 auto;padding:22px}.top{display:flex;justify-content:space-between;align-items:center;gap:16px;margin-bottom:16px}.brand{font-weight:700;font-size:17px}.sub{color:var(--muted);font-size:12px}.warning{background:#fff8e6;border:1px solid #efd58a;color:#624a00;padding:10px 13px;border-radius:10px;margin-bottom:14px}.stats{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:10px;margin:14px 0}.stat,.panel,.card{background:var(--panel);border:1px solid var(--line);border-radius:12px;box-shadow:var(--shadow)}.stat{padding:13px 15px}.stat b{font-size:24px;display:block}.stat span{font-size:12px;color:var(--muted)}.panel{padding:14px;margin-bottom:14px}.filters{display:grid;grid-template-columns:repeat(auto-fit,minmax(145px,1fr));gap:9px}.field{display:flex;flex-direction:column;gap:4px;min-width:0}.field label,.tiny-label{font-size:11px;color:var(--muted)}input,select,textarea{min-width:0;width:100%;padding:8px 9px;border:1px solid var(--line);border-radius:8px;background:#fff;color:var(--text)}.more-filters{display:grid;grid-template-columns:repeat(auto-fit,minmax(145px,1fr));gap:9px;margin-top:9px}.toolbar{display:flex;justify-content:space-between;align-items:center;gap:10px;flex-wrap:wrap;margin:12px 0}.toolbar-left,.toolbar-right{display:flex;gap:8px;align-items:center;flex-wrap:wrap}.results{display:grid;gap:10px}.card{padding:13px}.card-head{display:flex;justify-content:space-between;gap:12px}.title{font-weight:650;font-size:15px;line-height:1.35}.channel{color:var(--muted);font-size:12px;margin-top:3px}.chips{display:flex;gap:5px;flex-wrap:wrap;margin-top:7px}.chip{font-size:11px;color:#46546a;background:#eff2f7;padding:3px 7px;border-radius:999px}.metrics{display:flex;gap:13px;flex-wrap:wrap;color:#49566b;font-size:12px;margin:10px 0}.card-actions{display:flex;align-items:center;gap:7px;flex-wrap:wrap}.card-fields{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:8px;align-items:end;margin-top:9px}.multi-checks{display:grid;grid-template-columns:1fr;gap:5px;max-height:168px;overflow:auto;padding:8px;border:1px solid var(--line);border-radius:8px;background:#fff}.multi-option{display:flex;align-items:flex-start;gap:7px;padding:2px 3px;font-size:12px;line-height:1.35;color:var(--text);cursor:pointer}.multi-option input{width:16px;min-width:16px;height:16px;padding:0;margin:1px 0 0;flex:0 0 16px;accent-color:var(--accent)}.multi-option span{flex:1}.multi-checks:focus-within{border-color:var(--accent)}.editorial-flags{display:flex;gap:12px;flex-wrap:wrap;margin-top:8px;padding:9px;background:#f8faff;border:1px solid var(--line);border-radius:8px}.editorial-flags label{display:flex;gap:5px;align-items:center;font-size:12px;color:var(--muted)}.editorial-flags input{width:auto}.stage5-note{background:#eef4ff;border:1px solid #cfddfb;color:#29436d;padding:10px 13px;border-radius:10px;margin-bottom:14px}.review-check{display:flex;gap:6px;align-items:center;white-space:nowrap;font-size:12px;color:var(--muted);padding:8px 2px}.review-check input{width:auto}.status{font-size:11px;font-weight:650;padding:4px 8px;border-radius:20px;background:#eff2f7;color:#4b5563}.status.selected{background:#e8f7ef;color:var(--green)}.status.later{background:#fff5db;color:var(--amber)}.status.rejected{background:#fff0ef;color:var(--red)}.pagination{display:flex;justify-content:center;align-items:center;gap:10px;margin:18px 0}.muted{color:var(--muted)}.theme-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:9px;margin-top:10px}.theme{border:1px solid var(--line);border-radius:9px;padding:10px}.theme strong{display:block}.theme small{color:var(--muted)}.empty{padding:28px;text-align:center;color:var(--muted);background:#fff;border:1px dashed var(--line);border-radius:10px}.footer{font-size:11px;color:var(--muted);margin:18px 0}.hidden{display:none!important}
@media(max-width:1050px){.filters{grid-template-columns:repeat(3,minmax(0,1fr))}.more-filters{grid-template-columns:repeat(3,minmax(0,1fr))}}
@media(max-width:650px){.wrap{padding:12px}.top{align-items:flex-start;flex-direction:column}.stats{grid-template-columns:repeat(2,minmax(0,1fr))}.filters,.more-filters{grid-template-columns:repeat(2,minmax(0,1fr))}.card-fields{grid-template-columns:1fr 1fr}.card-fields .note-field{grid-column:1/-1}.review-check{grid-column:1/-1}}
</style>
</head>
<body>
<div class="wrap">
  <header class="top"><div><div class="brand">AI Video Factory RU</div><div class="sub">Discovery Stage 5.2 · локальная редакторская панель</div></div><div class="toolbar-right"><button id="importBtn">Импортировать отбор</button><input id="importFile" type="file" accept="application/json,.json" class="hidden"><button id="exportBtn" class="primary">Экспортировать отбор JSON</button></div></header>
  <div id="expiryWarning" class="warning"></div>
  <div class="stage5-note">Формат найденного видео и формат будущего ролика — разные решения. Все редакторские метки ниже назначаются человеком вручную. Заголовки YouTube не классифицируются автоматически; тема интервью или обзора может быть независимо раскрыта в другом допустимом формате.</div>
  <section class="stats"><div class="stat"><b id="statTotal">0</b><span>Видео в исходном отчёте</span></div><div class="stat"><b id="statSelected">0</b><span>Добавлено в отбор</span></div><div class="stat"><b id="statReviewed">0</b><span>Просмотрено редактором</span></div><div class="stat"><b id="statThemes">0</b><span>Редакторских тем</span></div></section>
  <section class="panel">
    <div class="filters">
      <div class="field"><label for="q">Поиск по названию или каналу</label><input id="q" type="search" placeholder="Введите слова из названия…"></div>
      <div class="field"><label for="market">Контекст рынка</label><select id="market"><option value="any">Все рынки</option><option value="ru">RU</option><option value="global">Global</option><option value="mostPopular:RU">mostPopular RU</option><option value="mostPopular:US">mostPopular US</option></select></div>
      <div class="field"><label for="stream">Направление поиска</label><select id="stream"><option value="any">Все направления</option></select></div>
      <div class="field"><label for="window">Период поиска</label><select id="window"><option value="any">Все периоды</option><option value="1">24 часа</option><option value="7">7 дней</option><option value="chart">Чарт</option></select></div>
      <div class="field"><label for="orderFilter">Сортировка источника</label><select id="orderFilter"><option value="any">Любая</option><option value="viewCount">viewCount · по просмотрам</option><option value="date">date · по дате</option><option value="youtube_algorithm">mostPopular · чарт</option><option value="unknown">Старый отчёт / неизвестно</option></select></div>
      <div class="field"><label for="duration">Длительность</label><select id="duration"><option value="any">Любая</option><option value="under">Менее 60 сек.</option><option value="over">60 сек. и более</option><option value="unknown">Неизвестна</option></select></div>
      <div class="field"><label for="decision">Статус отбора</label><select id="decision"><option value="any">Все статусы</option><option value="unreviewed">Не отмечено</option><option value="selected">В отборе</option><option value="later">Отложено</option><option value="rejected">Исключено</option></select></div>
    </div>
    <div class="more-filters">
      <div class="field"><label for="language">Язык (ручная отметка)</label><select id="language"><option value="any">Любой</option><option value="unset">Не отмечен</option><option value="ru">Русский</option><option value="en">Английский</option><option value="other">Другой</option></select></div>
      <div class="field"><label for="format">Хронометраж (ручная отметка)</label><select id="format"><option value="any">Любой</option><option value="unset">Не отмечен</option><option value="short">Короткий</option><option value="long">Длинный</option></select></div>
      <div class="field"><label for="profileFilter">Производственный профиль</label><select id="profileFilter"><option value="any">Любой</option><option value="unset">Не назначен</option></select></div>
      <div class="field"><label for="fitFilter">Пригодность темы</label><select id="fitFilter"><option value="any">Любая</option></select></div>
      <div class="field"><label for="category">Категория YouTube ID</label><select id="category"><option value="any">Все категории</option></select></div>
      <div class="field"><label for="themeFilter">Редакторская тема</label><select id="themeFilter"><option value="any">Все темы</option></select></div>
      <div class="field"><label for="sort">Сортировка исходных полей</label><select id="sort"><option value="views_desc">Просмотры: больше</option><option value="date_desc">Публикация: новее</option><option value="date_asc">Публикация: старее</option><option value="views_asc">Просмотры: меньше</option></select></div>
      <div class="field"><label for="pageSize">На странице</label><select id="pageSize"><option>25</option><option>50</option><option>100</option><option>200</option></select></div>
    </div>
    <div class="toolbar"><div class="toolbar-left"><span id="resultInfo" class="muted">Загрузка…</span><button id="resetFilters" class="small">Сбросить фильтры</button></div><div class="toolbar-right"><span id="saveState" class="muted">Изменения не сохранены</span></div></div>
  </section>
  <section class="panel">
    <strong>Редакторские темы</strong>
    <div class="sub">Отметьте флажками видео в списке и создайте тему либо добавьте их к существующей. Тема — ваша редакторская идея, а не автоматическая классификация YouTube.</div>
    <div class="toolbar"><div class="toolbar-left" style="min-width:min(100%,360px);flex:1"><input id="themeTitle" type="text" maxlength="160" placeholder="Название новой редакторской темы"></div><button id="createTheme" class="primary">Создать тему из отмеченных</button><select id="themeTarget" style="max-width:280px"><option value="">Выберите существующую тему</option></select><button id="addToTheme">Добавить отмеченные</button><button id="clearBulk" class="small">Снять отметки</button></div>
    <div id="themes" class="theme-grid"><div class="muted">Пока нет созданных тем.</div></div>
  </section>
  <main id="results" class="results"></main>
  <nav class="pagination"><button id="prevPage">← Назад</button><span id="pageInfo" class="muted">Страница 1</span><button id="nextPage">Вперёд →</button></nav>
  <div class="footer">Страница работает автономно: не загружает данные в Интернет и не обращается к YouTube API. Значения просмотров/лайков/комментариев показаны как исходные поля API, без расчёта рейтингов. `viewCount` — общий объём просмотров, а не скорость роста; `date` — дата публикации. Рынок RU/Global не определяет язык. Все editorial/production метки задаёт редактор вручную. Экспорт Stage 5.2 использует schema 1.1 и сохраняет совместимость импорта со старой schema 1.0. Сохраняйте отбор кнопкой экспорта; файлы editor.html и editorial.json нужно обновить или удалить в срок, указанный выше.</div>
</div>
<script type="application/json" id="source-data">__SOURCE_JSON__</script>
<script>
(() => {
'use strict';
const source = JSON.parse(document.getElementById('source-data').textContent);
const videos = source.videos || [];
const catalog = source.production_catalog || {};
const expiryTime = Date.parse(source.source_refresh_or_delete_by_utc || '');
if (!Number.isFinite(expiryTime) || Date.now() >= expiryTime) {
  document.body.innerHTML = '<main style="max-width:760px;margin:12vh auto;padding:24px;font:16px Segoe UI,Arial,sans-serif;color:#172033"><h1>Срок действия отчёта истёк</h1><p>Эта страница содержит данные YouTube API, срок обновления или удаления которых наступил. Удалите editor.html и editorial.json либо создайте новый отчёт discover.</p></main>';
  return;
}
const byId = new Map(videos.map(v => [v.video_id, v]));
const $ = id => document.getElementById(id);
let editorial = {schema_version:'1.1', source_generated_at_utc:source.source_generated_at_utc, source_refresh_or_delete_by_utc:source.source_refresh_or_delete_by_utc, videos:{}, themes:[]};
let page = 1, dirty = false, idCounter = 0;
const bulk = new Set();
const statusLabels = {unreviewed:'Не отмечено',selected:'В отборе',later:'Отложено',rejected:'Исключено'};
const referenceLabels = {explainer:'Объясняющий ролик',documentary:'Документалистика',interview:'Интервью',podcast:'Подкаст',vlog:'Влог',reaction:'Реакция',review:'Обзор',tutorial:'Обучение',screen_recording:'Запись экрана',live_stream:'Прямой эфир',gameplay:'Геймплей',match_or_event_footage:'Матч / событие',animation:'Анимация',music_or_performance:'Музыка / выступление',compilation:'Подборка',news_reportage:'Новостной репортаж',other:'Другое',unknown:'Не определён'};
const fitLabels = {pending_review:'Требует оценки',suitable_now:'Подходит сейчас',suitable_after_research:'После исследования',suitable_after_extension:'После расширения системы',conditional_on_rights:'При подтверждении прав',unsuitable:'Не подходит'};
const blockerLabels = {requires_on_camera_presenter:'Нужен ведущий в кадре',requires_human_recorded_voice:'Нужен живой голос',requires_interview_guest:'Нужен гость / интервью',requires_field_filming:'Нужна съёмка на месте',requires_physical_experiment:'Нужен физический эксперимент',requires_live_performance:'Нужно живое выступление',requires_unlicensed_third_party_footage:'Нет прав на сторонние кадры',requires_gameplay_capture:'Нужна запись геймплея',facts_not_verifiable:'Факты не подтверждены',no_usable_assets:'Нет подходящих материалов',unavailable_api_or_tool:'Нет нужного API / инструмента',budget_exceeds_limit:'Выход за бюджет',other:'Другое'};
const assetLabels = {licensed_stock:'Лицензированный сток',public_domain_archive:'Архив / общественное достояние',original_graphics:'Оригинальная графика',verified_data_chart:'График по проверенным данным',screen_capture:'Запись экрана',generated_still:'Сгенерированное изображение',generated_video:'Сгенерированное видео',procedural_3d:'Процедурная 3D-графика',ai_tts:'ИИ-озвучка',text_only:'Только текст'};
const rightsLabels = {pending:'Не проверены',verified:'Проверены',not_applicable:'Не применимо',blocked:'Заблокировано'};
const factLabels = {pending:'Не проверены',verified:'Проверены',partially_verified:'Частично проверены',failed:'Не прошли проверку'};
const budgetLabels = {not_estimated:'Не оценён',within_limit:'В пределах лимита',requires_approval:'Нужно согласование',over_limit:'Выше лимита'};
const profileStatusLabels = {partially_implemented:'Реализован частично',conditional_on_quality_and_rights:'Зависит от качества и прав',requires_new_subsystem:'Нужен новый модуль',future_experimental:'Будущий эксперимент'};
const flagFields = ['human_presence_required','human_voice_required','field_filming_required','physical_experiment_required','guest_required'];
const productionBlockers = new Set(['requires_on_camera_presenter','requires_human_recorded_voice','requires_interview_guest','requires_field_filming','requires_physical_experiment']);
const flagLabels = {human_presence_required:'Нужно присутствие в кадре',human_voice_required:'Нужна человеческая запись голоса',field_filming_required:'Нужна съёмка на месте',physical_experiment_required:'Нужен физический эксперимент',guest_required:'Нужен гость'};
const validSet = values => new Set(Array.isArray(values) ? values : []);
const optionHtml = (values, selected, labels={}) => values.map(v=>`<option value="${esc(v)}" ${selected===v?'selected':''}>${esc(labels[v]||v)}</option>`).join('');
const multiHtml = (field, values, selected, labels={}) => values.map(v=>`<label class="multi-option"><input type="checkbox" data-multi-field="${esc(field)}" value="${esc(v)}" ${(selected||[]).includes(v)?'checked':''}><span>${esc(labels[v]||v)}</span></label>`).join('');
function profileById(id){return (catalog.profiles||[]).find(p=>p.id===id)||null;}
function eligibleForSuitableNow(a){return !flagFields.some(key=>Boolean(a[key])) && !(Array.isArray(a.blockers)&&a.blockers.some(key=>productionBlockers.has(key)));}
const esc = value => String(value ?? '').replace(/[&<>"']/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
const num = value => value == null ? 'н/д' : new Intl.NumberFormat('ru-RU').format(value);
const date = value => { if (!value) return 'н/д'; const d=new Date(value); return Number.isNaN(d.getTime()) ? 'н/д' : new Intl.DateTimeFormat('ru-RU',{dateStyle:'medium',timeStyle:'short',timeZone:'UTC'}).format(d)+' UTC'; };
const secs = v => Number.isFinite(v.duration_seconds) ? v.duration_seconds : null;
const bucket = v => secs(v) == null ? 'unknown' : secs(v) < 60 ? 'under' : 'over';
const contexts = v => Array.isArray(v.found_in) ? v.found_in : [];
const defaultAnnotation = () => ({
 decision:'unreviewed', editor_note:'', language:'', format_preference:'', reviewed:false,
 reference_format:'unknown', production_profile:'', production_fit:'pending_review',
 blockers:[], asset_strategy:[], rights_status:'pending', fact_check_status:'pending', budget_status:'not_estimated',
 human_presence_required:false, human_voice_required:false, field_filming_required:false,
 physical_experiment_required:false, guest_required:false
});
const annotation = id => ({...defaultAnnotation(), ...(editorial.videos[id] || {})});
function markDirty(){dirty=true;$('saveState').textContent='Есть несохранённые изменения';$('saveState').style.color='#9a6700';updateStats();}
function markSaved(){dirty=false;$('saveState').textContent='Отбор экспортирован';$('saveState').style.color='#18794e';updateStats();}
function setAnnotation(id, patch){editorial.videos[id]={...annotation(id),...patch};markDirty();}
function updateStats(){
 $('statTotal').textContent=num(videos.length);
 $('statSelected').textContent=num(Object.values(editorial.videos).filter(x=>x.decision==='selected').length);
 $('statReviewed').textContent=num(Object.values(editorial.videos).filter(x=>x.reviewed).length);
 $('statThemes').textContent=num(editorial.themes.length);
}
function uniqueOptions(){
 const streams=new Map();
 for(const v of videos) for(const c of contexts(v)) if(c.stream_id && !streams.has(c.stream_id)) streams.set(c.stream_id,c.stream_title || c.stream_id);
 for(const [id,label] of [...streams.entries()].sort((a,b)=>a[1].localeCompare(b[1],'ru'))) $('stream').insertAdjacentHTML('beforeend',`<option value="${esc(id)}">${esc(label)}</option>`);
 const categories=[...new Set(videos.map(v=>v.category_id).filter(x=>x!=null && x!==''))].sort((a,b)=>String(a).localeCompare(String(b),undefined,{numeric:true}));
 for(const id of categories) $('category').insertAdjacentHTML('beforeend',`<option value="${esc(id)}">YouTube category_id ${esc(id)}</option>`);
 for(const p of (catalog.profiles||[])) $('profileFilter').insertAdjacentHTML('beforeend',`<option value="${esc(p.id)}">${esc(p.title)}</option>`);
 for(const [id,label] of Object.entries(fitLabels)) $('fitFilter').insertAdjacentHTML('beforeend',`<option value="${esc(id)}">${esc(label)}</option>`);
}
function populateThemeFilters(){
 const previous=$('themeFilter').value;
 const target=$('themeTarget').value;
 const opts=editorial.themes.map(t=>`<option value="${esc(t.id)}">${esc(t.title)}</option>`).join('');
 $('themeFilter').innerHTML='<option value="any">Все темы</option>'+opts;
 $('themeTarget').innerHTML='<option value="">Выберите существующую тему</option>'+opts;
 $('themeFilter').value=editorial.themes.some(t=>t.id===previous)?previous:'any';
 $('themeTarget').value=editorial.themes.some(t=>t.id===target)?target:'';
}
function renderThemes(){
 populateThemeFilters();
 if(!editorial.themes.length){$('themes').innerHTML='<div class="muted">Пока нет созданных тем.</div>';return;}
 $('themes').innerHTML=editorial.themes.map(t=>`<div class="theme"><strong>${esc(t.title)}</strong><small>${(t.video_ids||[]).length} видео</small><div class="toolbar"><button class="small" data-theme-open="${esc(t.id)}">Показать видео</button><button class="small danger" data-theme-delete="${esc(t.id)}">Удалить тему</button></div></div>`).join('');
}
function matches(v){
 const q=$('q').value.trim().toLocaleLowerCase('ru-RU');
 const cs=contexts(v), a=annotation(v.video_id);
 if(q && !(String(v.title||'')+' '+String(v.channel_title||'')).toLocaleLowerCase('ru-RU').includes(q)) return false;
 if($('market').value!=='any' && !cs.some(c=>c.market===$('market').value)) return false;
 if($('stream').value!=='any' && !cs.some(c=>c.stream_id===$('stream').value)) return false;
 if($('window').value==='chart' && !cs.some(c=>c.window_days==null)) return false;
 if(['1','7'].includes($('window').value) && !cs.some(c=>String(c.window_days)===$('window').value)) return false;
 if($('orderFilter').value!=='any' && !cs.some(c=>String(c.order||'unknown')===$('orderFilter').value)) return false;
 if($('duration').value!=='any' && bucket(v)!==$('duration').value) return false;
 if($('decision').value==='unreviewed' && a.decision!=='unreviewed') return false;
 if($('decision').value!=='any' && $('decision').value!=='unreviewed' && a.decision!==$('decision').value) return false;
 if($('language').value==='unset' && a.language) return false;
 if(['ru','en','other'].includes($('language').value) && a.language!==$('language').value) return false;
 if($('format').value==='unset' && a.format_preference) return false;
 if(['short','long'].includes($('format').value) && a.format_preference!==$('format').value) return false;
 if($('profileFilter').value==='unset' && a.production_profile) return false;
 if($('profileFilter').value!=='any' && $('profileFilter').value!=='unset' && a.production_profile!==$('profileFilter').value) return false;
 if($('fitFilter').value!=='any' && a.production_fit!==$('fitFilter').value) return false;
 if($('category').value!=='any' && String(v.category_id??'')!==$('category').value) return false;
 if($('themeFilter').value!=='any' && !editorial.themes.find(t=>t.id===$('themeFilter').value)?.video_ids?.includes(v.video_id)) return false;
 return true;
}
function sortedFiltered(){
 const list=videos.filter(matches);
 const mode=$('sort').value;
 list.sort((a,b)=>{
  if(mode==='views_desc') return (b.view_count??-1)-(a.view_count??-1);
  if(mode==='views_asc') return (a.view_count??Number.MAX_SAFE_INTEGER)-(b.view_count??Number.MAX_SAFE_INTEGER);
  const da=Date.parse(a.published_at||'')||0, db=Date.parse(b.published_at||'')||0;
  return mode==='date_asc' ? da-db : db-da;
 });
 return list;
}
function card(v){
 const a=annotation(v.video_id), cs=contexts(v);
 const labels=[...new Set(cs.map(c=>`${c.market} · ${c.stream_title||c.stream_id} · ${c.window_days==null?'чарт':c.window_days+' дн.'} · ${c.order||'unknown'}`))];
 const sourceQueries=[...new Set(cs.map(c=>c.query).filter(Boolean))];
 const themeNames=editorial.themes.filter(t=>(t.video_ids||[]).includes(v.video_id)).map(t=>t.title);
 const chips=[...labels.slice(0,4),...(v.category_id?['YouTube category_id '+v.category_id]:[]),...themeNames.map(n=>'Тема: '+n)];
 const selectedProfile=profileById(a.production_profile);
 const profileStatus=selectedProfile?profileStatusLabels[selectedProfile.status]||selectedProfile.status:'';
 const videoUrl='https://www.youtube.com/watch?v='+encodeURIComponent(v.video_id);
 return `<article class="card" data-video="${esc(v.video_id)}"><div class="card-head"><div style="min-width:0"><div class="title"><a href="${videoUrl}" target="_blank" rel="noopener noreferrer">${esc(v.title||'(без названия)')}</a></div><div class="channel">${esc(v.channel_title||'Канал не указан')} · ${esc(v.video_id)}</div><div class="chips">${chips.map(x=>`<span class="chip">${esc(x)}</span>`).join('')}</div></div><span class="status ${esc(a.decision)}">${esc(statusLabels[a.decision]||statusLabels.unreviewed)}</span></div>
 <div class="metrics"><span>Просмотры: <b>${num(v.view_count)}</b></span><span>Лайки: ${num(v.like_count)}</span><span>Комментарии: ${num(v.comment_count)}</span><span>Опубликовано: ${esc(date(v.published_at))}</span><span>Длительность: ${esc(v.duration||'н/д')}</span></div>
 <div class="card-actions"><label class="review-check"><input type="checkbox" data-bulk="${esc(v.video_id)}" ${bulk.has(v.video_id)?'checked':''}> Отметить для темы</label><button class="small ${a.decision==='selected'?'primary':''}" data-decision="selected">В отбор</button><button class="small" data-decision="later">Отложить</button><button class="small danger" data-decision="rejected">Исключить</button><button class="small" data-decision="unreviewed">Сбросить статус</button><a href="${videoUrl}" target="_blank" rel="noopener noreferrer" class="sub">Открыть на YouTube ↗</a></div>
 <div class="card-fields">
  <div class="field"><label>Язык — ручная отметка</label><select data-field="language"><option value="">Не отмечен</option><option value="ru" ${a.language==='ru'?'selected':''}>Русский</option><option value="en" ${a.language==='en'?'selected':''}>Английский</option><option value="other" ${a.language==='other'?'selected':''}>Другой</option></select></div>
  <div class="field"><label>Хронометраж — ручная отметка</label><select data-field="format_preference"><option value="">Не отмечен</option><option value="short" ${a.format_preference==='short'?'selected':''}>Короткий</option><option value="long" ${a.format_preference==='long'?'selected':''}>Длинный</option></select></div>
  <div class="field"><label>Формат найденного видео</label><select data-field="reference_format">${optionHtml(catalog.reference_formats||['unknown'],a.reference_format,referenceLabels)}</select></div>
  <div class="field"><label>Наш производственный профиль</label><select data-field="production_profile"><option value="">Не выбран</option>${(catalog.profiles||[]).map(p=>`<option value="${esc(p.id)}" ${a.production_profile===p.id?'selected':''}>${esc(p.title)} — ${esc(profileStatusLabels[p.status]||p.status)}</option>`).join('')}</select></div>
  <div class="field"><label>Пригодность темы для нас</label><select data-field="production_fit">${optionHtml(catalog.production_fit||['pending_review'],a.production_fit,fitLabels)}</select></div>
  <div class="field"><label>Права на материалы</label><select data-field="rights_status">${optionHtml(catalog.rights_status||['pending'],a.rights_status,rightsLabels)}</select></div>
  <div class="field"><label>Проверка фактов</label><select data-field="fact_check_status">${optionHtml(catalog.fact_check_status||['pending'],a.fact_check_status,factLabels)}</select></div>
  <div class="field"><label>Бюджет</label><select data-field="budget_status">${optionHtml(catalog.budget_status||['not_estimated'],a.budget_status,budgetLabels)}</select></div>
  <div class="field"><label>Блокеры (можно несколько)</label><div class="multi-checks" data-field="blockers">${multiHtml('blockers',catalog.blockers||[],a.blockers,blockerLabels)}</div></div>
  <div class="field"><label>Стратегия материалов (можно несколько)</label><div class="multi-checks" data-field="asset_strategy">${multiHtml('asset_strategy',catalog.asset_strategies||[],a.asset_strategy,assetLabels)}</div></div>
  <div class="field note-field"><label>Заметка редактора</label><input type="text" maxlength="1000" data-field="editor_note" value="${esc(a.editor_note||'')}" placeholder="Чем интересна тема и как раскрыть её самостоятельно?"></div>
  <label class="review-check"><input type="checkbox" data-field="reviewed" ${a.reviewed?'checked':''}> Просмотрено</label>
 </div>
 <div class="editorial-flags">${flagFields.map(key=>`<label><input type="checkbox" data-field="${key}" ${a[key]?'checked':''}> ${esc(flagLabels[key])}</label>`).join('')}</div>
 <div class="sub" style="margin-top:7px">${selectedProfile?`Профиль: ${esc(profileStatus)}. ${esc(selectedProfile.summary||'')}`:'Профиль не выбран.'} ${sourceQueries.length?`Поисковые запросы: ${esc(sourceQueries.slice(0,3).join(' | '))}`:''}</div></article>`;
}
function render(){
 const list=sortedFiltered(), size=Number($('pageSize').value), pages=Math.max(1,Math.ceil(list.length/size));
 page=Math.min(page,pages); const start=(page-1)*size, shown=list.slice(start,start+size);
 $('resultInfo').textContent=`Показано ${list.length ? start+1 : 0}–${Math.min(start+size,list.length)} из ${num(list.length)} подходящих видео · всего ${num(videos.length)}`;
 $('pageInfo').textContent=`Страница ${page} из ${pages}`;$('prevPage').disabled=page<=1;$('nextPage').disabled=page>=pages;
 $('results').innerHTML=shown.length?shown.map(card).join(''):'<div class="empty">По заданным фильтрам ничего не найдено.</div>';
 updateStats();
}
function resetFilters(){['q','market','stream','window','orderFilter','duration','decision','language','format','profileFilter','fitFilter','category','themeFilter','sort','pageSize'].forEach(id=>$(id).value=({market:'any',stream:'any',window:'any',orderFilter:'any',duration:'any',decision:'any',language:'any',format:'any',profileFilter:'any',fitFilter:'any',category:'any',themeFilter:'any',sort:'views_desc',pageSize:'25'}[id]??''));page=1;render();}
function exportEditorial(){
 const out={application:'AI Video Factory RU editorial selection',schema_version:'1.1',source_generated_at_utc:source.source_generated_at_utc,source_refresh_or_delete_by_utc:source.source_refresh_or_delete_by_utc,updated_at_utc:new Date().toISOString(),videos:editorial.videos,themes:editorial.themes};
 const blob=new Blob([JSON.stringify(out,null,2)],{type:'application/json;charset=utf-8'}), url=URL.createObjectURL(blob), a=document.createElement('a');
 a.href=url;a.download='editorial.json';document.body.appendChild(a);a.click();a.remove();URL.revokeObjectURL(url);markSaved();
}
function importEditorial(file){
 const reader=new FileReader(); reader.onload=()=>{try{
  const data=JSON.parse(String(reader.result||''));
  if(!['1.0','1.1'].includes(data.schema_version)||data.application!=='AI Video Factory RU editorial selection') throw new Error('Формат файла не распознан. Поддерживаются Stage 4 schema 1.0 и Stage 5.2 schema 1.1.');
  if(data.source_generated_at_utc!==source.source_generated_at_utc||data.source_refresh_or_delete_by_utc!==source.source_refresh_or_delete_by_utc) throw new Error('Файл отбора создан для другого исходного отчёта Discovery. Сначала откройте соответствующую версию editor.html.');
  const clean={};
  const refSet=validSet(catalog.reference_formats), profileSet=validSet((catalog.profiles||[]).map(p=>p.id));
  const fitSet=validSet(catalog.production_fit), blockerSet=validSet(catalog.blockers), assetSet=validSet(catalog.asset_strategies);
  const rightsSet=validSet(catalog.rights_status), factSet=validSet(catalog.fact_check_status), budgetSet=validSet(catalog.budget_status);
  const safeEnum=(value,set,fallback)=>set.has(value)?value:fallback;
  const safeArray=(value,set)=>Array.isArray(value)?[...new Set(value.filter(x=>typeof x==='string'&&set.has(x)))]:[];
  for(const [id,a] of Object.entries(data.videos||{})) if(byId.has(id)&&a&&typeof a==='object'&&!Array.isArray(a)) clean[id]={
   decision:['unreviewed','selected','later','rejected'].includes(a.decision)?a.decision:'unreviewed',
   editor_note:String(a.editor_note||'').slice(0,1000),
   language:['','ru','en','other'].includes(a.language)?a.language:'',
   format_preference:['','short','long'].includes(a.format_preference)?a.format_preference:'', reviewed:Boolean(a.reviewed),
   reference_format:safeEnum(a.reference_format,refSet,'unknown'),
   production_profile:safeEnum(a.production_profile,profileSet,''),
   production_fit:safeEnum(a.production_fit,fitSet,'pending_review'),
   blockers:safeArray(a.blockers,blockerSet), asset_strategy:safeArray(a.asset_strategy,assetSet),
   rights_status:safeEnum(a.rights_status,rightsSet,'pending'),
   fact_check_status:safeEnum(a.fact_check_status,factSet,'pending'),
   budget_status:safeEnum(a.budget_status,budgetSet,'not_estimated'),
   human_presence_required:Boolean(a.human_presence_required), human_voice_required:Boolean(a.human_voice_required),
   field_filming_required:Boolean(a.field_filming_required), physical_experiment_required:Boolean(a.physical_experiment_required),
   guest_required:Boolean(a.guest_required)
  };
  const themes=Array.isArray(data.themes)?data.themes.filter(t=>t&&typeof t.title==='string').map(t=>({id:String(t.id||('theme-'+Date.now())),title:t.title.slice(0,160),video_ids:[...new Set((t.video_ids||[]).filter(id=>byId.has(id)))]})):[];
  editorial.videos=clean;editorial.themes=themes;bulk.clear();dirty=false;$('saveState').textContent='Отбор загружен';$('saveState').style.color='#18794e';renderThemes();page=1;render();
 }catch(e){alert('Не удалось импортировать отбор: '+e.message);}};reader.onerror=()=>alert('Не удалось прочитать файл JSON.');reader.readAsText(file,'UTF-8');
}
$('results').addEventListener('click',e=>{const b=e.target.closest('[data-decision]');if(!b)return;const c=e.target.closest('[data-video]');if(!c)return;setAnnotation(c.dataset.video,{decision:b.dataset.decision});render();});
$('results').addEventListener('change',e=>{
 const c=e.target.closest('[data-video]');if(!c)return;const id=c.dataset.video;
 if(e.target.dataset.bulk){if(e.target.checked)bulk.add(id);else bulk.delete(id);return;}
 const multiField=e.target.dataset.multiField;
 if(multiField){
  const value=[...c.querySelectorAll(`input[data-multi-field="${multiField}"]:checked`)].map(input=>input.value);
  const current=annotation(id);
  if(multiField==='blockers'&&current.production_fit==='suitable_now'&&value.some(key=>productionBlockers.has(key))){
   e.target.checked=false;
   alert('Нельзя добавить этот блокер, пока тема отмечена «Подходит сейчас». Сначала измените оценку пригодности.');
   return;
  }
  setAnnotation(id,{[multiField]:value});
  return;
 }
 const field=e.target.dataset.field;if(!field)return;
 const value=e.target.type==='checkbox'?e.target.checked:e.target.value;
 const current=annotation(id);
 if(field==='production_fit'&&value==='suitable_now'&&!eligibleForSuitableNow(current)){
  alert('Нельзя отметить «Подходит сейчас»: отмечено требование ведущего/человеческого голоса/съёмки/гостя/эксперимента. Сначала измените планируемый формат или выберите другую оценку.');
  e.target.value=current.production_fit;return;
 }
 if(flagFields.includes(field)&&value&&current.production_fit==='suitable_now'){
  alert('Сначала измените пригодность темы с «Подходит сейчас», затем можно отметить это производственное требование.');
  e.target.checked=false;return;
 }
 if(field==='reviewed'){setAnnotation(id,{reviewed:value});return;}
 setAnnotation(id,{[field]:value});if(field!=='editor_note')render();
 });
$('results').addEventListener('blur',()=>{},true);
window.addEventListener('beforeunload',event=>{if(dirty){event.preventDefault();event.returnValue='';}});
$('themes').addEventListener('click',e=>{const del=e.target.closest('[data-theme-delete]');if(del){editorial.themes=editorial.themes.filter(t=>t.id!==del.dataset.themeDelete);markDirty();renderThemes();render();return;}const open=e.target.closest('[data-theme-open]');if(open){$('themeFilter').value=open.dataset.themeOpen;page=1;render();window.scrollTo({top:0,behavior:'smooth'});}});
$('createTheme').addEventListener('click',()=>{const title=$('themeTitle').value.trim();if(!title){alert('Введите название темы.');return;}if(!bulk.size){alert('Сначала отметьте флажками хотя бы одно видео.');return;}const ids=[...bulk];const theme={id:'theme-'+Date.now()+'-'+(++idCounter),title,video_ids:ids};editorial.themes.push(theme);bulk.clear();$('themeTitle').value='';markDirty();renderThemes();render();});
$('addToTheme').addEventListener('click',()=>{const theme=editorial.themes.find(t=>t.id===$('themeTarget').value);if(!theme){alert('Выберите существующую тему.');return;}if(!bulk.size){alert('Сначала отметьте флажками видео.');return;}theme.video_ids=[...new Set([...(theme.video_ids||[]),...bulk])];bulk.clear();markDirty();renderThemes();render();});
$('clearBulk').addEventListener('click',()=>{bulk.clear();render();});
$('exportBtn').addEventListener('click',exportEditorial);$('importBtn').addEventListener('click',()=>$('importFile').click());$('importFile').addEventListener('change',e=>{const f=e.target.files?.[0];if(f)importEditorial(f);e.target.value='';});
['q','market','stream','window','orderFilter','duration','decision','language','format','profileFilter','fitFilter','category','themeFilter','sort','pageSize'].forEach(id=>$(id).addEventListener(id==='q'?'input':'change',()=>{page=1;render();}));
$('resetFilters').addEventListener('click',resetFilters);$('prevPage').addEventListener('click',()=>{page=Math.max(1,page-1);render();});$('nextPage').addEventListener('click',()=>{page++;render();});
$('expiryWarning').textContent=`Исходный отчёт: ${source.source_generated_at_utc}. Обновить или удалить данные до: ${source.source_refresh_or_delete_by_utc}. HTML содержит поля YouTube API и после указанной даты должен быть удалён или пересоздан.`;
uniqueOptions();renderThemes();render();
})();
</script>
</body>
</html>
'''
