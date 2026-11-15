import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from youtify import db as states
from youtify.config import load_config
from youtify.db import DB
from youtify.matcher import match_file, normalize_title
from youtify.media import plan, probe, prepare_for_upload
from youtify.metadata import (assemble_description, build_metadata, clean_chapter_title,
                               make_windows, merge_keywords, normalize_chapters)
from youtify.transcript import Segment, format_ts, parse_vtt
from youtify.youtube import pick_subtitle

CATALOG = [
    {"id": "dQw4w9WgXcQ", "title": "How I Grew My Garden: 5 Tips | Beginner Guide", "duration": 600},
    {"id": "abc_DEF-123", "title": "Composting 101 (Full Tutorial)", "duration": 1200},
    {"id": "zzzzzzzzzzz", "title": "Composting 102 (Full Tutorial)", "duration": 1250},
    {"id": "hindi000001", "title": "खेती के 5 आसान तरीके", "duration": 300},
]


# ---------------- matcher
def test_match_by_bracketed_id():
    m = match_file("Anything at all [abc_DEF-123]", None, CATALOG)
    assert m.yt_id == "abc_DEF-123" and m.reason == "id-in-filename"


def test_match_by_title_with_ytdlp_lookalike_chars():
    m = match_file("How I Grew My Garden： 5 Tips ｜ Beginner Guide", 601, CATALOG)
    assert m.yt_id == "dQw4w9WgXcQ"


def test_near_duplicate_titles_resolved_by_duration():
    assert match_file("Composting 101 Full Tutorial", 1201, CATALOG).yt_id == "abc_DEF-123"
    assert match_file("Composting 102 Full Tutorial", 1249, CATALOG).yt_id == "zzzzzzzzzzz"


def test_ambiguous_without_duration_is_not_matched():
    m = match_file("Composting 10 Full Tutorial", None, CATALOG)
    assert m.yt_id is None and m.best_guess in {"abc_DEF-123", "zzzzzzzzzzz"}


def test_non_latin_titles():
    assert match_file("खेती के 5 आसान तरीके", 300, CATALOG).yt_id == "hindi000001"
    assert normalize_title("खेती के 5 आसान तरीके!") == "खेती के 5 आसान तरीके"


def test_unrelated_file_not_matched():
    assert match_file("IMG_4412 final export", 30, CATALOG).yt_id is None


# ---------------- transcript
VTT = """WEBVTT
Kind: captions

00:00:00.000 --> 00:00:02.000 align:start position:0%
hello<00:00:00.500><c> everyone</c>

00:00:02.000 --> 00:00:02.010
hello everyone

00:00:02.010 --> 00:00:05.000
hello everyone
welcome &amp; enjoy

01:02:03.500 --> 01:02:05.000
last line
"""


def test_parse_vtt_dedupes_rolling_captions():
    segs = parse_vtt(VTT)
    assert [s.text for s in segs] == ["hello everyone", "welcome & enjoy", "last line"]
    assert segs[-1].start == pytest.approx(3723.5)


def test_format_ts():
    assert format_ts(65) == "01:05"
    assert format_ts(3725) == "1:02:05"
    assert format_ts(5, force_hours=True) == "0:00:05"


def test_pick_subtitle_prefers_manual_then_orig(tmp_path):
    for lang in ("en", "en-orig", "de"):
        (tmp_path / f"vid.{lang}.vtt").write_text("WEBVTT")
    p, kind = pick_subtitle(tmp_path, "vid", ["en", "en-orig", "en-.*"], manual_langs=[])
    assert p.name == "vid.en-orig.vtt" and kind == "auto"
    p, kind = pick_subtitle(tmp_path, "vid", ["en", "en-orig", "en-.*"], manual_langs=["en"])
    assert p.name == "vid.en.vtt" and kind == "manual"


# ---------------- chapters / description
def test_normalize_chapters_rules():
    raw = [(5, "Intro"), (8, "Too close"), (120, '"Soil prep."'), (300, "03:00 - Watering"), (599, "end")]
    assert normalize_chapters(raw, 600) == [(0, "Intro"), (120, "Soil prep"), (300, "Watering")]
    assert normalize_chapters([(0, "a"), (100, "b")], 600) == []  # fewer than 3


def test_clean_chapter_title():
    assert clean_chapter_title("Chapter 2: Planting Seeds.") == "Planting Seeds"


def test_merge_keywords():
    assert merge_keywords(["#Garden", "garden", " soil "], ["Soil", "compost"]) == ["Garden", "soil", "compost"]


