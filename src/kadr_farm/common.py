from __future__ import annotations
import json, re, hashlib, os, tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]

def ensure_dir(path: str | Path) -> Path:
    p = Path(path)
    p.mkdir(parents=True, exist_ok=True)
    return p

def write_json(path: str | Path, payload: Any) -> None:
    """Write JSON atomically so interrupted runs cannot leave truncated checkpoints."""
    p = Path(path)
    ensure_dir(p.parent)
    encoded = json.dumps(payload, ensure_ascii=False, indent=2)
    temp_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", newline="\n",
            dir=p.parent, prefix=f".{p.name}.", suffix=".tmp", delete=False
        ) as stream:
            temp_name = stream.name
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, p)
        temp_name = None
    finally:
        if temp_name is not None:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass

def read_json(path: str | Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))

def words(text: str) -> list[str]:
    return re.findall(r"[A-Za-zА-Яа-яЁё0-9]+(?:[.-][A-Za-zА-Яа-яЁё0-9]+)*", text or "")

def word_count(text: str) -> int:
    return len(words(text))

def slugify(text: str, max_length: int = 60) -> str:
    s = re.sub(r"[^0-9A-Za-zА-Яа-яЁё]+", "-", (text or "").lower()).strip("-")
    return (s[:max_length].strip("-") or "video")

def stable_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]

def parse_json_response(text: str) -> Any:
    """Parse valid JSON even if a provider wraps it in a Markdown code fence."""
    raw = (text or "").strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.I)
    raw = re.sub(r"\s*```$", "", raw)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        start_obj, end_obj = raw.find("{"), raw.rfind("}")
        start_arr, end_arr = raw.find("["), raw.rfind("]")
        if start_obj >= 0 and end_obj > start_obj:
            return json.loads(raw[start_obj:end_obj + 1])
        if start_arr >= 0 and end_arr > start_arr:
            return json.loads(raw[start_arr:end_arr + 1])
        raise
