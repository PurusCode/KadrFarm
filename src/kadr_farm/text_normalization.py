"""Conservative Russian text normalization for TTS.

Only explicitly supported numeric patterns are expanded automatically. Ambiguous
or unsupported numeric tokens are left intact so the manifest can flag them for
human review instead of silently guessing grammatical case.
"""
from __future__ import annotations

import importlib.metadata
import re
from typing import Any

NORMALIZER_VERSION = "0.2.2"
try:
    from num2words import num2words as _num2words  # type: ignore
except ImportError:  # allow non-TTS commands to run without the optional extra
    _num2words = None

MONTHS = (
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
)

# Keep spoken replacements separate from units whose grammatical form depends
# on the numeric quantity.
_PRE_REPLACEMENTS: list[tuple[str, str]] = [
    (r"\bDeepSeek\s*-?\s*V4\.1\s*-?\s*Flash\b", "ДипСик Ви четыре точка один Флэш"),
    (r"\bGPT-OSS\b", "джи пи ти осс"),
    (r"\bH\.264\b", "эйч двести шестьдесят четыре"),
    (r"\bB-roll\b", "би ролл"),
    (r"\bИИ\b", "искусственный интеллект"),
    (r"\bLLM\b", "эл эл эм"),
    (r"\bAPI\b", "эй пи ай"),
    (r"\bGPU\b", "джи пи ю"),
    (r"\bCPU\b", "си пи ю"),
    (r"\bRAM\b", "оперативная память"),
    (r"\bVRAM\b", "видеопамять"),
    (r"\bDeepSeek\b", "ДипСик"),
    (r"\bOllama\b", "Олама"),
    (r"\bQwen\b", "Кьювэн"),
    (r"\bV4\.1\b", "версии четыре точка один"),
]
_POST_REPLACEMENTS: list[tuple[str, str]] = [
    (r"\bGB\b", "гигабайт"), (r"\bTB\b", "терабайт"),
    (r"\bMB\b", "мегабайт"), (r"\bUSD\b", "долларов США"),
    (r"\bRUB\b", "рублей"),
]

# Supports regular or space-separated thousands and a comma/dot decimal.
_NUMBER = r"\d{1,3}(?:[ \u00a0\u202f]\d{3})+(?:[.,]\d+)?|\d+(?:[.,]\d+)?"
_INTEGER = r"\d{1,3}(?:[ \u00a0\u202f]\d{3})+|\d+"


def normalizer_backend() -> str:
    if _num2words is None:
        return "UNAVAILABLE: install the project's TTS extra (num2words)"
    try:
        return f"num2words {importlib.metadata.version('num2words')}"
    except importlib.metadata.PackageNotFoundError:
        return "num2words (version unknown)"


def _clean_number(value: str) -> str:
    cleaned = re.sub(r"[ \u00a0\u202f]", "", str(value).strip()).replace(",", ".")
    if "." in cleaned:
        whole, fractional = cleaned.split(".", 1)
        fractional = fractional.rstrip("0")
        cleaned = whole if not fractional else f"{whole}.{fractional}"
    return cleaned


def _words(value: str | int, **kwargs: Any) -> str | None:
    if _num2words is None:
        return None
    try:
        return str(_num2words(value, lang="ru", **kwargs)).strip()
    except (TypeError, ValueError, KeyError, NotImplementedError, OverflowError):
        return None


def _number_words(value: str | int, *, gender: str | None = None) -> str | None:
    kwargs = {"gender": gender} if gender else {}
    return _words(_clean_number(str(value)), **kwargs)


def _ordinal_words(value: str | int, *, case: str, gender: str = "m") -> str | None:
    # num2words' Russian converter uses short case codes, not English names.
    # Keeping friendly aliases here makes call sites self-documenting while
    # matching the public RU API (n/g/d/a/i/p).
    case_codes = {
        "nominative": "n", "genitive": "g", "dative": "d",
        "accusative": "a", "instrumental": "i", "prepositional": "p",
        "n": "n", "g": "g", "d": "d", "a": "a", "i": "i", "p": "p",
    }
    normalized_case = case_codes.get(case)
    if normalized_case is None:
        return None
    return _words(int(value), to="ordinal", case=normalized_case, gender=gender)