def test_assemble_description_fits_limit():
    chapters = [(i * 60, f"Chapter number {i}") for i in range(80)]
    text = assemble_description("Summary. " * 300, chapters, ["kw"] * 3, 5000, "Watch: {youtube_url}",
                                "https://youtu.be/x")
    assert len(text) <= 4000
    assert text.rstrip().endswith("Watch: https://youtu.be/x")
    assert "\n0:00:00 Chapter number 0" in text


def test_make_windows():
    segs = [Segment(t, t + 5, f"s{t}") for t in range(0, 1800, 10)]
    w = make_windows(segs, 1800)
    assert len(w) == 6 and w[0].start == 0 and w[1].start == 300
    assert len(make_windows([Segment(0, 5, "x"), Segment(100, 105, "y")], 120)) == 1


class FakeLLM:
    model = "fake"

    def __init__(self):
        self.calls = 0

    def generate_json(self, prompt, schema, system=None):
        self.calls += 1
        if "chapter_title" in schema["properties"]:
            return {"chapter_title": f"Topic {self.calls}", "summary": f"Section {self.calls} summary."}
        return {"description": "Grow tomatoes faster with these proven tips. Visit https://spam.com now!\n\n#garden",
                "keywords": ["tomato growing", "garden tips"]}


class FakeLazy:
    def __init__(self):
        self.llm = FakeLLM()

    def get(self):
        return self.llm

    def unload(self):
        pass


class NoLLM:
    def get(self):
        raise AssertionError("AI must not be used in youtube mode")

    def unload(self):
        pass


def _channel(**kw):
    base = dict(key="g", language="en", style_notes="", footer="Watch: {youtube_url}",
                keywords_in_description=True, spotify_show_id="SHOW", append_chapters=True)
    base.update(kw)
    return SimpleNamespace(**base)


def test_build_metadata_generates_chapters_and_cleans_output():
    segs = [Segment(t, t + 5, f"talk {t}") for t in range(0, 1500, 15)]
    info = {"id": "dQw4w9WgXcQ", "title": "Tomatoes", "duration": 1500, "tags": ["Tomatoes"],
            "description": "Links: https://x.y", "upload_date": "20240101"}
    meta = build_metadata(llm=FakeLLM(), channel=_channel(), info=info, segments=segs,
                          transcript_source="youtube_auto", thumbnail=None)
    assert meta["chapters_source"] == "generated" and len(meta["chapters"]) == 5
    assert "https://spam.com" not in meta["summary"] and "#garden" not in meta["summary"]
    assert meta["description"].startswith("Grow tomatoes faster")
    assert "Chapters:\n00:00 Topic 1\n05:00 Topic 2" in meta["description"]
    assert "Topics: tomato growing, garden tips, Tomatoes" in meta["description"]
    assert meta["description"].endswith("Watch: https://www.youtube.com/watch?v=dQw4w9WgXcQ")


def test_build_metadata_prefers_youtube_chapters_and_handles_no_transcript():
    info = {"id": "x" * 11, "title": "T", "duration": 900, "webpage_url": "https://youtu.be/x",
            "chapters": [{"start_time": 0, "title": "Start"}, {"start_time": 200, "title": "Middle"},
                         {"start_time": 600, "title": "End"}]}
    llm = FakeLLM()
    meta = build_metadata(llm=llm, channel=_channel(), info=info, segments=[], transcript_source="none",
                          thumbnail=None)
    assert llm.calls == 1
    assert meta["chapters_source"] == "youtube"
    assert [c["title"] for c in meta["chapters"]] == ["Start", "Middle", "End"]


# ---------------- media (needs ffmpeg)
needs_ffmpeg = pytest.mark.skipif(subprocess.run(["which", "ffmpeg"], capture_output=True).returncode != 0,
                                  reason="ffmpeg missing")


def _make_video(path: Path, vcodec: str, acodec: str):
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=320x180:rate=25",
                    "-f", "lavfi", "-i", "sine=frequency=440", "-t", "2", "-c:v", vcodec, "-c:a", acodec,
                    str(path)], check=True)


