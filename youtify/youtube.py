"""YouTube access via yt-dlp: channel catalog, per-video metadata, captions, thumbnail.

No API key is needed. Videos come from your Mac, except for channels with `download_missing: true`:
those are downloaded one at a time (max 1080p by default) just before upload and deleted right after.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

log = logging.getLogger(__name__)

MEMBERS_ONLY_HINTS = ("members-only", "members only", "channel's members", "join this channel",
                      "available to this channel", "subscriber_only")


class MembersOnlyError(Exception):
    """The video is for channel members only, so it can't be downloaded. Skip it."""


def is_members_only(text: str) -> bool:
    t = (text or "").lower()
    return any(h in t for h in MEMBERS_ONLY_HINTS)


def _extract(ydl, url: str, yt_id: str, download: bool):
    import yt_dlp

    try:
        return ydl.extract_info(url, download=download)
    except yt_dlp.utils.DownloadError as e:
        if is_members_only(str(e)):
            raise MembersOnlyError(f"{yt_id} is a members-only video") from e
        raise


def _ydl_opts(cookies_from_browser: str | None, **extra) -> dict:
    opts = {
        "quiet": True,
        "no_warnings": True,
        "ignoreerrors": True,
        "skip_download": True,
        "sleep_interval_requests": 1,
    }
    if cookies_from_browser:
        opts["cookiesfrombrowser"] = (cookies_from_browser,)
    opts.update(extra)
    return opts


def fetch_catalog(channel_url: str, include_streams: bool = True, include_shorts: bool = False,
                  cookies_from_browser: str | None = None) -> list[dict]:
    """List a channel's public videos. Position 0 is the newest video."""
    import yt_dlp

    tabs = ["videos"] + (["streams"] if include_streams else []) + (["shorts"] if include_shorts else [])
    entries: dict[str, dict] = {}
    for tab in tabs:
        url = f"{channel_url}/{tab}"
        with yt_dlp.YoutubeDL(_ydl_opts(cookies_from_browser, extract_flat="in_playlist")) as ydl:
            info = ydl.extract_info(url, download=False)
        if not info:
            log.info("No %s tab for %s", tab, channel_url)
            continue
        for i, e in enumerate(info.get("entries") or []):
            if not e or not e.get("id") or e["id"] in entries:
                continue
            entries[e["id"]] = {
                "id": e["id"],
                "title": e.get("title") or "",
                "duration": e.get("duration"),
                "kind": tab,
                "position": i if tab == "videos" else 100000 + i,
                "members_only": e.get("availability") == "subscriber_only",
            }
    if not entries:
        raise RuntimeError(f"Could not list any videos for {channel_url}. Check the URL and your internet connection.")
    return list(entries.values())


def fetch_details(yt_id: str, out_dir: Path, subtitle_langs: list[str],
                  cookies_from_browser: str | None = None, refresh: bool = False,
                  want_subtitles: bool = True) -> dict:
    """Fetch metadata, best caption track and thumbnail for one video (cached on disk).

    Returns {"info": dict, "subtitle_path": Path|None, "subtitle_kind": "manual"|"auto"|None,
             "thumbnail_path": Path|None}
    """
    import yt_dlp

    out_dir.mkdir(parents=True, exist_ok=True)
    info_path = out_dir / "info.json"
    if not refresh and want_subtitles and info_path.exists():
        # Cached without captions (YouTube-description mode) but captions are needed now.
        refresh = not json.loads(info_path.read_text()).get("subtitles_requested", True)
    if refresh or not info_path.exists():
        opts = _ydl_opts(
            cookies_from_browser,
            ignoreerrors=False,
            writesubtitles=want_subtitles,
            writeautomaticsub=want_subtitles,
            subtitleslangs=subtitle_langs,
            subtitlesformat="vtt/best",
            writethumbnail=True,
            outtmpl=str(out_dir / "%(id)s.%(ext)s"),
            postprocessors=[{"key": "FFmpegThumbnailsConvertor", "format": "jpg"}],
        )
        url = f"https://www.youtube.com/watch?v={yt_id}"
        with yt_dlp.YoutubeDL(opts) as ydl:
            try:
                info = _extract(ydl, url, yt_id, download=True)
            except yt_dlp.utils.DownloadError as e:
                if "subtitle" not in str(e).lower():
                    raise
                # Caption download got rate limited: keep going without captions.
                log.warning("Caption download failed for %s (%s); retrying without captions", yt_id, e)
                opts.update(writesubtitles=False, writeautomaticsub=False)
                with yt_dlp.YoutubeDL(opts) as ydl2:
                    info = ydl2.extract_info(url, download=True)
            info = ydl.sanitize_info(info)
        keep = {k: info.get(k) for k in (
            "id", "title", "description", "tags", "categories", "chapters", "duration",
            "upload_date", "webpage_url", "channel", "language", "subtitles", "requested_subtitles",
        )}
        keep["subtitles"] = sorted((info.get("subtitles") or {}).keys())
        keep["requested_subtitles"] = sorted((info.get("requested_subtitles") or {}).keys())
        keep["subtitles_requested"] = want_subtitles
        info_path.write_text(json.dumps(keep, ensure_ascii=False, indent=2))

    info = json.loads(info_path.read_text())
    sub_path, sub_kind = pick_subtitle(out_dir, yt_id, subtitle_langs, info.get("subtitles") or [])
    thumb = ensure_jpg_thumbnail(out_dir, yt_id)
    return {
        "info": info,
        "subtitle_path": sub_path,
        "subtitle_kind": sub_kind,
        "thumbnail_path": thumb if thumb.exists() else None,
    }


