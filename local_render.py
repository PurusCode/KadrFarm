import json
from pathlib import Path
from kadr_farm.config import load_settings
from kadr_farm.production_director import _produce
from kadr_farm.director_assembler import assemble_director_video
from kadr_farm.director_audio import build_master_audio

settings = load_settings()
out_dir = Path("output/director_test_v4") # Укажите папку, где лежит старый план

# Загружаем уже готовые планы и сцены
plans = json.loads((out_dir / "director_plan.json").read_text(encoding="utf-8"))["plans"]
scenes = json.loads((out_dir / "pilot_storyboard.json").read_text(encoding="utf-8"))["scenes"]

# 1. Перегенерируем визуалы (локально)
print("Generating animated visuals...")
beats, production = _produce(settings, scenes, plans, out_dir / "production", 0.9)

# 2. Пересобираем аудио (локально)
print("Building audio...")
audio_report = build_master_audio(beats, out_dir / "production" / "audio_master.wav")

# 3. Склеиваем видео с переходами
print("Assembling video with crossfades...")
assembly = assemble_director_video(settings, beats, Path(audio_report["path"]), out_dir / "final_animated.mp4", out_dir / "production" / "assembly", pause_seconds=0.12)

print("Done! Watch final_animated.mp4")