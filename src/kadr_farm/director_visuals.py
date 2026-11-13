"""Semantic visual adapters and post-render visual asset audit.

The renderer deliberately never prints visual_description. That field is a
private instruction for the renderer. Only short, validated on_screen_text and
narration-derived labels may become visible in the exported frame.
"""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont

W, H = 1920, 1080
BG = (9, 15, 27)
BG2 = (17, 29, 48)
PANEL = (22, 37, 59)
PANEL_LIGHT = (30, 49, 75)
WHITE = (245, 248, 252)
MUTED = (165, 181, 203)
BLUE = (75, 156, 226)
CYAN = (75, 210, 214)
GREEN = (83, 190, 142)
ORANGE = (255, 164, 76)
RED = (230, 86, 91)
PURPLE = (164, 130, 235)

_DESCRIPTION_CUES = re.compile(
    r"стилизован|макет|изобраз|покажи|виден|видна|видно|на экране|в кадре|"
    r"композици|крупн(?:ый|ого) план|статичн|динамичн|фон|иконк|слева|справа|"
    r"появля|камера|ракурс|сцена|отрисов|расположен|анимиру|цветов(?:ая|ой) схем",
    re.IGNORECASE,
)
_ROLE_LABELS = {"запрос", "ответ", "ответ модели", "вопрос", "пользователь", "источник", "проверка"}
_GENERIC_WORDS = {
    "это", "как", "что", "для", "при", "или", "если", "когда", "чтобы", "также",
    "который", "которая", "которые", "этот", "эта", "эти", "будет", "может", "можно",
    "нужно", "важно", "сам", "сама", "сами", "свой", "свои", "один", "одна", "одни",
    "очень", "просто", "например", "потому", "перед", "после", "между", "через",
}


def _font(size: int, bold: bool = False):
    paths = (
        ["C:/Windows/Fonts/arialbd.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"]
        if bold else
        ["C:/Windows/Fonts/arial.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"]
    )
    for path in paths:
        try:
            return ImageFont.truetype(path, size=size)
        except OSError:
            continue
    return ImageFont.load_default()


def _text_width(draw: ImageDraw.ImageDraw, text: str, font) -> int:
    box = draw.textbbox((0, 0), text, font=font)
    return box[2] - box[0]


def _wrap(draw: ImageDraw.ImageDraw, text: str, font, width: int) -> list[str]:
    words = str(text or "").split()
    lines: list[str] = []
    current = ""
    for word in words:
        trial = f"{current} {word}".strip()
        if current and _text_width(draw, trial, font) > width:
            lines.append(current)
            current = word
        else:
            current = trial
    if current:
        lines.append(current)
    return lines


def _paragraph(draw, xy, text, font, fill, width, gap=12, max_lines=3) -> list[str]:
    x, y = xy
    lines = _wrap(draw, str(text or ""), font, width)[:max_lines]
    for line in lines:
        draw.text((x, y), line, font=font, fill=fill)
        y += draw.textbbox((0, 0), line, font=font)[3] + gap
    return lines


def _panel(draw, box, fill=PANEL, outline=BLUE, radius=28, width=3):
    draw.rounded_rectangle(box, radius=radius, fill=fill, outline=outline, width=width)


def _instruction_like(text: str) -> bool:
    value = str(text or "").strip()
    return bool(_DESCRIPTION_CUES.search(value)) and len(value.split()) >= 3


def _suspicious_metric(text: str) -> bool:
    value = str(text or "").lower()
    return bool(re.search(r"\d+(?:[.,]\d+)?\s*%|[×xх]\s*\d+|\bв\s+\d+\s+раз", value))


def _safe_labels(beat: dict[str, Any]) -> list[str]:
    result = []
    for raw in beat.get("on_screen_text") or []:
        value = re.sub(r"\s+", " ", str(raw or "")).strip(" \t\r\n•—-:;")
        if not value or len(value) > 40 or _instruction_like(value) or _suspicious_metric(value):
            continue
        if value not in result:
            result.append(value)
    return result[:4]


def _speech_words(text: str, limit: int = 8) -> list[str]:
    words = re.findall(r"[A-Za-zА-Яа-яЁё0-9%+-]+", str(text or ""))
    return [w for w in words if len(w) > 2 and w.lower() not in _GENERIC_WORDS][:limit]


def _speech_phrase(text: str, max_words: int = 9, max_chars: int = 76) -> str:
    raw = re.split(r"(?<=[.!?])\s+", str(text or "").strip())[0]
    words = raw.split()[:max_words]
    phrase = " ".join(words).strip(" ,;:—-")
    if len(phrase) > max_chars:
        phrase = phrase[:max_chars - 1].rsplit(" ", 1)[0] + "…"
    return phrase or "Ключевая мысль"