@needs_ffmpeg
def test_media_plans_and_conversion(tmp_path):
    good = tmp_path / "good.mp4"
    _make_video(good, "libx264", "aac")
    assert plan(good, probe(good)) == "as_is"
    assert prepare_for_upload(good, tmp_path / "w") == (good, False)

    webm = tmp_path / "v.webm"
    _make_video(webm, "libvpx-vp9", "libopus")
    assert plan(webm, probe(webm)) == "transcode"
    out, tmp = prepare_for_upload(webm, tmp_path / "w")
    p = probe(out)
    assert tmp and out.suffix == ".mp4" and p.vcodec == "h264" and p.acodec == "aac"


# ---------------- config + db + pipeline scan
def test_config_and_scan(tmp_path, monkeypatch):
    vids = tmp_path / "vids"
    vids.mkdir()
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(f"""
data_dir: {tmp_path / 'data'}
channels:
  - name: Garden Life
    youtube_url: https://www.youtube.com/@garden/videos
    spotify_show_id: SHOW1
    video_dirs: [{vids}]
""")
    cfg = load_config(cfg_path)
    cfg.ensure_dirs()
    ch = cfg.channels[0]
    assert ch.key == "garden-life" and ch.youtube_url == "https://www.youtube.com/@garden"
    assert ch.subtitle_langs == ["en", "en-orig", "en-.*"]

    db = DB(cfg.db_path)
    db.replace_catalog("garden-life", [dict(c, position=i) for i, c in enumerate(CATALOG)])
    for name in ("Composting 101 (Full Tutorial) [abc_DEF-123].mp4", "random clip.mov", ".hidden.mp4"):
        (vids / name).write_bytes(b"")

    import youtify.media as media
    import youtify.pipeline as pipeline
    monkeypatch.setattr(media, "probe", lambda p: media.Probe(1200.0, "h264", "aac", 1080))
    monkeypatch.setattr(pipeline.time, "time", lambda: 10**10)  # files are "old enough"

    stats = pipeline.scan(cfg, db)
    assert stats == {"new": 1, "unmatched": 1, "download": 0}
    ep = db.episode("abc_DEF-123")
    assert ep["status"] == states.PENDING and ep["position"] == 1
    # second scan: nothing new, unmatched file not re-processed
    assert pipeline.scan(cfg, db) == {"new": 0, "unmatched": 0, "download": 0}


