"""Make sure a video file is in a format Spotify accepts (MP4/MOV, H.264/HEVC, AAC).

Uses Apple's hardware encoder (h264_videotoolbox) on the M2, which is fast and needs little RAM.
Files that are already fine are uploaded untouched.
"""
from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

log = logging.getLogger(__name__)

OK_CONTAINERS = {".mp4", ".mov", ".m4v"}
OK_VIDEO = {"h264", "hevc"}
OK_AUDIO = {"aac"}
SPOTIFY_RECOMMENDED_MAX_BYTES = 10 * 1024**3


@dataclass
class Probe:
    duration: float | None
    vcodec: str | None
    acodec: str | None
    height: int | None


def probe(path: Path) -> Probe:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", "-show_format", str(path)],
        capture_output=True, text=True, check=True,
    ).stdout
    data = json.loads(out)
    v = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"
              and not (s.get("disposition") or {}).get("attached_pic")), None)
    a = next((s for s in data.get("streams", []) if s.get("codec_type") == "audio"), None)
    dur = (data.get("format") or {}).get("duration")
    return Probe(
        duration=float(dur) if dur else None,
        vcodec=v.get("codec_name") if v else None,
        acodec=a.get("codec_name") if a else None,
        height=int(v["height"]) if v and v.get("height") else None,
    )


def plan(path: Path, p: Probe) -> str:
    """'as_is' | 'fix_audio' | 'remux' | 'transcode'"""
    if p.vcodec is None:
        raise ValueError(f"{path.name} has no video stream")
    video_ok = p.vcodec in OK_VIDEO
    audio_ok = p.acodec is None or p.acodec in OK_AUDIO
    container_ok = path.suffix.lower() in OK_CONTAINERS
    if video_ok and audio_ok:
        return "as_is" if container_ok else "remux"
    if video_ok:
        return "fix_audio"
    return "transcode"


@lru_cache(maxsize=1)
def _has_videotoolbox() -> bool:
    try:
        out = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True).stdout
        return "h264_videotoolbox" in out
    except FileNotFoundError:
        return False


def ffmpeg_args(src: Path, dst: Path, action: str, height: int | None) -> list[str]:
    base = ["ffmpeg", "-nostdin", "-y", "-loglevel", "error", "-i", str(src),
            "-map", "0:v:0", "-map", "0:a:0?"]
    audio = ["-c:a", "aac", "-b:a", "192k", "-ar", "48000"]
    if action == "remux":
        return base + ["-c", "copy", "-movflags", "+faststart", str(dst)]
    if action == "fix_audio":
        return base + ["-c:v", "copy", *audio, "-movflags", "+faststart", str(dst)]
    h = height or 1080
    bitrate = "12M" if h > 1080 else "8M" if h > 720 else "5M"
    if _has_videotoolbox():
        video = ["-c:v", "h264_videotoolbox", "-b:v", bitrate, "-profile:v", "high"]
    else:
        video = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20"]
    return base + [*video, "-pix_fmt", "yuv420p", *audio, "-movflags", "+faststart", str(dst)]


def prepare_for_upload(src: Path, work_dir: Path) -> tuple[Path, bool]:
    """Returns (path_to_upload, is_temporary_file)."""
    p = probe(src)
    action = plan(src, p)
    if action == "as_is":
        _warn_size(src)
        return src, False
    work_dir.mkdir(parents=True, exist_ok=True)
    dst = work_dir / (src.stem[:120] + ".spotify.mp4")
    part = dst.with_suffix(".part.mp4")
    log.info("Converting %s for Spotify (%s: video=%s audio=%s)", src.name, action, p.vcodec, p.acodec)
    subprocess.run(ffmpeg_args(src, part, action, p.height), check=True)
    part.rename(dst)
    _warn_size(dst)
    return dst, True


def _warn_size(path: Path) -> None:
    size = path.stat().st_size
    if size > SPOTIFY_RECOMMENDED_MAX_BYTES:
        log.warning("%s is %.1f GB; Spotify recommends <= 10 GB (hard limit 60 GB)", path.name, size / 1024**3)