def _plural_form(value: int, one: str, few: str, many: str) -> str:
    n100 = value % 100
    if 11 <= n100 <= 14:
        return many
    n10 = value % 10
    if n10 == 1:
        return one
    if 2 <= n10 <= 4:
        return few
    return many


def _integer_value(raw: str) -> int | None:
    cleaned = re.sub(r"[ \u00a0\u202f]", "", raw)
    if re.fullmatch(r"\d+", cleaned):
        try:
            return int(cleaned)
        except ValueError:
            return None
    return None


def _date_replacement(match: re.Match[str]) -> str:
    day = int(match.group("day"))
    month = match.group("month")
    year = int(match.group("year"))
    if not 1 <= day <= 31 or not 1 <= year <= 9999:
        return match.group(0)
    day_words = _ordinal_words(day, case="genitive")
    year_words = _ordinal_words(year, case="genitive")
    if not day_words or not year_words:
        return match.group(0)
    return f"{day_words} {month.lower()} {year_words} года"


def _date_numeric_replacement(match: re.Match[str]) -> str:
    day, month_num, year = (int(match.group(i)) for i in (1, 2, 3))
    if not 1 <= day <= 31 or not 1 <= month_num <= 12:
        return match.group(0)
    proxy = {"day": str(day), "month": MONTHS[month_num - 1], "year": str(year)}
    return _date_replacement(_DictMatch(proxy, match.group(0)))


class _DictMatch:
    """Tiny match-compatible object used to share the date renderer."""
    def __init__(self, values: dict[str, str], original: str) -> None:
        self.values, self.original = values, original
    def group(self, key: str | int = 0) -> str:
        if key == 0:
            return self.original
        if isinstance(key, int):
            return self.original
        return self.values[key]


def _clock_replacement(match: re.Match[str]) -> str:
    hour, minute = int(match.group("hour")), int(match.group("minute"))
    if hour > 23 or minute > 59:
        return match.group(0)
    # Час — мужской род, минута — женский. For 1/2 and compound numbers
    # ending in 1/2, gender changes the spoken cardinal (один/одна, два/две).
    hour_words = _number_words(hour, gender="m")
    minute_words = _number_words(minute, gender="f")
    if not hour_words or not minute_words:
        return match.group(0)
    return f"{hour_words} {_plural_form(hour, 'час', 'часа', 'часов')} {minute_words} {_plural_form(minute, 'минута', 'минуты', 'минут')}"


def _percentage_replacement(match: re.Match[str]) -> str:
    raw = match.group("number")
    spelled = _number_words(raw)
    if not spelled:
        return match.group(0)
    if "." in _clean_number(raw):
        noun = "процента"
    else:
        n = _integer_value(raw)
        noun = _plural_form(n or 0, "процент", "процента", "процентов")
    return f"{spelled} {noun}"

