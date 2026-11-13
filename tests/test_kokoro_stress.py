from __future__ import annotations

import json
from pathlib import Path

import pytest

from kadr_farm.kokoro_stress import (
    apply_contextual_stress,
    load_stress_config,
    stress_config_sha256,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = PROJECT_ROOT / "data" / "kokoro_stress_config.json"


@pytest.fixture(scope="module")
def stress_config():
    return load_stress_config(DEFAULT_CONFIG)


def test_loads_user_dictionary_and_context_rules(stress_config):
    assert stress_config["ruaccent"]["custom_dict"] == {"пора": "пор+а"}
    assert stress_config["ruaccent"]["custom_homographs"] == {}
    assert [rule["id"] for rule in stress_config["context_rules"]] == [
        "door_lock_nominative_singular"
    ]


def test_fixes_door_lock_when_ruaccent_marks_either_syllable(stress_config):
    rules = stress_config["context_rules"]
    assert apply_contextual_stress("Дверн+ой з+амок сломался.", rules) == "Дверн+ой зам+ок сломался."
    assert apply_contextual_stress("Дверн+ой зам+ок сломался.", rules) == "Дверн+ой зам+ок сломался."
    assert apply_contextual_stress("дверной замок, конечно, сломался.", rules) == "дверной зам+ок, конечно, сломался."


def test_does_not_change_castle_or_unverified_word_forms(stress_config):
    rules = stress_config["context_rules"]
    castle = "Старый з+амок стоял на холме."
    oblique_form = "Ремонт дверного замка завершён."
    assert apply_contextual_stress(castle, rules) == castle
    # The genitive form has not been evaluated and is deliberately not guessed.
    assert apply_contextual_stress(oblique_form, rules) == oblique_form


def test_rule_requires_the_door_adjective_as_a_separate_word(stress_config):
    rules = stress_config["context_rules"]
    unrelated = "Недверн+ой з+амок не является словосочетанием из правила."
    assert apply_contextual_stress(unrelated, rules) == unrelated


def test_config_hash_is_stable_for_unchanged_file():
    first = stress_config_sha256(DEFAULT_CONFIG)
    second = stress_config_sha256(DEFAULT_CONFIG)
    assert len(first) == 64
    assert first == second


def test_rejects_invalid_regex_in_config(tmp_path):
    bad_config = {
        "schema_version": 1,
        "ruaccent": {"custom_dict": {}, "custom_homographs": {}},
        "context_rules": [
            {
                "id": "bad",
                "pattern": "(?",
                "replacement": "x",
            }
        ],
    }
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(bad_config), encoding="utf-8")
    with pytest.raises(ValueError, match="Некорректное регулярное выражение"):
        load_stress_config(path)
