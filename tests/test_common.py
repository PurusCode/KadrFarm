from kadr_farm.common import parse_json_response, word_count, slugify
from kadr_farm.youtube import iso_duration_seconds
from kadr_farm.tts import normalize_for_speech
from kadr_farm import text_normalization as normalizer


def _fake_num2words(value, lang="ru", to="cardinal", case=None, gender=None):
    key = str(value).replace(",", ".")
    table = {
        ("10", "cardinal", None): "десять",
        ("12.5", "cardinal", None): "двенадцать целых пять десятых",
        ("8", "cardinal", None): "восемь",
        ("16", "ordinal", "g"): "шестнадцатого",
        ("2026", "ordinal", "g"): "две тысячи двадцать шестого",
    }
    return table.get((key, to, case)) or table.get((key, to, None)) or f"ЧИСЛО_{key}"

def test_parse_json_with_fence():
    assert parse_json_response('```json\n{"ok": true}\n```') == {"ok": True}

def test_word_count_and_slug():
    assert word_count("Привет, мир! DeepSeek-V4.1") == 3
    assert slugify("ИИ и технологии: обзор") == "ии-и-технологии-обзор"

def test_duration():
    assert iso_duration_seconds("PT1H2M3S") == 3723
    assert iso_duration_seconds("PT7M") == 420
    assert iso_duration_seconds("bad") == 0

def test_speech_normalization(monkeypatch):
    monkeypatch.setattr(normalizer, "_num2words", _fake_num2words)
    text = normalize_for_speech("DeepSeek V4.1-Flash использует API и 10% памяти")
    assert "ДипСик" in text
    assert "эй пи ай" in text
    assert "десять процентов" in text

def test_speech_normalization_handles_model_name_and_units(monkeypatch):
    monkeypatch.setattr(normalizer, "_num2words", _fake_num2words)
    text = normalize_for_speech("DeepSeek V4.1 Flash: 12.5% памяти, 8 GB и GPT-OSS")
    assert "ДипСик Ви четыре точка один Флэш" in text
    assert "двенадцать целых пять десятых процента" in text
    assert "восемь гигабайт" in text
    assert "джи пи ти осс" in text


def test_speech_normalization_expands_numeric_russian_date(monkeypatch):
    monkeypatch.setattr(normalizer, "_num2words", _fake_num2words)
    text = normalize_for_speech("Дата публикации — 16.10.2026.")
    assert "шестнадцатого октября две тысячи двадцать шестого года" in text
    assert "16.10.2026" not in text
