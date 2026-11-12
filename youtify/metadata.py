"""Episode metadata: description, keywords, chapters.

Default (description_source: youtube): the YouTube description is copied exactly; see
build_youtube_metadata at the bottom. No AI involved.

AI mode (description_source: ai) strategy (works with a small 3B model in 8 GB RAM):
  1. Split the transcript into 3-12 time sections (map step). For each, the LLM writes a short
     chapter title + 1-2 sentence summary. Each call stays far below the model's context size.
  2. One final call turns the section summaries into a Spotify description + search keywords
     (reduce step).
  3. Chapters: the creator's own YouTube chapters if present, else the generated section titles.
     Spotify builds chapters from timestamps in the description ("00:00 Intro").
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .transcript import Segment, format_ts

log = logging.getLogger(__name__)

SPOTIFY_DESCRIPTION_LIMIT = 4000
MIN_CHAPTER_GAP = 10  # seconds
MIN_CHAPTERS = 3

LANGUAGE_NAMES = {
    "en": "English", "hi": "Hindi", "bn": "Bengali", "ta": "Tamil", "te": "Telugu", "mr": "Marathi",
    "gu": "Gujarati", "kn": "Kannada", "ml": "Malayalam", "pa": "Punjabi", "ur": "Urdu",
    "es": "Spanish", "pt": "Portuguese", "fr": "French", "de": "German", "it": "Italian",
    "nl": "Dutch", "ru": "Russian", "ar": "Arabic", "tr": "Turkish", "id": "Indonesian",
    "ja": "Japanese", "ko": "Korean", "zh": "Chinese", "vi": "Vietnamese", "th": "Thai",
}

SECTION_SCHEMA = {
    "type": "object",
    "properties": {
        "chapter_title": {"type": "string"},
        "summary": {"type": "string"},
    },
    "required": ["chapter_title", "summary"],
}

FINAL_SCHEMA = {
    "type": "object",
    "properties": {
        "description": {"type": "string"},
        "keywords": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["description", "keywords"],
}

SYSTEM = ("You are an expert podcast producer and SEO copywriter. You only state facts that are "
          "present in the material you are given. You always answer with the requested JSON.")

URL_RE = re.compile(r"https?://\S+|www\.\S+")


@dataclass
class Window:
    start: float
    idx: int = 0
    segments: list[Segment] = field(default_factory=list)

    @property
    def text(self) -> str:
        return " ".join(s.text for s in self.segments)


def make_windows(segments: list[Segment], duration: float) -> list[Window]:
    if not segments:
        return []
    duration = max(duration, segments[-1].end)
    n = 1 if duration < 240 else max(3, min(12, int(duration // 300)))
    width = duration / n
    windows: list[Window] = []
    for seg in segments:
        idx = min(n - 1, int(seg.start // width))
        if not windows or windows[-1].idx != idx:
            windows.append(Window(start=seg.start, idx=idx))
        windows[-1].segments.append(seg)
    windows[0].start = 0.0
    return windows


def sample_text(text: str, max_chars: int) -> str:
    """Keep beginning, middle and end of an over-long section."""
    if len(text) <= max_chars:
        return text
    head, part = int(max_chars * 0.5), int(max_chars * 0.25)
    mid = len(text) // 2
    return " … ".join([text[:head], text[mid - part // 2: mid + part // 2], text[-part:]])


def clean_chapter_title(t: str) -> str:
    t = re.sub(r"^\s*(\d{1,2}:)?\d{1,2}:\d{2}\s*[-–—:]?\s*", "", t or "")
    t = re.sub(r"^\s*(chapter|section|part)\s*\d+\s*[:.\-–]\s*", "", t, flags=re.I)
    t = t.strip().strip("\"'“”‘’*#").strip().rstrip(".:;,")
    t = re.sub(r"\s+", " ", t)
    return t[:60].rstrip()


def normalize_chapters(chapters: list[tuple[float, str]], duration: float | None) -> list[tuple[int, str]]:
    """Sorted, starting at 0, >=10s apart, inside the video, non-empty titles; [] if fewer than 3."""
    out: list[tuple[int, str]] = []
    for start, title in sorted(chapters, key=lambda c: c[0]):
        title = clean_chapter_title(title)
        start = int(start)
        if not title or start < 0 or (duration and start >= duration - 5):
            continue
        if not out:
            start = 0
        elif start - out[-1][0] < MIN_CHAPTER_GAP:
            continue
        out.append((start, title))
    return out if len(out) >= MIN_CHAPTERS else []


def format_chapters(chapters: list[tuple[int, str]], duration: float | None) -> str:
    hours = bool(duration and duration >= 3600) or any(s >= 3600 for s, _ in chapters)
    return "\n".join(f"{format_ts(s, force_hours=hours)} {t}" for s, t in chapters)


def clean_summary(text: str) -> str:
    text = URL_RE.sub("", text or "")
    text = re.sub(r"(?m)^\s*#\w+(\s+#\w+)*\s*$", "", text)       # hashtag-only lines
    text = re.sub(r"(?m)^\s*(\d{1,2}:)?\d{1,2}:\d{2}.*$", "", text)  # stray timestamps
    text = text.strip().strip('"').strip()
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def merge_keywords(primary: list, secondary: list | None, limit: int = 15) -> list[str]:
    seen, out = set(), []
    for kw in list(primary or []) + list(secondary or []):
        if not isinstance(kw, str):
            continue
        kw = re.sub(r"\s+", " ", kw.strip().lstrip("#").strip(" ,.;\"'"))
        if not kw or len(kw) > 60 or kw.lower() in seen:
            continue
        seen.add(kw.lower())
        out.append(kw)
        if len(out) >= limit:
            break
    return out


def assemble_description(summary: str, chapters: list[tuple[int, str]], keywords: list[str],
                         duration: float | None, footer: str, youtube_url: str,
                         keywords_in_description: bool = True,
                         limit: int = SPOTIFY_DESCRIPTION_LIMIT) -> str:
    footer = (footer or "").replace("{youtube_url}", youtube_url).strip()
    chapter_list = list(chapters)
    use_keywords = keywords_in_description and bool(keywords)

    def build() -> str:
        parts = [summary.strip()]
        if chapter_list:
            parts.append("Chapters:\n" + format_chapters(chapter_list, duration))
        if use_keywords:
            parts.append("Topics: " + ", ".join(keywords))
        if footer:
            parts.append(footer)
        return "\n\n".join(p for p in parts if p)

    text = build()
    if len(text) > limit:
        use_keywords = False
        text = build()
    while len(text) > limit and len(chapter_list) > MIN_CHAPTERS:
        chapter_list = chapter_list[::2]  # keep every other chapter (still starts at 00:00)
        text = build()
    if len(text) > limit:
        overflow = len(text) - limit + 1
        summary = summary[: max(0, len(summary) - overflow)].rstrip() + "…"
        text = build()
    return text


def _lang(code: str) -> str:
    return LANGUAGE_NAMES.get(code.split("-")[0], code)


def section_prompt(title: str, start: float, text: str, language: str) -> str:
    return f"""Label one section of a video transcript so it can become a podcast chapter.

