"""Add or change a channel in config.yaml from a few plain inputs (links, times like "7am").

Edits the YAML as text so the comments in the config stay as they are.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from .config import slugify

# Settings every new channel gets unless told otherwise (same as Gay Timeline).
CHANNEL_DEFAULTS: dict = {
    "video_dirs": [],
    "download_missing": True,
    "download_max_height": 1080,
    "language": "en",
    "description_source": "youtube",
    "append_chapters": False,
    "footer": "",
    "keywords_in_description": False,
    "include_shorts": False,
    "min_duration_seconds": 60,
    "episode_type": "full",
}
FIELD_ORDER = ["youtube_url", "spotify_show_id", *CHANNEL_DEFAULTS, "upload_from_video",
               "upload_after_video", "season_number", "next_episode_number", "upload_times",
               "spotify_login", "enabled"]

_VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")


def parse_youtube_channel(s: str) -> str:
    s = s.strip()
    if s.startswith("@"):
        return f"https://www.youtube.com/{s}"
    m = re.search(r"youtube\.com/((?:@|channel/|c/|user/)[^/?#\s]+)", s)
    if not m:
        raise ValueError(f"Not a YouTube channel link: {s!r} (expected e.g. https://www.youtube.com/@Name)")
    return f"https://www.youtube.com/{m.group(1)}"


def parse_spotify_show(s: str) -> str:
    s = s.strip()
    m = re.search(r"show/([A-Za-z0-9]{10,})", s)
    if m:
        return m.group(1)
    if re.fullmatch(r"[A-Za-z0-9]{10,}", s):
        return s
    raise ValueError(f"Not a Spotify show link: {s!r} (expected e.g. https://creators.spotify.com/home/show/<id>)")


def parse_video_id(s: str) -> str:
    s = s.strip()
    for pat in (r"youtu\.be/([A-Za-z0-9_-]{11})", r"[?&]v=([A-Za-z0-9_-]{11})",
                r"/(?:shorts|live|embed)/([A-Za-z0-9_-]{11})"):
        m = re.search(pat, s)
        if m:
            return m.group(1)
    if _VIDEO_ID.match(s):
        return s
    raise ValueError(f"Not a YouTube video link: {s!r}")


def parse_times(s: str) -> list[str]:
    """'7am, 7:05pm' / '07:00 19:05' / '7am and 7:05 pm' -> ['07:00', '19:05']."""
    out = []
    for part in re.split(r"\s*(?:,|;|\band\b|&)\s*|\s+(?=\d)", s.strip().lower()):
        part = part.replace(" ", "").replace(".", "")
        if not part:
            continue
        m = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?(am|pm)?(ist)?", part)
        if not m:
            raise ValueError(f"Not a time: {part!r} (use e.g. 7am, 7:05pm or 19:05)")
        h, mi, ampm = int(m.group(1)), int(m.group(2) or 0), m.group(3)
        if ampm:
            if not 1 <= h <= 12:
                raise ValueError(f"Not a time: {part!r}")
            h = h % 12 + (12 if ampm == "pm" else 0)
        if not (0 <= h < 24 and 0 <= mi < 60):
            raise ValueError(f"Not a time: {part!r}")
        out.append(f"{h:02d}:{mi:02d}")
    if not out:
        raise ValueError("No upload time given")
    return sorted(set(out))


def _yaml_value(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if v is None:
        return "null"
    if isinstance(v, int):
        return str(v)
    return json.dumps(v)  # strings and lists: JSON is valid YAML


def _block_range(lines: list[str], key: str) -> tuple[int, int] | None:
    """(start, end) line indexes of the channel whose name slugifies to `key`."""
    start = None
    for i, line in enumerate(lines):
        m = re.match(r"^  - name:\s*(.+?)\s*(#.*)?$", line)
        if start is not None and (m or (line.strip() and not line.startswith(" "))):
            return start, i
        if m and slugify(m.group(1).strip("'\"")) == key:
            start = i
    return (start, len(lines)) if start is not None else None


def upsert_channel(config_path: Path, name: str, fields: dict) -> bool:
    """Add channel `name` (defaults + fields) or update just `fields` on it. True = added."""
    text = config_path.read_text()
    lines = text.splitlines()
    rng = _block_range(lines, slugify(name))
    if rng is None:
        data = {**CHANNEL_DEFAULTS, **fields}
        block = [f"  - name: {name}"]
        block += [f"    {k}: {_yaml_value(data[k])}" for k in FIELD_ORDER if k in data]
        block += [f"    {k}: {_yaml_value(v)}" for k, v in data.items() if k not in FIELD_ORDER]
        if not re.search(r"^channels:", text, re.M):
            lines.append("channels:")
        while lines and not lines[-1].strip():
            lines.pop()
        lines += [""] + block
        config_path.write_text("\n".join(lines) + "\n")
        return True
    start, end = rng
    while end > start + 1 and not lines[end - 1].strip():
        end -= 1
    for k, v in fields.items():
        new = f"    {k}: {_yaml_value(v)}"
        for i in range(start + 1, end):
            if re.match(rf"^    {re.escape(k)}:", lines[i]):
                lines[i] = new
                break
        else:
            lines.insert(end, new)
            end += 1
    config_path.write_text("\n".join(lines) + "\n")
    return False
