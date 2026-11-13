from kadr_farm.llm_structured import _schema_for_provider
from kadr_farm.models import StoryboardResponse

def test_generated_json_schema_is_strict_and_all_properties_required():
    schema=_schema_for_provider(StoryboardResponse)
    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(schema["properties"])