def test_unknown_config_key_rejected(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("channels:\n  - name: a\n    youtube_url: u\n    spotify_show_id: s\n    vidoe_dirs: []\n")
    with pytest.raises(ValueError, match="vidoe_dirs"):
        load_config(p)


def test_failure_accounting(tmp_path):
    db = DB(tmp_path / "s.db")
    db.add_episode("a" * 11, "c", "t", "/f", 0, states.READY)
    assert db.record_failure("a" * 11, "boom", max_attempts=2) == 1
    assert db.episode("a" * 11)["status"] == states.READY
    db.record_failure("a" * 11, "boom", max_attempts=2)
    assert db.episode("a" * 11)["status"] == states.FAILED


def test_cli_parses_all_commands():
    from youtify.cli import COMMANDS, build_parser

    p = build_parser()
    for cmd in COMMANDS:
        extra = {"show": ["x"], "map": ["f", "x"], "regenerate": ["x"], "skip": ["x"], "mark-uploaded": ["x"]}
        assert p.parse_args([cmd, *extra.get(cmd, [])]).cmd == cmd


def test_prepare_episode_end_to_end(tmp_path, monkeypatch):
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(f"""
data_dir: {tmp_path / 'data'}
run: {{require_metadata_approval: true}}
channels:
  - name: G
    youtube_url: https://www.youtube.com/@g
    spotify_show_id: SHOW1
    description_source: ai
    footer: "Watch: {{youtube_url}}"
""")
    cfg = load_config(cfg_path)
    cfg.ensure_dirs()
    db = DB(cfg.db_path)
    db.add_episode("abc_DEF-123", "g", "old title", str(tmp_path / "v.mp4"), 3)
    vtt = tmp_path / "abc_DEF-123.en.vtt"
    vtt.write_text("WEBVTT\n\n" + "\n\n".join(
        f"00:{m:02d}:00.000 --> 00:{m:02d}:05.000\nline {m}" for m in range(0, 30)))

    import youtify.pipeline as pipeline
    import youtify.youtube as yt
    monkeypatch.setattr(yt, "fetch_details", lambda *a, **k: {
        "info": {"id": "abc_DEF-123", "title": "Composting 101 (Full Tutorial)", "duration": 1805,
                 "upload_date": "20230105", "tags": ["compost"], "description": ""},
        "subtitle_path": vtt, "subtitle_kind": "manual", "thumbnail_path": None})

    pipeline.prepare_episode(cfg, db, FakeLazy(), db.episode("abc_DEF-123"))
    ep = db.episode("abc_DEF-123")
    assert ep["status"] == states.AWAITING_APPROVAL
    assert ep["title"] == "Composting 101 (Full Tutorial)" and ep["upload_date"] == "20230105"
    meta = json.loads(Path(ep["metadata_path"]).read_text())
    assert meta["transcript_source"] == "youtube_manual" and len(meta["chapters"]) == 6
    assert Path(ep["metadata_path"]).with_suffix(".md").exists()
    assert (cfg.cache_dir / "abc_DEF-123" / "transcript.json").exists()


def test_run_all_continues_after_upload_failure(tmp_path, monkeypatch):
    import youtify.pipeline as pipeline

    cfg_path = tmp_path / "c.yaml"
    cfg_path.write_text(f"data_dir: {tmp_path}\nrun: {{notify: false}}\nchannels:\n"
                        "  - {name: a, youtube_url: u, spotify_show_id: s}\n")
    cfg = load_config(cfg_path)
    cfg.ensure_dirs()
    db = DB(cfg.db_path)
    calls = []
    monkeypatch.setattr(pipeline, "sync_catalogs", lambda *a, **k: calls.append("sync"))
    monkeypatch.setattr(pipeline, "scan", lambda *a, **k: calls.append("scan") or {"new": 0, "unmatched": 0})
    monkeypatch.setattr(pipeline, "prepare_batch", lambda c, d, n, *a, **k: calls.append(("prep", n)) or 1)

    def boom(*a, **k):
        calls.append("upload")
        raise RuntimeError("Not logged in")
    monkeypatch.setattr(pipeline, "upload_batch", boom)

    s = pipeline.run_all(cfg, db, minutes=60)
    assert calls == ["sync", "scan", ("prep", 5), "upload", ("prep", 29)]  # 1 channel
    assert s == {"metadata": 2, "uploaded": 0, "error": "Not logged in"}


# ---------------- YouTube-description mode (default, no AI)
from youtify.metadata import build_youtube_metadata, youtube_description  # noqa: E402

YT_CHAPTERS = [{"start_time": 0, "title": "Intro"}, {"start_time": 90, "title": "Setup"},
               {"start_time": 400, "title": "Results"}]


def test_youtube_description_copied_exactly():
    desc = "Line one with a link https://example.com\n\n#garden #tips\n\n0:00 Intro\n1:30 Setup\n6:40 Results"
    info = {"id": "x" * 11, "title": "T", "duration": 900, "description": desc, "chapters": YT_CHAPTERS,
            "tags": ["garden"]}
    meta = build_youtube_metadata(channel=_channel(footer="", keywords_in_description=False),
                                  info=info, thumbnail=None)
    # links and timestamps kept; hashtags removed (they don't work on Spotify)
    assert meta["description"] == desc.replace("\n\n#garden #tips", "")
    assert meta["chapters_source"] == "left to Spotify (auto chapters)"
    assert meta["keywords"] == ["garden"] and meta["model"] == "none"


def test_youtube_description_gets_chapters_when_missing():
    info = {"id": "x" * 11, "title": "T", "duration": 900, "description": "Just text.",
            "chapters": YT_CHAPTERS}
    meta = build_youtube_metadata(channel=_channel(footer="", keywords_in_description=False),
                                  info=info, thumbnail=None)
    assert meta["description"] == "Just text.\n\nChapters:\n00:00 Intro\n01:30 Setup\n06:40 Results"
    off = build_youtube_metadata(channel=_channel(footer="", keywords_in_description=False,
                                                  append_chapters=False), info=info, thumbnail=None)
    assert off["description"] == "Just text."


def test_long_youtube_description_trimmed_but_keeps_chapters():
    chapters = [(0, "Intro"), (90, "Setup"), (400, "Results")]
    body = "\n\n".join(f"Paragraph {i} " + "words " * 40 for i in range(30))
    desc = "Opening line.\n0:00 Intro\n1:30 Setup\n6:40 Results\n\n" + body
    out, appended = youtube_description(desc, chapters, 900, "", "u", [], append_chapters=True)
    assert len(out) <= 4000 and appended
    assert out.startswith("Opening line.") and out.endswith("Chapters:\n00:00 Intro\n01:30 Setup\n06:40 Results")
    assert "…" in out
    short, appended = youtube_description("Short.", [], 900, "Watch: {youtube_url}", "https://y", [])
    assert short == "Short.\n\nWatch: https://y" and not appended


def test_empty_youtube_description_falls_back_to_title():
    meta = build_youtube_metadata(channel=_channel(footer="", keywords_in_description=False),
                                  info={"id": "x" * 11, "title": "My Title", "description": ""}, thumbnail=None)
    assert meta["description"] == "My Title" and meta["description_source"] == "title_fallback"


def _yt_cfg(tmp_path, mode):
    p = tmp_path / "c.yaml"
    p.write_text(f"data_dir: {tmp_path / 'd'}\nchannels:\n"
                 f"  - {{name: G, youtube_url: u, spotify_show_id: S, description_source: {mode}}}\n")
    cfg = load_config(p)
    cfg.ensure_dirs()
    return cfg


@pytest.mark.parametrize("mode,desc,uses_ai", [("youtube", "", False), ("youtube_or_ai", "Plenty of real text here, long enough to keep as is.", False),
                                              ("youtube_or_ai", "hi", True)])
def test_prepare_episode_modes(tmp_path, monkeypatch, mode, desc, uses_ai):
    import youtify.pipeline as pipeline
    import youtify.youtube as yt

    cfg = _yt_cfg(tmp_path, mode)
    db = DB(cfg.db_path)
    db.add_episode("abc_DEF-123", "g", "t", "/v.mp4", 0)
    seen = {}

    def fake_fetch(*a, want_subtitles=True, **k):
        seen["subs"] = want_subtitles
        return {"info": {"id": "abc_DEF-123", "title": "Title", "duration": 100, "description": desc},
                "subtitle_path": None, "subtitle_kind": None, "thumbnail_path": None}
    monkeypatch.setattr(yt, "fetch_details", fake_fetch)
    cfg.transcription.whisper_fallback = False
    pipeline.prepare_episode(cfg, db, FakeLazy() if uses_ai else NoLLM(), db.episode("abc_DEF-123"))
    meta = json.loads(Path(db.episode("abc_DEF-123")["metadata_path"]).read_text())
    assert seen["subs"] is (mode != "youtube")
    assert meta["description_source"] == ("ai" if uses_ai else "title_fallback" if not desc else "youtube")
    if mode == "youtube_or_ai" and not uses_ai:
        assert meta["description"] == desc


def test_bad_description_source_rejected(tmp_path):
    with pytest.raises(ValueError, match="description_source"):
        _yt_cfg(tmp_path, "magic")


def test_chapters_not_added_by_default():
    info = {"id": "x" * 11, "title": "T", "duration": 900, "description": "Just text.",
            "chapters": YT_CHAPTERS}
    from youtify.config import ChannelConfig
    ch = ChannelConfig(name="c", youtube_url="u", spotify_show_id="s")
    assert ch.append_chapters is False
    meta = build_youtube_metadata(channel=ch, info=info, thumbnail=None)
    assert meta["description"] == "Just text."
    long_desc = "Intro.\n0:00 A\n1:30 B\n6:40 C\n\n" + "\n\n".join("word " * 60 for _ in range(30))
    out, appended = youtube_description(long_desc, [(0, "A"), (90, "B"), (400, "C")], 900, "", "u", [])
    assert len(out) <= 4000 and not appended and "Chapters:" not in out
    assert out.startswith(long_desc[:30])


# ---------------- download_missing: queue, download, upload, delete
def test_download_missing_flow(tmp_path, monkeypatch):
    import json as _json
    import youtify.pipeline as pipeline
    import youtify.youtube as youtube
    import youtify.media as media
    import youtify.spotify as spotify

    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(f"""
data_dir: {tmp_path / 'data'}
spotify: {{pause_between_uploads_seconds: 0}}
channels:
  - name: Garden Life
    youtube_url: https://www.youtube.com/@garden
    spotify_show_id: SHOW1
    video_dirs: []
    download_missing: true
""")
    cfg = load_config(cfg_path)
    cfg.ensure_dirs()
    assert cfg.channels[0].download_max_height == 1080
    db = DB(cfg.db_path)
    cat = [dict(id="vidAAAAAAA1", title="Long one", duration=900),
           dict(id="vidAAAAAAA2", title="Tiny", duration=20),
           dict(id="vidAAAAAAA3", title="Already there", duration=600)]
    db.replace_catalog("garden-life", [dict(c, position=i) for i, c in enumerate(cat)])
    stats = pipeline.scan(cfg, db)
    assert stats["download"] == 3
    assert db.episode("vidAAAAAAA2")["status"] == states.SKIPPED
    db.update_episode("vidAAAAAAA3", status=states.UPLOADED)  # e.g. via skip-existing
    assert pipeline.scan(cfg, db)["download"] == 0  # idempotent

    meta = tmp_path / "m.json"
    meta.write_text(_json.dumps({"title": "Long one", "description": "d"}))
    db.update_episode("vidAAAAAAA1", status=states.READY, metadata_path=str(meta))

    calls = {}

    def fake_download(yt_id, dest_dir, max_height, cookies):
        calls["height"] = max_height
        dest_dir.mkdir(parents=True, exist_ok=True)
        f = dest_dir / f"{yt_id}.mp4"
        f.write_bytes(b"x")
        (dest_dir / f"{yt_id}.f137.mp4.part").write_bytes(b"x")
        return f

    class FakeUp:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *e): pass
        def is_logged_in(self, show): return True
        def upload(self, job, action):
            calls["uploaded_file_existed"] = job.video_path.exists()
            return "https://spotify/ep"

    monkeypatch.setattr(youtube, "download_video", fake_download)
    monkeypatch.setattr(spotify, "SpotifyUploader", FakeUp)
    monkeypatch.setattr(media, "prepare_for_upload", lambda src, wd: (src, False))
    n = pipeline.upload_batch(cfg, db, 5)
    assert n == 1 and calls == {"height": 1080, "uploaded_file_existed": True}
    assert db.episode("vidAAAAAAA1")["status"] == states.UPLOADED
    assert list(cfg.downloads_dir.glob("*")) == []  # downloaded copy deleted

    # a failed upload also deletes the download
    db.update_episode("vidAAAAAAA1", status=states.READY)
    FakeUp.upload = lambda self, job, action: (_ for _ in ()).throw(RuntimeError("boom"))
    assert pipeline.upload_batch(cfg, db, 5) == 0
    assert list(cfg.downloads_dir.glob("*")) == []