def _unit_replacement(match: re.Match[str]) -> str:
    raw, unit_raw = match.group("number"), match.group("unit")
    unit_lower = unit_raw.lower().replace(" ", "")
    is_decimal = "." in _clean_number(raw)
    n = _integer_value(raw)

    # Numeric gender follows the counted noun for integer quantities: one minute
    # is "одна минута", two seconds are "две секунды". Decimal fractions use
    # the feminine fractional form "одна целая ...", independent of noun gender;
    # the noun itself takes the explicitly selected genitive-singular form below.
    feminine_units = {
        "миллисекунда", "миллисекунды", "миллисекунд",
        "секунда", "секунды", "секунд",
        "минута", "минуты", "минут",
        "мс", "с", "сек", "мин",
    }
    numeral_gender = None if is_decimal else ("f" if unit_lower in feminine_units else "m")
    spelled = _number_words(raw, gender=numeral_gender)
    if not spelled:
        return match.group(0)

    # Known Russian unit nouns are re-inflected from the numeric value rather
    # than trusting a possibly mismatched ending from the source text.
    full_unit_forms = {
        "градус": ("градус", "градуса", "градусов"),
        "градуса": ("градус", "градуса", "градусов"),
        "градусов": ("градус", "градуса", "градусов"),
        "гигабайт": ("гигабайт", "гигабайта", "гигабайт"),
        "гигабайта": ("гигабайт", "гигабайта", "гигабайт"),
        "гигабайтов": ("гигабайт", "гигабайта", "гигабайт"),
        "мегабайт": ("мегабайт", "мегабайта", "мегабайт"),
        "мегабайта": ("мегабайт", "мегабайта", "мегабайт"),
        "мегабайтов": ("мегабайт", "мегабайта", "мегабайт"),
        "терабайт": ("терабайт", "терабайта", "терабайт"),
        "терабайта": ("терабайт", "терабайта", "терабайт"),
        "терабайтов": ("терабайт", "терабайта", "терабайт"),
        "килобайт": ("килобайт", "килобайта", "килобайт"),
        "килобайта": ("килобайт", "килобайта", "килобайт"),
        "килобайтов": ("килобайт", "килобайта", "килобайт"),
        "миллисекунда": ("миллисекунда", "миллисекунды", "миллисекунд"),
        "миллисекунды": ("миллисекунда", "миллисекунды", "миллисекунд"),
        "миллисекунд": ("миллисекунда", "миллисекунды", "миллисекунд"),
        "секунда": ("секунда", "секунды", "секунд"),
        "секунды": ("секунда", "секунды", "секунд"),
        "секунд": ("секунда", "секунды", "секунд"),
        "минута": ("минута", "минуты", "минут"),
        "минуты": ("минута", "минуты", "минут"),
        "минут": ("минута", "минуты", "минут"),
    }
    aliases = {
        "°c": ("градус", "градуса", "градусов"),
        "°с": ("градус", "градуса", "градусов"),
        "гб": ("гигабайт", "гигабайта", "гигабайт"),
        "gb": ("гигабайт", "гигабайта", "гигабайт"),
        "мб": ("мегабайт", "мегабайта", "мегабайт"),
        "mb": ("мегабайт", "мегабайта", "мегабайт"),
        "тб": ("терабайт", "терабайта", "терабайт"),
        "tb": ("терабайт", "терабайта", "терабайт"),
        "кб": ("килобайт", "килобайта", "килобайт"),
        "kb": ("килобайт", "килобайта", "килобайт"),
        "мс": ("миллисекунда", "миллисекунды", "миллисекунд"),
        "с": ("секунда", "секунды", "секунд"),
        "сек": ("секунда", "секунды", "секунд"),
        "мин": ("минута", "минуты", "минут"),
    }
    forms = full_unit_forms.get(unit_lower) or aliases.get(unit_lower)
    if not forms:
        return match.group(0)
    noun = forms[1] if is_decimal else _plural_form(n or 0, *forms)
    suffix = " Цельсия" if unit_lower in {"°c", "°с"} else ""
    return f"{spelled} {noun}{suffix}"

def _currency_replacement(match: re.Match[str]) -> str:
    raw, unit_raw = match.group("number"), match.group("unit")
    spelled = _number_words(raw)
    if not spelled:
        return match.group(0)
    unit = unit_raw.lower()
    n = _integer_value(raw)
    decimal = "." in _clean_number(raw)
    if unit in {"rub", "₽"} or unit.startswith("руб"):
        noun = "рубля" if decimal else _plural_form(n or 0, "рубль", "рубля", "рублей")
        return f"{spelled} {noun}"
    if unit in {"usd", "$"} or unit.startswith("доллар"):
        noun = "доллара США" if decimal else _plural_form(n or 0, "доллар США", "доллара США", "долларов США")
        return f"{spelled} {noun}"
    return f"{spelled} {unit_raw}"

def _qwen_model_replacement(match: re.Match[str]) -> str:
    version_major, version_minor, size, suffix = match.groups()
    major_words = _number_words(version_major)
    minor_words = _number_words(version_minor)
    size_words = _number_words(size)
    if not major_words or not minor_words or not size_words:
        return match.group(0)
    return f"Кьювэн {major_words} точка {minor_words}, {size_words} {'би' if suffix.lower() == 'b' else 'эм'}"


