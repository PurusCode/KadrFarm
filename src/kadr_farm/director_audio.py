"""Narration concatenation and original procedural sound design (no third-party music)."""
from __future__ import annotations
import math, wave, hashlib
from pathlib import Path
from typing import Any
import numpy as np

SAMPLE_RATE=24_000

def _read_wav(path: Path):
    with wave.open(str(path),"rb") as wf:
        ch=wf.getnchannels(); sr=wf.getframerate(); sw=wf.getsampwidth(); frames=wf.getnframes()
        raw=wf.readframes(frames)
    if sw!=2 or ch!=1 or sr!=SAMPLE_RATE:
        raise ValueError(f"{path}: expected mono PCM16 {SAMPLE_RATE} Hz, got ch={ch}, sr={sr}, width={sw}")
    return np.frombuffer(raw,dtype="<i2").astype(np.float32)/32768.0

def _write_wav(path: Path, samples: np.ndarray):
    path.parent.mkdir(parents=True,exist_ok=True)
    values=np.clip(samples,-0.98,0.98)
    pcm=(values*32767.0).astype("<i2")
    with wave.open(str(path),"wb") as wf:
        wf.setnchannels(1); wf.setsampwidth(2); wf.setframerate(SAMPLE_RATE)
        wf.writeframes(pcm.tobytes())

def _effect(name: str, duration: float=0.45) -> np.ndarray:
    n=max(1,int(SAMPLE_RATE*duration)); t=np.arange(n,dtype=np.float32)/SAMPLE_RATE
    key=name.lower()
    env=np.exp(-t*5.5)
    if any(x in key for x in ("ding","success","chime","ping")):
        sig=np.sin(2*np.pi*880*t)+0.55*np.sin(2*np.pi*1320*t)*np.exp(-t*2)
        env=np.exp(-t*4)
    elif any(x in key for x in ("error","buzzer","wrong")):
        sig=np.sin(2*np.pi*145*t)+0.35*np.sin(2*np.pi*190*t)
        env=np.minimum(1,t*45)*np.exp(-t*2)
    elif any(x in key for x in ("whoosh","shatter","sweep")):
        seed=int.from_bytes(hashlib.sha256(name.encode("utf-8")).digest()[:4],"big")
        rng=np.random.default_rng(seed)
        noise=rng.normal(0,1,n).astype(np.float32)
        smooth=np.convolve(noise,np.ones(35,dtype=np.float32)/35,mode="same")
        sig=noise*0.35+smooth*1.8
        env=np.sin(np.clip(t/duration,0,1)*math.pi)**1.5
    elif any(x in key for x in ("typing","key","click","tap","pop")):
        sig=np.sin(2*np.pi*(620+180*np.sin(t*35))*t)
        env=np.exp(-t*30)
    else:
        sig=np.sin(2*np.pi*520*t)+0.25*np.sin(2*np.pi*780*t)
    return (sig*env*0.20).astype(np.float32)

def _music_pad(duration: float, cue: str) -> np.ndarray:
    # Durations often originate from an exact integer sample count divided by
    # SAMPLE_RATE. Floating-point multiplication can produce N-ε, so int()
    # would silently drop the final sample (e.g. 215278.99999999997).
    n=max(0,int(round(SAMPLE_RATE*duration)))
    if not n: return np.zeros(0,dtype=np.float32)
    t=np.arange(n,dtype=np.float32)/SAMPLE_RATE
    text=(cue or "").lower()
    if any(x in text for x in ("ominous","tense","mystery","тревог","напряж")):
        freqs=(82.4,123.5,164.8); volume=0.025
    elif any(x in text for x in ("upbeat","lo-fi","light","весел","лёгк")):
        freqs=(146.8,220.0,293.7); volume=0.018
    else:
        freqs=(110.0,164.8,220.0); volume=0.018
    slow=0.78+0.22*np.sin(2*np.pi*0.12*t)
    pad=sum(np.sin(2*np.pi*f*t + i*0.4)/(i+1) for i,f in enumerate(freqs))
    fade=np.minimum(1.0,np.minimum(t/0.35,(duration-t)/0.35)).clip(0,1)
    return (pad*slow*fade*volume).astype(np.float32)

def build_master_audio(beats: list[dict[str,Any]], output_path: Path,
                       *, pause_seconds: float=0.12, add_procedural_bed: bool=True) -> dict[str,Any]:
    """Concatenate exact beat WAVs; beat boundaries are where visuals change."""
    if not beats: raise ValueError("Нет аудиобитов")
    narration_parts=[]; timeline=[]; cursor=0.0
    for index,beat in enumerate(beats):
        samples=_read_wav(Path(beat["audio_path"]))
        duration=len(samples)/SAMPLE_RATE
        gap=pause_seconds if index<len(beats)-1 else 0.0
        narration_parts.append(samples)
        if gap: narration_parts.append(np.zeros(int(SAMPLE_RATE*gap),dtype=np.float32))
        timeline.append({"scene_id":beat["scene_id"],"beat_id":beat["beat_id"],
            "start_sec":round(cursor,4),"voice_end_sec":round(cursor+duration,4),
            "end_sec":round(cursor+duration+gap,4),"voice_duration_sec":round(duration,4),
            "gap_after_sec":gap})
        cursor += duration+gap
    narration=np.concatenate(narration_parts).astype(np.float32)
    mixed=narration.copy()
    offsets={(x["scene_id"],x["beat_id"]):x["start_sec"] for x in timeline}
    if add_procedural_bed:
        for beat,slot in zip(beats,timeline):
            # Timeline values are rounded for JSON readability; convert with
            # round(), then clamp to the actual narration buffer. The last
            # rounded endpoint can otherwise land one sample past the buffer.
            start=max(0,min(len(mixed),int(round(slot["start_sec"]*SAMPLE_RATE))))
            end=max(start,min(len(mixed),int(round(slot["end_sec"]*SAMPLE_RATE))))
            target_length=end-start
            if target_length <= 0:
                continue
            bed=_music_pad(target_length/SAMPLE_RATE,beat.get("music_cue", ""))
            # Keep the bed exactly aligned to the destination slice even if a
            # future duration conversion introduces a one-sample discrepancy.
            if len(bed) < target_length:
                bed=np.pad(bed,(0,target_length-len(bed)))
            elif len(bed) > target_length:
                bed=bed[:target_length]
            mixed[start:end] += bed
    effect_count=0
    for beat,slot in zip(beats,timeline):
        for sfx in beat.get("audio_sfx",[])[:2]:
            fx=_effect(str(sfx.get("name", "accent")))
            start=min(len(mixed),int((slot["start_sec"]+0.08)*SAMPLE_RATE))
            end=min(len(mixed),start+len(fx))
            if end>start:
                mixed[start:end] += fx[:end-start]
                effect_count += 1
    _write_wav(output_path,mixed)
    return {"path":str(output_path.resolve()),"sample_rate":SAMPLE_RATE,
            "duration_seconds":round(len(mixed)/SAMPLE_RATE,4),"beat_count":len(beats),
            "silence_between_beats_sec":pause_seconds,"procedural_bed":add_procedural_bed,
            "effect_count":effect_count,"timeline":timeline,
            "note":"Procedural ambient bed/SFX are original synthesized accents, not a licensed commercial soundtrack."}