def test_download_format_caps_height():
    import youtify.youtube as youtube
    captured = {}

    class FakeYDL:
        def __init__(self, opts): captured.update(opts)
        def __enter__(self): return self
        def __exit__(self, *e): pass
        def extract_info(self, url, download): raise RuntimeError("stop")

    import yt_dlp
    orig = yt_dlp.YoutubeDL
    yt_dlp.YoutubeDL = FakeYDL
    try:
        with pytest.raises(RuntimeError):
            youtube.download_video("x" * 11, __import__("pathlib").Path("/tmp/t2pdl"), 1080)
    finally:
        yt_dlp.YoutubeDL = orig
    assert "height<=1080" in captured["format"] and "2160" not in captured["format"]
    assert captured["merge_output_format"] == "mp4" and captured["skip_download"] is False


def test_strip_youtube_formatting():
    from youtify.metadata import strip_youtube_formatting
    s = ("Bob Mizer launched *Physique Pictorial* in 1953. _Italic words_ here.\n"
         "Link: https://en.wikipedia.org/wiki/Manual_Enterprises,_Inc._v._Day\n"
         "5 * 3 = 15, snake_case_name, a*b*c")
    out = strip_youtube_formatting(s)
    assert "launched Physique Pictorial in" in out
    assert "Italic words here" in out
    assert "https://en.wikipedia.org/wiki/Manual_Enterprises,_Inc._v._Day" in out
    assert "5 * 3 = 15, snake_case_name, a*b*c" in out


