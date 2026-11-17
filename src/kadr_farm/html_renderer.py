"""HTML/CSS motion renderer via Playwright.

Тёплая презентационная тема, 6 шаблонов, анимации синхронизированы
с измеренной длительностью бита. Без сетевых шрифтов, без хардкод-задержек,
запись видео в изолированном каталоге.
"""
from __future__ import annotations

import asyncio
import html
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from playwright.async_api import async_playwright

W, H, FPS = 1920, 1080, 25

BASE_CSS = """
* { box-sizing: border-box; margin: 0; padding: 0; }
body {
  width: 1920px; height: 1080px; overflow: hidden; position: relative;
  background: linear-gradient(135deg, #fdf3dc 0%, #f7d98b 55%, #f0b45c 100%);
  font-family: 'Segoe UI', 'Arial', sans-serif; color: #26221c;
  display: flex; flex-direction: column; justify-content: center; align-items: center;
}
body::after {
  content: ''; position: absolute; inset: 0; pointer-events: none; opacity: .06;
  background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='240' height='240'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.9' numOctaves='2'/%3E%3C/filter%3E%3Crect width='240' height='240' filter='url(%23n)'/%3E%3C/svg%3E");
}
.kicker {
  position: absolute; top: 64px; left: 96px; font-size: 26px; font-weight: 700;
  letter-spacing: 3px; text-transform: uppercase; color: #b4552d;
  opacity: 0; animation: fadeUp .45s ease-out forwards;
}
.card {
  background: #fffdf7; border: 3px solid #2a2620; border-radius: 28px;
  box-shadow: 0 18px 40px rgba(90,60,20,.18);
}
@keyframes fadeUp { from { opacity: 0; transform: translateY(34px); } to { opacity: 1; transform: translateY(0); } }
@keyframes popIn { 0% { opacity: 0; transform: scale(.6); } 70% { transform: scale(1.06); } 100% { opacity: 1; transform: scale(1); } }
@keyframes slideL { from { opacity: 0; transform: translateX(-90px); } to { opacity: 1; transform: translateX(0); } }
@keyframes slideR { from { opacity: 0; transform: translateX(90px); } to { opacity: 1; transform: translateX(0); } }
@keyframes kenburns { from { transform: scale(1.0) translate(0,0); } to { transform: scale(1.09) translate(-1.2%, -1%); } }
@keyframes fadeIn { to { opacity: 1; } }
"""


def _labels(beat: dict[str, Any], limit: int = 4, width: int = 42) -> list[str]:
    raw = beat.get("on_screen_text") or []
    if isinstance(raw, str):
        raw = [raw]
    out = []
    for item in raw:
        text = re.sub(r"\s+", " ", str(item or "")).strip()
        if text:
            out.append(html.escape(text[:width]))
    return out[:limit]


def _delays(n: int, duration: float, start: float = 0.3, tail: float = 0.6) -> list[float]:
    if n <= 0:
        return []
    start = min(start, max(0.15, duration * 0.2))
    last = max(start, duration - tail)
    if n == 1:
        return [start]
    return [round(start + i * (last - start) / (n - 1), 3) for i in range(n)]


def _kicker(beat: dict[str, Any], kind: str) -> str:
    section = {
        "comparison": "СРАВНЕНИЕ", "diagram": "СХЕМА", "scheme": "СХЕМА",
        "chart": "ПРИНЦИП", "quote": "ЦИТАТА", "joke": "ПАУЗА",
        "photo": "ФОТО", "text": "КЛЮЧЕВАЯ МЫСЛЬ",
    }.get(kind, "ОБЪЯСНЕНИЕ")
    return f'<div class="kicker">{section}</div>'


def _t_thesis(beat, labels, duration):
    lines = labels[:3] or [html.escape(str(beat.get("scene_meaning") or "Ключевая мысль")[:80])]
    delays = [round(0.3 + i * 0.5, 3) for i in range(len(lines))]
    body = "".join(
        f'<div class="line" style="animation-delay:{d}s">{t}</div>'
        for t, d in zip(lines, delays)
    )
    css = """
    .line { font-size: 86px; font-weight: 900; text-align: center; max-width: 1600px;
            line-height: 1.15; margin: 14px 0; opacity: 0; animation: fadeUp .5s ease-out forwards; }
    .line:nth-child(odd) { color: #b4552d; }
    """
    return body, css


