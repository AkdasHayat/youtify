"""Transcripts: parse YouTube VTT captions, or transcribe locally with Whisper (MLX) as fallback."""
from __future__ import annotations

import html
import json
import logging
import re
import subprocess
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path

log = logging.getLogger(__name__)

_TS = re.compile(r"(?:(\d+):)?(\d{1,2}):(\d{2})[.,](\d{3})")
_TAG = re.compile(r"<[^>]+>")


@dataclass
class Segment:
    start: float
    end: float
    text: str


def _ts_to_sec(s: str) -> float:
    m = _TS.search(s)
    if not m:
        raise ValueError(f"bad timestamp {s!r}")
    h, mnt, sec, ms = m.groups()
    return int(h or 0) * 3600 + int(mnt) * 60 + int(sec) + int(ms) / 1000


def parse_vtt(text: str) -> list[Segment]:
    """Parse WebVTT, collapsing YouTube's 'rolling' auto-caption duplicates."""
    segments: list[Segment] = []
    recent: list[str] = []
    blocks = re.split(r"\n\s*\n", text.replace("\r\n", "\n"))
    for block in blocks:
        lines = block.strip().split("\n")
        idx = next((i for i, ln in enumerate(lines) if "-->" in ln), None)
        if idx is None:
            continue
        start_s, _, end_s = lines[idx].partition("-->")
        try:
            start, end = _ts_to_sec(start_s), _ts_to_sec(end_s)
        except ValueError:
            continue
        for raw in lines[idx + 1:]:
            line = html.unescape(_TAG.sub("", raw)).strip()
            line = re.sub(r"\s+", " ", line)
            if not line or line in recent:
                continue
            segments.append(Segment(start, end, line))
            recent = (recent + [line])[-3:]
    return segments


def format_ts(seconds: float, force_hours: bool = False) -> str:
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    if h or force_hours:
        return f"{h}:{m:02d}:{sec:02d}"
    return f"{m:02d}:{sec:02d}"


def save_segments(path: Path, segments: list[Segment], source: str) -> None:
    path.write_text(json.dumps({"source": source, "segments": [asdict(s) for s in segments]},
                               ensure_ascii=False))


def load_segments(path: Path) -> tuple[list[Segment], str]:
    data = json.loads(path.read_text())
    return [Segment(**s) for s in data["segments"]], data["source"]


def transcribe_whisper(video: Path, model: str, language: str | None) -> list[Segment]:
    """Local transcription with mlx-whisper (Apple Silicon). Audio is extracted to a temp WAV first."""
    try:
        import mlx_whisper  # type: ignore
    except ImportError as e:
        raise RuntimeError(
            "No YouTube captions and mlx-whisper is not installed. "
            "Install with: pip install 'youtify[whisper]' (or set transcription.whisper_fallback: false)"
        ) from e
    with tempfile.TemporaryDirectory() as td:
        wav = Path(td) / "audio.wav"
        subprocess.run(
            ["ffmpeg", "-nostdin", "-y", "-loglevel", "error", "-i", str(video),
             "-vn", "-ac", "1", "-ar", "16000", str(wav)],
            check=True,
        )
        log.info("Transcribing %s with %s (this can take a few minutes)", video.name, model)
        result = mlx_whisper.transcribe(str(wav), path_or_hf_repo=model, language=language)
    return [Segment(float(s["start"]), float(s["end"]), s["text"].strip())
            for s in result.get("segments", []) if s.get("text", "").strip()]
