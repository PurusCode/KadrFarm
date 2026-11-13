import pytest

from kadr_farm.director_shots import _check_group_coverage


def shot(shot_id, source_beat_id, spoken_text):
    return {"shot_id": shot_id, "source_beat_id": source_beat_id,
            "spoken_text": spoken_text}


def test_shot_coverage_allows_meaningful_subdivision():
    _check_group_coverage(
        [{"spoken_text": "Сначала запрос. Затем ответ."}],
        [shot("b01_s01", "b01", "Сначала запрос."),
         shot("b01_s02", "b01", "Затем ответ.")],
        "s_001",
    )


def test_shot_coverage_accepts_one_shot_for_a_whole_beat():
    _check_group_coverage(
        [{"spoken_text": "Один кадр удерживает всю мысль."}],
        [shot("b01_s01", "b01", "Один кадр удерживает всю мысль.")],
        "s_001",
    )


def test_shot_coverage_rejects_missing_words():
    with pytest.raises(ValueError, match="exactly preserve"):
        _check_group_coverage(
            [{"spoken_text": "Сначала запрос. Затем ответ."}],
            [shot("b01_s01", "b01", "Сначала запрос.")],
            "s_001",
        )


def test_shot_coverage_rejects_reordered_fragments():
    with pytest.raises(ValueError, match="exactly preserve"):
        _check_group_coverage(
            [{"spoken_text": "Сначала запрос. Затем ответ."}],
            [shot("b01_s01", "b01", "Затем ответ."),
             shot("b01_s02", "b01", "Сначала запрос.")],
            "s_001",
        )


def test_shot_coverage_rejects_unknown_source_beat():
    with pytest.raises(ValueError, match="unknown source_beat_id"):
        _check_group_coverage(
            [{"spoken_text": "Фраза."}],
            [shot("b02_s01", "b02", "Фраза.")],
            "s_001",
        )


def test_shot_coverage_rejects_duplicate_shot_ids():
    with pytest.raises(ValueError, match="duplicate shot_id"):
        _check_group_coverage(
            [{"spoken_text": "Фраза один. Фраза два."}],
            [shot("b01_s01", "b01", "Фраза один."),
             shot("b01_s01", "b01", "Фраза два.")],
            "s_001",
        )


def test_shot_coverage_rejects_more_than_four_shots_per_beat():
    with pytest.raises(ValueError, match="more than four"):
        _check_group_coverage(
            [{"spoken_text": "А. Б. В. Г. Д."}],
            [shot(f"b01_s{i:02d}", "b01", text)
             for i, text in enumerate(["А.", "Б.", "В.", "Г.", "Д."], 1)],
            "s_001",
        )
