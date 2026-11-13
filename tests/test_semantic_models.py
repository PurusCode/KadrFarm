import pytest
from pydantic import ValidationError
from kadr_farm.models import (
    ChartData, Claim, ClaimStatus, SourceAvailability,
    VisualBeatDraft, VisualPlanDraft, VisualType, AssetStrategy, Scene,
)

def test_chart_requires_matching_labels_values_and_source():
    with pytest.raises(ValidationError):
        ChartData(title="Test", labels=["A", "B"], values=[1],
                  unit="%", source_url="https://example.org", methodology="Test")
    chart = ChartData(title="Test", labels=["A", "B"], values=[1, 2],
                      unit="%", source_url="https://example.org", methodology="Source table 1")
    assert len(chart.labels) == len(chart.values)

def test_verified_claim_requires_human_approval():
    with pytest.raises(ValidationError):
        Claim(text="Claim with a factual assertion", source_urls=["https://example.org"],
              status=ClaimStatus.VERIFIED, source_availability=[SourceAvailability.REACHABLE])
    claim = Claim(text="Claim with a factual assertion", source_urls=["https://example.org"],
                  status=ClaimStatus.VERIFY_BEFORE_PUBLISH,
                  source_availability=[SourceAvailability.REACHABLE])
    assert not claim.verified_by_human

def test_visual_beats_normalize_to_one_whole_scene():
    beat = dict(visual_type=VisualType.BROLL, visual_description="A long visual description",
                asset_strategy=AssetStrategy.STOCK, provider_id="pixabay", gen_prompt=None,
                search_queries=["technology"], sub_duration_ratio=0.35, on_screen_text=[])
    base = dict(scene_id="s_001", visual_type=VisualType.DIAGRAM,
        visual_description="A sufficiently detailed visual description", visual_beats=[beat, beat],
        asset_strategy=AssetStrategy.RENDERED, provider_id="internal_renderer", gen_prompt=None,
        search_queries=[], on_screen_text=[], speaker_emotion=None, pacing="normal",
        camera_move="static", music_cue=None, audio_sfx=[], claims=[], chart_data=None,
        humor_note="", transition_in="cut", transition_out="cut")
    plan = VisualPlanDraft(**base)
    ratios = [item.sub_duration_ratio for item in plan.visual_beats]
    assert ratios == [0.5, 0.5]
    assert sum(ratios) == 1.0

def test_scene_rejects_unknown_fields():
    with pytest.raises(ValidationError):
        Scene(id="s_001", block_id="b1", voice_text="Long enough spoken narration",
              meaning="A meaningful phrase", purpose="A clear scene purpose",
              visual_type="diagram", visual_description="A sufficiently detailed description",
              asset_strategy="rendered", provider_id="internal_renderer", gen_prompt=None,
              unexpected="not allowed")


def test_stock_strategy_cannot_claim_pexels_provider():
    with pytest.raises(ValidationError, match="provider_id=pixabay"):
        VisualPlanDraft(scene_id="s_001", visual_type="photo",
            visual_description="A photo visual description that is sufficiently detailed",
            visual_beats=[], asset_strategy="stock", provider_id="pexels", gen_prompt=None,
            search_queries=["technology"], on_screen_text=[], speaker_emotion=None,
            pacing="normal", camera_move="static", music_cue=None, audio_sfx=[],
            claims=[], chart_data=None, humor_note="", transition_in="cut", transition_out="cut")


def test_chart_visual_beat_uses_parent_chart_data_and_internal_renderer():
    beat = dict(visual_type="chart", visual_description="An actual data chart beat",
        asset_strategy="rendered", provider_id="internal_renderer", gen_prompt=None,
        search_queries=[], sub_duration_ratio=1.0, on_screen_text=[])
    chart = dict(title="Observed data", labels=["A","B"], values=[-1,2], unit="%",
        source_url="https://example.org/data", methodology="Values copied from source table")
    plan=VisualPlanDraft(scene_id="s_001", visual_type="diagram",
        visual_description="A sufficiently detailed scene-level composition", visual_beats=[beat],
        asset_strategy="rendered", provider_id="internal_renderer", gen_prompt=None,
        search_queries=[], on_screen_text=[], speaker_emotion=None, pacing="normal",
        camera_move="static", music_cue=None, audio_sfx=[], claims=[], chart_data=chart,
        humor_note="", transition_in="cut", transition_out="cut")
    assert plan.chart_data.values == [-1.0, 2.0]
