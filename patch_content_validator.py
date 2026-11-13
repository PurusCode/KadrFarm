from __future__ import annotations

from pathlib import Path
import shutil


PROJECT_FILE = Path("src/kadr_farm/content.py")


def replace_once(source: str, old: str, new: str, label: str) -> str:
    count = source.count(old)
    if count != 1:
        raise SystemExit(
            f"Патч остановлен: фрагмент «{label}» найден {count} раз(а), ожидался ровно один. "
            "Файл не изменён."
        )
    return source.replace(old, new, 1)


def main() -> None:
    if not PROJECT_FILE.is_file():
        raise SystemExit(
            "Не найден src/kadr_farm/content.py. Запустите скрипт из корня video-farm."
        )

    source = PROJECT_FILE.read_text(encoding="utf-8-sig")

    start = source.find("def validate_script(")
    end = source.find("\ndef generate_script(", start)
    if start < 0 or end < 0:
        raise SystemExit(
            "Не удалось найти границы validate_script()/generate_script(). Файл не изменён."
        )

    new_validator = r'''def validate_script(script: dict[str, Any], *, min_words: int = 1100) -> list[str]:
    """Проверяет структуру и базовые требования к сценарию.

    Ошибки структуры и объёма затем блокируют сохранение как обычного сценария.
    Менее критичные замечания по длине текста слайдов остаются предупреждениями.
    """
    warnings: list[str] = []

    if not isinstance(script, dict):
        return ["Корень сценария должен быть JSON-объектом"]

    title = str(script.get("title", "")).strip()
    title_len = len(title)
    if not title:
        warnings.append("Заголовок видео отсутствует")
    elif not 55 <= title_len <= 70:
        warnings.append(
            f"Заголовок видео должен содержать 55–70 символов, сейчас {title_len}"
        )

    blocks = script.get("blocks")
    if not isinstance(blocks, list):
        warnings.append("Поле blocks должно быть списком")
        return warnings

    if len(blocks) != 7:
        warnings.append(
            f"Ожидалось ровно 7 блоков (хук + 5 секций + CTA), получено {len(blocks)}"
        )

    voice_parts = [
        str(block.get("voice_text", ""))
        for block in blocks
        if isinstance(block, dict)
    ]
    count = word_count(" ".join(voice_parts))
    minimum = max(1, int(min_words))
    if minimum < 800:
        maximum = max(minimum + 50, int(minimum * 1.2))
    else:
        # Для штатной цели 1100 это сохраняет прежний диапазон 1100–1500.
        maximum = max(minimum + 50, int(minimum * 1500 / 1100))

    if count < minimum:
        warnings.append(f"Мало слов: {count}; требуется минимум {minimum}")
    if count > maximum:
        warnings.append(f"Слишком много слов: {count}; допустимый максимум {maximum}")

    expected_kinds = ["hook", "section", "section", "section", "section", "section", "cta"]
    if not blocks or not isinstance(blocks[0], dict) or blocks[0].get("kind") != "hook":
        warnings.append("Первый блок должен иметь kind=hook")
    if not blocks or not isinstance(blocks[-1], dict) or blocks[-1].get("kind") != "cta":
        warnings.append("Последний блок должен иметь kind=cta")

    allowed_slide_types = {
        "text", "metric", "comparison", "quote", "list", "bar_chart",
        "progress", "photo", "split", "storyboard",
    }
    long_slide_types = {"list", "storyboard", "comparison", "bar_chart", "progress", "split"}

    for i, block in enumerate(blocks, 1):
        if not isinstance(block, dict):
            warnings.append(f"Блок {i} должен быть JSON-объектом")
            continue

        if i <= len(expected_kinds) and block.get("kind") != expected_kinds[i - 1]:
            warnings.append(
                f"Блок {i}: ожидался kind={expected_kinds[i - 1]}, получен {block.get('kind')}"
            )

        if not str(block.get("heading", "")).strip():
            warnings.append(f"Блок {i}: heading отсутствует")
        if not str(block.get("voice_text", "")).strip():
            warnings.append(f"Блок {i}: voice_text пустой")

        slides = block.get("slides", [])
        if not isinstance(slides, list):
            warnings.append(f"Блок {i}: slides должно быть списком")
            slides = []
        if len(slides) != 3:
            warnings.append(f"Блок {i}: slides должно быть ровно 3, сейчас {len(slides)}")

        for j, slide in enumerate(slides, 1):
            if not isinstance(slide, dict):
                warnings.append(f"Блок {i}, слайд {j}: слайд должен быть JSON-объектом")
                continue

            slide_title = str(slide.get("title", "")).strip()
            body = str(slide.get("body", "")).strip()
            if not slide_title:
                warnings.append(f"Блок {i}, слайд {j}: отсутствует title")
            if not body:
                warnings.append(f"Блок {i}, слайд {j}: отсутствует body")
            if len(slide_title) > 70:
                warnings.append(f"Блок {i}, слайд {j}: заголовок длиннее 70 символов")

            slide_type = str(slide.get("type", "text")).strip().lower()
            if slide_type not in allowed_slide_types:
                warnings.append(f"Блок {i}, слайд {j}: type недопустим ({slide_type})")

            label = " ".join(part for part in (slide_title, body) if part)
            wc = word_count(label)
            if wc > 12:
                warnings.append(f"Блок {i}, слайд {j}: много текста ({wc} слов), сократите")
            if slide_type not in long_slide_types and wc > 7:
                warnings.append(
                    f"Блок {i}, слайд {j}: короткий слайд должен укладываться в 3–7 слов (сейчас {wc})"
                )

            photo_keyword = str(slide.get("photo_keyword", "")).strip()
            if photo_keyword and (
                not photo_keyword.isascii()
                or not any(char.isalpha() for char in photo_keyword)
            ):
                warnings.append(
                    f"Блок {i}, слайд {j}: photo_keyword содержит неанглийские символы"
                )

        keywords = block.get("broll_keywords", [])
        if not isinstance(keywords, list):
            warnings.append(f"Блок {i}: broll_keywords должно быть списком из 2–5 запросов")
            continue
        if not 2 <= len(keywords) <= 5:
            warnings.append(f"Блок {i}: broll_keywords должно быть 2–5, сейчас {len(keywords)}")

        for j, keyword in enumerate(keywords, 1):
            if (
                not isinstance(keyword, str)
                or not keyword.strip()
                or not keyword.isascii()
                or not any(char.isalpha() for char in keyword)
            ):
                warnings.append(
                    f"Блок {i}: B-roll запрос {j} пустой или не на английском"
                )

    return warnings
'''
    source = source[:start] + new_validator + source[end:]

    source = replace_once(
        source,
        '    for i, b in enumerate(payload["blocks"], 1):\n        b.setdefault("id", f"block_{i:02d}")',
        '    for i, b in enumerate(payload["blocks"], 1):\n'
        '        if not isinstance(b, dict):\n'
        '            raise ValueError(f"Блок {i} в ответе LLM должен быть JSON-объектом")\n'
        '        b.setdefault("id", f"block_{i:02d}")',
        "проверка типа блока перед нормализацией",
    )

    source = replace_once(
        source,
        '        b["slides"] = [dict(s) for s in b["slides"] if isinstance(s, dict)]',
        '        raw_slides = b.get("slides", [])\n'
        '        if not isinstance(raw_slides, list):\n'
        '            raw_slides = []\n'
        '        b["slides"] = [dict(s) for s in raw_slides if isinstance(s, dict)]',
        "безопасная нормализация списка слайдов",
    )

    source = replace_once(
        source,
        '        b["broll_keywords"] = [str(x) for x in b["broll_keywords"] if str(x).strip()][:5]',
        '        raw_keywords = b.get("broll_keywords", [])\n'
        '        if not isinstance(raw_keywords, list):\n'
        '            raw_keywords = []\n'
        '        b["broll_keywords"] = [str(x) for x in raw_keywords if str(x).strip()][:5]',
        "безопасная нормализация B-roll ключевых слов",
    )

    old_final = (
        '    payload["word_count"] = word_count(" ".join(str(b.get("voice_text", "")) for b in payload.get("blocks", [])))\n'
        '    payload["generated_at"] = dt.datetime.now(dt.timezone.utc).isoformat()\n'
        '    payload["quality_warnings"] = validate_script(payload)\n'
        '    write_json(out_path, payload)\n'
        '    return payload'
    )
    new_final = '''    payload["word_count"] = word_count(" ".join(str(b.get("voice_text", "")) for b in payload.get("blocks", []) if isinstance(b, dict)))
    payload["generated_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
    payload["quality_warnings"] = validate_script(payload, min_words=min_words)

    # Структурные ошибки и несоответствие объёма блокируют публикационный JSON.
    # Черновик сохраняется отдельно для диагностики, а исходный out_path не перезаписывается.
    critical_markers = (
        "Заголовок видео отсутствует",
        "Заголовок видео должен содержать",
        "Поле blocks должно быть списком",
        "Ожидалось ровно 7 блоков",
        "Мало слов:",
        "Слишком много слов:",
        "Первый блок должен иметь kind=hook",
        "Последний блок должен иметь kind=cta",
        "ожидался kind=",
        "должен быть JSON-объектом",
        "heading отсутствует",
        "voice_text пустой",
        "slides должно быть списком",
        "slides должно быть ровно 3",
        "слайд должен быть JSON-объектом",
        "отсутствует title",
        "отсутствует body",
        "type недопустим",
        "broll_keywords должно быть списком",
        "broll_keywords должно быть 2–5",
        "пустой или не на английском",
        "photo_keyword содержит неанглийские символы",
    )
    critical_warnings = [
        warning
        for warning in payload["quality_warnings"]
        if any(marker in warning for marker in critical_markers)
    ]
    if critical_warnings:
        rejected_path = out_path.with_name(f"{out_path.stem}.rejected{out_path.suffix}")
        rejected_path.parent.mkdir(parents=True, exist_ok=True)
        write_json(rejected_path, payload)
        details = "\\n".join(f"- {warning}" for warning in critical_warnings)
        raise ValueError(
            "Сценарий не прошёл обязательную проверку. "
            f"Черновик сохранён отдельно: {rejected_path}\\n{details}"
        )

    write_json(out_path, payload)
    return payload'''
    source = replace_once(source, old_final, new_final, "итоговая проверка перед сохранением")

    backup = PROJECT_FILE.with_suffix(PROJECT_FILE.suffix + ".bak")
    if not backup.exists():
        shutil.copy2(PROJECT_FILE, backup)

    PROJECT_FILE.write_text(source, encoding="utf-8")
    print(f"Патч применён: {PROJECT_FILE}")
    print(f"Резервная копия: {backup}")
    print("Обновлены validate_script(), нормализация блоков/слайдов/B-roll и финальный quality gate.")
    print("Предупреждения только о длине текста на слайдах остаются мягкими; критические дефекты блокируют обычное сохранение.")


if __name__ == "__main__":
    main()
