"""Command line interface. Run `youtify -h`."""
from __future__ import annotations

import argparse
import json
import logging
import shutil
import sys
import time
from importlib import resources
from logging.handlers import RotatingFileHandler
from pathlib import Path

from . import db as states
from .config import DEFAULT_CONFIG_PATH, Config, expand_path, load_config
from .db import DB

log = logging.getLogger("youtify")


def setup_logging(cfg: Config | None, verbose: bool) -> None:
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%H:%M:%S")
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    root.handlers = [console]
    if cfg:
        cfg.logs_dir.mkdir(parents=True, exist_ok=True)
        fh = RotatingFileHandler(cfg.logs_dir / "youtify.log", maxBytes=5_000_000, backupCount=3)
        fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
        root.addHandler(fh)
    for noisy in ("urllib3", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


# ------------------------------------------------------------------ commands
def cmd_init(args) -> int:
    dest = expand_path(args.config) if args.config else DEFAULT_CONFIG_PATH
    if dest.exists() and not args.force:
        print(f"{dest} already exists (use --force to overwrite).")
        return 1
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(resources.files("youtify").joinpath("config.example.yaml").read_text())
    print(f"Wrote {dest}\nEdit it (channels, folders, Spotify show ids), then run: youtify doctor")
    return 0


def cmd_doctor(cfg: Config, db: DB, args) -> int:
    ok = True

    def check(label: str, passed: bool, hint: str = "") -> None:
        nonlocal ok
        ok &= passed
        print(f"  [{'OK' if passed else '!!'}] {label}" + ("" if passed or not hint else f"  ->  {hint}"))

    print("Tools")
    check("ffmpeg", bool(shutil.which("ffmpeg")), "brew install ffmpeg")
    check("ffprobe", bool(shutil.which("ffprobe")), "brew install ffmpeg")
    try:
        import yt_dlp  # noqa: F401
        check("yt-dlp", True)
    except ImportError:
        check("yt-dlp", False, "pip install -U yt-dlp")
    try:
        import playwright  # noqa: F401
        check("playwright", True)
    except ImportError:
        check("playwright", False, "pip install playwright && playwright install chromium")
    if cfg.uses_ai:
        from .llm import LLMError, OllamaClient
        try:
            OllamaClient(cfg.llm.host, cfg.llm.model).check()
            check(f"Ollama + model {cfg.llm.model} (AI descriptions)", True)
        except LLMError as e:
            check(f"Ollama + model {cfg.llm.model} (AI descriptions)", False, str(e))
        if cfg.transcription.whisper_fallback:
            try:
                import mlx_whisper  # noqa: F401
                check("mlx-whisper (fallback transcription)", True)
            except ImportError:
                check("mlx-whisper (fallback transcription)", False,
                      "optional: pip install 'youtify[whisper]' or set whisper_fallback: false")
    else:
        print("  [--] AI not needed: all channels copy their YouTube descriptions")

    print("Channels")
    for ch in cfg.channels:
        state = "" if ch.enabled else " (disabled)"
        check(f"{ch.key}{state}: Spotify show id set", not ch.spotify_show_id.startswith("REPLACE"),
              "copy it from creators.spotify.com/pod/show/<id>/...")
        for d in ch.video_dirs:
            check(f"{ch.key}: folder {d}", d.exists(), "create it or fix video_dirs")
        n = len(db.catalog(ch.key))
        check(f"{ch.key}: {n} YouTube videos known", n > 0, "run: youtify sync")
    print("\nAll good." if ok else "\nFix the items marked !! above.")
    return 0 if ok else 1


def cmd_login(cfg: Config, db: DB, args) -> int:
    from .spotify import BRAVE_APP, SpotifyUploader, brave_running, copy_brave_profile

    if args.from_brave:
        if brave_running():
            print("Brave is open. Quit Brave completely (Cmd+Q), then run this command again.")
            return 1
        if not cfg.spotify.browser_path:
            cfg.spotify.browser_path = BRAVE_APP
        try:
            name = copy_brave_profile(cfg.browser_profile_dir, args.profile or cfg.spotify.brave_profile)
        except (PermissionError, RuntimeError) as e:
            print(e)
            return 1
        print(f"Copied your Brave profile '{name}' (your logins). You can reopen Brave now.")
        with SpotifyUploader(cfg.spotify, cfg.logs_dir, cfg.browser_profile_dir, cfg.selectors_path,
                             headless=False) as up:
            show = cfg.enabled_channels[0].spotify_show_id
            if up.is_logged_in(show):
                print("Logged in to Spotify for Creators. Saved in", cfg.browser_profile_dir)
                return 0
            print("Not logged in yet. Log in once in the window that opened (your Google account "
                  "should already be signed in there), then press Enter.")
            input()
            if up.is_logged_in(show):
                print("Logged in. Saved in", cfg.browser_profile_dir)
                return 0
        print("Still not logged in. Make sure you are logged in to creators.spotify.com in Brave.")
        return 1

    with SpotifyUploader(cfg.spotify, cfg.logs_dir, cfg.browser_profile_dir, cfg.selectors_path,
                         headless=False) as up:
        up.interactive_login()
        show = cfg.enabled_channels[0].spotify_show_id
        if up.is_logged_in(show):
            print("Logged in. The session is saved in", cfg.browser_profile_dir)
            return 0
        print("Still not logged in (or the show id is wrong). Try again.")
        return 1


def cmd_sync(cfg, db, args) -> int:
    from .pipeline import sync_catalogs

    sync_catalogs(cfg, db, force=True)
    return 0


def cmd_scan(cfg, db, args) -> int:
    from .pipeline import scan

    s = scan(cfg, db)
    print(f"{s['new']} new episode(s) matched, {s['unmatched']} file(s) unmatched, "
          f"{s.get('download', 0)} queued to download from YouTube.")
    return 0


def cmd_prepare(cfg, db, args) -> int:
    from .pipeline import prepare_batch, run_lock

    with run_lock(cfg):
        n = prepare_batch(cfg, db, args.limit)
    print(f"Prepared metadata for {n} episode(s). Review files: {cfg.review_dir}")
    return 0


def cmd_upload(cfg, db, args) -> int:
    from .pipeline import run_lock, upload_batch

    with run_lock(cfg):
        n = upload_batch(cfg, db, args.limit, dry_run=args.dry_run, finish_action=args.action)
    print(f"Uploaded {n} episode(s).")
    return 0


def catch_up_due(db: DB, at: str, now=None) -> bool:
    """For the scheduled job: True (and remember today) if the daily run at `at` (HH:MM) is due."""
    import datetime as dt

    now = now or dt.datetime.now()
    hour, minute = (int(x) for x in at.split(":"))
    if (now.hour, now.minute) < (hour, minute):
        return False  # too early; the timed trigger will start it
    today = now.date().isoformat()
    if db.get_kv("last_scheduled_run") == today:
        return False
    db.set_kv("last_scheduled_run", today)
    return True


def _wait_for_network(timeout: float = 300) -> None:
    import socket

    end = time.time() + timeout
    while time.time() < end:
        try:
            socket.create_connection(("www.youtube.com", 443), timeout=5).close()
            return
        except OSError:
            time.sleep(10)


def cmd_run(cfg, db, args) -> int:
    from .pipeline import run_all, run_lock

    if args.catch_up:
        if not catch_up_due(db, args.catch_up):
            log.info("scheduled run: not due (before %s or already ran today)", args.catch_up)
            return 0
        log.info("scheduled run starting")
        _wait_for_network()
    with run_lock(cfg):
        s = run_all(cfg, db, args.minutes)
    print(json.dumps(s))
    return 1 if s["error"] else 0


def cmd_status(cfg, db, args) -> int:
    counts = db.counts()
    cols = [states.PENDING, states.AWAITING_APPROVAL, states.READY, states.UPLOADED, states.FAILED, states.SKIPPED]
    print(f"{'channel':<24}{'yt':>6}" + "".join(f"{c[:9]:>11}" for c in cols) + f"{'unmatched':>11}")
    for ch in cfg.channels:
        row = f"{ch.key:<24}{len(db.catalog(ch.key)):>6}"
        row += "".join(f"{counts.get((ch.key, c), 0):>11}" for c in cols)
        row += f"{len(db.unmatched(ch.key)):>11}"
        print(row)
    failed = db.episodes(states.FAILED)
    if failed:
        print("\nFailed (fix, then `youtify retry <id>`):")
        for e in failed:
            print(f"  {e['yt_id']}  {e['title'][:50]}\n      {e['last_error']}")
    um = db.unmatched()
    if um and args.verbose:
        print("\nUnmatched files (link with `youtify map <file> <youtube_id>`):")
        for u in um:
            g = db.catalog_entry(u["best_guess"]) if u["best_guess"] else None
            print(f"  {u['file_path']}\n      best guess: {u['best_guess']} {g['title'] if g else ''} ({u['score']:.2f})")
    return 0


def cmd_list(cfg, db, args) -> int:
    for e in db.episodes(args.status, args.channel):
        print(f"{e['yt_id']}  {e['status']:<17} {e['upload_date'] or '--------'}  {e['channel']:<16} {e['title'][:60]}")
    return 0


def cmd_show(cfg, db, args) -> int:
    e = _ep(db, args.yt_id)
    print(dict(e))
    if e["metadata_path"]:
        md = Path(e["metadata_path"]).with_suffix(".md")
        print("\n" + (md.read_text() if md.exists() else Path(e["metadata_path"]).read_text()))
    return 0


def cmd_missing(cfg, db, args) -> int:
    for ch in cfg.channels:
        if args.channel and ch.key != args.channel:
            continue
        have = {e["yt_id"] for e in db.episodes(channel=ch.key)}
        missing = [r for r in db.catalog(ch.key) if r["yt_id"] not in have]
        print(f"[{ch.key}] {len(missing)} YouTube video(s) with no local file:")
        for r in missing:
            print(f"  {r['yt_id']}  {r['title']}")
    return 0


def cmd_map(cfg, db, args) -> int:
    from .pipeline import map_file

    map_file(cfg, db, args.file, args.yt_id)
    print("Linked.")
    return 0


def _ep(db: DB, yt_id: str):
    e = db.episode(yt_id)
    if not e:
        raise SystemExit(f"No episode {yt_id}")
    return e


def _set_status(db: DB, ids: list[str], status: str, **extra) -> None:
    for i in ids:
        _ep(db, i)
        db.update_episode(i, status=status, **extra)
        print(f"{i} -> {status}")


def cmd_approve(cfg, db, args) -> int:
    ids = [e["yt_id"] for e in db.episodes(states.AWAITING_APPROVAL)] if args.all else args.ids
    _set_status(db, ids, states.READY)
    return 0


def cmd_retry(cfg, db, args) -> int:
    ids = [e["yt_id"] for e in db.episodes(states.FAILED)] if args.all else args.ids
    for i in ids:
        e = _ep(db, i)
        status = states.READY if e["metadata_path"] and Path(e["metadata_path"]).exists() else states.PENDING
        db.update_episode(i, status=status, attempts=0, last_error=None)
        print(f"{i} -> {status}")
    return 0


def cmd_regenerate(cfg, db, args) -> int:
    for i in args.ids:
        e = _ep(db, i)
        if e["status"] == states.UPLOADED:
            print(f"{i} is already uploaded; skipping")
            continue
        if args.transcript:
            (cfg.cache_dir / i / "transcript.json").unlink(missing_ok=True)
        db.update_episode(i, status=states.PENDING, attempts=0, last_error=None)
        print(f"{i} -> pending (run `youtify prepare`)")
    return 0


def cmd_skip(cfg, db, args) -> int:
    _set_status(db, args.ids, states.SKIPPED)
    return 0


def cmd_mark_uploaded(cfg, db, args) -> int:
    _set_status(db, args.ids, states.UPLOADED, uploaded_at=time.time(), last_error=None)
    return 0


def cmd_skip_existing(cfg, db, args) -> int:
    """Mark YouTube videos that are already episodes on Spotify as uploaded, so they are not re-uploaded."""
    from difflib import SequenceMatcher

    from .matcher import normalize_title
    from .pipeline import queue_downloads, sync_catalogs
    from .spotify import SpotifyUploader, existing_episode_titles

    sync_catalogs(cfg, db)
    queue_downloads(cfg, db)
    total = 0
    with SpotifyUploader(cfg.spotify, cfg.logs_dir, cfg.browser_profile_dir, cfg.selectors_path,
                         headless=False) as up:
        for ch in cfg.enabled_channels:
            if args.channel and ch.key != args.channel:
                continue
            titles = existing_episode_titles(up, ch.spotify_show_id)
            print(f"[{ch.key}] {len(titles)} episode(s) found on Spotify")
            catalog = [(r["yt_id"], r["title"], normalize_title(r["title"])) for r in db.catalog(ch.key)]
            unmatched = []
            newest = None  # (position, yt_id) of the newest YouTube video already on Spotify
            for t in titles:
                nt = normalize_title(t)
                best, score = None, 0.0
                for yt_id, yt_title, nyt in catalog:
                    sc = 1.0 if nt == nyt else SequenceMatcher(None, nt, nyt).ratio()
                    if sc > score:
                        best, score = (yt_id, yt_title), sc
                if best and score >= 0.9:
                    ep = db.episode(best[0])
                    if ep and ep["status"] != states.UPLOADED:
                        if not args.dry_run:
                            db.update_episode(best[0], status=states.UPLOADED, last_error=None,
                                              spotify_url="(was already on Spotify)")
                        total += 1
                    print(f"  already on Spotify: {best[0]}  {best[1][:70]}")
                    entry = db.catalog_entry(best[0])
                    if entry and (newest is None or entry["position"] < newest[0]):
                        newest = (entry["position"], best[0], best[1])
                else:
                    unmatched.append(t)
            for t in unmatched:
                print(f"  ?? on Spotify but no matching YouTube video: {t[:80]}")
            if newest:
                from .pipeline import apply_upload_boundary

                print(f"\n  Latest episode on Spotify: {newest[2][:80]}")
                newer = [r for r in db.catalog(ch.key) if r["position"] < newest[0]]
                print(f"  {len(newer)} YouTube video(s) are newer; only those will be uploaded (oldest first).")
                if not args.dry_run:
                    db.set_kv(f"upload_after:{ch.key}", newest[1])
                    skipped = apply_upload_boundary(cfg, db, ch)
                    print(f"  Skipped {skipped} older video(s).")
    verb = "Would mark" if args.dry_run else "Marked"
    print(f"\n{verb} {total} video(s) as already uploaded. They will be skipped.")
    return 0


def cmd_schedule(cfg, db, args) -> int:
    from . import schedule

    if args.off:
        schedule.uninstall()
        print("Daily schedule removed.")
        return 0
    hour, minute = (int(x) for x in args.time.split(":"))
    p = schedule.install(cfg.config_path, cfg.logs_dir, hour, minute)
    print(f"Installed {p}: runs daily at {hour:02d}:{minute:02d} (or at next wake/login if the Mac was asleep or off).")
    return 0


def cmd_debug_spotify(cfg, db, args) -> int:
    from .spotify import SpotifyUploader

    ch = cfg.channel(args.channel) if args.channel else cfg.enabled_channels[0]
    with SpotifyUploader(cfg.spotify, cfg.logs_dir, cfg.browser_profile_dir, cfg.selectors_path,
                         headless=False) as up:
        up.debug(ch.spotify_show_id)
    return 0


# ------------------------------------------------------------------ parser
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="youtify", description=__doc__)
    p.add_argument("--config", help=f"config file (default {DEFAULT_CONFIG_PATH})")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("init", help="write a starter config")
    s.add_argument("--force", action="store_true")
    sub.add_parser("doctor", help="check that everything is set up")
    s = sub.add_parser("login", help="log in to Spotify for Creators once (saved)")
    s.add_argument("--from-brave", action="store_true", help="reuse your existing Brave login (quit Brave first)")
    s.add_argument("--profile", help='Brave profile name, e.g. "personal"')
    sub.add_parser("sync", help="refresh the YouTube video lists now")
    sub.add_parser("scan", help="find new video files on this Mac and match them")
    s = sub.add_parser("prepare", help="build episode metadata (YouTube description, chapters, ...)")
    s.add_argument("--limit", type=int, default=10)
    s = sub.add_parser("upload", help="upload ready episodes to Spotify")
    s.add_argument("--limit", type=int, default=5)
    s.add_argument("--dry-run", action="store_true", help="convert files but do not open Spotify")
    s.add_argument("--action", choices=["draft", "publish", "handoff"], help="override finish_action")
    s = sub.add_parser("run", help="full daily job: sync, scan, prepare, upload")
    s.add_argument("--minutes", type=float, help="override max_runtime_minutes")
    s.add_argument("--catch-up", metavar="HH:MM", help="(used by the schedule) run only once a day, after HH:MM")
    sub.add_parser("status", help="overview per channel")
    s = sub.add_parser("list", help="list episodes")
    s.add_argument("--status")
    s.add_argument("--channel")
    s = sub.add_parser("show", help="show one episode and its generated metadata")
    s.add_argument("yt_id")
    s = sub.add_parser("missing", help="YouTube videos that have no local file")
    s.add_argument("--channel")
    s = sub.add_parser("map", help="manually link a file to a YouTube video id")
    s.add_argument("file")
    s.add_argument("yt_id")
    for name, help_ in (("approve", "approve metadata for upload"), ("retry", "retry failed episodes")):
        s = sub.add_parser(name, help=help_)
        s.add_argument("ids", nargs="*")
        s.add_argument("--all", action="store_true")
    s = sub.add_parser("regenerate", help="rebuild metadata (e.g. after editing on YouTube)")
    s.add_argument("ids", nargs="+")
    s.add_argument("--transcript", action="store_true", help="also redo the transcript")
    s = sub.add_parser("skip", help="never upload these episodes")
    s.add_argument("ids", nargs="+")
    s = sub.add_parser("mark-uploaded", help="mark as already on Spotify")
    s.add_argument("ids", nargs="+")
    s = sub.add_parser("skip-existing", help="skip YouTube videos that are already episodes on Spotify")
    s.add_argument("--channel")
    s.add_argument("--dry-run", action="store_true")
    s = sub.add_parser("schedule", help="install the daily macOS schedule")
    s.add_argument("--time", default="10:41", help="HH:MM, 24h (default 10:41)")
    s.add_argument("--off", action="store_true", help="remove the schedule")
    s = sub.add_parser("debug-spotify", help="open Spotify uploader with the Playwright Inspector")
    s.add_argument("--channel")
    return p