def test_strip_youtube_extras():
    from youtify.metadata import strip_youtube_extras
    s = ("Intro line about #1 hits.\n\n"
         "💛 Join Gay Timeline Memberships: https://www.youtube.com/channel/X/join\n\n"
         "Your support keeps this channel telling forgotten stories. Every membership matters. Thank you.\n\n"
         "Watch: https://youtu.be/abc\n\n"
         "Source: https://en.wikipedia.org/wiki/Bob_Mizer\n\n"
         "#GayHistory #LGBTQHistory #GayTimeline")
    out = strip_youtube_extras(s)
    assert out == "Intro line about #1 hits.\n\nSource: https://en.wikipedia.org/wiki/Bob_Mizer"


def test_members_only_detection():
    from youtify.youtube import is_members_only
    assert is_members_only("ERROR: [youtube] abc: Join this channel to get access to members-only content "
                           "like this video, and other exclusive perks.")
    assert is_members_only("This video is available to this channel's members on level: Supporter")
    assert not is_members_only("Sign in to confirm you're not a bot")


def test_members_only_video_skipped_and_next_uploaded(tmp_path, monkeypatch):
    import json as _json
    import youtify.pipeline as pipeline
    import youtify.youtube as youtube
    import youtify.media as media
    import youtify.spotify as spotify

    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(f"""
data_dir: {tmp_path / 'data'}
spotify: {{pause_between_uploads_seconds: 0}}
channels:
  - name: Garden Life
    youtube_url: https://www.youtube.com/@garden
    spotify_show_id: SHOW1
    video_dirs: []
    download_missing: true
    season_number: 1
    next_episode_number: 10
""")
    cfg = load_config(cfg_path)
    cfg.ensure_dirs()
    db = DB(cfg.db_path)
    cat = [dict(id="vidNEWER0001", title="Newer", duration=900, position=0),
           dict(id="vidMEMBERS01", title="Members", duration=900, position=1),
           dict(id="vidCATALOGM1", title="Flagged in list", duration=900, position=2, members_only=True)]
    db.replace_catalog("garden-life", cat)
    db.set_kv("members_only:vidCATALOGM1", "1")
    pipeline.scan(cfg, db)
    assert db.episode("vidCATALOGM1")["status"] == states.SKIPPED  # known from the channel list
    meta = tmp_path / "m.json"
    meta.write_text(_json.dumps({"title": "t", "description": "d"}))
    for v in ("vidNEWER0001", "vidMEMBERS01"):
        db.update_episode(v, status=states.READY, metadata_path=str(meta))

    def fake_download(yt_id, dest_dir, max_height, cookies):
        if yt_id == "vidMEMBERS01":
            raise youtube.MembersOnlyError("members only")
        dest_dir.mkdir(parents=True, exist_ok=True)
        f = dest_dir / f"{yt_id}.mp4"
        f.write_bytes(b"x")
        return f

    uploaded = []

    class FakeUp:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *e): pass
        def is_logged_in(self, show): return True
        def upload(self, job, action):
            uploaded.append((job.yt_id, job.episode_number))
            return "https://spotify/ep"

    monkeypatch.setattr(youtube, "download_video", fake_download)
    monkeypatch.setattr(spotify, "SpotifyUploader", FakeUp)
    monkeypatch.setattr(media, "prepare_for_upload", lambda src, wd: (src, False))
    # limit 1: the members-only video (older, so first in line) is skipped and the next one goes up
    assert pipeline.upload_batch(cfg, db, 1) == 1
    assert uploaded == [("vidNEWER0001", 10)]  # members-only video did not use up an episode number
    ep = db.episode("vidMEMBERS01")
    assert ep["status"] == states.SKIPPED and "members-only" in ep["last_error"]
    assert ep["attempts"] == 0


