from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from kadr_farm.visual_story_contract_v2 import (
    AssetRequest,
    ExecutionOutputSpec,
    ExecutionPlan,
    RendererCapabilities,
    SceneCompositionPlan,
    VisualStoryPlan,
    validate_continuity_references,
    validate_global_plan_coverage,
    validate_scene_composition_plan,
    validate_scene_plan_coverage,
)


FIXTURES = Path(__file__).parent / "fixtures" / "visual_story_contract_v2"


def fixture(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def source_scenes():
    return [
        {"id": "s_001", "voice_text": "Нейросеть уверенно отвечает, даже когда ошибается."},
        {"id": "s_002", "voice_text": "Но убедительный тон не доказывает достоверность ответа."},
    ]


def test_valid_global_plan_fixture_and_optional_empty_motifs():
    plan = VisualStoryPlan.model_validate(fixture("global_plan_valid.json"))
    assert plan.schema_version == "2.0"
    assert plan.motifs == []
    assert all(scene.motif_refs == [] for scene in plan.scenes)
    validate_global_plan_coverage(plan, source_scenes())


def test_valid_complex_scene_fixture_covers_all_typed_object_families():
    plan = SceneCompositionPlan.model_validate(fixture("scene_plan_complex_valid.json"))
    kinds = {obj.kind for obj in plan.shots[0].composition.objects}
    assert kinds == {
        "image", "video", "text", "shape", "icon", "callout", "group",
        "node_graph", "chart", "table", "formula", "counter",
    }
    validate_scene_composition_plan(plan, source_scenes()[0])


def test_contrast_continuity_does_not_require_same_object_or_continuity_id():
    plan = SceneCompositionPlan.model_validate(fixture("scene_plan_contrast_valid.json"))
    link = plan.shots[0].continuity.incoming[0]
    assert link.relation == "contrast"
    assert link.continuity_id is None
    assert link.reference.object_id is None


def test_contrast_link_may_optionally_keep_an_object_but_does_not_require_it():
    data = fixture("scene_plan_contrast_valid.json")
    data["shots"][0]["continuity"]["incoming"][0]["continuity_id"] = "headline_theme"
    data["shots"][0]["continuity"]["incoming"][0]["reference"]["object_id"] = "headline"
    # The referenced source and the current shot both explicitly identify the
    # same continuing object. Contrast itself still does not require carry-over.
    source = fixture("scene_plan_complex_valid.json")
    headline = next(obj for obj in source["shots"][0]["composition"]["objects"] if obj["object_id"] == "headline")
    headline["continuity_id"] = "headline_theme"
    target_card = data["shots"][0]["composition"]["objects"][0]
    target_card["continuity_id"] = "headline_theme"
    SceneCompositionPlan.model_validate(data)
    source_plan = SceneCompositionPlan.model_validate(source)
    from kadr_farm.visual_story_contract_v2 import validate_continuity_references
    validate_continuity_references([source_plan, SceneCompositionPlan.model_validate(data)])


def test_unknown_global_field_is_rejected():
    data = fixture("global_plan_valid.json")
    data["director_guess"] = "ignore this"
    with pytest.raises(ValidationError):
        VisualStoryPlan.model_validate(data)


def test_unknown_scene_field_is_rejected():
    data = fixture("scene_plan_complex_valid.json")
    data["shots"][0]["composition"]["objects"][0]["content"]["invented_prompt"] = "anything"
    with pytest.raises(ValidationError):
        SceneCompositionPlan.model_validate(data)


def test_exact_pixel_coordinates_are_not_part_of_director_layer_contract():
    data = fixture("scene_plan_complex_valid.json")
    data["shots"][0]["composition"]["objects"][0]["layer"]["x"] = 0.3
    with pytest.raises(ValidationError):
        SceneCompositionPlan.model_validate(data)


def test_unknown_enum_is_rejected():
    data = fixture("global_plan_valid.json")
    data["scenes"][0]["relation_to_previous"]["kind"] = "repeat_the_same_image"
    with pytest.raises(ValidationError):
        VisualStoryPlan.model_validate(data)


def test_duplicate_scene_ids_are_rejected():
    data = fixture("global_plan_valid.json")
    data["scenes"][1]["scene_id"] = data["scenes"][0]["scene_id"]
    with pytest.raises(ValidationError, match="duplicate scene_id"):
        VisualStoryPlan.model_validate(data)


def test_global_plan_requires_exact_source_scene_coverage_and_order():
    plan = VisualStoryPlan.model_validate(fixture("global_plan_valid.json"))
    with pytest.raises(ValueError, match="coverage/order differs"):
        validate_global_plan_coverage(plan, list(reversed(source_scenes())))
    with pytest.raises(ValueError, match="coverage/order differs"):
        validate_global_plan_coverage(plan, source_scenes() + [{"id": "s_003", "voice_text": "Третья мысль."}])


def test_non_introducing_scene_relation_must_reference_an_earlier_scene():
    data = fixture("global_plan_valid.json")
    data["scenes"][1]["relation_to_previous"]["source_scene_id"] = "s_999"
    with pytest.raises(ValidationError, match="existing earlier scene"):
        VisualStoryPlan.model_validate(data)


def test_first_scene_must_introduce_story():
    data = fixture("global_plan_valid.json")
    data["scenes"][0]["relation_to_previous"]["kind"] = "contrasts"
    data["scenes"][0]["relation_to_previous"]["source_scene_id"] = "s_002"
    with pytest.raises(ValidationError):
        VisualStoryPlan.model_validate(data)


def test_duplicate_object_ids_are_rejected():
    data = fixture("scene_plan_complex_valid.json")
    data["shots"][0]["composition"]["objects"].append(copy.deepcopy(data["shots"][0]["composition"]["objects"][0]))
    with pytest.raises(ValidationError, match="duplicate object_id"):
        SceneCompositionPlan.model_validate(data)


def test_group_with_unknown_child_is_rejected():
    data = fixture("scene_plan_complex_valid.json")
    group = next(obj for obj in data["shots"][0]["composition"]["objects"] if obj["kind"] == "group")
    group["content"]["child_object_ids"].append("missing_child")
    with pytest.raises(ValidationError, match="group references unknown child"):
        SceneCompositionPlan.model_validate(data)


def test_group_cycles_are_rejected():
    data = fixture("scene_plan_complex_valid.json")
    objects = data["shots"][0]["composition"]["objects"]
    first_group = next(obj for obj in objects if obj["kind"] == "group")
    objects.append({
        "object_id": "nested_group", "kind": "group", "role": "supporting",
        "content": {"child_object_ids": [first_group["object_id"]]},
    })
    first_group["content"]["child_object_ids"].append("nested_group")
    with pytest.raises(ValidationError, match="group containment cycle"):
        SceneCompositionPlan.model_validate(data)


def test_node_graph_edges_must_reference_existing_nodes():
    data = fixture("scene_plan_complex_valid.json")
    graph = next(obj for obj in data["shots"][0]["composition"]["objects"] if obj["kind"] == "node_graph")
    graph["content"]["edges"][0]["to_node_id"] = "does_not_exist"
    with pytest.raises(ValidationError, match="unknown node"):
        SceneCompositionPlan.model_validate(data)


def test_chart_requires_explicit_data_provenance_or_illustrative_label():
    data = fixture("scene_plan_complex_valid.json")
    chart = next(obj for obj in data["shots"][0]["composition"]["objects"] if obj["kind"] == "chart")
    chart["content"]["data_mode"] = "provided"
    chart["content"].pop("illustrative_label")
    with pytest.raises(ValidationError, match="must reference approved values"):
        SceneCompositionPlan.model_validate(data)


def test_illustrative_chart_cannot_be_presented_without_disclaimer():
    data = fixture("scene_plan_complex_valid.json")
    chart = next(obj for obj in data["shots"][0]["composition"]["objects"] if obj["kind"] == "chart")
    chart["content"].pop("illustrative_label")
    with pytest.raises(ValidationError, match="illustrative_label"):
        SceneCompositionPlan.model_validate(data)


def test_event_target_must_exist():
    data = fixture("scene_plan_complex_valid.json")
    data["shots"][0]["events"][0]["target"]["object_id"] = "missing_object"
    with pytest.raises(ValidationError, match="event target object does not exist"):
        SceneCompositionPlan.model_validate(data)


def test_event_trigger_must_reference_declared_speech_anchor():
    data = fixture("scene_plan_complex_valid.json")
    data["shots"][0]["events"][0]["trigger"]["anchor_id"] = "missing_anchor"
    with pytest.raises(ValidationError, match="unknown speech anchor"):
        SceneCompositionPlan.model_validate(data)


def test_incompatible_action_is_rejected():
    data = fixture("scene_plan_complex_valid.json")
    event = data["shots"][0]["events"][0]
    event["action"] = "reveal_row"
    event["target"] = {"object_id": "desk_video", "subtarget_id": "row_not_real"}
    with pytest.raises(ValidationError, match="reveal_row requires a table target"):
        SceneCompositionPlan.model_validate(data)


def test_reveal_node_requires_existing_subtarget():
    data = fixture("scene_plan_complex_valid.json")
    data["shots"][0]["events"][0]["action"] = "reveal_node"
    data["shots"][0]["events"][0]["target"]["subtarget_id"] = "missing_node"
    with pytest.raises(ValidationError, match="declared node_graph node"):
        SceneCompositionPlan.model_validate(data)


def test_unknown_event_action_is_rejected():
    data = fixture("scene_plan_complex_valid.json")
    data["shots"][0]["events"][0]["action"] = "invented_animation"
    with pytest.raises(ValidationError):
        SceneCompositionPlan.model_validate(data)


def test_action_requires_typed_parameters():
    data = fixture("scene_plan_complex_valid.json")
    data["shots"][0]["events"][0]["action"] = "move"
    data["shots"][0]["events"][0].pop("subtarget_id", None)
    data["shots"][0]["events"][0]["target"] = {"object_id": "headline"}
    with pytest.raises(ValidationError, match="move is missing its required destination/target parameter"):
        SceneCompositionPlan.model_validate(data)


def test_voice_text_anchor_is_exact_and_hash_bound():
    data = fixture("scene_plan_complex_valid.json")
    plan = SceneCompositionPlan.model_validate(data)
    source = source_scenes()[0]
    validate_scene_composition_plan(plan, source)
    changed = dict(source, voice_text=source["voice_text"].replace("уверенно", "спокойно"))
    with pytest.raises(ValueError, match="source_text_sha256"):
        validate_scene_composition_plan(plan, changed)


def test_speech_anchor_cannot_cut_a_word_or_change_the_excerpt():
    data = fixture("scene_plan_complex_valid.json")
    plan = SceneCompositionPlan.model_validate(data)
    source = source_scenes()[0]
    bad = copy.deepcopy(data)
    anchor = bad["shots"][0]["speech_anchors"][0]
    anchor["char_start"] = 1
    anchor["text_excerpt"] = source["voice_text"][1:]
    anchor["char_end"] = len(source["voice_text"])
    mutated = SceneCompositionPlan.model_validate(bad)
    with pytest.raises(ValueError, match="splits a word"):
        validate_scene_composition_plan(mutated, source)


def test_scene_plan_coverage_requires_one_plan_per_source_scene_in_order():
    plans = [
        SceneCompositionPlan.model_validate(fixture("scene_plan_complex_valid.json")),
        SceneCompositionPlan.model_validate(fixture("scene_plan_contrast_valid.json")),
    ]
    validate_scene_plan_coverage(plans, source_scenes())
    with pytest.raises(ValueError, match="coverage/order differs"):
        validate_scene_plan_coverage(list(reversed(plans)), source_scenes())


def test_incoming_contrast_reference_resolves_to_existing_earlier_shot():
    plans = [
        SceneCompositionPlan.model_validate(fixture("scene_plan_complex_valid.json")),
        SceneCompositionPlan.model_validate(fixture("scene_plan_contrast_valid.json")),
    ]
    validate_continuity_references(plans)
    assert plans[1].shots[0].continuity.incoming[0].continuity_id is None


def test_unresolved_continuity_reference_is_rejected():
    plans_data = [fixture("scene_plan_complex_valid.json"), fixture("scene_plan_contrast_valid.json")]
    plans_data[1]["shots"][0]["continuity"]["incoming"][0]["reference"]["shot_id"] = "missing_shot"
    plans = [SceneCompositionPlan.model_validate(item) for item in plans_data]
    with pytest.raises(ValueError, match="unknown shot"):
        validate_continuity_references(plans)


def test_render_capability_registry_has_no_duplicate_values():
    with pytest.raises(ValidationError, match="duplicate supported kind"):
        RendererCapabilities(
            renderer_id="pillow", renderer_version="1.0",
            supported_kinds=["image", "image"], supported_formats=["png"],
        )


def test_video_asset_request_requires_qualitative_duration_but_no_seconds():
    valid = AssetRequest(
        request_id="asset_01", scene_id="s_001", shot_id="shot_01", object_id="video_01",
        media_kind="video", description="Hands typing at a laptop", search_queries=["hands typing laptop"],
        crop_intent="keep hands and screen visible", provider_allowlist=["pixabay"], desired_duration="medium",
    )
    assert valid.desired_duration == "medium"
    data = valid.model_dump()
    data["duration_sec"] = 5.0
    with pytest.raises(ValidationError):
        AssetRequest.model_validate(data)


def test_execution_plan_error_diagnostic_forces_blocked_status():
    args = dict(
        run_id="run_001", storyboard_sha256="a" * 64, global_plan_sha256="b" * 64,
        scene_plan_sha256={"s_001": "c" * 64}, capability_registry_version="registry-0",
        output_spec=ExecutionOutputSpec(width=1920, height=1080, fps=25),
    )
    with pytest.raises(ValidationError, match="cannot have status=ready"):
        ExecutionPlan(**args, diagnostics=[{"code":"renderer.unsupported","severity":"error","message":"Renderer action is not supported"}])
    plan = ExecutionPlan(**args, status="blocked", diagnostics=[{"code":"renderer.unsupported","severity":"error","message":"Renderer action is not supported"}])
    assert plan.status == "blocked"


def test_blocked_execution_plan_requires_error_diagnostic():
    args = dict(
        run_id="run_001", storyboard_sha256="a" * 64, global_plan_sha256="b" * 64,
        scene_plan_sha256={"s_001": "c" * 64}, capability_registry_version="registry-0",
        output_spec=ExecutionOutputSpec(width=1920, height=1080, fps=25), status="blocked",
    )
    with pytest.raises(ValidationError, match="must contain at least one error diagnostic"):
        ExecutionPlan(**args)


def test_global_plan_storyboard_fingerprint_is_verified():
    from kadr_farm.visual_story_contract_v2 import validate_global_plan_against_storyboard

    storyboard = {"schema_version": "1.0-draft", "title": "Test", "scenes": source_scenes()}
    canonical = json.dumps(storyboard, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    data = fixture("global_plan_valid.json")
    data["storyboard_sha256"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    plan = VisualStoryPlan.model_validate(data)
    validate_global_plan_against_storyboard(plan, storyboard)
    storyboard["title"] = "changed"
    with pytest.raises(ValueError, match="storyboard_sha256"):
        validate_global_plan_against_storyboard(plan, storyboard)


def test_formula_rejects_arbitrary_code_and_unknown_identifiers():
    data = fixture("scene_plan_complex_valid.json")
    formula = next(obj for obj in data["shots"][0]["composition"]["objects"] if obj["kind"] == "formula")
    formula["content"]["expression"] = "__import__('os').system('whoami')"
    formula["content"]["value_refs"] = ["__import__", "os", "system"]
    with pytest.raises(ValidationError, match="unsupported syntax"):
        SceneCompositionPlan.model_validate(data)

    data = fixture("scene_plan_complex_valid.json")
    formula = next(obj for obj in data["shots"][0]["composition"]["objects"] if obj["kind"] == "formula")
    formula["content"]["value_refs"] = ["unexpected_ref"]
    with pytest.raises(ValidationError, match="exactly match value_refs"):
        SceneCompositionPlan.model_validate(data)


def test_numeric_text_requires_source_reference():
    data = fixture("scene_plan_complex_valid.json")
    headline = next(obj for obj in data["shots"][0]["composition"]["objects"] if obj["object_id"] == "headline")
    headline["content"]["text"] = "5 ПРИЧИН УВЕРЕННОГО ОТВЕТА"
    headline["content"]["emphasis_spans"] = []
    headline["content"].pop("text_source_ref")
    with pytest.raises(ValidationError, match="numeric text requires"):
        SceneCompositionPlan.model_validate(data)
    headline["content"]["text_source_ref"] = "approved_fact:reason_count"
    SceneCompositionPlan.model_validate(data)


def test_numeric_table_literal_requires_source_reference():
    data = fixture("scene_plan_complex_valid.json")
    table = next(obj for obj in data["shots"][0]["composition"]["objects"] if obj["kind"] == "table")
    table["content"]["data_mode"] = "provided"
    table["content"].pop("illustrative_label")
    table["content"]["rows"][0]["cells"][1] = {"text": "12"}
    with pytest.raises(ValidationError, match="numeric table text requires source_ref"):
        SceneCompositionPlan.model_validate(data)
    table["content"]["rows"][0]["cells"][1]["source_ref"] = "approved_fact:value_12"
    for row in table["content"]["rows"]:
        for cell in row["cells"]:
            if "illustrative_value" in cell:
                cell["value_ref"] = "approved_fact:" + row["row_id"]
                cell.pop("illustrative_value")
    SceneCompositionPlan.model_validate(data)


def test_execution_plan_render_task_dependencies_must_be_acyclic():
    from kadr_farm.visual_story_contract_v2 import RenderTask, RendererTarget

    args = dict(
        run_id="run_001", storyboard_sha256="a" * 64, global_plan_sha256="b" * 64,
        scene_plan_sha256={"s_001": "c" * 64}, capability_registry_version="registry-0",
        output_spec=ExecutionOutputSpec(width=1920, height=1080, fps=25),
    )
    target = RendererTarget(renderer_id="pillow", renderer_version="1.0")
    task_a = RenderTask(task_id="task_a", scene_id="s_001", shot_id="shot_01", object_id="obj_a", target=target, depends_on=["task_b"], input_fingerprint="d" * 64, expected_media_type="image/png")
    task_b = RenderTask(task_id="task_b", scene_id="s_001", shot_id="shot_01", object_id="obj_b", target=target, depends_on=["task_a"], input_fingerprint="e" * 64, expected_media_type="image/png")
    with pytest.raises(ValidationError, match="dependency cycle"):
        ExecutionPlan(**args, render_tasks=[task_a, task_b])


def test_continuity_id_must_be_declared_on_current_shot_object_too():
    source = fixture("scene_plan_complex_valid.json")
    source_headline = next(obj for obj in source["shots"][0]["composition"]["objects"] if obj["object_id"] == "headline")
    source_headline["continuity_id"] = "headline_theme"
    target = fixture("scene_plan_contrast_valid.json")
    link = target["shots"][0]["continuity"]["incoming"][0]
    link["continuity_id"] = "headline_theme"
    link["reference"]["object_id"] = "headline"
    plans = [SceneCompositionPlan.model_validate(source), SceneCompositionPlan.model_validate(target)]
    with pytest.raises(ValueError, match="current shot"):
        validate_continuity_references(plans)


def test_formula_rejects_unreferenced_numeric_constants():
    data = fixture("scene_plan_complex_valid.json")
    formula = next(obj for obj in data["shots"][0]["composition"]["objects"] if obj["kind"] == "formula")
    formula["content"]["expression"] = "evidence_count / 2"
    formula["content"]["value_refs"] = ["evidence_count"]
    with pytest.raises(ValidationError, match="numeric constants must be declared"):
        SceneCompositionPlan.model_validate(data)


def test_numeric_graph_node_label_requires_provenance():
    data = fixture("scene_plan_complex_valid.json")
    graph = next(obj for obj in data["shots"][0]["composition"]["objects"] if obj["kind"] == "node_graph")
    graph["content"]["nodes"][0]["label"] = "12 источников"
    graph["content"]["nodes"][0].pop("source_ref", None)
    with pytest.raises(ValidationError, match="numeric graph node label requires"):
        SceneCompositionPlan.model_validate(data)
    graph["content"]["nodes"][0]["source_ref"] = "approved_fact:source_count"
    SceneCompositionPlan.model_validate(data)


def test_non_finite_numeric_values_are_rejected():
    data = fixture("scene_plan_complex_valid.json")
    counter = next(obj for obj in data["shots"][0]["composition"]["objects"] if obj["kind"] == "counter")
    counter["content"]["data_mode"] = "illustrative"
    counter["content"].pop("value_ref", None)
    counter["content"]["illustrative_label"] = "Иллюстративный пример"
    counter["content"]["illustrative_value"] = float("nan")
    with pytest.raises(ValidationError):
        SceneCompositionPlan.model_validate(data)


def test_director_contract_cannot_rewrite_or_duplicate_source_voice_text():
    global_data = fixture("global_plan_valid.json")
    global_data["scenes"][0]["voice_text"] = "изменённая речь"
    with pytest.raises(ValidationError):
        VisualStoryPlan.model_validate(global_data)

    scene_data = fixture("scene_plan_complex_valid.json")
    scene_data["voice_text"] = "другая речь"
    with pytest.raises(ValidationError):
        SceneCompositionPlan.model_validate(scene_data)


def test_complete_bundle_validates_storyboard_hash_and_all_passes_together():
    from kadr_farm.visual_story_contract_v2 import validate_visual_story_bundle

    storyboard = {
        "schema_version": "1.0-draft",
        "title": "Test",
        "scenes": source_scenes(),
    }
    canonical = json.dumps(storyboard, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    global_data = fixture("global_plan_valid.json")
    global_data["storyboard_sha256"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    global_plan = VisualStoryPlan.model_validate(global_data)
    scene_plans = [
        SceneCompositionPlan.model_validate(fixture("scene_plan_complex_valid.json")),
        SceneCompositionPlan.model_validate(fixture("scene_plan_contrast_valid.json")),
    ]
    validate_visual_story_bundle(global_plan, scene_plans, storyboard)
    storyboard["title"] = "tampered"
    with pytest.raises(ValueError, match="storyboard_sha256"):
        validate_visual_story_bundle(global_plan, scene_plans, storyboard)

@pytest.mark.parametrize(
    ("fixture_name", "model"),
    [
        ("global_plan_invalid_unknown_field.json", VisualStoryPlan),
        ("global_plan_invalid_enum.json", VisualStoryPlan),
        ("scene_plan_invalid_missing_object_ref.json", SceneCompositionPlan),
        ("scene_plan_invalid_incompatible_action.json", SceneCompositionPlan),
        ("scene_plan_invalid_anchor_span.json", SceneCompositionPlan),
    ],
)
def test_invalid_json_fixtures_are_rejected(fixture_name, model):
    with pytest.raises(ValidationError):
        model.model_validate(fixture(fixture_name))
