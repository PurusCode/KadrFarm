from __future__ import annotations

from types import SimpleNamespace

from kadr_farm.visual_professional_layout_v2 import (
    AUTOFIT_SCRIPT,
    build_layout_variants,
)
from kadr_farm.visual_professional_renderer_v2 import build_shot_html
from kadr_farm.visual_story_v2 import VisualStoryPlan


def _plan(objects, events=None):
    text = "Проверка автоматической компоновки текста."
    return VisualStoryPlan.model_validate({
        "schema_version": "2.0",
        "title": "AutoFit tests",
        "width": 1920,
        "height": 1080,
        "fps": 25,
        "background": "#101827",
        "speech_units": [{"unit_id": "u01", "text": text, "char_start": 0, "char_end": len(text)}],
        "shots": [{
            "shot_id": "s01",
            "editorial_purpose": "Проверка",
            "visual_summary": "Адаптация типографики",
            "speech_unit_ids": ["u01"],
            "objects": objects,
            "events": events or [],
        }],
    })


def test_counter_uses_authored_font_size_in_html():
    plan = _plan([{
        "object_id": "metric",
        "kind": "counter",
        "layer": {"x": 0.8, "y": 0.7, "width": 0.2, "height": 0.25},
        "content": {"value": 3, "label": "ЭТАПА ДО РЕЗУЛЬТАТА", "font_size": 130},
    }])
    html, _ = build_shot_html(plan, plan.shots[0], {})
    assert "--counter-value-size:130px" in html
    assert "font-size:var(--counter-value-size" in html


def test_update_text_final_state_is_included_in_autofit_variants():
    shot = SimpleNamespace(events=[SimpleNamespace(
        action="update_text", target_id="title", event_id="replace_title",
        params={"text": "Окончательный длинный заголовок"},
    )])
    variants = build_layout_variants(shot)
    assert variants == [{
        "target_id": "title",
        "selector": ".type-main,.callout-copy,.counter-value",
        "text": "Окончательный длинный заголовок",
        "event_id": "replace_title",
        "action": "update_text",
    }]


def test_count_up_final_value_and_suffix_are_included_in_autofit_variants():
    shot = SimpleNamespace(events=[SimpleNamespace(
        action="count_up", target_id="metric", event_id="count_final",
        params={"to": 125.5, "decimals": 1, "suffix": "%"},
    )])
    variants = build_layout_variants(shot)
    assert [item["selector"] for item in variants] == [".counter-value", ".counter-value"]
    assert [item["text"] for item in variants] == ["0.0%", "125.5%"]


def test_autofit_script_has_readability_floors_and_reports_unresolved_items():
    assert "minimum readable size" not in AUTOFIT_SCRIPT  # surfaced by Python preflight
    assert "minScale" in AUTOFIT_SCRIPT
    assert "unresolved_count" in AUTOFIT_SCRIPT
    assert "document.fonts.ready" not in AUTOFIT_SCRIPT  # readiness is awaited by page bootstrap


def test_autofit_script_checks_actual_object_and_clipping_bounds():
    assert "text_outside_object" in AUTOFIT_SCRIPT
    assert "ancestor_clipping" in AUTOFIT_SCRIPT
    assert "component_container_clipping" in AUTOFIT_SCRIPT


def test_autofit_never_enlarges_authored_small_type():
    assert "Math.min(minFont, fontSize)" in AUTOFIT_SCRIPT
    assert "minFont:effectiveMinFont" in AUTOFIT_SCRIPT


def test_autofit_scales_line_height_with_font_size():
    assert "lineHeight:style.lineHeight === 'normal' ? null : numeric(style.lineHeight)" in AUTOFIT_SCRIPT
    assert "element.style.lineHeight = `${item.lineHeight * scale}px`" in AUTOFIT_SCRIPT


def test_autofit_and_qc_treat_scrollable_layouts_as_non_visible_overflow():
    assert "['hidden','clip','scroll','auto']" in AUTOFIT_SCRIPT