def ensure_jpg_thumbnail(out_dir: Path, yt_id: str) -> Path | None:
    """YouTube often serves .webp thumbnails; Spotify wants JPG/PNG. Convert with ffmpeg if needed."""
    import subprocess

    jpg = out_dir / f"{yt_id}.jpg"
    if jpg.exists() and jpg.stat().st_size > 0:
        return jpg
    for ext in ("webp", "png", "jpeg", "image"):
        src = out_dir / f"{yt_id}.{ext}"
        if src.exists():
            r = subprocess.run(["ffmpeg", "-nostdin", "-y", "-loglevel", "error", "-i", str(src),
                                "-q:v", "2", str(jpg)], capture_output=True, text=True)
            if r.returncode == 0 and jpg.exists():
                return jpg
            log.warning("Could not convert thumbnail %s: %s", src.name, r.stderr.strip()[:200])
    return None


def pick_subtitle(out_dir: Path, yt_id: str, langs: list[str], manual_langs: list[str]):
    """Prefer human captions, then original-language auto captions, then anything else."""
    import re

    files = {p.name[len(yt_id) + 1:-4]: p for p in out_dir.glob(f"{yt_id}.*.vtt")}
    if not files:
        return None, None

    def rank(lang: str) -> tuple:
        manual = lang in manual_langs
        pref = len(langs)
        for i, pat in enumerate(langs):
            if re.fullmatch(pat, lang):
                pref = i
                break
        matched = pref < len(langs)
        return (0 if matched else 1, 0 if manual else 1, 0 if lang.endswith("-orig") else 1, pref, lang)

    best = min(files, key=rank)
    return files[best], ("manual" if best in manual_langs else "auto")


def download_video(yt_id: str, dest_dir: Path, max_height: int = 1080,
                   cookies_from_browser: str | None = None) -> Path:
    """Download one video (at most `max_height`p) as MP4 into dest_dir. Returns the file path.

    H.264 + AAC is preferred so Spotify accepts the file without re-encoding.
    """
    import yt_dlp

    dest_dir.mkdir(parents=True, exist_ok=True)
    h = int(max_height)
    fmt = (f"bv*[height<={h}][vcodec^=avc1]+ba[ext=m4a]/"
           f"bv*[height<={h}][vcodec^=avc1]+ba/"
           f"bv*[height<={h}]+ba/b[height<={h}]/b")
    opts = _ydl_opts(
        cookies_from_browser,
        ignoreerrors=False,
        skip_download=False,
        format=fmt,
        merge_output_format="mp4",
        outtmpl=str(dest_dir / f"{yt_id}.%(ext)s"),
        noplaylist=True,
        retries=5,
        fragment_retries=5,
    )
    with yt_dlp.YoutubeDL(opts) as ydl:
        _extract(ydl, f"https://www.youtube.com/watch?v={yt_id}", yt_id, download=True)
    files = [p for p in dest_dir.glob(f"{yt_id}.*")
             if p.suffix.lower() in (".mp4", ".mkv", ".webm", ".mov", ".m4v") and p.is_file()]
    if not files:
        raise RuntimeError(f"Download of {yt_id} produced no video file")
    return max(files, key=lambda p: p.stat().st_size)


def delete_download(yt_id: str, dest_dir: Path) -> None:
    """Remove a downloaded video and any leftovers (.part, separate audio/video streams)."""
    for p in dest_dir.glob(f"{yt_id}.*"):
        try:
            p.unlink()
        except OSError:
            pass