def _headline(scene: dict[str, Any], beat: dict[str, Any]) -> str:
    labels = [x for x in _safe_labels(beat) if x.strip().lower() not in _ROLE_LABELS]
    meaning = re.sub(r"\s+", " ", str(scene.get("meaning") or scene.get("purpose") or "")).strip()
    generic_prefix = re.match(r"^(анонс плана видео|постановка главного вопроса|визуальная сцена)\b", meaning, flags=re.I)
    if generic_prefix:
        meaning = ""
    if meaning and not re.match(r"^(и|или|а|но|что|как)\b", meaning, flags=re.I) and not _instruction_like(meaning) and len(meaning) <= 78:
        return meaning.rstrip(" .:;—")
    if labels:
        return labels[0]
    text = str(beat.get("spoken_text") or "")
    low = text.lower()
    rules = [
        (("галлюцинац", "уверенно", "ложн", "ошиб"), "Уверенный ответ ещё не истина"),
        (("токен", "токенизац"), "Как текст превращается в токены"),
        (("вероятност", "следующ", "предсказ"), "Как модель выбирает продолжение"),
        (("ссылк", "первоисточник", "провер"), "Проверяем источник"),
        (("архитектур", "transformer", "attention"), "Что происходит внутри модели"),
        (("база данных", "поисковик", "архив"), "Модель — не поисковик"),
        (("t9", "смартфон", "телефон"), "Аналогия с предиктивным вводом"),
    ]
    for keys, title in rules:
        if any(key in low for key in keys):
            return title
    return _speech_phrase(text, max_words=8, max_chars=72)


def _motif(scene: dict[str, Any], beat: dict[str, Any]) -> str:
    text = " ".join(str(scene.get(k) or "") for k in ("meaning", "purpose")) + " "
    text += str(beat.get("spoken_text") or "") + " " + str(beat.get("visual_description") or "")
    low = text.lower()
    if any(x in low for x in ("токен", "token", "слово", "текст разбива", "кусочк")) or re.search(r"\bтекст\b", low):
        return "tokens"
    if any(x in low for x in ("ссылк", "404", "браузер", "url", "битая ссылк")):
        return "browser"
    if any(x in low for x in ("телефон", "смартфон", "t9", "клавиатур")):
        return "phone"
    if any(x in low for x in ("поисковик", "поиск", "провер", "документ", "источн")):
        return "search"
    if any(x in low for x in ("галлюцинац", "ошиб", "ложн", "неверн", "уверенн")):
        return "warning"
    if any(x in low for x in ("базу данных", "архив", "хранени", "сервер")):
        return "database"
    if any(x in low for x in ("архитектур", "transformer", "attention", "нейросет", "модель")):
        return "network"
    if any(x in low for x in ("защит", "безопас", "проверен")):
        return "shield"
    return "network"


def _background() -> Image.Image:
    im = Image.new("RGB", (W, H), BG)
    draw = ImageDraw.Draw(im)
    # Quiet depth and grid, deliberately low-contrast so content stays dominant.
    for y in range(H):
        mix = y / max(1, H - 1)
        c = tuple(int(BG[i] * (1 - mix) + BG2[i] * mix) for i in range(3))
        draw.line((0, y, W, y), fill=c, width=1)
    for x in range(0, W, 96):
        draw.line((x, 0, x, H), fill=(20, 34, 53), width=1)
    for y in range(0, H, 96):
        draw.line((0, y, W, y), fill=(20, 34, 53), width=1)
    draw.ellipse((1450, -280, 2160, 430), fill=(17, 43, 68))
    return im


def _header(draw, title: str, section: str = "РАЗБОР") -> list[str]:
    draw.rounded_rectangle((96, 62, 112, 110), radius=8, fill=CYAN)
    draw.text((140, 68), section.upper()[:24], font=_font(23, True), fill=CYAN)
    lines = _paragraph(draw, (96, 132), title, _font(52, True), WHITE, 1650, 9, 2)
    return lines


def _draw_database(draw, cx: int, cy: int, scale: float = 1.0, color=BLUE):
    w, h = int(300 * scale), int(250 * scale)
    x0, x1 = cx - w // 2, cx + w // 2
    y0, y1 = cy - h // 2, cy + h // 2
    ry = int(44 * scale)
    draw.rectangle((x0, y0 + ry, x1, y1 - ry), fill=(31, 57, 83), outline=color, width=max(3, int(5*scale)))
    draw.ellipse((x0, y0, x1, y0 + 2*ry), fill=(42, 79, 111), outline=color, width=max(3, int(5*scale)))
    draw.arc((x0, y1 - 2*ry, x1, y1), 0, 180, fill=color, width=max(3, int(5*scale)))
    for yy in (y0 + int(95*scale), y0 + int(165*scale)):
        draw.arc((x0, yy - ry, x1, yy + ry), 0, 180, fill=color, width=max(2, int(4*scale)))


def _draw_network(draw, cx: int, cy: int, scale: float = 1.0, color=CYAN):
    nodes = [(-230, -80), (-120, -190), (-100, 55), (20, -15), (145, -190), (155, 105), (260, -35)]
    nodes = [(cx + int(x*scale), cy + int(y*scale)) for x, y in nodes]
    edges = [(0,1),(0,2),(1,3),(2,3),(3,4),(3,5),(4,6),(5,6),(1,4),(2,5)]
    for a, b in edges:
        draw.line((*nodes[a], *nodes[b]), fill=(68, 121, 159), width=max(3, int(7*scale)))
    for i, (x, y) in enumerate(nodes):
        r = int((27 if i == 3 else 21)*scale)
        draw.ellipse((x-r, y-r, x+r, y+r), fill=color if i == 3 else (39, 76, 108), outline=WHITE, width=max(2, int(3*scale)))