def _t_steps(beat, labels, duration):
    items = labels[:4] or ["Шаг 1", "Шаг 2", "Шаг 3"]
    delays = _delays(len(items), duration)
    parts = []
    for i, (t, d) in enumerate(zip(items, delays)):
        if i:
            parts.append(f'<div class="arrow" style="animation-delay:{d}s">➜</div>')
        parts.append(
            f'<div class="card step" style="animation-delay:{d}s">'
            f'<div class="num">{i + 1}</div><div class="txt">{t}</div></div>'
        )
    css = """
    .row { display: flex; gap: 28px; align-items: stretch; justify-content: center; max-width: 1760px; }
    .step { flex: 1; max-width: 380px; min-height: 260px; padding: 36px 28px;
            display: flex; flex-direction: column; justify-content: center; align-items: center;
            opacity: 0; animation: popIn .5s cubic-bezier(.175,.885,.32,1.275) forwards; }
    .num { width: 64px; height: 64px; border-radius: 50%; background: #e8862e; color: #fffdf7;
           font-size: 34px; font-weight: 900; display: flex; align-items: center; justify-content: center; margin-bottom: 22px; }
    .step:nth-child(4n+3) .num { background: #2a9d8f; }
    .step:nth-child(4n+5) .num { background: #7d5ba6; }
    .txt { font-size: 34px; font-weight: 700; text-align: center; }
    .arrow { font-size: 56px; color: #b4552d; align-self: center; opacity: 0; animation: fadeIn .3s ease-out forwards; }
    """
    return f'<div class="row">{"".join(parts)}</div>', css


def _t_comparison(beat, labels, duration):
    left = labels[0] if labels else "Вариант А"
    right = labels[1] if len(labels) > 1 else "Вариант Б"
    d = _delays(2, duration, start=0.5)
    css = """
    .cols { display: flex; gap: 70px; align-items: stretch; }
    .col { width: 700px; min-height: 420px; padding: 60px 48px; display: flex; align-items: center;
           justify-content: center; font-size: 56px; font-weight: 900; text-align: center; opacity: 0; }
    .col.left { animation: slideL .55s ease-out forwards; border-top: 14px solid #3e7cb1; }
    .col.right { animation: slideR .55s ease-out forwards; border-top: 14px solid #e8862e; }
    .vs { position: absolute; left: 50%; top: 50%; transform: translate(-50%,-50%);
          width: 110px; height: 110px; border-radius: 50%; background: #26221c; color: #fdf3dc;
          font-size: 44px; font-weight: 900; display: flex; align-items: center; justify-content: center;
          opacity: 0; animation: popIn .45s ease-out forwards; }
    """
    mid = round((d[0] + d[1]) / 2 + 0.2, 3)
    body = (
        f'<div class="cols"><div class="card col left" style="animation-delay:{d[0]}s">{left}</div>'
        f'<div class="card col right" style="animation-delay:{d[1]}s">{right}</div></div>'
        f'<div class="vs" style="animation-delay:{mid}s">VS</div>'
    )
    return body, css


def _t_quote(beat, labels, duration):
    text = labels[0] if labels else html.escape(str(beat.get("spoken_text") or "")[:110])
    author = labels[1] if len(labels) > 1 else ""
    d = _delays(2 if author else 1, duration, start=0.5)
    css = """
    .mark { position: absolute; top: 120px; left: 130px; font-size: 340px; font-weight: 900;
            color: #b4552d; opacity: .14; line-height: 1; }
    .q { font-size: 66px; font-weight: 700; font-style: italic; text-align: center; max-width: 1500px;
         line-height: 1.3; opacity: 0; animation: fadeUp .6s ease-out forwards; }
    .a { margin-top: 44px; font-size: 34px; font-weight: 700; color: #b4552d; opacity: 0;
         animation: fadeUp .5s ease-out forwards; }
    """
    body = f'<div class="mark">«</div><div class="q" style="animation-delay:{d[0]}s">{text}</div>'
    if author:
        body += f'<div class="a" style="animation-delay:{d[1]}s">— {author}</div>'
    return body, css


