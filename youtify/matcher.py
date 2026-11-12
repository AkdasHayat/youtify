"""Match local video files to YouTube videos.

1. A YouTube ID in the filename wins (yt-dlp style "Title [dQw4w9WgXcQ].mp4", or any 11-char token
   that is a known video ID of that channel).
2. Otherwise fuzzy title match, with the file's duration used to confirm or reject a candidate.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher

ID_IN_BRACKETS = re.compile(r"[\[(]([A-Za-z0-9_-]{11})[\])]")
ID_TOKEN = re.compile(r"(?<![A-Za-z0-9_-])([A-Za-z0-9_-]{11})(?![A-Za-z0-9_-])")

# yt-dlp / macOS replace characters that are illegal in filenames with look-alikes.
_LOOKALIKES = str.maketrans({
    "／": "/", "＂": '"', "＊": "*", "：": ":", "＜": "<", "＞": ">", "？": "?", "｜": "|",
    "⧸": "/", "⧹": "\\", "＇": "'", "_": " ",
})

ACCEPT_SCORE = 0.86
MIN_MARGIN = 0.05


def normalize_title(s: str) -> str:
    s = ID_IN_BRACKETS.sub(" ", s)
    s = s.translate(_LOOKALIKES)
    s = unicodedata.normalize("NFKC", s).lower()
    # Drop punctuation/symbols/emoji only; keep letters AND combining marks (Hindi vowel signs etc.).
    s = "".join("" if unicodedata.category(c) == "Cf" else
                " " if unicodedata.category(c)[0] in "PSC" else c for c in s)
    return re.sub(r"\s+", " ", s).strip()


@dataclass
class Match:
    yt_id: str | None
    score: float
    best_guess: str | None
    reason: str


def match_file(stem: str, file_duration: float | None, catalog: list[dict]) -> Match:
    """catalog: dicts with id, title, duration."""
    ids = {c["id"]: c for c in catalog}
    m = ID_IN_BRACKETS.search(stem)
    if m and m.group(1) in ids:
        return Match(m.group(1), 1.0, m.group(1), "id-in-filename")
    for tok in ID_TOKEN.findall(stem):
        if tok in ids:
            return Match(tok, 1.0, tok, "id-in-filename")

    target = normalize_title(stem)
    if not target:
        return Match(None, 0.0, None, "empty-name")

    scored = []
    for c in catalog:
        cand = normalize_title(c["title"])
        if not cand:
            continue
        score = SequenceMatcher(None, target, cand).ratio()
        if target == cand:
            score = 1.0
        elif target in cand or cand in target:
            score = max(score, 0.9 * min(len(target), len(cand)) / max(len(target), len(cand)) + 0.1)
        if file_duration and c.get("duration"):
            diff = abs(file_duration - float(c["duration"]))
            if diff <= 3:
                score += 0.08
            elif diff > 15:
                score -= 0.3
        scored.append((min(score, 1.0), c["id"]))
    if not scored:
        return Match(None, 0.0, None, "empty-catalog")
    scored.sort(reverse=True)
    best_score, best_id = scored[0]
    second = scored[1][0] if len(scored) > 1 else 0.0
    if best_score >= ACCEPT_SCORE and best_score - second >= MIN_MARGIN:
        return Match(best_id, best_score, best_id, "title")
    reason = "ambiguous" if best_score >= ACCEPT_SCORE else "low-score"
    return Match(None, best_score, best_id, reason)
