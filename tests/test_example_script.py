from pathlib import Path

from kadr_farm.common import read_json, word_count
from kadr_farm.content import FALLBACK_TOPICS, fit_title, validate_script

ROOT = Path(__file__).resolve().parents[1]


def test_example_script_meets_mvp_structure():
    script = read_json(ROOT / "examples" / "deepseek_v4_1_flash" / "script.json")
    assert 55 <= len(script["title"]) <= 70
    assert 1100 <= word_count(" ".join(b["voice_text"] for b in script["blocks"])) <= 1500
    assert len(script["blocks"]) == 7
    assert script["blocks"][0]["kind"] == "hook"
    assert script["blocks"][-1]["kind"] == "cta"
    assert all(3 <= len(b["slides"]) <= 5 for b in script["blocks"])
    assert all(2 <= len(b["broll_keywords"]) <= 5 for b in script["blocks"])
    assert validate_script(script) == []


def test_fallback_topic_titles_fit_spec():
    titles = [fit_title(t[0]) for t in FALLBACK_TOPICS]
    assert len(titles) == 20
    assert all(55 <= len(t) <= 70 for t in titles)
    assert len({t.casefold() for t in titles}) == len(titles)