def _draw_phone(draw, cx: int, cy: int, scale: float = 1.0, color=CYAN):
    w, h = int(250*scale), int(430*scale)
    box = (cx-w//2, cy-h//2, cx+w//2, cy+h//2)
    _panel(draw, box, fill=(8, 17, 30), outline=color, radius=int(35*scale), width=max(4, int(7*scale)))
    draw.rounded_rectangle((box[0]+int(22*scale), box[1]+int(48*scale), box[2]-int(22*scale), box[3]-int(58*scale)), radius=int(20*scale), fill=(24, 43, 64))
    for row in range(4):
        for col in range(3):
            x = box[0] + int((58+68*col)*scale)
            y = box[1] + int((190+64*row)*scale)
            r = int(18*scale)
            draw.ellipse((x-r, y-r, x+r, y+r), fill=(55, 92, 126), outline=(124, 163, 190), width=max(1, int(2*scale)))
    draw.rounded_rectangle((cx-int(30*scale), box[1]+int(19*scale), cx+int(30*scale), box[1]+int(25*scale)), radius=4, fill=MUTED)


def _draw_browser(draw, cx: int, cy: int, scale: float = 1.0, color=BLUE):
    w, h = int(550*scale), int(350*scale)
    x0, y0 = cx-w//2, cy-h//2
    x1, y1 = x0+w, y0+h
    _panel(draw, (x0, y0, x1, y1), fill=(13, 25, 42), outline=color, radius=int(24*scale), width=max(3, int(5*scale)))
    draw.rounded_rectangle((x0, y0, x1, y0+int(65*scale)), radius=int(20*scale), fill=(37, 57, 79))
    for i, c in enumerate((RED, ORANGE, GREEN)):
        r=int(8*scale); x=x0+int((28+28*i)*scale); y=y0+int(32*scale)
        draw.ellipse((x-r,y-r,x+r,y+r),fill=c)
    draw.rounded_rectangle((x0+int(145*scale), y0+int(20*scale), x1-int(25*scale), y0+int(45*scale)), radius=10, fill=(15, 30, 48))
    for i, yy in enumerate((125, 190, 255)):
        draw.rounded_rectangle((x0+int(42*scale), y0+int(yy*scale), x1-int((85+18*i)*scale), y0+int((yy+13)*scale)), radius=5, fill=(58, 88, 117))


def _draw_document(draw, cx: int, cy: int, scale: float = 1.0, color=ORANGE):
    w, h = int(260*scale), int(340*scale)
    x0, y0 = cx-w//2, cy-h//2
    draw.rounded_rectangle((x0, y0, x0+w, y0+h), radius=int(18*scale), fill=(229, 236, 243), outline=color, width=max(3,int(5*scale)))
    for i in range(5):
        yy=y0+int((75+47*i)*scale)
        draw.rounded_rectangle((x0+int(35*scale),yy,x0+w-int((40+20*(i%2))*scale),yy+int(9*scale)),radius=4,fill=(91,113,133))


def _draw_shield(draw, cx: int, cy: int, scale: float = 1.0, color=GREEN):
    pts=[(cx,cy-int(220*scale)),(cx+int(190*scale),cy-int(145*scale)),(cx+int(155*scale),cy+int(95*scale)),(cx,cy+int(220*scale)),(cx-int(155*scale),cy+int(95*scale)),(cx-int(190*scale),cy-int(145*scale))]
    draw.polygon(pts,fill=(24,70,65),outline=color)
    draw.line((cx-int(85*scale),cy,cx-int(18*scale),cy+int(70*scale),cx+int(105*scale),cy-int(85*scale)),fill=WHITE,width=max(6,int(16*scale)),joint="curve")


def _draw_warning(draw, cx: int, cy: int, scale: float = 1.0, color=ORANGE):
    pts=[(cx,cy-int(245*scale)),(cx+int(250*scale),cy+int(190*scale)),(cx-int(250*scale),cy+int(190*scale))]
    draw.polygon(pts,fill=(73,47,32),outline=color)
    draw.line((cx,cy-int(95*scale),cx,cy+int(55*scale)),fill=WHITE,width=max(8,int(20*scale)))
    r=int(14*scale); draw.ellipse((cx-r,cy+int(105*scale)-r,cx+r,cy+int(105*scale)+r),fill=WHITE)


def _draw_search(draw, cx: int, cy: int, scale: float = 1.0, color=CYAN):
    r=int(155*scale)
    draw.ellipse((cx-r,cy-r,cx+r,cy+r),outline=color,width=max(7,int(17*scale)))
    draw.line((cx+int(110*scale),cy+int(110*scale),cx+int(285*scale),cy+int(285*scale)),fill=color,width=max(12,int(28*scale)))
    for i in range(3):
        yy=cy-int(60*scale)+int(i*60*scale)
        draw.rounded_rectangle((cx-int(80*scale),yy,cx+int(80*scale),yy+int(8*scale)),radius=4,fill=WHITE)


def _draw_tokens(draw, cx: int, cy: int, scale: float = 1.0, color=PURPLE):
    phrases=[("СЛОВО",BLUE),("ТОКЕН",CYAN),("КОНТЕКСТ",ORANGE)]
    for i,(label,c) in enumerate(phrases):
        x=cx-int(310*scale)+int(i*220*scale)
        y=cy-int(55*scale)
        ww=int(190*scale); hh=int(125*scale)
        _panel(draw,(x,y,x+ww,y+hh),fill=(29,47,73),outline=c,radius=int(20*scale),width=max(3,int(5*scale)))
        draw.text((x+int(22*scale),y+int(40*scale)),label,font=_font(int(26*scale),True),fill=WHITE)
        if i<2:
            draw.line((x+ww+int(7*scale),cy,x+ww+int(32*scale),cy),fill=color,width=max(3,int(6*scale)))
            draw.polygon([(x+ww+int(32*scale),cy),(x+ww+int(18*scale),cy-int(10*scale)),(x+ww+int(18*scale),cy+int(10*scale))],fill=color)


def _icon(draw, motif: str, cx: int, cy: int, scale: float = 1.0):
    mapping = {
        "database": _draw_database,
        "network": _draw_network,
        "phone": _draw_phone,
        "browser": _draw_browser,
        "search": _draw_search,
        "shield": _draw_shield,
        "warning": _draw_warning,
        "tokens": _draw_tokens,
    }
    mapping.get(motif, _draw_network)(draw, cx, cy, scale)


def _comparison(draw, beat: dict[str, Any], motif: str) -> list[str]:
    labels = _safe_labels(beat)
    left = labels[0] if labels else "ПОИСК / ХРАНЕНИЕ"
    right = labels[1] if len(labels) > 1 else "ГЕНЕРАЦИЯ ТЕКСТА"
    _panel(draw, (95, 340, 900, 900), fill=(39, 35, 46), outline=RED, radius=34, width=4)
    _panel(draw, (1020, 340, 1825, 900), fill=(20, 45, 60), outline=CYAN, radius=34, width=4)
    draw.text((150, 395), "A", font=_font(36, True), fill=RED)
    draw.text((1075, 395), "B", font=_font(36, True), fill=GREEN)
    _paragraph(draw, (225, 405), left.upper(), _font(35, True), WHITE, 580, 10, 2)
    _paragraph(draw, (1150, 405), right.upper(), _font(35, True), WHITE, 590, 10, 2)
    left_motif = "database" if motif in ("network", "database") else "browser"
    right_motif = "network"
    _icon(draw, left_motif, 490, 685, 0.68)
    _icon(draw, right_motif, 1420, 685, 0.68)
    draw.text((915, 630), "≠", font=_font(90, True), fill=ORANGE)
    return [left, right]


def _infer_steps(beat: dict[str, Any], motif: str) -> list[str]:
    labels = _safe_labels(beat)
    if len(labels) >= 3:
        return labels[:4]
    spoken = str(beat.get("spoken_text") or "").lower()
    if motif == "tokens":
        defaults = ["ИСХОДНЫЙ ТЕКСТ", "ТОКЕНИЗАТОР", "ПОСЛЕДОВАТЕЛЬНОСТЬ ТОКЕНОВ"]
    elif motif == "browser" or motif == "search":
        defaults = ["УТВЕРЖДЕНИЕ", "ПЕРВОИСТОЧНИК", "СВЕРКА КОНТЕКСТА"]
    elif any(x in spoken for x in ("вероятност", "следующ", "предсказ")):
        defaults = ["КОНТЕКСТ", "ВЕРОЯТНОСТИ ТОКЕНОВ", "ВЫБОР ПРОДОЛЖЕНИЯ"]
    elif motif == "warning":
        defaults = ["ЗАПРОС", "УВЕРЕННЫЙ ОТВЕТ", "ПРОВЕРКА ФАКТОВ"]
    else:
        defaults = ["ВХОДНОЙ ТЕКСТ", "ОБРАБОТКА КОНТЕКСТА", "СЛЕДУЮЩИЙ ТОКЕН"]
    if labels:
        # Preserve the model's first precise label but never invent a missing final stage.
        defaults[0] = labels[0]
    if len(labels) == 2:
        defaults[1] = labels[1]
    return defaults


def _flow(draw, beat: dict[str, Any], motif: str) -> list[str]:
    labels = _infer_steps(beat, motif)
    count = min(4, max(3, len(labels)))
    gap = 45
    left = 90
    usable = W - 2*left
    box_w = (usable - gap*(count-1)) // count
    y0, y1 = 455, 780
    accents = [BLUE, CYAN, PURPLE, ORANGE]
    for i, label in enumerate(labels[:count]):
        x0=left+i*(box_w+gap); x1=x0+box_w
        _panel(draw,(x0,y0,x1,y1),fill=(24+i*2,40+i*2,63+i*2),outline=accents[i],radius=30,width=4)
        draw.ellipse((x0+30,y0+28,x0+94,y0+92),fill=accents[i])
        draw.text((x0+49,y0+39),str(i+1),font=_font(29,True),fill=BG)
        _paragraph(draw,(x0+34,y0+142),label.upper(),_font(32,True),WHITE,box_w-68,14,3)
        if i<count-1:
            ax=x1+7; bx=x1+gap-7; ay=(y0+y1)//2
            draw.line((ax,ay,bx,ay),fill=CYAN,width=8)
            draw.polygon([(bx,ay),(bx-22,ay-15),(bx-22,ay+15)],fill=CYAN)
    draw.text((100, 850), "СХЕМА ПРИНЦИПА · НЕ ЧИСЛОВЫЕ ИЗМЕРЕНИЯ", font=_font(22, True), fill=MUTED)
    return labels[:count]


def _text_visual(draw, beat: dict[str, Any], motif: str) -> list[str]:
    labels = _safe_labels(beat)
    spoken = _speech_phrase(str(beat.get("spoken_text") or ""), 8, 64)
    phrase = labels[0] if labels else spoken
    _panel(draw,(130,355,1790,850),fill=(16,31,51),outline=PURPLE,radius=38,width=4)
    _paragraph(draw,(220,435),phrase.upper(),_font(76,True),WHITE,1450,18,3)
    # A tokenization motif is explicitly labelled as a schematic, not as the output
    # of a particular production tokenizer.
    if motif == "tokens":
        _draw_tokens(draw,960,735,0.7)
        draw.text((250, 885), "УСЛОВНАЯ СХЕМА · ГРАНИЦЫ ЗАВИСЯТ ОТ ТОКЕНИЗАТОРА", font=_font(22,True), fill=MUTED)
    else:
        words = _speech_words(phrase, 5)
        if not words:
            words = ["КОНТЕКСТ", "СМЫСЛ", "ОТВЕТ"]
        x=220; y=680
        for i, word in enumerate(words[:5]):
            f=_font(34,True); ww=_text_width(draw,word.upper(),f)+44
            _panel(draw,(x,y,x+ww,y+86),fill=(30,49,75),outline=[BLUE,CYAN,GREEN,ORANGE,PURPLE][i],radius=16,width=3)
            draw.text((x+22,y+22),word.upper(),font=f,fill=WHITE)
            x+=ww+16
    return [phrase]


def _chat_visual(draw, beat: dict[str, Any]) -> list[str]:
    labels = _safe_labels(beat)
    _panel(draw,(265,315,1655,900),fill=(12,24,40),outline=(74,102,134),radius=34,width=4)
    draw.rounded_rectangle((265,315,1655,398),radius=28,fill=(35,55,78))
    draw.text((315,342),"УЧЕБНАЯ СИМУЛЯЦИЯ ИНТЕРФЕЙСА",font=_font(25,True),fill=CYAN)
    # Use only words from the actual narration or the director's short visible labels.
    meaningful = [x for x in labels if x.strip().lower() not in _ROLE_LABELS]
    user_text = meaningful[0] if meaningful else _speech_phrase(str(beat.get("spoken_text") or ""),10,80)
    ai_label = meaningful[1] if len(meaningful)>1 else "Продолжение текста по контексту"
    _panel(draw,(345,465,1430,600),fill=(34,54,79),outline=BLUE,radius=26,width=2)
    draw.text((385,485),"ЗАПРОС",font=_font(20,True),fill=CYAN)
    _paragraph(draw,(385,525),user_text,_font(34),WHITE,980,8,2)
    _panel(draw,(650,650,1565,805),fill=(25,63,57),outline=GREEN,radius=26,width=2)
    draw.text((690,670),"ОТВЕТ МОДЕЛИ",font=_font(20,True),fill=GREEN)
    _paragraph(draw,(690,710),ai_label,_font(34,True),WHITE,825,8,2)
    draw.text((345,835),"Макет для объяснения · не скриншот реального продукта",font=_font(21),fill=MUTED)
    return [user_text, ai_label]


def _quote_visual(draw, beat: dict[str, Any]) -> list[str]:
    labels = _safe_labels(beat)
    quote = labels[0] if labels else _speech_phrase(str(beat.get("spoken_text") or ""), 12, 105)
    _panel(draw,(160,365,1760,850),fill=(20,34,54),outline=ORANGE,radius=36,width=4)
    draw.text((245,405),"“",font=_font(145,True),fill=ORANGE)
    _paragraph(draw,(360,525),quote,_font(60,True),WHITE,1230,20,4)
    return [quote]


def _joke_visual(draw, beat: dict[str, Any], motif: str) -> list[str]:
    labels = _safe_labels(beat)
    caption = labels[0] if labels else _speech_phrase(str(beat.get("spoken_text") or ""), 8, 70)
    _panel(draw,(120,350,1800,880),fill=(31,35,55),outline=ORANGE,radius=42,width=4)
    _draw_warning(draw,470,635,0.58,ORANGE)
    _panel(draw,(790,480,1655,745),fill=(50,54,76),outline=PURPLE,radius=32,width=3)
    _paragraph(draw,(855,545),caption,_font(48,True),WHITE,720,15,4)
    draw.polygon([(810,720),(760,770),(860,740)],fill=(50,54,76))
    return [caption]


def _illustration(draw, beat: dict[str, Any], motif: str) -> list[str]:
    labels = _safe_labels(beat)
    main = labels[0] if labels else _speech_phrase(str(beat.get("spoken_text") or ""), 8, 70)
    _panel(draw,(150,350,1770,900),fill=(15,30,49),outline=(54,87,122),radius=38,width=3)
    _icon(draw,motif,645,630,0.78)
    _panel(draw,(1030,470,1640,790),fill=(28,48,72),outline=CYAN,radius=28,width=3)
    _paragraph(draw,(1090,535),main,_font(43,True),WHITE,480,16,4)
    secondary = labels[1:3]
    y=790
    for i,label in enumerate(secondary):
        draw.rounded_rectangle((1085+i*300,y,1360+i*300,y+10),radius=5,fill=[BLUE,ORANGE][i])
    return [main,*secondary]


def render_beat_visual(scene: dict[str, Any], beat: dict[str, Any], output_path: Path) -> Path:
    """Render a beat using a real semantic template; never print visual_description."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    kind = str(beat.get("visual_type", "illustration")).lower()
    motif = _motif(scene, beat)
    im = _background()
    draw = ImageDraw.Draw(im)
    title = _headline(scene, beat)
    visible = _header(draw, title, section={
        "comparison": "СРАВНЕНИЕ", "diagram": "СХЕМА", "scheme": "СХЕМА",
        "chart": "ПРИНЦИП", "text": "КЛЮЧЕВАЯ МЫСЛЬ", "screencast": "ДЕМОНСТРАЦИЯ",
        "quote": "ЦИТАТА", "joke": "ПАУЗА", "illustration": "ОБЪЯСНЕНИЕ",
        "photo": "ИЛЛЮСТРАЦИЯ", "video": "ИЛЛЮСТРАЦИЯ", "broll": "ИЛЛЮСТРАЦИЯ",
    }.get(kind, "ОБЪЯСНЕНИЕ"))
    if kind == "comparison":
        content = _comparison(draw, beat, motif)
        template = "comparison"
    elif kind in ("diagram", "scheme", "chart"):
        content = _flow(draw, beat, motif)
        template = "process_flow"
    elif kind == "text":
        content = _text_visual(draw, beat, motif)
        template = "kinetic_typography"
    elif kind == "screencast":
        content = _chat_visual(draw, beat)
        template = "simulated_interface"
    elif kind == "quote":
        content = _quote_visual(draw, beat)
        template = "editorial_quote"
    elif kind == "joke":
        content = _joke_visual(draw, beat, motif)
        template = "visual_punchline"
    else:
        content = _illustration(draw, beat, motif)
        template = f"illustration_{motif}"
    visible.extend(str(x) for x in content if str(x).strip())
    # Add a subtle footer, not a repeated production watermark.
    draw.text((100, 1015), "СХЕМА ДЛЯ ОБЪЯСНЕНИЯ", font=_font(18, True), fill=(116,139,163))
    im.save(output_path, format="PNG", optimize=True)
    metadata = {
        "render_version": "1.1.0",
        "template_id": template,
        "visual_type": kind,
        "visual_family": motif,
        "display_title": title,
        "visible_text": visible,
        "description_rendered": False,
        "description_used_only_as_private_semantic_hint": True,
        "dimensions": [W, H],
    }
    output_path.with_suffix(output_path.suffix + ".meta.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return output_path


def render_stock_overlay(texts: list[str], output_path: Path) -> Path:
    """Transparent lower-third for stock footage; only short display copy is allowed."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    clean = []
    for raw in texts:
        value = re.sub(r"\s+", " ", str(raw or "")).strip()
        if value and len(value) <= 40 and not _instruction_like(value) and not _suspicious_metric(value):
            clean.append(value)
    captions = clean[:2]
    if captions:
        block_h = min(190, 68 + 58 * len(captions))
        draw.rounded_rectangle((95, H-block_h-70, W-95, H-70), radius=28,
                               fill=(8, 16, 29, 220), outline=(75, 210, 214, 230), width=3)
        y = H-block_h-35
        for caption in captions:
            draw.text((145, y), caption, font=_font(35, True), fill=(250, 252, 255, 255))
            y += 55
    layer.save(output_path, format="PNG")
    output_path.with_suffix(output_path.suffix + ".meta.json").write_text(
        json.dumps({"render_version":"1.1.0","template_id":"stock_lower_third",
                    "visible_text":captions,"description_rendered":False},ensure_ascii=False,indent=2),
        encoding="utf-8"
    )
    return output_path


def _dhash(image: Image.Image) -> int:
    gray = image.convert("L").resize((9, 8), Image.Resampling.LANCZOS)
    px = list(gray.tobytes())
    value = 0
    for y in range(8):
        for x in range(8):
            value = (value << 1) | (1 if px[y*9+x] > px[y*9+x+1] else 0)
    return value


def _hamming(a: int, b: int) -> int:
    return (a ^ b).bit_count()


def _frame_for_asset(path: Path, media_type: str, ffmpeg: str, scratch: Path) -> Path:
    if media_type != "video":
        return path
    scratch.mkdir(parents=True, exist_ok=True)
    frame = scratch / (hashlib.sha1(str(path).encode("utf-8")).hexdigest()[:12] + ".jpg")
    try:
        subprocess.run([ffmpeg,"-y","-hide_banner","-loglevel","error","-ss","0.7","-i",str(path),
                        "-frames:v","1",str(frame)],capture_output=True,check=True)
        return frame if frame.is_file() else path
    except (OSError, subprocess.CalledProcessError):
        return path


def audit_visual_assets(beats: list[dict[str, Any]], output_path: Path,
                        ffmpeg: str = "ffmpeg") -> dict[str, Any]:
    """Audit actual asset files for readability, duplicate imagery and renderer leaks.

    This is a deterministic technical/content-surface gate, not an AI proof that a
    visual semantically matches narration. The latter remains human-reviewable.
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)
    scratch = output_path.parent / "_visual_audit_frames"
    entries: list[dict[str, Any]] = []
    errors: list[str] = []
    warnings: list[str] = []
    seen: list[tuple[str, int, str]] = []
    repeated_kinds: list[str] = []
    for index, beat in enumerate(beats):
        path = Path(str(beat.get("visual_path") or ""))
        entry = {
            "scene_id": beat.get("scene_id"), "beat_id": beat.get("beat_id"),
            "visual_type": beat.get("visual_type"), "source": beat.get("source"),
            "path": str(path), "status": "passed",
        }
        if not path.is_file():
            entry["status"] = "failed"
            errors.append(f"{entry['scene_id']}/{entry['beat_id']}: visual file missing")
            entries.append(entry)
            continue
        metadata_paths = [path.with_suffix(path.suffix + ".meta.json")]
        overlay_value = beat.get("overlay_path")
        if overlay_value:
            overlay_file = Path(str(overlay_value))
            if not overlay_file.is_file():
                entry["status"] = "failed"
                errors.append(f"{entry['scene_id']}/{entry['beat_id']}: stock overlay file missing")
            else:
                metadata_paths.append(overlay_file.with_suffix(overlay_file.suffix + ".meta.json"))
        visible_text: list[str] = []
        for meta_path in metadata_paths:
            if not meta_path.is_file():
                continue
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                entry["template_id"] = entry.get("template_id") or meta.get("template_id")
                visible_text.extend(meta.get("visible_text", []))
                if meta.get("description_rendered") is True:
                    entry["status"] = "failed"
                    errors.append(f"{entry['scene_id']}/{entry['beat_id']}: internal visual description leaked onto screen")
                if any(_instruction_like(str(x)) for x in meta.get("visible_text", [])):
                    entry["status"] = "failed"
                    errors.append(f"{entry['scene_id']}/{entry['beat_id']}: director instruction-like text in visible copy")
            except (OSError, json.JSONDecodeError) as exc:
                entry["status"] = "failed"
                errors.append(f"{entry['scene_id']}/{entry['beat_id']}: invalid visual metadata: {exc}")
        if visible_text:
            entry["visible_text"] = visible_text
        try:
            sample = _frame_for_asset(path, str(beat.get("media_type") or "image"), ffmpeg, scratch)
            with Image.open(sample) as image:
                image.verify()
            with Image.open(sample) as image:
                if image.width < 640 or image.height < 360:
                    entry["status"] = "failed"
                    errors.append(f"{entry['scene_id']}/{entry['beat_id']}: rendered frame below 640x360")
                else:
                    entry["dimensions"] = [image.width, image.height]
                    phash = _dhash(image)
                    entry["dhash"] = f"{phash:016x}"
                    for previous_id, previous_hash, previous_path in seen:
                        distance = _hamming(phash, previous_hash)
                        if distance <= 1:
                            entry["duplicate_of"] = previous_id
                            entry["duplicate_distance"] = distance
                            warnings.append(f"{entry['scene_id']}/{entry['beat_id']}: near-duplicate visual of {previous_id}")
                            break
                    seen.append((f"{entry['scene_id']}/{entry['beat_id']}", phash, str(path)))
        except (OSError, ValueError) as exc:
            entry["status"] = "failed"
            errors.append(f"{entry['scene_id']}/{entry['beat_id']}: visual cannot be decoded: {exc}")
        kind = str(beat.get("visual_type") or "unknown")
        repeated_kinds.append(kind)
        entries.append(entry)
    for i in range(2, len(repeated_kinds)):
        if repeated_kinds[i] == repeated_kinds[i-1] == repeated_kinds[i-2]:
            warnings.append(f"Three consecutive beats use visual_type={repeated_kinds[i]!r}; check visual monotony")
    report = {
        "status": "failed" if errors else ("passed_with_warnings" if warnings else "passed"),
        "asset_count": len(beats), "decoded_count": sum(1 for x in entries if x.get("dimensions")),
        "errors": errors, "warnings": warnings, "entries": entries,
        "semantic_frame_to_speech": "human_review_required",
        "description_leak_policy": "visual_description is private renderer guidance and must never appear as visible copy",
    }
    output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def audit_final_video_frames(video_path: Path, timeline: list[dict[str, Any]],
                             output_path: Path, contact_sheet_path: Path,
                             ffmpeg: str = "ffmpeg") -> dict[str, Any]:
    """Extract and inspect representative frames from the actual assembled MP4.

    The generated contact sheet is based on final.mp4, not source stills, so stock
    overlays, crop, motion, encoding and timeline mistakes are visible to reviewers.
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)
    contact_sheet_path.parent.mkdir(parents=True, exist_ok=True)
    frame_dir = output_path.parent / "_final_video_frames"
    frame_dir.mkdir(parents=True, exist_ok=True)
    entries: list[dict[str, Any]] = []
    errors: list[str] = []
    warnings: list[str] = []
    seen: list[tuple[str, int]] = []
    for index, slot in enumerate(timeline, 1):
        start = max(0.0, float(slot.get("start_sec", 0.0)))
        end = max(start, float(slot.get("end_sec", start)))
        duration = max(0.0, end - start)
        sample_time = start + min(0.45, max(0.08, duration * 0.4))
        frame_path = frame_dir / f"frame_{index:04d}.jpg"
        entry = {
            "scene_id": slot.get("scene_id"), "beat_id": slot.get("beat_id"),
            "sample_time_sec": round(sample_time, 4), "spoken_text": slot.get("spoken_text", ""),
            "frame_path": str(frame_path.resolve()), "status": "passed",
        }
        if not video_path.is_file():
            entry["status"] = "failed"
            errors.append("Final MP4 is missing; cannot extract representative frames")
            entries.append(entry)
            break
        try:
            subprocess.run(
                [ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-ss", f"{sample_time:.4f}",
                 "-i", str(video_path), "-frames:v", "1", "-q:v", "2", str(frame_path)],
                capture_output=True, check=True,
            )
            with Image.open(frame_path) as frame:
                frame.verify()
            with Image.open(frame_path) as frame:
                if frame.width < 640 or frame.height < 360:
                    entry["status"] = "failed"
                    errors.append(f"{entry['scene_id']}/{entry['beat_id']}: final MP4 frame below 640x360")
                else:
                    entry["dimensions"] = [frame.width, frame.height]
                    phash = _dhash(frame)
                    entry["dhash"] = f"{phash:016x}"
                    for previous_id, previous_hash in seen:
                        distance = _hamming(phash, previous_hash)
                        if distance <= 1:
                            entry["duplicate_of"] = previous_id
                            entry["duplicate_distance"] = distance
                            warnings.append(f"Final frame {entry['scene_id']}/{entry['beat_id']} is near-duplicate of {previous_id}")
                            break
                    seen.append((f"{entry['scene_id']}/{entry['beat_id']}", phash))
        except (OSError, subprocess.CalledProcessError, ValueError) as exc:
            entry["status"] = "failed"
            errors.append(f"{entry['scene_id']}/{entry['beat_id']}: failed to extract/decode final frame: {exc}")
        entries.append(entry)

    thumb_w, thumb_h, label_h = 640, 360, 88
    cols = 2
    rows = max(1, (len(entries) + cols - 1) // cols)
    sheet = Image.new("RGB", (cols * thumb_w, rows * (thumb_h + label_h)), (9, 14, 24))
    draw = ImageDraw.Draw(sheet)
    for i, entry in enumerate(entries):
        frame_path = Path(entry["frame_path"])
        x0, y0 = (i % cols) * thumb_w, (i // cols) * (thumb_h + label_h)
        try:
            with Image.open(frame_path) as source:
                image = source.convert("RGB")
                image.thumbnail((thumb_w, thumb_h), Image.Resampling.LANCZOS)
            sheet.paste(image, (x0 + (thumb_w-image.width)//2, y0 + (thumb_h-image.height)//2))
        except OSError:
            draw.rectangle((x0, y0, x0+thumb_w, y0+thumb_h), fill=(63, 28, 37))
            draw.text((x0+20, y0+150), "FRAME EXTRACTION FAILED", font=_font(22, True), fill=WHITE)
        label = f"{entry['scene_id']} / {entry['beat_id']} · {entry['sample_time_sec']:.2f}s"
        draw.text((x0+16, y0+thumb_h+7), label[:84], font=_font(20, True), fill=CYAN)
        draw.text((x0+16, y0+thumb_h+38), str(entry.get("spoken_text", ""))[:82], font=_font(16), fill=WHITE)
    sheet.save(contact_sheet_path, format="JPEG", quality=90)

    report = {
        "status": "failed" if errors else ("passed_with_warnings" if warnings else "passed"),
        "video": str(video_path.resolve()), "sampled_frame_count": len(entries),
        "decoded_frame_count": sum(1 for x in entries if x.get("dimensions")),
        "errors": errors, "warnings": warnings, "entries": entries,
        "contact_sheet": str(contact_sheet_path.resolve()),
        "semantic_frame_to_speech": "human_review_required",
        "note": "Frames are extracted from the encoded final MP4; image similarity is not semantic validation.",
    }
    output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def create_contact_sheet(items: list[dict[str, Any]], output_path: Path,
                         ffmpeg: str = "ffmpeg") -> Path:
    """Create a review sheet with a frame and its exact spoken phrase."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    thumb_w, thumb_h, label_h = 640, 360, 88
    cols = 2
    rows = max(1, (len(items) + cols - 1) // cols)
    sheet = Image.new("RGB", (cols*thumb_w, rows*(thumb_h+label_h)), (9, 14, 24))
    draw = ImageDraw.Draw(sheet)
    scratch = output_path.parent / "_contact_frames"
    for i, item in enumerate(items):
        path = Path(item["visual_path"])
        image_path = _frame_for_asset(path, str(item.get("media_type") or "image"), ffmpeg, scratch)
        try:
            with Image.open(image_path) as src:
                im = src.convert("RGB")
                im.thumbnail((thumb_w, thumb_h), Image.Resampling.LANCZOS)
            x0 = (i % cols)*thumb_w
            y0 = (i // cols)*(thumb_h+label_h)
            x = x0+(thumb_w-im.width)//2
            y = y0+(thumb_h-im.height)//2
            sheet.paste(im, (x,y))
        except OSError:
            continue
        lx, ly = (i % cols)*thumb_w+16, (i // cols)*(thumb_h+label_h)+thumb_h+8
        label = f"{item['scene_id']} / {item['beat_id']} · {str(item.get('visual_type') or '').upper()}"
        draw.text((lx,ly), label[:88], font=_font(20,True), fill=CYAN)
        draw.text((lx,ly+31), str(item.get("spoken_text", ""))[:82], font=_font(17), fill=WHITE)
    sheet.save(output_path, format="JPEG", quality=90)
    return output_path
