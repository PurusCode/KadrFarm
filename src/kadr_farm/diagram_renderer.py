"""Original local visual renderer for charts, diagrams, title cards and jokes."""
from __future__ import annotations

from pathlib import Path
from typing import Any
import textwrap
from PIL import Image, ImageDraw, ImageFont


W, H = 1920, 1080
BG = (13, 20, 36)
PANEL = (25, 36, 57)
WHITE = (244, 247, 252)
MUTED = (165, 178, 198)
ACCENT = (255, 153, 64)
BLUE = (80, 166, 230)
GREEN = (75, 190, 145)


def _font(size: int, bold: bool = False):
    candidates = [
        "C:/Windows/Fonts/arialbd.ttf" if bold else "C:/Windows/Fonts/arial.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/Library/Fonts/Arial Bold.ttf" if bold else "/Library/Fonts/Arial.ttf",
    ]
    for path in candidates:
        try:
            return ImageFont.truetype(path, size=size)
        except OSError:
            continue
    return ImageFont.load_default()


def _wrap(draw: ImageDraw.ImageDraw, text: str, font, max_width: int) -> list[str]:
    words = str(text or "").split()
    lines, current = [], ""
    for word in words:
        test = f"{current} {word}".strip()
        if current and draw.textbbox((0, 0), test, font=font)[2] > max_width:
            lines.append(current)
            current = word
        else:
            current = test
    if current:
        lines.append(current)
    return lines


def _draw_wrapped(draw, xy, text, font, fill, max_width, line_gap=14):
    x, y = xy
    lines = _wrap(draw, text, font, max_width)
    for line in lines:
        draw.text((x, y), line, font=font, fill=fill)
        y += draw.textbbox((0, 0), line, font=font)[3] + line_gap
    return y


def render_scene_visual(scene: dict[str, Any], output_path: Path) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    im = Image.new("RGB", (W, H), BG)
    draw = ImageDraw.Draw(im)
    # Subtle grid and accent rail.
    for x in range(0, W, 80):
        draw.line((x, 0, x, H), fill=(20, 30, 47), width=1)
    draw.rounded_rectangle((74, 68, 88, H - 68), radius=7, fill=ACCENT)
    draw.text((125, 80), "AI VIDEO FACTORY  /  VISUAL STORY", font=_font(24, True), fill=ACCENT)

    title = str(scene.get("meaning") or scene.get("visual_description") or "Иллюстрация")
    draw.text((125, 155), " ".join(title.split())[:100], font=_font(48, True), fill=WHITE)
    visual_type = scene.get("visual_type", "diagram")
    chart = scene.get("chart_data")
    if visual_type == "chart" and chart:
        labels = chart["labels"]
        values = chart["values"]
        unit = chart["unit"]
        x0, y0, x1, y1 = 180, 360, 1660, 850
        draw.rounded_rectangle((x0, y0, x1, y1), radius=30, fill=PANEL, outline=(54, 72, 98), width=2)
        low, high = min(values), max(values)
        span = max(high - low, 1e-9)
        plot_top, plot_bottom = y0 + 70, y1 - 145
        plot_height = plot_bottom - plot_top
        baseline_value = 0.0 if low <= 0 <= high else (low if low > 0 else high)
        baseline_y = plot_top + int((high - baseline_value) / span * plot_height)
        draw.line((x0 + 35, baseline_y, x1 - 35, baseline_y), fill=(110, 128, 151), width=3)
        bar_w = min(150, 900 // max(1, len(values)))
        gap = (x1 - x0 - 160 - bar_w * len(values)) / max(1, len(values))
        for i, (label, value) in enumerate(zip(labels, values)):
            bx = x0 + 80 + int(i * (bar_w + gap))
            value_y = plot_top + int((high - value) / span * plot_height)
            top_y, bottom_y = min(value_y, baseline_y), max(value_y, baseline_y)
            if bottom_y - top_y < 3:
                bottom_y = top_y + 3
            draw.rounded_rectangle((bx, top_y, bx + bar_w, bottom_y), radius=12,
                                   fill=ACCENT if value == high else BLUE)
            draw.text((bx, y1 - 110), str(label)[:16], font=_font(23), fill=WHITE)
            label_y = max(plot_top - 34, top_y - 42)
            draw.text((bx, label_y), f"{value:g} {unit}", font=_font(22, True), fill=WHITE)
        _draw_wrapped(draw, (180, 900), str(chart.get("methodology", "")), _font(18), MUTED, 1450)
    elif visual_type in {"diagram", "scheme", "comparison"}:
        labels = scene.get("on_screen_text") or ["Причина", "Механизм", "Результат"]
        labels = (labels + ["", "", ""])[:3]
        boxes = [(180, 430, 590, 700), (755, 430, 1165, 700), (1330, 430, 1740, 700)]
        for i, (box, label) in enumerate(zip(boxes, labels)):
            draw.rounded_rectangle(box, radius=28, fill=PANEL, outline=ACCENT if i == 1 else BLUE, width=4)
            _draw_wrapped(draw, (box[0] + 28, box[1] + 65), label or f"Этап {i+1}",
                          _font(34, True), WHITE, box[2] - box[0] - 56, 14)
            if i < 2:
                cy = (box[1] + box[3]) // 2
                draw.line((box[2] + 28, cy, boxes[i+1][0] - 28, cy), fill=ACCENT, width=10)
                draw.polygon([(boxes[i+1][0]-28, cy), (boxes[i+1][0]-52, cy-18),
                              (boxes[i+1][0]-52, cy+18)], fill=ACCENT)
    elif visual_type == "joke":
        draw.rounded_rectangle((180, 360, 1740, 830), radius=52, fill=PANEL, outline=ACCENT, width=4)
        draw.text((280, 420), "НЕОЖИДАННЫЙ ПОВОРОТ", font=_font(28, True), fill=ACCENT)
        _draw_wrapped(draw, (280, 520), str(scene.get("humor_note") or scene.get("visual_description")),
                      _font(48, True), WHITE, 1320, 22)
    else:
        draw.rounded_rectangle((180, 360, 1740, 830), radius=42, fill=PANEL, outline=(54, 72, 98), width=2)
        _draw_wrapped(draw, (270, 435), str(scene.get("visual_description", "")),
                      _font(38), WHITE, 1380, 24)

    screen = scene.get("on_screen_text") or []
    if screen:
        footer = "   •   ".join(str(x) for x in screen)
        draw.rounded_rectangle((150, 910, 1770, 1000), radius=24, fill=(31, 47, 70))
        draw.text((190, 930), footer[:150], font=_font(27, True), fill=WHITE)
    im.save(output_path, format="PNG", optimize=True)
    return output_path