def test_chromium_autofit_fits_real_text_components_and_dynamic_counter_state():
    import pytest
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        pytest.skip("Playwright is not installed")

    html = """<!doctype html><html><head><meta charset='utf-8'><style>
    *{box-sizing:border-box}html,body{margin:0;width:100%;height:100%;overflow:hidden}
    .scene-canvas{position:relative;width:1920px;height:1080px}
    .scene-object{position:absolute;transform:translate(-50%,-50%);overflow:visible;contain:layout style}
    .type-layout{height:100%;width:100%;display:flex;flex-direction:column;justify-content:center;align-items:stretch;text-align:left;padding:0;overflow:hidden}
    .type-eyebrow{font:700 clamp(14px,1.3vw,26px)/1.1 Arial,sans-serif;letter-spacing:.18em;margin-bottom:clamp(12px,1.5vw,28px)}
    .type-main{font-size:var(--type-size);font-weight:850;line-height:var(--type-line);letter-spacing:-1.2px;text-wrap:balance;overflow-wrap:anywhere}
    .callout{width:100%;height:100%;padding:5%;display:flex;flex-direction:column;justify-content:center}
    .callout-eyebrow{font-size:clamp(13px,1vw,22px);font-weight:800;letter-spacing:.17em;margin-bottom:14px}
    .callout-copy{font-size:clamp(20px,2.3vw,48px);font-weight:720;line-height:1.12;text-wrap:balance}
    .counter{width:100%;height:100%;display:flex;flex-direction:column;justify-content:center;gap:10px}
    .counter-value{font-size:var(--counter-value-size,clamp(64px,9vw,190px));font-weight:850;line-height:.95}
    .counter-label{font-size:clamp(17px,1.8vw,36px);line-height:1.2;font-weight:600}
    </style></head><body><main class='scene-canvas'>
    <div class='scene-object object-callout' data-object-id='opening_note' style='left:25%;top:79%;width:42%;height:11%'><div class='callout'><div class='callout-eyebrow'>ПЕРВЫЙ ШАГ</div><div class='callout-copy'>Разрозненные сигналы нужно связать в систему.</div></div></div>
    <div class='scene-object object-text' data-object-id='process_kicker' style='left:27%;top:15%;width:45%;height:10%'><div class='type-layout'><div class='type-eyebrow'>КАК ПОЯВЛЯЕТСЯ СТРУКТУРА</div><div class='type-main' style='--type-size:68px;--type-line:1.02'>ТРИ ЭТАПА</div></div></div>
    <div class='scene-object object-counter' data-object-id='steps_counter' style='left:84%;top:76%;width:20%;height:23%'><div class='counter' style='--counter-value-size:130px'><div class='counter-value'>0</div><div class='counter-label'>ЭТАПА ДО РЕЗУЛЬТАТА</div></div></div>
    </main></body></html>"""
    with sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch(headless=True)
        except Exception as exc:
            pytest.skip(f"Chromium is not installed for Playwright: {exc}")
        try:
            page = browser.new_page(viewport={"width": 1920, "height": 1080})
            page.set_content(html, wait_until="load")
            manifest = page.evaluate(AUTOFIT_SCRIPT, {"variants": [
                {"target_id":"steps_counter", "selector":".counter-value", "text":"0.0%"},
                {"target_id":"steps_counter", "selector":".counter-value", "text":"125.5%"},
            ]})
            assert manifest["unresolved_count"] == 0
            statuses = {item["object_id"]: item["status"] for item in manifest["objects"]}
            assert statuses["opening_note"] == "fitted"
            assert statuses["process_kicker"] == "fitted"
            assert statuses["steps_counter"] in {"fitted", "unchanged"}
            counter = next(item for item in manifest["objects"] if item["object_id"] == "steps_counter")
            assert counter["states_checked"] == 3
            for object_id in ("opening_note", "process_kicker", "steps_counter"):
                geometry = page.locator(f'[data-object-id="{object_id}"]').evaluate("""el => {
                    const root=el.getBoundingClientRect();
                    const nodes=[...el.querySelectorAll('.callout-eyebrow,.callout-copy,.type-eyebrow,.type-main,.counter-value,.counter-label')];
                    return nodes.filter(n => (n.textContent||'').trim()).map(n => {
                        const r=n.getBoundingClientRect();
                        return r.top >= root.top - 0.5 && r.bottom <= root.bottom + 0.5 &&
                               r.left >= root.left - 0.5 && r.right <= root.right + 0.5;
                    });
                }""")
                assert geometry and all(geometry), f"{object_id} still overflows: {geometry}"
        finally:
            browser.close()


def test_component_container_qc_only_flags_actual_clipping_modes():
    from kadr_farm.visual_professional_qc_v2 import _BROWSER_GEOMETRY_SCRIPT

    assert "const boxStyle = getComputedStyle(box);" in _BROWSER_GEOMETRY_SCRIPT
    assert "boxClipsX" in _BROWSER_GEOMETRY_SCRIPT
    assert "boxClipsY" in _BROWSER_GEOMETRY_SCRIPT
    assert "if (clippedX || clippedY)" in _BROWSER_GEOMETRY_SCRIPT
