"""Orchestration: sync catalog -> scan Mac folders -> generate metadata -> upload to Spotify."""
from __future__ import annotations

import fcntl
import json
import logging
import platform
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path

from . import db as states
from .config import VIDEO_EXTS, ChannelConfig, Config
from .db import DB

log = logging.getLogger(__name__)


class Deadline:
    def __init__(self, minutes: float):
        self.end = time.monotonic() + minutes * 60

    def remaining_min(self) -> float:
        return (self.end - time.monotonic()) / 60

    def expired(self, reserve_min: float = 0) -> bool:
        return self.remaining_min() <= reserve_min


@contextmanager
def run_lock(cfg: Config):
    path = cfg.data_dir / ".run.lock"
    f = open(path, "w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        f.close()
        raise RuntimeError("Another youtify run is in progress.") from None
    try:
        yield
    finally:
        fcntl.flock(f, fcntl.LOCK_UN)
        f.close()


def notify(title: str, message: str) -> None:
    if platform.system() != "Darwin":
        return
    esc = lambda s: s.replace("\\", "\\\\").replace('"', '\\"')  # noqa: E731
    subprocess.run(["osascript", "-e", f'display notification "{esc(message)}" with title "{esc(title)}"'],
                   check=False, capture_output=True)


# ---------------------------------------------------------------- catalog
def sync_catalogs(cfg: Config, db: DB, force: bool = False, only: str | None = None) -> None:
    from .youtube import fetch_catalog

    for ch in cfg.enabled_channels:
        if only and ch.key != only:
            continue
        age_h = (time.time() - db.catalog_refreshed_at(ch.key)) / 3600
        if not force and age_h < cfg.run.catalog_refresh_hours:
            continue
        log.info("[%s] Refreshing YouTube video list", ch.key)
        try:
            entries = fetch_catalog(ch.youtube_url, ch.include_streams, ch.include_shorts,
                                    cfg.run.youtube_cookies_from_browser)
        except Exception as e:  # noqa: BLE001
            log.error("[%s] Could not refresh catalog: %s", ch.key, e)
            continue
        db.replace_catalog(ch.key, entries)
        for e in entries:
            if e.get("members_only"):
                db.set_kv(f"members_only:{e['id']}", "1")
                skip_members_only(db, e["id"])
        log.info("[%s] %d videos on YouTube", ch.key, len(entries))


MEMBERS_ONLY_NOTE = "members-only video (skipped)"


def skip_members_only(db: DB, yt_id: str) -> bool:
    """Mark a members-only video as skipped (unless it is already on Spotify). Returns True if changed."""
    ep = db.episode(yt_id)
    if not ep or ep["status"] in (states.UPLOADED, states.SKIPPED):
        return False
    db.update_episode(yt_id, status=states.SKIPPED, last_error=MEMBERS_ONLY_NOTE)
    log.info("  %s is members-only on YouTube; skipping it and moving to the next video", yt_id)
    return True


# ---------------------------------------------------------------- scan
def iter_video_files(ch: ChannelConfig):
    for d in ch.video_dirs:
        if not d.exists():
            log.warning("[%s] Folder not found: %s", ch.key, d)
            continue
        it = d.rglob("*") if ch.recursive else d.glob("*")
        for p in it:
            if (p.is_file() and p.suffix.lower() in VIDEO_EXTS and not p.name.startswith(".")
                    and ".spotify." not in p.name):
                yield p


def scan(cfg: Config, db: DB) -> dict[str, int]:
    from .matcher import match_file
    from .media import probe

    stats = {"new": 0, "unmatched": 0}
    for ch in cfg.enabled_channels:
        catalog_rows = db.catalog(ch.key)
        if not catalog_rows:
            log.warning("[%s] Catalog empty; run `youtify sync` first", ch.key)
            continue
        taken = {e["yt_id"] for e in db.episodes(channel=ch.key)}
        catalog = [dict(id=r["yt_id"], title=r["title"], duration=r["duration"], position=r["position"])
                   for r in catalog_rows]
        free = [c for c in catalog if c["id"] not in taken]
        refreshed = db.catalog_refreshed_at(ch.key)
        for path in iter_video_files(ch):
            spath = str(path)
            if db.episode_by_path(spath):
                continue
            st = path.stat()
            if time.time() - st.st_mtime < 120:
                continue  # still being copied/exported
            prev = db.unmatched_entry(spath)
            if (prev and prev["size"] == st.st_size and prev["mtime"] == st.st_mtime
                    and prev["seen_at"] > refreshed):
                continue  # nothing changed since we last failed to match it
            try:
                duration = probe(path).duration
            except Exception as e:  # noqa: BLE001
                log.warning("[%s] Cannot read %s (%s)", ch.key, path.name, e)
                continue
            m = match_file(path.stem, duration, free)
            if m.yt_id:
                entry = next(c for c in free if c["id"] == m.yt_id)
                if duration and duration < ch.min_duration_seconds:
                    status = states.SKIPPED
                else:
                    status = states.PENDING
                db.add_episode(m.yt_id, ch.key, entry["title"], spath, entry["position"], status)
                free = [c for c in free if c["id"] != m.yt_id]
                stats["new"] += 1
                log.info("[%s] Matched %s -> %s (%s, %.2f)", ch.key, path.name, m.yt_id, m.reason, m.score)
            else:
                db.set_unmatched(spath, ch.key, st.st_size, st.st_mtime, m.best_guess, m.score)
                stats["unmatched"] += 1
                guess = db.catalog_entry(m.best_guess) if m.best_guess else None
                log.warning("[%s] Could not match %s (%s; best guess: %s %.2f). Use `youtify map`.",
                            ch.key, path.name, m.reason, guess["title"] if guess else "-", m.score)
    stats["download"] = queue_downloads(cfg, db)
    for ch in cfg.enabled_channels:
        apply_upload_boundary(cfg, db, ch)
    return stats


def is_download_path(cfg: Config, path: str | Path) -> bool:
    return Path(path).parent == cfg.downloads_dir


BOUNDARY_NOTE = "older than latest Spotify episode"


def unskip_inside_boundary(cfg: Config, db: DB, ch: ChannelConfig) -> int:
    """After the start video moved earlier: queue again videos that the old boundary skipped."""
    boundary = upload_boundary(cfg, db, ch)
    n = 0
    for e in db.episodes(states.SKIPPED, ch.key):
        if e["last_error"] == BOUNDARY_NOTE and e["position"] is not None \
                and (boundary is None or e["position"] < boundary):
            db.update_episode(e["yt_id"], status=states.PENDING, last_error=None)
            n += 1
    return n


def upload_boundary(cfg: Config, db: DB, ch: ChannelConfig) -> int | None:
    """Catalog position of the newest video already on Spotify (videos at this position or older are skipped)."""
    if ch.upload_from_video:
        entry = db.catalog_entry(ch.upload_from_video)
        return entry["position"] + 1 if entry else None
    yt_id = ch.upload_after_video or db.get_kv(f"upload_after:{ch.key}")
    if not yt_id:
        return None
    entry = db.catalog_entry(yt_id)
    return entry["position"] if entry else None


def apply_upload_boundary(cfg: Config, db: DB, ch: ChannelConfig) -> int:
    """Skip every queued (not yet uploaded) episode that is older than the boundary."""
    boundary = upload_boundary(cfg, db, ch)
    if boundary is None:
        return 0
    n = 0
    for e in db.episodes(channel=ch.key):
        if e["status"] in (states.PENDING, states.READY, states.AWAITING_APPROVAL, states.FAILED) \
                and e["position"] is not None and e["position"] >= boundary:
            db.update_episode(e["yt_id"], status=states.SKIPPED, last_error=BOUNDARY_NOTE)
            n += 1
    return n


def episode_number_for(cfg: Config, db: DB, ch: ChannelConfig, yt_id: str) -> int | None:
    """Stable episode number per video: assigned once (next free number), reused on retries."""
    if ch.next_episode_number is None:
        return None
    key = f"episode_number:{yt_id}"
    have = db.get_kv(key)
    if have:
        return int(have)
    nxt = int(db.get_kv(f"next_episode_number:{ch.key}") or ch.next_episode_number)
    nxt = max(nxt, int(ch.next_episode_number))
    db.set_kv(key, str(nxt))
    db.set_kv(f"next_episode_number:{ch.key}", str(nxt + 1))
    return nxt


def queue_downloads(cfg: Config, db: DB) -> int:
    """For channels with download_missing: queue every YouTube video that has no local file.

    Runs after the folder scan, so a video you do have on the Mac always uses your own file.
    """
    added = 0
    for ch in cfg.enabled_channels:
        if not ch.download_missing:
            continue
        taken = {e["yt_id"] for e in db.episodes(channel=ch.key)}
        boundary = upload_boundary(cfg, db, ch)
        for r in db.catalog(ch.key):
            if r["yt_id"] in taken:
                continue
            status = states.PENDING
            if r["duration"] and r["duration"] < ch.min_duration_seconds:
                status = states.SKIPPED
            if boundary is not None and r["position"] is not None and r["position"] >= boundary:
                status = states.SKIPPED  # older than (or equal to) the newest video already on Spotify
            members_only = bool(db.get_kv(f"members_only:{r['yt_id']}"))
            if members_only:
                status = states.SKIPPED
            path = str(cfg.downloads_dir / f"{r['yt_id']}.mp4")
            db.add_episode(r["yt_id"], ch.key, r["title"], path, r["position"], status)
            if members_only:
                db.update_episode(r["yt_id"], last_error=MEMBERS_ONLY_NOTE)
            added += 1
        if added:
            log.info("[%s] %d video(s) will be downloaded from YouTube at upload time", ch.key, added)
    return added


def map_file(cfg: Config, db: DB, file_path: str, yt_id: str) -> None:
    path = str(Path(file_path).expanduser().resolve())
    entry = db.catalog_entry(yt_id)
    if not entry:
        raise ValueError(f"{yt_id} is not in any channel catalog (run `youtify sync`)")
    if db.episode(yt_id):
        raise ValueError(f"{yt_id} is already linked to a file")
    db.add_episode(yt_id, entry["channel"], entry["title"], path, entry["position"])


# ---------------------------------------------------------------- metadata
class LazyLLM:
    """Starts/checks Ollama only if an episode actually needs AI (YouTube mode never does)."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._client = None

    def get(self):
        if self._client is None:
            from .llm import OllamaClient

            c = self.cfg.llm
            client = OllamaClient(c.host, c.model, c.num_ctx, c.temperature, c.timeout_seconds)
            client.check()
            self._client = client
        return self._client

    def unload(self) -> None:
        if self._client is not None:
            self._client.unload()


EXTRA_FOR_SKIPS = 25  # look this many episodes further ahead to replace members-only skips
MIN_YOUTUBE_DESCRIPTION = 40  # chars; shorter counts as "empty" for youtube_or_ai


def prepare_episode(cfg: Config, db: DB, llm: LazyLLM, ep) -> None:
    from .metadata import build_metadata, build_youtube_metadata, review_markdown
    from .youtube import fetch_details

    ch = cfg.channel(ep["channel"])
    cache = cfg.cache_dir / ep["yt_id"]
    cache.mkdir(parents=True, exist_ok=True)
    mode = ch.description_source
    details = fetch_details(ep["yt_id"], cache, ch.subtitle_langs, cfg.run.youtube_cookies_from_browser,
                            want_subtitles=(mode != "youtube"))
    info = details["info"]
    thumb = str(details["thumbnail_path"]) if details["thumbnail_path"] else None

    yt_desc = (info.get("description") or "").strip()
    use_ai = mode == "ai" or (mode == "youtube_or_ai" and len(yt_desc) < MIN_YOUTUBE_DESCRIPTION)
    if use_ai:
        segments, source = _transcript(cfg, ch, ep, details, cache, llm)
        meta = build_metadata(llm=llm.get(), channel=ch, info=info, segments=segments,
                              transcript_source=source, thumbnail=thumb,
                              section_max_chars=cfg.llm.section_max_chars)
    else:
        meta = build_youtube_metadata(channel=ch, info=info, thumbnail=thumb)

    out_dir = cfg.review_dir / ch.key
    out_dir.mkdir(parents=True, exist_ok=True)
    base = out_dir / f"{info.get('upload_date') or '00000000'}_{ep['yt_id']}"
    json_path = base.with_suffix(".json")
    json_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2))
    base.with_suffix(".md").write_text(review_markdown(meta, str(json_path)))

    status = states.AWAITING_APPROVAL if cfg.run.require_metadata_approval else states.READY
    db.update_episode(ep["yt_id"], title=info["title"], upload_date=info.get("upload_date"),
                      metadata_path=str(json_path), status=status, attempts=0, last_error=None)


def _transcript(cfg: Config, ch: ChannelConfig, ep, details: dict, cache: Path, llm: LazyLLM):
    from .transcript import load_segments, parse_vtt, save_segments, transcribe_whisper

    tpath = cache / "transcript.json"
    if tpath.exists():
        return load_segments(tpath)
    if details["subtitle_path"]:
        segments = parse_vtt(details["subtitle_path"].read_text(errors="replace"))
        source = f"youtube_{details['subtitle_kind']}"
    elif cfg.transcription.whisper_fallback:
        llm.unload()  # give Whisper the RAM
        segments = transcribe_whisper(Path(ep["file_path"]), cfg.transcription.whisper_model, ch.language)
        source = "whisper"
    else:
        return [], "none"
    save_segments(tpath, segments, source)
    return segments, source


def prepare_batch(cfg: Config, db: DB, limit: int, deadline: Deadline | None = None,
                  reserve_min: float = 0, channel: str | None = None) -> int:
    # Fetch extra so a members-only video (skipped) is replaced by the next one.
    eps = db.needing_metadata(limit + EXTRA_FOR_SKIPS, channel)
    if not eps:
        return 0
    from .llm import LLMError
    from .youtube import MembersOnlyError

    llm = LazyLLM(cfg)
    done = 0
    tried = 0
    try:
        for ep in eps:
            if tried >= limit:
                break
            if deadline and deadline.expired(reserve_min):
                log.info("Time budget reached; stopping metadata preparation")
                break
            log.info("[%s] Preparing metadata for %s — %s", ep["channel"], ep["yt_id"], ep["title"])
            t0 = time.monotonic()
            try:
                prepare_episode(cfg, db, llm, ep)
                done += 1
                tried += 1
                log.info("  done in %.0fs", time.monotonic() - t0)
            except MembersOnlyError:
                skip_members_only(db, ep["yt_id"])
            except LLMError as e:
                tried += 1
                if "Cannot reach" in str(e) or "not pulled" in str(e):
                    raise
                db.record_failure(ep["yt_id"], f"metadata: {e}", cfg.run.max_attempts)
                log.error("  failed: %s", e)
            except Exception as e:  # noqa: BLE001
                tried += 1
                db.record_failure(ep["yt_id"], f"metadata: {type(e).__name__}: {e}", cfg.run.max_attempts)
                log.exception("  failed")
    finally:
        llm.unload()
    return done


# ---------------------------------------------------------------- upload
def _thumbnail_for(cfg: Config, yt_id: str, meta: dict) -> Path | None:
    if meta.get("thumbnail") and Path(meta["thumbnail"]).exists():
        return Path(meta["thumbnail"])
    from .youtube import ensure_jpg_thumbnail

    return ensure_jpg_thumbnail(cfg.cache_dir / yt_id, yt_id)


def upload_batch(cfg: Config, db: DB, limit: int, deadline: Deadline | None = None,
                 dry_run: bool = False, finish_action: str | None = None,
                 per_channel: bool = False, quotas: dict[str, int] | None = None,
                 _channels: set[str] | None = None) -> int:
    """Upload up to `limit` episodes in total, or up to `limit` per channel when per_channel,
    or quotas[channel_key] per channel when quotas is given."""
    if _channels is None:
        # One browser session per Spotify login (channels may be on different Spotify accounts).
        groups: dict = {}
        for ch in cfg.enabled_channels:
            groups.setdefault(cfg.profile_dir_for(ch), set()).add(ch.key)
        if len(groups) > 1:
            total, errors = 0, []
            for keys in groups.values():
                try:
                    total += upload_batch(cfg, db, limit, deadline, dry_run, finish_action,
                                          per_channel, quotas, _channels=keys)
                except Exception as e:  # noqa: BLE001  one account logged out -> still do the others
                    log.error("Upload stopped for %s: %s", ", ".join(sorted(keys)), e)
                    errors.append(e)
            if errors and not total:
                raise errors[0]
            return total
        _channels = {ch.key for ch in cfg.enabled_channels}
    from .media import prepare_for_upload
    from .spotify import (NotLoggedIn, RetryableUploadError, SpotifyUploader, UncertainUploadError,
                          UploadJob)

    from .youtube import MembersOnlyError

    # Fetch extra so a members-only video (skipped) is replaced by the next one.
    if quotas is not None:
        per_channel = True
    else:
        quotas = {ch.key: limit for ch in cfg.enabled_channels} if per_channel else {}
    if per_channel:
        eps = [ep for ch in cfg.enabled_channels if ch.key in _channels and quotas.get(ch.key, 0) > 0
               for ep in db.ready_for_upload(quotas[ch.key] + EXTRA_FOR_SKIPS, ch.key)]
    else:
        eps = [ep for ep in db.ready_for_upload(limit + EXTRA_FOR_SKIPS + 200)
               if ep["channel"] in _channels][: limit + EXTRA_FOR_SKIPS]
    if not eps:
        return 0
    action = finish_action or cfg.spotify.finish_action
    uploaded = 0
    tried: dict[str | None, int] = {}  # attempts per channel (key None = all channels together)
    profile = cfg.profile_dir_for(cfg.channel(eps[0]["channel"]))
    profile.mkdir(parents=True, exist_ok=True)
    with SpotifyUploader(cfg.spotify, cfg.logs_dir, profile, cfg.selectors_path,
                         headless=False if action == "handoff" else None) as up:
        if not up.is_logged_in(cfg.channel(eps[0]["channel"]).spotify_show_id):
            ch0 = cfg.channel(eps[0]["channel"])
            hint = f"youtify login --channel {ch0.key}" if ch0.spotify_login else "youtify login"
            raise NotLoggedIn(f"Not logged in to the Spotify account of {ch0.name}. Run `{hint}`.")
        for ep in eps:
            key = ep["channel"] if per_channel else None
            if tried.get(key, 0) >= (quotas.get(key, 0) if per_channel else limit):
                continue
            if db.get_kv(f"members_only:{ep['yt_id']}"):
                skip_members_only(db, ep["yt_id"])
                continue
            if sum(tried.values()):
                time.sleep(cfg.spotify.pause_between_uploads_seconds)
            if deadline and deadline.expired(reserve_min=15):
                log.info("Time budget reached; stopping uploads")
                break
            meta = json.loads(Path(ep["metadata_path"]).read_text())
            ch = cfg.channel(ep["channel"])
            src = Path(ep["file_path"])
            downloaded = False
            if not src.exists() and is_download_path(cfg, src) and not dry_run:
                from .youtube import delete_download, download_video

                log.info("[%s] Downloading %s from YouTube (max %dp)", ep["channel"], ep["yt_id"],
                         ch.download_max_height)
                try:
                    src = download_video(ep["yt_id"], cfg.downloads_dir, ch.download_max_height,
                                         cfg.run.youtube_cookies_from_browser)
                    downloaded = True
                except MembersOnlyError:
                    delete_download(ep["yt_id"], cfg.downloads_dir)
                    db.set_kv(f"members_only:{ep['yt_id']}", "1")
                    skip_members_only(db, ep["yt_id"])
                    continue
                except Exception as e:  # noqa: BLE001
                    tried[key] = tried.get(key, 0) + 1
                    delete_download(ep["yt_id"], cfg.downloads_dir)
                    n = db.record_failure(ep["yt_id"], f"download: {type(e).__name__}: {e}", cfg.run.max_attempts)
                    log.error("  download failed (attempt %d/%d): %s", n, cfg.run.max_attempts, e)
                    continue
            tried[key] = tried.get(key, 0) + 1
            if not src.exists() and not (dry_run and is_download_path(cfg, src)):
                db.record_failure(ep["yt_id"], f"upload: file missing {src}", cfg.run.max_attempts, give_up=True)
                continue
            log.info("[%s] Uploading %s — %s", ep["channel"], ep["yt_id"], meta["title"])
            tmp = False
            video = src
            try:
                if dry_run and not src.exists():
                    log.info("  dry run: would download %s and upload it", ep["yt_id"])
                    continue
                video, tmp = prepare_for_upload(src, cfg.work_dir)
                job = UploadJob(
                    yt_id=ep["yt_id"], show_id=ch.spotify_show_id,
                    video_path=video, title=meta["title"], description=meta["description"],
                    thumbnail=_thumbnail_for(cfg, ep["yt_id"], meta),
                    season_number=ch.season_number,
                    episode_number=None if dry_run else episode_number_for(cfg, db, ch, ep["yt_id"]),
                    episode_type=ch.episode_type,
                )
                if dry_run:
                    log.info("  dry run: would upload %s (%d chars of description)", video, len(job.description))
                    continue
                url = up.upload(job, action)
                db.update_episode(ep["yt_id"], status=states.UPLOADED, spotify_url=url,
                                  uploaded_at=time.time(), last_error=None)
                uploaded += 1
                log.info("  uploaded (%s) as season %s episode %s: %s", action, job.season_number,
                         job.episode_number, url)
                if tmp and cfg.run.delete_transcodes_after_upload:
                    video.unlink(missing_ok=True)
            except NotLoggedIn:
                raise
            except UncertainUploadError as e:
                db.record_failure(ep["yt_id"], f"upload-unverified: {e}. Check Spotify, then run "
                                  f"`youtify mark-uploaded {ep['yt_id']}` or `youtify retry {ep['yt_id']}`",
                                  cfg.run.max_attempts, give_up=True)
                log.error("  could not verify upload; NOT retrying automatically: %s", e)
            except (RetryableUploadError, Exception) as e:  # noqa: BLE001
                n = db.record_failure(ep["yt_id"], f"upload: {type(e).__name__}: {e}", cfg.run.max_attempts)
                log.error("  failed (attempt %d/%d): %s", n, cfg.run.max_attempts, e)
            finally:
                if downloaded:
                    from .youtube import delete_download

                    if tmp:
                        video.unlink(missing_ok=True)
                    delete_download(ep["yt_id"], cfg.downloads_dir)
                    log.info("  deleted the downloaded copy")
    return uploaded


# ---------------------------------------------------------------- daily run
def due_upload_slots(cfg: Config, db: DB, now=None, mark: bool = True) -> dict[str, int]:
    """For scheduled runs: uploads owed per channel = its upload_times passed today that no run
    has handled yet (so missed times are caught up later the same day). Channels without
    upload_times get max_uploads_per_run on every run. mark=True remembers them as handled."""
    import datetime as dt

    now = now or dt.datetime.now()
    today = now.date().isoformat()
    owed = {}
    for ch in cfg.enabled_channels:
        if not ch.upload_times:
            owed[ch.key] = cfg.run.max_uploads_per_run
            continue
        passed = sum(1 for t in ch.upload_times if t <= now.strftime("%H:%M"))
        key = f"slots_done:{ch.key}:{today}"
        done = int(db.get_kv(key) or 0)
        if passed > done:
            owed[ch.key] = passed - done
            if mark:
                db.set_kv(key, str(passed))
    return owed


def run_all(cfg: Config, db: DB, minutes: float | None = None,
            quotas: dict[str, int] | None = None) -> dict:
    """Daily job. RAM-friendly order for 8 GB: LLM work and browser work never overlap.
    quotas = uploads per channel key (default: max_uploads_per_run for every channel)."""
    deadline = Deadline(minutes or cfg.run.max_runtime_minutes)
    target = cfg.run.max_uploads_per_run
    if quotas is None:
        quotas = {ch.key: target for ch in cfg.enabled_channels}
    summary = {"metadata": 0, "uploaded": 0, "error": None}
    try:
        sync_catalogs(cfg, db)
        stats = scan(cfg, db)
        log.info("Scan: %d new episode(s), %d unmatched file(s)", stats["new"], stats["unmatched"])

        # Phase 1: make sure today's uploads have metadata (uses at most ~40% of the budget).
        # max_uploads_per_run applies to each channel separately.
        for ch in cfg.enabled_channels:
            want = quotas.get(ch.key, 0)
            missing = max(0, want - len(db.ready_for_upload(want, ch.key))) if want else 0
            if missing:
                budget_left = deadline.remaining_min() * 0.6
                summary["metadata"] += prepare_batch(cfg, db, missing, deadline,
                                                     reserve_min=budget_left, channel=ch.key)

        # Phase 2: upload (browser only; the model is unloaded).
        try:
            if any(quotas.values()):
                summary["uploaded"] = upload_batch(cfg, db, target, deadline, quotas=quotas)
        except Exception as e:  # noqa: BLE001  (e.g. logged out) - still prepare metadata below
            summary["error"] = str(e)
            log.error("Upload phase stopped: %s", e)

        # Phase 3: use the remaining time to prepare future episodes.
        rest = cfg.run.max_metadata_per_run - summary["metadata"]
        if rest > 0 and not deadline.expired(10):
            summary["metadata"] += prepare_batch(cfg, db, rest, deadline, reserve_min=5)
    except Exception as e:  # noqa: BLE001
        summary["error"] = str(e)
        log.exception("Run stopped with an error")
    finally:
        if cfg.run.notify:
            msg = f"{summary['uploaded']} uploaded to Spotify, {summary['metadata']} prepared."
            if summary["error"]:
                msg = f"Problem: {summary['error'][:120]}"
            elif summary["uploaded"] and cfg.spotify.finish_action == "draft":
                msg += " Review drafts and publish."
            notify("youtify", msg)
    return summary
