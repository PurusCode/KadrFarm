from __future__ import annotations
import logging, math, re
from pathlib import Path
from typing import Any
from PIL import Image, ImageDraw, ImageFont, ImageFilter
from .common import ensure_dir, read_json, slugify, write_json
from .config import Settings

log = logging.getLogger(__name__)

CYAN = (43, 218, 238)
PURPLE = (157, 94, 255)
ORANGE = (255, 159, 67)
WHITE = (242, 246, 255)
MUTED = (157, 174, 198)
BG = (8, 12, 26)


def _font_path(bold: bool = False, mono: bool = False) -> str | None:
    from os import environ
    candidates = []
    if environ.get("WINDIR"):
        win = Path(environ["WINDIR"]) / "Fonts"
        candidates += [win / ("consolab.ttf" if mono and bold else "consola.ttf" if mono else "segoeuib.ttf" if bold else "segoeui.ttf"), win / ("arialbd.ttf" if bold else "arial.ttf")]
    if mono:
        candidates += [Path("/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf")]
    candidates += [Path("/usr/share/fonts/truetype/lato/Lato-Bold.ttf" if bold else "/usr/share/fonts/truetype/lato/Lato-Regular.ttf"),
                   Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")]
    for p in candidates:
        if p.exists(): return str(p)
    return None


def _font(size: int, bold: bool = False, mono: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    fp = _font_path(bold, mono)
    try: return ImageFont.truetype(fp, size=size) if fp else ImageFont.load_default()
    except Exception: return ImageFont.load_default()


def _wrap(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont, max_width: int) -> list[str]:
    lines: list[str] = []
    for paragraph in (text or "").split("\n"):
        words = paragraph.split()
        if not words:
            lines.append(""); continue
        line = ""
        for w in words:
            trial = (line + " " + w).strip()
            if draw.textbbox((0, 0), trial, font=font)[2] <= max_width or not line:
                line = trial
            else:
                lines.append(line); line = w
        if line: lines.append(line)
    return lines


def _draw_wrapped(draw: ImageDraw.ImageDraw, xy: tuple[int, int], text: str, font: ImageFont.ImageFont,
                  fill: tuple[int, int, int], max_width: int, line_gap: int = 12, max_lines: int = 5) -> int:
    x, y = xy
    lines = _wrap(draw, text, font, max_width)[:max_lines]
    for line in lines:
        draw.text((x, y), line, font=font, fill=fill, stroke_width=0)
        y += (draw.textbbox((0, 0), line or "Ag", font=font)[3] + line_gap)
    return y


def _background(width: int, height: int, index: int) -> Image.Image:
    # Vectorized RGB gradient keeps rendering fast even for 30+ Full HD slides.
    import numpy as np
    xs = np.linspace(0.0, 1.0, width, dtype=np.float32)[None, :]
    ys = np.linspace(0.0, 1.0, height, dtype=np.float32)[:, None]
    cx = 0.72 if index % 2 else 0.2
    glow = np.maximum(0.0, 1.0 - np.sqrt(((xs - cx) ** 2) + ((ys - 0.15) ** 2)) * 2.2)
    arr = np.empty((height, width, 3), dtype=np.uint8)
    arr[:, :, 0] = np.clip(BG[0] + glow * 12, 0, 255).astype(np.uint8)
    arr[:, :, 1] = np.clip(BG[1] + glow * 7, 0, 255).astype(np.uint8)
    arr[:, :, 2] = np.clip(BG[2] + glow * 29, 0, 255).astype(np.uint8)
    im = Image.fromarray(arr, mode="RGB")
    d = ImageDraw.Draw(im)
    # Subtle technical grid, kept low-contrast to preserve text legibility.
    for x in range(36, width, 80): d.line((x, 0, x, height), fill=(17, 25, 45), width=1)
    for y in range(30, height, 80): d.line((0, y, width, y), fill=(17, 25, 45), width=1)
    # Corner marks and accents.
    d.line((48, 44, 170, 44), fill=CYAN, width=4); d.line((48, 44, 48, 130), fill=CYAN, width=4)
    d.line((width - 170, height - 46, width - 48, height - 46), fill=PURPLE, width=4)
    d.line((width - 48, height - 132, width - 48, height - 46), fill=PURPLE, width=4)
    return im


def _image_cover(path: Path, size: tuple[int, int]) -> Image.Image:
    src = Image.open(path).convert("RGB")
    w, h = size; ratio = max(w / src.width, h / src.height)
    src = src.resize((int(src.width * ratio), int(src.height * ratio)), Image.Resampling.LANCZOS)
    left = (src.width - w) // 2; top = (src.height - h) // 2
    return src.crop((left, top, left + w, top + h))


def _find_image(keyword: str, manifest: dict[str, Any] | None) -> Path | None:
    if not manifest or not keyword: return None
    k = keyword.lower()
    for x in manifest.get("images", []):
        if k in x.get("query", "").lower() or x.get("query", "").lower() in k:
            p = Path(x.get("file", ""))
            if p.exists(): return p
    for x in manifest.get("images", []):
        p = Path(x.get("file", ""))
        if p.exists(): return p
    return None


def _render_one(slide: dict[str, Any], heading: str, index: int, out: Path, settings: Settings,
                assets: dict[str, Any] | None = None) -> dict[str, Any]:
    w, h = settings.width, settings.height
    im = _background(w, h, index)
    d = ImageDraw.Draw(im)
    typ = str(slide.get("type", "text")).lower()
    title = str(slide.get("title", "")).strip() or heading
    body = str(slide.get("body", "")).strip()
    metric = str(slide.get("metric", "")).strip()
    items = slide.get("items", []) if isinstance(slide.get("items", []), list) else []
    photo_path = _find_image(str(slide.get("photo_keyword", "")), assets)
    # Consistent header hierarchy.
    d.text((92, 78), "VIDEO FARM  /  TECH BRIEF", font=_font(25, True, True), fill=CYAN)
    d.text((92, 140), title[:110], font=_font(52, True), fill=WHITE)
    d.line((92, 222, w - 92, 222), fill=(42, 62, 90), width=2)
    content_top = 280
    if typ in {"metric", "progress"} and metric:
        d.rounded_rectangle((92, content_top, w - 92, h - 150), radius=26, fill=(12, 21, 42), outline=(37, 71, 105), width=2)
        d.text((145, content_top + 90), metric[:32], font=_font(130, True, True), fill=CYAN if typ == "metric" else PURPLE)
        if body: _draw_wrapped(d, (150, content_top + 270), body, _font(38, False), WHITE, w - 360, max_lines=3)
        if typ == "progress":
            d.rounded_rectangle((150, h - 245, w - 150, h - 205), radius=20, fill=(32, 42, 63))
            d.rounded_rectangle((150, h - 245, int(w - 150 * 0.7), h - 205), radius=20, fill=PURPLE)
    elif typ == "comparison":
        left = items[0] if len(items) > 0 else "Вариант A"
        right = items[1] if len(items) > 1 else "Вариант B"
        for x, label, color in [(92, left, CYAN), (w // 2 + 15, right, PURPLE)]:
            d.rounded_rectangle((x, content_top, x + w // 2 - 125, h - 155), radius=24, fill=(13, 22, 43), outline=color, width=3)
            _draw_wrapped(d, (x + 36, content_top + 48), str(label), _font(40, True), color, w // 2 - 200, max_lines=4)
        if body: _draw_wrapped(d, (130, content_top + 225), body, _font(35), WHITE, w - 280, max_lines=3)
        d.text((w // 2 - 32, content_top + 112), "VS", font=_font(46, True, True), fill=ORANGE)
    elif typ == "bar_chart":
        values = [min(1.0, max(0.05, 0.35 + 0.11 * i)) for i in range(max(3, min(5, len(items) or 4)))]
        labels = items or ["A", "B", "C", "D"]
        base_y = h - 205; chart_top = content_top + 45; chart_h = base_y - chart_top
        count = min(len(values), len(labels)); gap = 38; bar_w = (w - 260 - gap * (count - 1)) // count
        for i in range(count):
            x = 130 + i * (bar_w + gap); bh = int(chart_h * values[i])
            d.rounded_rectangle((x, base_y - bh, x + bar_w, base_y), radius=12, fill=CYAN if i % 2 == 0 else PURPLE)
            d.text((x, base_y + 26), str(labels[i])[:18], font=_font(25, True), fill=WHITE)
        if body: d.text((130, h - 130), body[:120], font=_font(26), fill=MUTED)
    elif typ == "list" or typ == "storyboard":
        chosen = items if items else ([body] if body else ["Факт 1", "Факт 2", "Факт 3"])
        y = content_top + 30
        for i, item in enumerate(chosen[:5], 1):
            color = CYAN if i % 2 else PURPLE
            d.rounded_rectangle((120, y, 180, y + 58), radius=13, fill=color)
            d.text((137, y + 10), str(i), font=_font(28, True, True), fill=BG)
            _draw_wrapped(d, (215, y + 4), str(item), _font(35, True), WHITE, w - 390, max_lines=2)
            y += 115
    elif typ == "quote":
        d.text((125, content_top + 35), "“", font=_font(160, True), fill=PURPLE)
        _draw_wrapped(d, (220, content_top + 105), body or title, _font(48, True), WHITE, w - 360, line_gap=22, max_lines=5)
        source = str(slide.get("source", ""))
        if source: d.text((220, h - 170), source[:100], font=_font(22), fill=MUTED)
    elif typ in {"photo", "split"} and photo_path and photo_path.exists():
        if typ == "photo":
            photo = _image_cover(photo_path, (w - 184, h - 330))
            im.paste(photo, (92, 255))
            # Opaque dark title panel, avoiding alpha in final PNG.
            d = ImageDraw.Draw(im)
            d.rectangle((92, h - 290, w - 92, h - 100), fill=(8, 12, 26))
            d.text((125, h - 260), title[:70], font=_font(43, True), fill=WHITE)
            if body: d.text((125, h - 190), body[:100], font=_font(27), fill=CYAN)
        else:
            photo = _image_cover(photo_path, (int(w * 0.43), h - 330))
            im.paste(photo, (92, 260))
            d = ImageDraw.Draw(im)
            _draw_wrapped(d, (int(w * 0.5), 310), title, _font(42, True), WHITE, int(w * 0.4), max_lines=4)
            _draw_wrapped(d, (int(w * 0.5), 520), body, _font(32), CYAN, int(w * 0.4), max_lines=5)
    elif typ == "photo" and not photo_path:
        d.rounded_rectangle((92, content_top, w - 92, h - 150), radius=24, fill=(12, 21, 42), outline=PURPLE, width=2)
        # Stylized image placeholder made from geometric elements, not a copyrighted image.
        d.ellipse((w // 2 - 120, content_top + 110, w // 2 + 120, content_top + 350), outline=CYAN, width=6)
        d.line((w // 2 - 220, content_top + 390, w // 2 + 220, content_top + 390), fill=PURPLE, width=5)
        _draw_wrapped(d, (w // 2 - 280, content_top + 430), body or "Иллюстрация будет добавлена из лицензируемого стока", _font(32), MUTED, 560, max_lines=3)
    else:
        # A structured card, not just text on a blank gradient.
        d.rounded_rectangle((92, content_top, w - 92, h - 150), radius=24, fill=(12, 21, 42), outline=(37, 71, 105), width=2)
        d.rectangle((125, content_top + 36, 137, h - 195), fill=CYAN)
        _draw_wrapped(d, (175, content_top + 45), body or title, _font(48, True), WHITE, w - 390, line_gap=20, max_lines=5)
        d.text((175, h - 205), "ПРОВЕРЬ ИСТОЧНИКИ ПЕРЕД ПУБЛИКАЦИЕЙ", font=_font(21, True, True), fill=ORANGE)
    # Footer with slide ordinal.
    d = ImageDraw.Draw(im)
    d.text((92, h - 78), f"{index:03d}  •  Авторская инфографика", font=_font(20, False, True), fill=MUTED)
    # Ensure no alpha channel in slide PNGs (H.264-safe opaque RGB).
    im.convert("RGB").save(out, format="PNG", optimize=True)
    asset_used = typ in {"photo", "split"} and photo_path is not None and photo_path.exists()
    return {"index": index, "file": str(out), "type": typ, "title": title, "size": list(im.size),
            "mode": "RGB", "asset_file": str(photo_path) if asset_used else None}


def render_slides(settings: Settings, script_path: Path, out_dir: Path, assets_manifest_path: Path | None = None) -> dict[str, Any]:
    script = read_json(script_path)
    assets = read_json(assets_manifest_path) if assets_manifest_path and assets_manifest_path.exists() else None
    ensure_dir(out_dir)
    manifest: dict[str, Any] = {"width": settings.width, "height": settings.height, "color_mode": "RGB", "slides": []}
    idx = 0
    for block in script.get("blocks", []):
        block_id = slugify(str(block.get("id", f"block_{idx}")), 36)
        for j, slide in enumerate(block.get("slides", []), 1):
            idx += 1
            out = out_dir / f"slide_{idx:03d}_{block_id}_{j:02d}.png"
            manifest["slides"].append({**_render_one(slide, str(block.get("heading", "")), idx, out, settings, assets),
                                       "block_id": block.get("id"), "source_slide_index": j})
    write_json(out_dir / "slides_manifest.json", manifest)
    return manifest
