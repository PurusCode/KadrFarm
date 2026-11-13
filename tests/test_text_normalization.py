from __future__ import annotations

import pytest

from kadr_farm import text_normalization as norm


def _fake_num2words(value, lang="ru", to="cardinal", case=None, gender=None):
    key = str(value).replace(",", ".")
    table = {
        ("25", "ordinal", "g"): "двадцать пятого",
        ("16", "ordinal", "g"): "шестнадцатого",
        ("2026", "ordinal", "g"): "две тысячи двадцать шестого",
        ("2026", "ordinal", "n"): "две тысячи двадцать шестой",
        ("2026", "ordinal", "p"): "две тысячи двадцать шестом",
        ("14", "cardinal", None): "четырнадцать",
        ("35", "cardinal", None): "тридцать пять",
        ("3.7", "cardinal", None): "три целых семь десятых",
        ("12.5", "cardinal", None): "двенадцать целых пять десятых",
        ("21.5", "cardinal", None): "двадцать одна целая пять десятых",
        ("1.25", "cardinal", None): "одна целая двадцать пять сотых",
        ("4500", "cardinal", None): "четыре тысячи пятьсот",
        ("8", "cardinal", None): "восемь",
        ("2", "cardinal", None): "два",
        ("5", "cardinal", None): "пять",
        ("3", "cardinal", None): "три",
    }
    gendered = {
        ("1", "m"): "один", ("1", "f"): "одна",
        ("2", "m"): "два", ("2", "f"): "две",
        ("21", "m"): "двадцать один", ("21", "f"): "двадцать одна",
        ("22", "m"): "двадцать два", ("22", "f"): "двадцать две",
    }
    result = gendered.get((key, gender)) if gender else None
    if result is None:
        result = table.get((key, to, case)) or table.get((key, to, None))
    if result is None:
        result = {"1": "один", "2": "два", "4": "четыре", "10": "десять"}.get(key, f"ЧИСЛО_{key}")
    return result


def test_prenormalizes_date_time_percent_measurement_and_currency(monkeypatch):
    monkeypatch.setattr(norm, "_num2words", _fake_num2words)
    source = (
        "Проверка проводится 25 октября 2026 года, в 14:35. "
        "Погрешность составила 3,7%; температура — 21,5 градуса. "
        "Файл занимает 1,25 гигабайта, а стоимость проекта — 4 500 рублей."
    )
    result = norm.normalize_for_speech(source)
    assert "двадцать пятого октября две тысячи двадцать шестого года" in result
    assert "четырнадцать часов тридцать пять минут" in result
    assert "три целых семь десятых процента" in result
    assert "двадцать одна целая пять десятых градуса" in result
    assert "одна целая двадцать пять сотых гигабайта" in result
    assert "четыре тысячи пятьсот рублей" in result
    assert norm.find_numeric_tokens(result) == []


def test_numeric_date_is_rendered_as_russian_ordinal_date(monkeypatch):
    monkeypatch.setattr(norm, "_num2words", _fake_num2words)
    result = norm.normalize_for_speech("Дата: 16.10.2026.")
    assert result == "Дата: шестнадцатого октября две тысячи двадцать шестого года."


def test_percent_and_capacity_abbreviations_expand(monkeypatch):
    monkeypatch.setattr(norm, "_num2words", _fake_num2words)
    result = norm.normalize_for_speech("Погрешность 12.5%; размер файла 8 GB.")
    assert "двенадцать целых пять десятых процента" in result
    assert "восемь гигабайт" in result
    assert norm.find_numeric_tokens(result) == []


def test_qwen_model_number_is_spoken_as_version_and_parameter_size(monkeypatch):
    monkeypatch.setattr(norm, "_num2words", _fake_num2words)
    result = norm.normalize_for_speech("Модель Qwen 2.5 3B формирует черновик.")
    assert "Кьювэн два точка пять, три би" in result
    assert norm.find_numeric_tokens(result) == []