COMMANDS = {
    "doctor": cmd_doctor, "login": cmd_login, "sync": cmd_sync, "scan": cmd_scan, "prepare": cmd_prepare,
    "upload": cmd_upload, "run": cmd_run, "status": cmd_status, "list": cmd_list, "show": cmd_show,
    "missing": cmd_missing, "map": cmd_map, "approve": cmd_approve, "retry": cmd_retry,
    "regenerate": cmd_regenerate, "skip": cmd_skip, "mark-uploaded": cmd_mark_uploaded,
    "skip-existing": cmd_skip_existing,
    "schedule": cmd_schedule, "debug-spotify": cmd_debug_spotify,
}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.cmd == "init":
        setup_logging(None, args.verbose)
        return cmd_init(args)
    try:
        cfg = load_config(args.config)
    except (FileNotFoundError, ValueError) as e:
        print(f"Config error: {e}", file=sys.stderr)
        return 2
    cfg.ensure_dirs()
    setup_logging(cfg, args.verbose)
    db = DB(cfg.db_path)
    try:
        return COMMANDS[args.cmd](cfg, db, args)
    except KeyboardInterrupt:
        print("\nInterrupted.")
        return 130
    except RuntimeError as e:
        log.error("%s", e)
        return 1
    finally:
        db.close()