Video title: {title}
Section starts at: {format_ts(start)}
Transcript of this section:
\"\"\"
{text}
\"\"\"

Return JSON with:
- "chapter_title": 2 to 6 words naming the main topic of this section. Specific, not generic (avoid "Introduction" unless it really is the intro). No timestamps, no quotes, no emojis.
- "summary": 1 to 2 sentences on what is discussed or shown in this section.
Write both in {_lang(language)}."""


def final_prompt(title: str, yt_description: str, tags: list[str], section_summaries: list[str],
                 style_notes: str, language: str) -> str:
    yt_desc = clean_summary(yt_description)[:2500] or "(none)"
    sections = "\n".join(f"- {s}" for s in section_summaries) or "(no transcript available)"
    style = style_notes.strip() or "Friendly, clear and professional."
    return f"""Write the Spotify episode description for this video podcast episode.

Episode title: {title}
Original YouTube description (context only; do not copy links, promos or sponsor text):
\"\"\"
{yt_desc}
\"\"\"
YouTube tags: {", ".join(tags[:30]) or "(none)"}
What the episode covers, in order:
{sections}

Show style notes: {style}

Rules for "description":
- 2 or 3 short paragraphs, 90 to 200 words in total, written in {_lang(language)}.
- The first sentence must hook the listener and naturally include the episode's main topic keyword.
- Mention the most valuable things the listener will learn or hear.
- Only use facts from the material above. No timestamps, no URLs, no hashtags, no emojis.
- Do not start with "In this episode" every time; vary the opening.