def test_members_only_skipped_during_prepare(tmp_path, monkeypatch):
    import youtify.pipeline as pipeline
    import youtify.youtube as yt

    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(f"""
data_dir: {tmp_path / 'data'}
channels:
  - name: Garden Life
    youtube_url: https://www.youtube.com/@garden
    spotify_show_id: SHOW1
    video_dirs: []
""")
    cfg = load_config(cfg_path)
    cfg.ensure_dirs()
    db = DB(cfg.db_path)
    db.add_episode("vidMEMBERS01", "garden-life", "M", "/x/a.mp4", 5)
    db.add_episode("vidNORMAL001", "garden-life", "N", "/x/b.mp4", 4)
    prepared = []

    def fake_prepare(cfg_, db_, llm, ep):
        if ep["yt_id"] == "vidMEMBERS01":
            raise yt.MembersOnlyError("members only")
        prepared.append(ep["yt_id"])

    monkeypatch.setattr(pipeline, "prepare_episode", fake_prepare)
    assert pipeline.prepare_batch(cfg, db, 1) == 1
    assert prepared == ["vidNORMAL001"]
    assert db.episode("vidMEMBERS01")["status"] == states.SKIPPED


def test_catch_up_due_once_per_day_after_time(tmp_path):
    import datetime as dt
    from youtify.cli import catch_up_due
    from youtify.db import DB

    db = DB(tmp_path / "s.sqlite3")
    d = dt.datetime(2026, 11, 13)
    assert not catch_up_due(db, "08:00", d.replace(hour=7, minute=59))
    assert catch_up_due(db, "08:00", d.replace(hour=8, minute=16))
    assert not catch_up_due(db, "08:00", d.replace(hour=9))
    assert catch_up_due(db, "08:00", d.replace(day=14, hour=8))


