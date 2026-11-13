import pytest
from kadr_farm.storyboard import validate_block_coverage, validate_storyboard_coverage

def test_valid_complete_coverage():
    validate_block_coverage("Первая мысль. Вторая мысль.", ["Первая мысль.", "Вторая мысль."], "b1")

@pytest.mark.parametrize("segments", [
    ["Первая мысль."],                              # missing sentence/end
    ["Первая мысль.", "Первая мысль."],             # duplicate
    ["Вторая мысль.", "Первая мысль."],             # reordered
    ["Перефразированная мысль.", "Вторая мысль."],  # paraphrase
])
def test_invalid_coverage_rejected(segments):
    with pytest.raises(ValueError):
        validate_block_coverage("Первая мысль. Вторая мысль.", segments, "b1")

def test_detects_uncovered_middle_text():
    with pytest.raises(ValueError):
        validate_block_coverage("A. B. C.", ["A.", "C."], "b1")

def test_blocks_cannot_be_moved_or_mixed():
    script = {"blocks":[{"id":"b1","voice_text":"Первый блок."},
                        {"id":"b2","voice_text":"Второй блок."}]}
    storyboard = {"scenes":[
        {"id":"s_001","block_id":"b2","voice_text":"Второй блок."},
        {"id":"s_002","block_id":"b1","voice_text":"Первый блок."},
    ]}
    with pytest.raises(ValueError, match="переставлены"):
        validate_storyboard_coverage(script, storyboard)