Rules for "keywords": 8 to 15 search phrases (1-4 words each) that listeners would type into Spotify or Google to find this episode. Most specific first."""


def build_metadata(*, llm, channel, info: dict, segments: list[Segment], transcript_source: str,
                   thumbnail: str | None, section_max_chars: int = 8000) -> dict:
    title = info["title"]
    duration = float(info.get("duration") or (segments[-1].end if segments else 0))
    youtube_url = info.get("webpage_url") or f"https://www.youtube.com/watch?v={info['id']}"

    windows = make_windows(segments, duration)
    sections: list[dict] = []
    for i, w in enumerate(windows, 1):
        log.info("  section %d/%d (%s)", i, len(windows), format_ts(w.start))
        res = llm.generate_json(
            section_prompt(title, w.start, sample_text(w.text, section_max_chars), channel.language),
            SECTION_SCHEMA, system=SYSTEM,
        )
        sections.append({"start": w.start, "title": res.get("chapter_title", ""),
                         "summary": res.get("summary", "").strip()})

    yt_chapters = [(c.get("start_time", 0), c.get("title", "")) for c in (info.get("chapters") or [])]
    chapters = normalize_chapters(yt_chapters, duration)
    chapters_source = "youtube" if chapters else "none"
    if not chapters and len(sections) >= MIN_CHAPTERS:
        chapters = normalize_chapters([(s["start"], s["title"]) for s in sections], duration)
        chapters_source = "generated" if chapters else "none"

    final = llm.generate_json(
        final_prompt(title, info.get("description") or "", info.get("tags") or [],
                     [s["summary"] for s in sections if s["summary"]], channel.style_notes,
                     channel.language),
        FINAL_SCHEMA, system=SYSTEM,
    )
    summary = clean_summary(final.get("description", ""))
    if len(summary) < 40:
        raise ValueError(f"LLM description too short: {summary!r}")
    keywords = merge_keywords(final.get("keywords") or [], info.get("tags"))

    description = assemble_description(
        summary, chapters, keywords, duration, channel.footer, youtube_url,
        keywords_in_description=channel.keywords_in_description,
    )
    return {
        "yt_id": info["id"],
        "channel": channel.key,
        "spotify_show_id": channel.spotify_show_id,
        "title": title,
        "youtube_url": youtube_url,
        "upload_date": info.get("upload_date"),
        "duration": duration,
        "summary": summary,
        "keywords": keywords,
        "chapters": [{"start": s, "time": format_ts(s), "title": t} for s, t in chapters],
        "chapters_source": chapters_source,
        "description_source": "ai",
        "transcript_source": transcript_source,
        "sections": sections,
        "thumbnail": thumbnail,
        "description": description,
        "model": getattr(llm, "model", "?"),
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def review_markdown(meta: dict, json_path: str) -> str:
    chapters = "\n".join(f"- {c['time']} {c['title']}" for c in meta["chapters"]) or "_none_"
    return f"""# {meta['title']}

- YouTube: {meta['youtube_url']}  (uploaded {meta.get('upload_date') or '?'})
- Spotify show id: `{meta['spotify_show_id']}`
- Description: {meta.get('description_source', 'ai')} · Chapters: {meta['chapters_source']} · Transcript: {meta['transcript_source']} · Model: {meta['model']}
- **To change what gets uploaded, edit the `description` field in:** `{json_path}`

## Description (exactly as it will be uploaded)

```
{meta['description']}
```

## Keywords

{", ".join(meta['keywords']) or "_none_"}

## Chapters

