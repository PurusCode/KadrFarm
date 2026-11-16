"""FFmpeg renderer/assembler for exact beat-WAV visual timelines."""
from __future__ import annotations
import json, subprocess
from pathlib import Path
from typing import Any
from .common import write_json


def _run(command: list[str]) -> None:
    result=subprocess.run(command,capture_output=True,text=True,encoding="utf-8",errors="replace")
    if result.returncode:
        raise RuntimeError(f"FFmpeg failed ({result.returncode}):\n{result.stderr[-5000:]}")

def _probe(ffprobe: str, path: Path) -> dict[str,Any]:
    r=subprocess.run([ffprobe,"-v","error","-show_entries",
        "format=duration,size:stream=codec_type,codec_name,width,height,sample_rate,channels",
        "-of","json",str(path)],capture_output=True,text=True,encoding="utf-8",errors="replace",check=True)
    return json.loads(r.stdout)

def _quote(path: Path) -> str:
    return str(path.resolve()).replace("\\","/").replace("'","'\\''")

def _render_segment(settings: Any, beat: dict[str,Any], duration: float, output: Path) -> None:
    source=Path(beat["visual_path"])
    if not source.is_file(): raise FileNotFoundError(f"Visual not found: {source}")
    width,height,fps=int(settings.width),int(settings.height),int(settings.fps)
    is_video=beat.get("media_type")=="video"
    overlay=beat.get("overlay_path")
    
    inputs = []
    maps = []
    
    steps = beat.get("steps", [])
    if not is_video and steps and len(steps) > 1 and not overlay:
        # МАГИЯ АНИМАЦИИ: Собираем видео из N-шагов с перекрестным затуханием
        n = len(steps)
        fade_dur = 0.4 # Длительность перехода между шагами
        t_step = (duration - (n - 1) * fade_dur) / n
        
        if t_step <= 0:
            t_step = duration / n
            fade_dur = 0.0
            
        fc_parts = []
        for i, step in enumerate(steps):
            # Высчитываем точную длительность для каждого слоя
            if fade_dur > 0:
                if i == 0 or i == n - 1:
                    l = t_step + fade_dur
                else:
                    l = t_step + 2 * fade_dur
            else:
                l = t_step
                
            inputs.extend(["-loop", "1", "-framerate", str(fps), "-t", f"{l:.4f}", "-i", str(step)])
            fc_parts.append(f"[{i}:v]scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height},setsar=1,format=yuv420p[v{i}]")
        
        # Пошаговое наложение слоев через xfade
        if n > 1 and fade_dur > 0:
            prev = "v0"
            for i in range(1, n):
                offset = i * t_step + (i - 1) * fade_dur
                out = "vout" if i == n - 1 else f"x{i}"
                fc_parts.append(f"[{prev}][v{i}]xfade=transition=fade:duration={fade_dur:.4f}:offset={offset:.4f}[{out}]")
                prev = out
        else:
            fc_parts.append(f"[v0]null[vout]")
            
        # Добавляем плавный зум ко всему результату
        motion=beat.get("motion","static")
        if motion in ("slow_zoom","slow_pan","zoom_out"):
            if motion == "slow_pan":
                zoom_expr="1.15"; x_expr="(iw-iw/zoom)*min(on/900,1)"
            elif motion == "zoom_out":
                zoom_expr="max(1.0,1.15-on*0.0005)"; x_expr="iw/2-(iw/zoom/2)"
            else:
                zoom_expr="min(zoom+0.0005,1.15)"; x_expr="iw/2-(iw/zoom/2)"
            fc_parts.append(f"[vout]zoompan=z='{zoom_expr}':x='{x_expr}':y='ih/2-(ih/zoom/2)':d=1:s={width}x{height}:fps={fps}[vfinal]")
        else:
            fc_parts.append(f"[vout]fps={fps}[vfinal]")
            
        maps = ["-filter_complex", ";".join(fc_parts), "-map", "[vfinal]"]
        
    else:
        # Если это стоковое видео или с оверлеем, используем старую логику
        if is_video:
            inputs=["-stream_loop","-1","-i",str(source)]
        else:
            inputs=["-loop","1","-framerate",str(fps),"-i",str(source)]
            
        if overlay and Path(overlay).is_file():
            inputs += ["-loop","1","-framerate",str(fps),"-i",str(overlay)]
            fc=(f"[0:v]scale={width}:{height}:force_original_aspect_ratio=increase,"
                f"crop={width}:{height},fps={fps},setsar=1[base];"
                f"[1:v]scale={width}:{height},format=rgba[ov];"
                f"[base][ov]overlay=0:0:shortest=1,format=yuv420p[v]")
            maps=["-filter_complex",fc,"-map","[v]"]
        else:
            base=(f"scale={width}:{height}:force_original_aspect_ratio=increase,"
                  f"crop={width}:{height},setsar=1")
            motion=beat.get("motion","static")
            if not is_video and motion in ("slow_zoom","slow_pan","zoom_out"):
                if motion == "slow_pan":
                    zoom_expr="1.15"; x_expr="(iw-iw/zoom)*min(on/900,1)"
                elif motion == "zoom_out":
                    zoom_expr="max(1.0,1.15-on*0.0005)"; x_expr="iw/2-(iw/zoom/2)"
                else:
                    zoom_expr="min(zoom+0.0005,1.15)"; x_expr="iw/2-(iw/zoom/2)"
                vf=(base+f",zoompan=z='{zoom_expr}':x='{x_expr}':y='ih/2-(ih/zoom/2)':d=1:s={width}x{height}:fps={fps},")
            else:
                vf=base+f",fps={fps},"
            vf += "format=yuv420p"
            if beat.get("transition_in")=="fade":
                vf += ",fade=t=in:st=0:d=0.16"
            maps=["-vf",vf]

    output.parent.mkdir(parents=True,exist_ok=True)
    cmd=[settings.ffmpeg,"-y","-hide_banner","-loglevel","error",*inputs,*maps,
        "-an","-t",f"{duration:.4f}","-r",str(fps),"-c:v","libx264",
        "-preset","veryfast","-crf","19","-profile:v","high","-pix_fmt","yuv420p",
        "-movflags","+faststart",str(output)]
    _run(cmd)

