"""Configuration loading and validation."""
from __future__ import annotations

import dataclasses
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

DEFAULT_CONFIG_PATH = Path(
    os.environ.get("YOUTIFY_CONFIG", "~/Youtify/config.yaml")
).expanduser()

VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".mkv", ".webm", ".avi"}
FINISH_ACTIONS = {"draft", "publish", "handoff"}
# youtube       = copy the YouTube description exactly (no AI, default)
# ai            = write a new description with the local AI model
# youtube_or_ai = YouTube description, but use AI when the YouTube one is empty/very short
DESCRIPTION_SOURCES = {"youtube", "ai", "youtube_or_ai"}


def expand_path(value) -> Path:
    return Path(os.path.expandvars(str(value))).expanduser()


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "channel"


@dataclass
class LLMConfig:
    model: str = "qwen2.5:3b"
    host: str = "http://localhost:11434"
    num_ctx: int = 8192
    temperature: float = 0.4
    timeout_seconds: int = 300
    section_max_chars: int = 8000


@dataclass
class TranscriptionConfig:
    # Used only when YouTube has no captions for a video.
    whisper_fallback: bool = True
    whisper_model: str = "mlx-community/whisper-small-mlx"


@dataclass
class RunConfig:
    max_runtime_minutes: int = 240
    max_uploads_per_run: int = 5
    max_metadata_per_run: int = 30
    catalog_refresh_hours: int = 24
    require_metadata_approval: bool = False
    max_attempts: int = 3
    delete_transcodes_after_upload: bool = True
    # e.g. "chrome" or "safari" if YouTube starts rate-limiting caption downloads.
    youtube_cookies_from_browser: str | None = None
    notify: bool = True


@dataclass
class SpotifyConfig:
    finish_action: str = "draft"  # draft | publish | handoff
    headless: bool = False
    upload_timeout_minutes: int = 120
    wizard_url: str = "https://creators.spotify.com/pod/show/{show_id}/episode/wizard"
    upload_thumbnail: bool = True
    pause_between_uploads_seconds: int = 20
    # Use this browser instead of Playwright's Chromium, e.g. Brave:
    # "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser"
    browser_path: str | None = None
    brave_profile: str | None = None  # Brave profile to copy logins from with `youtify login --from-brave`


@dataclass
class ChannelConfig:
    name: str
    youtube_url: str
    spotify_show_id: str
    video_dirs: list[Path] = field(default_factory=list)
    key: str = ""
    language: str = "en"
    subtitle_langs: list[str] = field(default_factory=list)
    include_shorts: bool = False
    include_streams: bool = True
    min_duration_seconds: int = 60
    recursive: bool = True
    description_source: str = "youtube"
    append_chapters: bool = False  # Spotify auto-generates chapters; true = add your YouTube chapters
    style_notes: str = ""         # AI mode only
    footer: str = ""
    keywords_in_description: bool = False
    # true = videos with no file on this Mac are downloaded from YouTube just before upload
    # (at most download_max_height p) and deleted right after.
    download_missing: bool = False
    download_max_height: int = 1080
    # Only upload YouTube videos published AFTER this video (its YouTube id). Set automatically by
    # `youtify skip-existing` to the newest video already on Spotify.
    upload_after_video: str = ""
    # Upload this video (YouTube id) and everything newer. Takes priority over upload_after_video.
    upload_from_video: str = ""
    season_number: int | None = None        # e.g. 1 -> filled into "Season number"
    next_episode_number: int | None = None  # e.g. 72 -> first upload gets 72, then 73, ...
    episode_type: str = "full"              # full | trailer | bonus
    # Daily times ("HH:MM", Mac's local time) when the schedule uploads 1 episode of this channel.
    # Empty = 1 upload on every run.
    upload_times: list[str] = field(default_factory=list)
    enabled: bool = True

    def __post_init__(self):
        if self.description_source not in DESCRIPTION_SOURCES:
            raise ValueError(f"description_source must be one of {sorted(DESCRIPTION_SOURCES)}")
        self.key = slugify(self.key or self.name)
        self.video_dirs = [expand_path(p) for p in self.video_dirs]
        self.youtube_url = self.youtube_url.rstrip("/")
        for tab in ("/videos", "/streams", "/shorts", "/featured"):
            if self.youtube_url.endswith(tab):
                self.youtube_url = self.youtube_url[: -len(tab)]
        times = []
        for t in self.upload_times:
            h, m = (int(x) for x in str(t).split(":"))
            if not (0 <= h < 24 and 0 <= m < 60):
                raise ValueError(f"upload_times: bad time {t!r}")
            times.append(f"{h:02d}:{m:02d}")
        self.upload_times = sorted(set(times))
        if not self.subtitle_langs:
            lang = self.language
            # Manual subs first, then the original-language auto captions.
            self.subtitle_langs = [lang, f"{lang}-orig", f"{lang}-.*"]