def _t_number(beat, labels, duration):
    value = labels[0] if labels else "100%"
    caption = labels[1] if len(labels) > 1 else ""
    d = _delays(2 if caption else 1, duration, start=0.4)
    css = """
    .val { font-size: 250px; font-weight: 900; color: #e8862e; line-height: 1;
           text-shadow: 0 10px 0 rgba(90,60,20,.12); opacity: 0;
           animation: popIn .6s cubic-bezier(.175,.885,.32,1.275) forwards; }
    .cap { margin-top: 36px; font-size: 46px; font-weight: 700; text-align: center; max-width: 1400px;
           opacity: 0; animation: fadeUp .5s ease-out forwards; }
    """
    body = f'<div class="val" style="animation-delay:{d[0]}s">{value}</div>'
    if caption:
        body += f'<div class="cap" style="animation-delay:{d[1]}s">{caption}</div>'
    return body, css


def _t_photo(beat, labels, duration):
    image = str(beat.get("image_path") or "")
    caption = labels[0] if labels else ""
    css = """
    .ph { position: absolute; inset: 0; width: 100%; height: 100%; object-fit: cover;
          animation: kenburns DURs linear forwards; }
    .cap { position: absolute; left: 96px; bottom: 72px; max-width: 1400px; padding: 26px 40px;
           font-size: 40px; font-weight: 700; opacity: 0; animation: fadeUp .5s ease-out forwards; }
    .shade { position: absolute; inset: 0; background: linear-gradient(to top, rgba(38,34,28,.35), transparent 40%); }
    """
    css = css.replace("DURs", f"{max(1.0, duration):.3f}s")
    cap_delay = round(max(0.4, duration - 1.4), 3)
    body = f'<img class="ph" src="{html.escape(image)}"><div class="shade"></div>'
    if caption:
        body += f'<div class="card cap" style="animation-delay:{cap_delay}s">{caption}</div>'
    return body, css


def _build_html(beat: dict[str, Any], duration: float) -> str:
    kind = str(beat.get("visual_type") or "text").lower()
    labels = _labels(beat)
    if kind == "photo" and beat.get("image_path"):
        body, css = _t_photo(beat, labels, duration)
        kicker = ""
    elif kind == "comparison":
        body, css = _t_comparison(beat, labels, duration)
        kicker = _kicker(beat, kind)
    elif kind == "quote":
        body, css = _t_quote(beat, labels, duration)
        kicker = _kicker(beat, kind)
    elif kind in ("diagram", "scheme", "chart"):
        if labels and re.match(r"^[\d.,]+[%×x]?$", labels[0]):
            body, css = _t_number(beat, labels, duration)
        else:
            body, css = _t_steps(beat, labels, duration)
        kicker = _kicker(beat, kind)
    else:
        body, css = _t_thesis(beat, labels, duration)
        kicker = _kicker(beat, kind)
    return (
        "<!DOCTYPE html><html lang='ru'><head><meta charset='UTF-8'>"
        f"<style>{BASE_CSS}{css}</style></head><body>{kicker}{body}</body></html>"
    )


def render_html_video(
    beat: dict[str, Any],
    duration: float,
    output_path: Path,
    *,
    fps: int = FPS,
) -> Path:
    """Рендерит анимированный MP4 ровно под измеренную длительность бита."""
    duration = max(1.0, float(duration))
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    html_path = output_path.with_suffix(".html")
    html_path.write_text(_build_html(beat, duration), encoding="utf-8")
    mp4_path = output_path.with_suffix(".mp4")

    async def _record(record_dir: Path) -> None:
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                args=["--hide-scrollbars", "--force-color-profile=srgb", "--mute-audio"]
            )
            context = await browser.new_context(
                viewport={"width": W, "height": H},
                record_video_dir=str(record_dir),
                record_video_size={"width": W, "height": H},
            )
            page = await context.new_page()
            await page.goto(html_path.resolve().as_uri())
            await page.evaluate("document.fonts.ready")
            await page.wait_for_timeout(int((duration + 0.3) * 1000))
            await context.close()
            await browser.close()

    with tempfile.TemporaryDirectory(dir=output_path.parent, prefix="rec_") as tmp:
        asyncio.run(_record(Path(tmp)))
        webm = next(Path(tmp).glob("*.webm"))
        subprocess.run(
            ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
             "-i", str(webm), "-t", f"{duration:.3f}", "-r", str(fps),
             "-c:v", "libx264", "-preset", "fast", "-pix_fmt", "yuv420p",
             "-an", str(mp4_path)],
            check=True,
        )
    html_path.unlink(missing_ok=True)
    return mp4_path