{chapters}
"""


# ---------------------------------------------------------------- YouTube description, no AI
TIMESTAMP_LINE = re.compile(r"(?m)^\s*[\[(]?(\d{1,2}:)?\d{1,2}:\d{2}[\])]?\s*[-–—:|]?\s*\S.*$")


def has_timestamps(text: str) -> bool:
    lines = TIMESTAMP_LINE.findall(text or "")
    return len(lines) >= MIN_CHAPTERS


def _truncate_paragraphs(text: str, room: int) -> str:
    """Cut at a paragraph (or line) boundary so the text fits in `room` characters."""
    if len(text) <= room:
        return text
    if room <= 1:
        return ""
    cut = text[: room - 1]
    for sep in ("\n\n", "\n", ". ", " "):
        i = cut.rfind(sep)
        if i >= room * 0.5:
            return cut[: i + (1 if sep == ". " else 0)].rstrip() + "…"
    return cut.rstrip() + "…"


_URL = re.compile(r"https?://\S+|www\.\S+")
_YT_STYLE = re.compile(r"(?<![\w*_])([*_])(?=\S)(.+?)(?<=\S)\1(?![\w*_])")


def strip_youtube_formatting(text: str) -> str:
    """Remove YouTube's bold/italic markers: *bold* -> bold, _italic_ -> italic.

    Spotify shows them as literal characters. Links are left untouched (they often contain _).
    """
    out, pos = [], 0
    for m in _URL.finditer(text or ""):
        out.append(_YT_STYLE.sub(r"\2", text[pos:m.start()]))
        out.append(m.group(0))
        pos = m.end()
    out.append(_YT_STYLE.sub(r"\2", (text or "")[pos:]))
    return "".join(out)


_YT_LINK = re.compile(r"(https?://)?(www\.|m\.)?(youtube\.com|youtu\.be)/\S*", re.I)
_HASHTAG = re.compile(r"(?<!\S)#\w*[^\W\d_]\w*")
_YT_ONLY_LINE = re.compile(r"member(ship)?s?\b|\bsubscribe\b", re.I)


def strip_youtube_extras(text: str) -> str:
    """Drop things that only make sense on YouTube: lines with YouTube links or about channel
    memberships/subscribing, and hashtags. Other links (sources etc.) stay."""
    lines = []
    for line in (text or "").split("\n"):
        if _YT_LINK.search(line) or _YT_ONLY_LINE.search(line):
            continue
        had_tags = bool(_HASHTAG.search(line))
        line = re.sub(r"[ \t]{2,}", " ", _HASHTAG.sub("", line)).rstrip()
        if had_tags and not line.strip():
            continue
        lines.append(line)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def youtube_description(yt_desc: str, chapters: list[tuple[int, str]], duration: float | None,
                        footer: str, youtube_url: str, keywords: list[str],
                        append_chapters: bool = False, keywords_in_description: bool = False,
                        limit: int = SPOTIFY_DESCRIPTION_LIMIT) -> tuple[str, bool]:
    """The YouTube description as-is, fitted to Spotify's limit.

    Returns (description, chapters_appended). Anything already in the YouTube description (links,
    timestamps, hashtags) is kept. Too long -> the end is trimmed at a paragraph break.
    Only with append_chapters=True (off by default; Spotify makes its own chapters): the video's
    chapters are added when the description has no timestamps, and a trim that would cut them off
    moves them into a compact list at the end instead.
    """
    body = (yt_desc or "").replace("\r\n", "\n").strip()
    footer = (footer or "").replace("{youtube_url}", youtube_url).strip()
    in_body = has_timestamps(body)
    add_chapters = bool(chapters) and append_chapters and not in_body

    def tail() -> str:
        parts = []
        if add_chapters:
            parts.append("Chapters:\n" + format_chapters(chapters, duration))
        if keywords_in_description and keywords:
            parts.append("Topics: " + ", ".join(keywords))
        if footer:
            parts.append(footer)
        return "\n\n".join(parts)

    def join(b: str) -> str:
        return "\n\n".join(p for p in (b, tail()) if p)

    if len(join(body)) <= limit:
        return join(body), add_chapters
    if in_body and chapters and append_chapters:
        # Keep chapters: pull the timestamp lines out of the text and list them compactly at the end.
        body = re.sub(r"\n{3,}", "\n\n", TIMESTAMP_LINE.sub("", body)).strip()
        add_chapters = True
    room = limit - len(tail()) - (2 if tail() else 0)
    return join(_truncate_paragraphs(body, room)), add_chapters


def build_youtube_metadata(*, channel, info: dict, thumbnail: str | None) -> dict:
    title = info["title"]
    duration = float(info.get("duration") or 0) or None
    youtube_url = info.get("webpage_url") or f"https://www.youtube.com/watch?v={info['id']}"
    yt_chapters = [(c.get("start_time", 0), c.get("title", "")) for c in (info.get("chapters") or [])]
    chapters = normalize_chapters(yt_chapters, duration)
    keywords = merge_keywords(info.get("tags") or [], None)
    yt_desc = strip_youtube_extras(strip_youtube_formatting((info.get("description") or "").strip()))
    source = "youtube"
    if not yt_desc:
        log.warning("  %s has an empty YouTube description; using the title instead", info["id"])
        yt_desc, source = title, "title_fallback"
    description, appended = youtube_description(
        yt_desc, chapters, duration, channel.footer, youtube_url, keywords,
        append_chapters=channel.append_chapters, keywords_in_description=channel.keywords_in_description,
    )
    return {
        "yt_id": info["id"],
        "channel": channel.key,
        "spotify_show_id": channel.spotify_show_id,
        "title": title,
        "youtube_url": youtube_url,
        "upload_date": info.get("upload_date"),
        "duration": duration,
        "summary": yt_desc,
        "keywords": keywords,
        "chapters": [{"start": s, "time": format_ts(s), "title": t} for s, t in chapters],
        "chapters_source": ("youtube (added to description)" if appended else
                            "left to Spotify (auto chapters)"),
        "description_source": source,
        "transcript_source": "not needed",
        "sections": [],
        "thumbnail": thumbnail,
        "description": description,
        "model": "none",
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