@dataclass
class Config:
    config_path: Path
    data_dir: Path
    llm: LLMConfig
    transcription: TranscriptionConfig
    run: RunConfig
    spotify: SpotifyConfig
    channels: list[ChannelConfig]

    @property
    def db_path(self) -> Path:
        return self.data_dir / "state.sqlite3"

    @property
    def review_dir(self) -> Path:
        return self.data_dir / "review"

    @property
    def cache_dir(self) -> Path:
        return self.data_dir / "cache"

    @property
    def work_dir(self) -> Path:
        return self.data_dir / "work"

    @property
    def downloads_dir(self) -> Path:
        return self.data_dir / "work" / "downloads"

    @property
    def logs_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def browser_profile_dir(self) -> Path:
        return self.data_dir / "browser-profile"

    @property
    def selectors_path(self) -> Path:
        return self.data_dir / "spotify_selectors.yaml"

    def ensure_dirs(self) -> None:
        for d in (self.data_dir, self.review_dir, self.cache_dir, self.work_dir,
                  self.logs_dir, self.browser_profile_dir):
            d.mkdir(parents=True, exist_ok=True)

    def channel(self, key: str) -> ChannelConfig:
        for ch in self.channels:
            if ch.key == key:
                return ch
        raise KeyError(f"Unknown channel '{key}'. Known: {[c.key for c in self.channels]}")

    @property
    def enabled_channels(self) -> list[ChannelConfig]:
        return [c for c in self.channels if c.enabled]

    @property
    def uses_ai(self) -> bool:
        return any(c.description_source != "youtube" for c in self.enabled_channels)


def _build(cls, data: dict | None, where: str):
    data = data or {}
    if not isinstance(data, dict):
        raise ValueError(f"'{where}' must be a mapping")
    names = {f.name for f in dataclasses.fields(cls)}
    unknown = set(data) - names
    if unknown:
        raise ValueError(f"Unknown key(s) in '{where}': {sorted(unknown)}. Allowed: {sorted(names)}")
    try:
        return cls(**data)
    except (TypeError, ValueError) as e:
        raise ValueError(f"Invalid '{where}': {e}") from None


def load_config(path: Path | str | None = None) -> Config:
    path = expand_path(path) if path else DEFAULT_CONFIG_PATH
    if not path.exists():
        raise FileNotFoundError(f"Config not found at {path}. Run `youtify init` first.")
    raw = yaml.safe_load(path.read_text()) or {}
    known = {"data_dir", "llm", "transcription", "run", "spotify", "channels"}
    unknown = set(raw) - known
    if unknown:
        raise ValueError(f"Unknown top-level key(s) in config: {sorted(unknown)}")

    channels = [_build(ChannelConfig, c, f"channels[{i}]") for i, c in enumerate(raw.get("channels") or [])]
    if not channels:
        raise ValueError("Config must define at least one channel under 'channels'.")
    keys = [c.key for c in channels]
    if len(set(keys)) != len(keys):
        raise ValueError(f"Channel keys must be unique, got {keys}")

    spotify = _build(SpotifyConfig, raw.get("spotify"), "spotify")
    if spotify.finish_action not in FINISH_ACTIONS:
        raise ValueError(f"spotify.finish_action must be one of {sorted(FINISH_ACTIONS)}")

    return Config(
        config_path=path,
        data_dir=expand_path(raw.get("data_dir", path.parent)),
        llm=_build(LLMConfig, raw.get("llm"), "llm"),
        transcription=_build(TranscriptionConfig, raw.get("transcription"), "transcription"),
        run=_build(RunConfig, raw.get("run"), "run"),
        spotify=spotify,
        channels=channels,
    )
