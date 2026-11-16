import asyncio
import subprocess
from pathlib import Path
from typing import Any
from playwright.async_api import async_playwright

HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <style>
        @import url('https://fonts.googleapis.com/css2?family=Montserrat:wght@400;700;900&display=swap');
        body {
            margin: 0; padding: 0; width: 1920px; height: 1080px;
            background: radial-gradient(circle at 50% 0%, #1a2a40 0%, #0a0f1a 100%);
            font-family: 'Montserrat', sans-serif; color: white;
            display: flex; flex-direction: column; justify-content: center; align-items: center;
            overflow: hidden;
        }
        .header {
            position: absolute; top: 80px; left: 100px;
            font-size: 24px; font-weight: 700; color: #4BD2D6;
            text-transform: uppercase; letter-spacing: 2px;
            animation: fadeIn 0.5s ease-out forwards;
        }
        .title {
            font-size: 64px; font-weight: 900; text-align: center;
            max-width: 1600px; line-height: 1.2; margin-bottom: 80px;
            opacity: 0; animation: slideUp 0.8s ease-out 0.3s forwards;
        }
        .grid {
            display: flex; gap: 40px; justify-content: center; align-items: center;
            opacity: 0; animation: fadeIn 0.5s ease-out 0.8s forwards;
        }
        .card {
            background: rgba(255, 255, 255, 0.05);
            backdrop-filter: blur(16px); -webkit-backdrop-filter: blur(16px);
            border: 2px solid rgba(75, 210, 214, 0.3);
            border-radius: 24px; padding: 40px; width: 300px; height: 200px;
            display: flex; flex-direction: column; justify-content: center; align-items: center;
            box-shadow: 0 8px 32px 0 rgba(0, 0, 0, 0.37);
            opacity: 0; transform: translateY(30px);
            animation: popIn 0.6s cubic-bezier(0.175, 0.885, 0.32, 1.275) forwards;
        }
        .card:nth-child(1) { animation-delay: 1.2s; border-color: #4B9CE2; }
        .card:nth-child(2) { animation-delay: 2.5s; border-color: #4BD2D6; }
        .card:nth-child(3) { animation-delay: 3.8s; border-color: #A482EB; }
        .card:nth-child(4) { animation-delay: 5.1s; border-color: #FFA44C; }
        .step-num {
            font-size: 28px; font-weight: 900; margin-bottom: 15px;
            background: linear-gradient(90deg, #4B9CE2, #4BD2D6); -webkit-background-clip: text; color: transparent;
        }
        .step-text { font-size: 32px; font-weight: 700; text-align: center; }
        
        @keyframes fadeIn { to { opacity: 1; } }
        @keyframes slideUp { from { opacity: 0; transform: translateY(40px); } to { opacity: 1; transform: translateY(0); } }
        @keyframes popIn { to { opacity: 1; transform: translateY(0); } }
    </style>
</head>
<body>
    <div class="header">СХЕМА ПРИНЦИПА</div>
    <div class="title">{title}</div>
    <div class="grid">
        {cards}
    </div>
</body>
</html>
"""

def render_html_video(beat: dict[str, Any], duration: float, output_path: Path) -> Path:
    """Генерирует видео из HTML/CSS с помощью Playwright."""
    title = str(beat.get("scene_meaning", "Ключевая мысль"))[:80]
    labels = beat.get("on_screen_text", ["Шаг 1", "Шаг 2", "Шаг 3"])
    
    cards_html = ""
    for i, label in enumerate(labels[:4]):
        cards_html += f'''
        <div class="card">
            <div class="step-num">ШАГ {i+1}</div>
            <div class="step-text">{label.upper()}</div>
        </div>
        '''
        
    html_content = HTML_TEMPLATE.replace("{title}", title).replace("{cards}", cards_html)
    html_path = output_path.with_suffix(".html")
    html_path.write_text(html_content, encoding="utf-8")
    
    video_path = output_path.with_suffix(".webm")
    
    async def _record():
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            context = await browser.new_context(
                viewport={"width": 1920, "height": 1080},
                record_video_dir=output_path.parent,
                record_video_size={"width": 1920, "height": 1080}
            )
            page = await context.new_page()
            await page.goto(f"file://{html_path.resolve()}")
            await page.wait_for_timeout(int(duration * 1000)) # Ждем нужное время
            await context.close() # Сохраняет видео
            await browser.close()
    
    asyncio.run(_record())
    
    # Playwright сохраняет видео со случайным именем, найдем его
    generated_webm = next(output_path.parent.glob("*.webm"))
    generated_webm.rename(video_path)
    
    # Конвертируем WebM в MP4 для идеальной совместимости с FFmpeg
    mp4_path = output_path.with_suffix(".mp4")
    subprocess.run([
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(video_path), "-c:v", "libx264", "-preset", "fast",
        "-pix_fmt", "yuv420p", str(mp4_path)
    ], check=True)
    
    video_path.unlink() # Удаляем webm
    html_path.unlink()  # Удаляем html
    return mp4_path