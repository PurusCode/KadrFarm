from types import SimpleNamespace
from kadr_farm.llm_structured import StructuredLLMRouter
from kadr_farm.models import StoryboardResponse

class FakeCompletions:
    def __init__(self, contents):
        self.contents=list(contents); self.calls=[]
    def create(self, **kwargs):
        self.calls.append(kwargs)
        content=self.contents.pop(0)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])

class FakeClient:
    def __init__(self, contents): self.completions=FakeCompletions(contents)
    @property
    def chat(self): return SimpleNamespace(completions=self.completions)

def test_invalid_llm_json_gets_one_repair_and_local_validation():
    client=FakeClient([
        '{"segments": []}',
        '{"segments": [{"voice_text":"Речь сцены.","meaning":"Смысл сцены","purpose":"Объяснить мысль"}]}'
    ])
    router=StructuredLLMRouter.__new__(StructuredLLMRouter)
    router.router=SimpleNamespace(clients=[("mock","mock-model",client)])
    result=router.complete(StoryboardResponse,"system","user")
    assert len(result.segments)==1
    assert len(client.completions.calls)==2
    assert client.completions.calls[0]["response_format"]["type"]=="json_schema"

def test_invalid_response_is_not_retried_forever():
    client=FakeClient(['{"wrong":true}', '{"still_wrong":true}'])
    router=StructuredLLMRouter.__new__(StructuredLLMRouter)
    router.router=SimpleNamespace(clients=[("mock","mock-model",client)])
    try:
        router.complete(StoryboardResponse,"system","user")
    except Exception:
        pass
    assert len(client.completions.calls)==2


def test_pydantic_validator_valueerror_is_serialized_and_repaired():
    import json
    from kadr_farm.models import Claim

    invalid = {
        "text": "Это проверяемое фактическое утверждение.",
        "source_urls": ["https://example.com/source"],
        "status": "verified",
        "source_availability": ["not_checked"],
        "verified_by_human": False,
    }
    repaired = {
        **invalid,
        "status": "verify_before_publish",
    }
    client = FakeClient([json.dumps(invalid), json.dumps(repaired)])
    router = StructuredLLMRouter.__new__(StructuredLLMRouter)
    router.router = SimpleNamespace(clients=[("mock", "mock-model", client)])

    result = router.complete(Claim, "system", "user")

    assert result.status.value == "verify_before_publish"
    assert len(client.completions.calls) == 2
    repair_prompt = client.completions.calls[1]["messages"][1]["content"]
    assert "ValueError" in repair_prompt
    assert "только после явного подтверждения человеком" in repair_prompt