def test_when_num2words_is_missing_digits_are_not_silently_misread(monkeypatch):
    monkeypatch.setattr(norm, "_num2words", None)
    result = norm.normalize_for_speech("Дата 25 октября 2026 года; погрешность 3,7%.")
    assert "25 октября 2026 года" in result
    assert norm.find_numeric_tokens(result)


def test_unrecognized_number_is_preserved_and_flagged_for_manual_review(monkeypatch):
    monkeypatch.setattr(norm, "_num2words", _fake_num2words)
    result = norm.normalize_for_speech("В модели используется 3 слоя и коэффициент 2.75.")
    assert "3 слоя" in result
    assert "2.75" in result
    assert norm.find_numeric_tokens(result) == ["3", "2.75"]


def test_reinflects_known_units_and_rubles_from_numeric_quantity(monkeypatch):
    monkeypatch.setattr(norm, "_num2words", _fake_num2words)
    result = norm.normalize_for_speech("5 градуса; 2 GB; 4 500 руб.; 2 USD.")
    assert "пять градусов" in result
    assert "два гигабайта" in result
    assert "четыре тысячи пятьсот рублей" in result
    assert "два доллара США" in result
    assert norm.find_numeric_tokens(result) == []


def test_real_num2words_ru_cases_when_installed():
    """Exercise the real package on Windows/CI when the TTS extra is installed."""
    if norm._num2words is None:
        pytest.skip("num2words is not installed in this environment")

    result = norm.normalize_for_speech(
        "Дата 16.10.2026; в 14:35; погрешность 3,7%; "
        "температура 21,5 градуса; файл 1,25 ГБ; стоимость 4 500 рублей. "
        "Осталась 1 минута; через 2 минуты начнём; длительность — 21 секунда; "
        "затем подождите 22 секунды; встреча в 14:02."
    )
    assert "шестнадцатого октября две тысячи двадцать шестого года" in result
    assert "четырнадцать часов тридцать пять минут" in result
    assert "три целых семь десятых процента" in result
    assert "двадцать одна целая пять десятых градуса" in result
    assert "одна целая двадцать пять сотых гигабайта" in result
    assert "четыре тысячи пятьсот рублей" in result
    assert "одна минута" in result
    assert "две минуты" in result
    assert "двадцать одна секунда" in result
    assert "двадцать две секунды" in result
    assert "четырнадцать часов две минуты" in result
    assert "длительность — двадцать одна секунда" in result
    assert norm.find_numeric_tokens(result) == []


def test_feminine_counted_units_get_feminine_cardinals(monkeypatch):
    """Counts of feminine nouns must say одна/две, not один/два."""
    monkeypatch.setattr(norm, "_num2words", _fake_num2words)
    result = norm.normalize_for_speech(
        "1 минута; 2 минуты; 21 секунда; 22 секунды; время 14:01."
    )
    assert "одна минута" in result
    assert "две минуты" in result
    assert "двадцать одна секунда" in result
    assert "двадцать две секунды" in result
    assert "четырнадцать часов одна минута" in result
    assert norm.find_numeric_tokens(result) == []


def test_masculine_counted_units_keep_masculine_cardinals(monkeypatch):
    monkeypatch.setattr(norm, "_num2words", _fake_num2words)
    result = norm.normalize_for_speech("1 градус; 2 градуса; 21 градус; 22 градуса.")
    assert "один градус" in result
    assert "два градуса" in result
    assert "двадцать один градус" in result
    assert "двадцать два градуса" in result
    assert norm.find_numeric_tokens(result) == []


def test_preserves_dash_punctuation_while_normalizing_numbers(monkeypatch):
    monkeypatch.setattr(norm, "_num2words", _fake_num2words)
    result = norm.normalize_for_speech("Температура — 21,5 градуса; осталось — 2 минуты.")
    assert "Температура — двадцать одна целая пять десятых градуса" in result
    assert "осталось — две минуты" in result
    assert norm.find_numeric_tokens(result) == []