def test_schedule_plist_runs_at_login_with_catch_up(tmp_path):
    from youtify.schedule import build_plist

    pl = build_plist(tmp_path / "c.yaml", tmp_path, ["19:05", "07:00", "08:00"])
    assert pl["RunAtLoad"] is True
    assert pl["ProgramArguments"][-2:] == ["run", "--scheduled"]
    assert pl["StartCalendarInterval"] == [{"Hour": 7, "Minute": 0}, {"Hour": 8, "Minute": 0},
                                           {"Hour": 19, "Minute": 5}]


def test_upload_limit_is_per_channel(tmp_path, monkeypatch):
    import json as _json
    import youtify.media as media
    import youtify.pipeline as pipeline
    import youtify.spotify as spotify

    cfg_path = tmp_path / "c.yaml"
    cfg_path.write_text(f"data_dir: {tmp_path}\nrun: {{notify: false}}\nchannels:\n"
                        "  - {name: a, youtube_url: u1, spotify_show_id: s1}\n"
                        "  - {name: b, youtube_url: u2, spotify_show_id: s2}\n")
    cfg = load_config(cfg_path)
    cfg.ensure_dirs()
    db = DB(cfg.db_path)
    meta = tmp_path / "m.json"
    meta.write_text(_json.dumps({"title": "T", "description": "d"}))
    for i, (yt_id, ch) in enumerate([("vidA0000001", "a"), ("vidA0000002", "a"),
                                     ("vidB0000001", "b"), ("vidB0000002", "b")]):
        f = tmp_path / f"{yt_id}.mp4"
        f.write_bytes(b"x")
        db.add_episode(yt_id, ch, "T", str(f), i)
        db.update_episode(yt_id, status=states.READY, metadata_path=str(meta))
    shows = []

    class FakeUp:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *e): pass
        def is_logged_in(self, show): return True
        def upload(self, job, action):
            shows.append(job.show_id)
            return "https://spotify/ep"

    monkeypatch.setattr(spotify, "SpotifyUploader", FakeUp)
    monkeypatch.setattr(media, "prepare_for_upload", lambda src, wd: (src, False))
    monkeypatch.setattr(pipeline.time, "sleep", lambda s: None)
    assert pipeline.upload_batch(cfg, db, 1, per_channel=True) == 2
    assert sorted(shows) == ["s1", "s2"]
    # without per_channel the limit is shared
    assert pipeline.upload_batch(cfg, db, 1) == 1


def test_due_upload_slots_and_from_video(tmp_path):
    import datetime as dt
    import youtify.pipeline as pipeline

    cfg_path = tmp_path / "c.yaml"
    cfg_path.write_text(f"data_dir: {tmp_path}\nrun: {{notify: false}}\nchannels:\n"
                        "  - {name: a, youtube_url: u1, spotify_show_id: s1, upload_times: ['08:00']}\n"
                        "  - {name: b, youtube_url: u2, spotify_show_id: s2, upload_times: ['19:05', '7:00'],"
                        " upload_from_video: vidB0000002}\n")
    cfg = load_config(cfg_path)
    assert cfg.channel("b").upload_times == ["07:00", "19:05"]
    db = DB(tmp_path / "s.sqlite3")
    d = dt.datetime(2026, 11, 15)
    assert pipeline.due_upload_slots(cfg, db, d.replace(hour=6, minute=59)) == {}
    assert pipeline.due_upload_slots(cfg, db, d.replace(hour=7)) == {"b": 1}
    assert pipeline.due_upload_slots(cfg, db, d.replace(hour=7, minute=30)) == {}
    # Mac off from 07:30 to 20:00 -> both missed slots caught up
    assert pipeline.due_upload_slots(cfg, db, d.replace(hour=20)) == {"a": 1, "b": 1}
    assert pipeline.due_upload_slots(cfg, db, d.replace(hour=23)) == {}
    assert pipeline.due_upload_slots(cfg, db, d.replace(day=16, hour=21)) == {"a": 1, "b": 2}

    # upload_from_video keeps that video itself (inclusive)
    db.replace_catalog("b", [{"id": f"vidB000000{i}", "title": "t", "position": i} for i in range(4)])
    assert pipeline.upload_boundary(cfg, db, cfg.channel("b")) == 3
