import pytest
from pydantic import ValidationError
from kadr_farm.models import Scene

def base_scene():
    return dict(id="s_001", block_id="b1", voice_text="This is a sufficiently long spoken narration.",
        meaning="Describe the meaning", purpose="Explain the purpose", visual_type="diagram",
        visual_description="A visual description long enough to be meaningful",
        asset_strategy="rendered", provider_id="internal_renderer", gen_prompt=None)

def test_timing_must_be_complete_and_consistent():
    with pytest.raises(ValidationError):
        Scene(**base_scene(), duration_sec=4.0, start_sec=0.0, end_sec=5.0)
    valid=Scene(**base_scene(), duration_sec=4.0, start_sec=0.0, end_sec=4.0)
    assert valid.end_sec-valid.start_sec == valid.duration_sec

def test_screen_text_limit_is_enforced_after_llm_stage():
    with pytest.raises(ValidationError):
        Scene(**base_scene(), on_screen_text=["Эта надпись явно длиннее сорока символов и должна быть отклонена валидатором"])