def normalize_for_speech(text: str) -> str:
    """Normalize well-defined numeric patterns for Russian speech.

    Ambiguous numeric patterns are intentionally left in digits. Use
    ``find_numeric_tokens`` after normalization to flag them for manual review.
    """
    # Preserve punctuation. Replacing an em/en dash globally with a comma
    # changes sentence structure (e.g. "Температура — 21 °C") and can make
    # the spoken phrase grammatically awkward. Numeric patterns are replaced
    # in-place below; dashes are not numeric tokens by themselves.
    value = text or ""

    # Preserve a common language-model identifier instead of treating 2.5 as a decimal.
    value = re.sub(r"\bQwen\s*(\d+)\.(\d+)\s*(\d+)\s*([BM])\b", _qwen_model_replacement, value, flags=re.I)

    # Numeric dates (DD.MM.YYYY / DD/MM/YYYY), then dates already partly verbalized.
    value = re.sub(r"(?<![\d.])(\d{1,2})[./](\d{1,2})[./](\d{4})(?!\d)", _date_numeric_replacement, value)
    month_alt = "|".join(MONTHS)
    value = re.sub(
        rf"(?P<day>\d{{1,2}})\s+(?P<month>{month_alt})\s+(?P<year>\d{{4}})(?:\s+года)?\b",
        _date_replacement, value, flags=re.I,
    )

    # Clock forms: 14:35 or 14 часов 35 минут.
    value = re.sub(r"(?<!\d)(?P<hour>\d{1,2}):(?P<minute>[0-5]\d)(?!\d)", _clock_replacement, value)
    value = re.sub(
        r"(?<!\w)(?P<hour>\d{1,2})\s+час(?:а|ов)?\s+(?P<minute>\d{1,2})\s+минут(?:а|ы)?(?!\w)",
        _clock_replacement, value, flags=re.I,
    )

    # Percentages. Decimal fractions take the singular genitive form “процента”.
    value = re.sub(
        rf"(?<!\w)(?P<number>{_NUMBER})\s*(?P<unit>%|процент(?:а|ов)?)(?!\w)",
        _percentage_replacement, value, flags=re.I,
    )

    # Currency amounts. Explicit Russian case/endings are preserved; currency codes are inflected.
    value = re.sub(
        rf"(?<!\w)(?P<number>{_NUMBER})\s*(?P<unit>RUB|USD|₽|\$)(?!\w)",
        _currency_replacement, value, flags=re.I,
    )
    value = re.sub(
        rf"(?<!\w)(?P<number>{_NUMBER})\s*(?P<unit>руб(?:ль|ля|лей|лями|лях)?\.?|доллар(?:а|ов)?(?:\s+США)?)(?!\w)",
        _currency_replacement, value, flags=re.I,
    )

    # Common units already written as words or abbreviated in Latin/Cyrillic.
    unit_pattern = (
        r"°\s*[CcСс]|градус(?:а|ов)?|"
        r"гигабайт(?:а|ов)?|мегабайт(?:а|ов)?|терабайт(?:а|ов)?|килобайт(?:а|ов)?|"
        r"миллисекунда|миллисекунды|миллисекунд|секунда|секунды|секунд|минута|минуты|минут|"
        r"ГБ|МБ|ТБ|КБ|GB|MB|TB|KB|мс|сек|мин"
    )
    value = re.sub(rf"(?<!\w)(?P<number>{_NUMBER})\s*(?P<unit>{unit_pattern})(?!\w)", _unit_replacement, value, flags=re.I)

    # Years with an explicit year noun; use the grammatical ordinal form.
    def year_repl(match: re.Match[str]) -> str:
        year, noun = int(match.group(1)), match.group(2).lower()
        case_map = {"год": "nominative", "года": "genitive", "году": "prepositional", "годом": "instrumental"}
        ordinal = _ordinal_words(year, case=case_map[noun])
        return f"{ordinal} {noun}" if ordinal else match.group(0)
    value = re.sub(r"(?<!\d)(\d{4})\s+(год|года|году|годом)(?!\w)", year_repl, value, flags=re.I)

    # Specific non-numeric abbreviations/models.
    for pattern, replacement in _PRE_REPLACEMENTS:
        value = re.sub(pattern, replacement, value, flags=re.I)

    # Do not convert arbitrary remaining numbers into cardinals: grammar depends on
    # the surrounding noun/case. Leave unsupported patterns intact so QC can flag them.
    # The script-generation prompt asks for ordinary quantities to be spelled in
    # voice_text; this normalizer only expands the explicitly supported patterns above.

    for pattern, replacement in _POST_REPLACEMENTS:
        value = re.sub(pattern, replacement, value, flags=re.I)
    return re.sub(r"\s+", " ", value).strip()


def find_numeric_tokens(text: str) -> list[str]:
    """Return remaining digit groups after normalization for editor/QC review."""
    return re.findall(r"(?<!\w)\d+(?:[.,:/-]\d+)*(?!\w)", text or "")
