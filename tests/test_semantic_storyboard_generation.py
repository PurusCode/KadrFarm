import json
from pathlib import Path
from kadr_farm import storyboard
from kadr_farm.models import StoryboardResponse

class FakeStructuredLLM:
    def __init__(self, _settings): pass
    def complete(self, schema, _system, prompt, **_kwargs):
        assert schema is StoryboardResponse
        assert "Первая мысль. Вторая мысль." in prompt
        return StoryboardResponse(segments=[
            {"voice_text":"Первая мысль.", "meaning":"Первая мысль", "purpose":"Вводный пример"},
            {"voice_text":"Вторая мысль.", "meaning":"Вторая мысль", "purpose":"Развитие объяснения"},
        ])

def test_create_storyboard_assigns_stable_ids_and_preserves_voice(tmp_path, monkeypatch):
    source=tmp_path/"script.json"; output=tmp_path/"storyboard.json"
    source.write_text(json.dumps({"title":"Тема", "blocks":[
        {"id":"block_a", "voice_text":"Первая мысль. Вторая мысль."}
    ]}, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(storyboard, "StructuredLLMRouter", FakeStructuredLLM)
    result=storyboard.create_storyboard(object(), source, output)
    assert [x["id"] for x in result["scenes"]] == ["s_001", "s_002"]
    assert "visual_type" not in result["scenes"][0]
    assert output.is_file()
