

from kadr_farm.common import word_count
from kadr_farm.content import FALLBACK_TOPICS, fit_title, validate_script




def test_example_script_meets_mvp_structure():
    # Ten words per sentence.
    # 10 words x 16 repetitions x 7 blocks = 1120 words.
    sentence = (
        "We check AI answers against sources and record uncertain claims."
    )
    voice_text = " ".join([sentence] * 16)

    kinds = [
        "hook",
        "section",
        "section",
        "section",
        "section",
        "section",
        "cta",
    ]

    blocks = []

    for index, kind in enumerate(kinds, 1):
        blocks.append({
            "id": f"block_{index}",
            "kind": kind,
            "heading": f"Section {index}",
            "voice_text": voice_text,
            "slides": [
                {
                    "type": "text",
                    "title": f"Idea {slide_index}",
                    "body": "Check the source",
                    "metric": "",
                    "photo_keyword": "",
                    "items": [],
                    "source": "",
                }
                for slide_index in range(1, 4)
            ],
            "broll_keywords": [
                "person checking documents",
                "computer research",
            ],
        })

    script = {
        "title": "How to check AI answers and catch errors before trusting them",
        "topic": "Checking AI answers",
        "description_summary": "Synthetic scenario for validator structure testing.",
        "hashtags": ["#AI", "#verification"],
        "target_duration_minutes": 8,
        "fact_checks": [],
        "music_policy": "No music without a confirmed license.",
        "blocks": blocks,
    }

    assert 55 <= len(script["title"]) <= 70

    assert 1100 <= word_count(
        " ".join(block["voice_text"] for block in script["blocks"])
    ) <= 1500

    assert [block["kind"] for block in script["blocks"]] == kinds

    assert all(
        len(block["slides"]) == 3
        for block in script["blocks"]
    )

    assert all(
        2 <= len(block["broll_keywords"]) <= 5
        for block in script["blocks"]
    )

    assert all(
        keyword.isascii() and any(char.isalpha() for char in keyword)
        for block in script["blocks"]
        for keyword in block["broll_keywords"]
    )

    assert validate_script(script) == []

def test_fallback_topic_titles_fit_spec():
    titles = [fit_title(t[0]) for t in FALLBACK_TOPICS]
    assert len(titles) == 20
    assert all(55 <= len(t) <= 70 for t in titles)
    assert len({t.casefold() for t in titles}) == len(titles)
