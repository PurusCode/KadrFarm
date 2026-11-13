import shutil
import wave
import json
import pytest
from pathlib import Path
from types import SimpleNamespace
from kadr_farm.assemble_v2 import assemble_v2
from kadr_farm.qc_scene_sync import run_scene_qc
from kadr_farm.diagram_renderer import render_scene_visual

@pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="FFmpeg is required")
def test_one_scene_audio_visual_assembles_and_passes_qc(tmp_path):
    wav=tmp_path/"scene.wav"
    with wave.open(str(wav),"wb") as stream:
        stream.setnchannels(1); stream.setsampwidth(2); stream.setframerate(24000)
        stream.writeframes(b"\x00\x00"*(24000*2))
    scene={"id":"s_001","block_id":"b1","voice_text":"Пробная речь.",
        "meaning":"Проверка сцены","purpose":"Проверить таймлайн",
        "visual_type":"diagram","visual_description":"Тестовая визуальная схема",
        "asset_strategy":"rendered","provider_id":"internal_renderer",
        "gen_prompt":None,"on_screen_text":["Тест"],"duration_sec":2.0,
        "start_sec":0.0,"end_sec":2.0,"transition_in":"cut","transition_out":"cut",
        "camera_move":"static"}
    png=render_scene_visual(scene,tmp_path/"visual.png")
    scene["media_refs"]={"audio":{"path":str(wav)},"visuals":[
        {"path":str(png),"media_type":"image","duration_ratio":1.0}]}
    storyboard=tmp_path/"storyboard.json"
    storyboard.write_text(json.dumps({"scenes":[scene]},ensure_ascii=False),encoding="utf-8")
    settings=SimpleNamespace(ffmpeg=shutil.which("ffmpeg"),ffprobe=shutil.which("ffprobe"),
                             width=640,height=360,fps=25)
    video=tmp_path/"result.mp4"
    assemble_v2(settings,storyboard,video)
    report=run_scene_qc(settings.ffprobe,storyboard,video,tmp_path/"qc.json")
    assert video.is_file()
    assert report["status"] in ("passed","passed_with_warnings")
    assert not report["errors"]