def assemble_director_video(settings: Any, beats: list[dict[str,Any]], audio_path: Path,
                            output_path: Path, work_dir: Path, pause_seconds: float=0.12) -> dict[str,Any]:
    if not beats: raise ValueError("Нечего монтировать: beat timeline пуст")
    work_dir.mkdir(parents=True,exist_ok=True)
    video_segments=[]; expected=0.0; timeline=[]
    for idx,beat in enumerate(beats,1):
        voice_duration=float(beat["audio_duration_sec"])
        gap=pause_seconds if idx<len(beats) else 0.0
        duration=voice_duration+gap
        if voice_duration<=0: raise ValueError(f"{beat['scene_id']}/{beat['beat_id']}: invalid WAV duration")
        seg=work_dir/f"beat_{idx:04d}.mp4"
        # Keep spare picture frames after the final narration sample. The final
        # -shortest mux trims this pad to the exact master-audio endpoint, avoiding
        # cumulative CFR frame-rounding from making the video end before the WAV.
        render_duration=duration+(3.0 if idx==len(beats) else 0.0)
        _render_segment(settings,beat,render_duration,seg)
        video_segments.append(seg)
        timeline.append({"scene_id":beat["scene_id"],"beat_id":beat["beat_id"],
            "start_sec":round(expected,4),"voice_end_sec":round(expected+voice_duration,4),
            "end_sec":round(expected+duration,4),"audio_path":beat["audio_path"],
            "visual_path":beat["visual_path"],"visual_source":beat.get("source"),
            "spoken_text":beat["spoken_text"]})
        expected += duration
    concat=work_dir/"video_segments.ffconcat"
    concat.write_text("ffconcat version 1.0\n"+"".join(f"file '{_quote(p)}'\n" for p in video_segments),encoding="utf-8")
    silent=work_dir/"picture_only.mp4"
    _run([settings.ffmpeg,"-y","-hide_banner","-loglevel","error","-f","concat","-safe","0",
          "-i",str(concat),"-an","-c","copy","-movflags","+faststart",str(silent)])
    output_path.parent.mkdir(parents=True,exist_ok=True)
    _run([settings.ffmpeg,"-y","-hide_banner","-loglevel","error","-i",str(silent),"-i",str(audio_path),
          "-map","0:v:0","-map","1:a:0","-c:v","copy","-c:a","aac","-b:a","192k",
          "-ar","44100","-ac","2","-shortest","-movflags","+faststart",str(output_path)])
    probe=_probe(settings.ffprobe,output_path)
    actual=float(probe.get("format",{}).get("duration") or 0)
    if actual<=0: raise RuntimeError("ffprobe не определил длительность финального MP4")
    tolerance=max(0.25,len(beats)/max(1,int(settings.fps))+0.15)
    if abs(actual-expected)>tolerance:
        raise RuntimeError(f"Final duration mismatch: video={actual:.3f}s timeline={expected:.3f}s tolerance={tolerance:.3f}s")
    report={"status":"passed","video":str(output_path.resolve()),"duration_sec":round(actual,4),
        "expected_duration_sec":round(expected,4),"beat_count":len(beats),"timeline":timeline,"ffprobe":probe}
    write_json(output_path.with_suffix(".timeline.json"),report)
    return report
