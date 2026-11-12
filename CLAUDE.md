# youtify — handoff notes (from a Claude chat session, 2026-11-11)

Owner: Ayush. Not technical: do the work for him, give him one-line commands to paste only when
something must run in his Terminal (e.g. Spotify login), and keep explanations short.

## What this is
Local automation: YouTube channel videos -> Spotify for Creators video-podcast episodes (saved as DRAFTS).
- Code: this folder (`~/Movies/tube2pod` — folder name kept on purpose; renaming it breaks the
  .venv), package `youtify/`, installed editable in `.venv`, command `youtify`
  (symlink in `~/.local/bin`). Renamed from tube2pod on 2026-11-12 (data moved ~/Tube2Pod -> ~/Youtify,
  sqlite paths updated). The old `tube2pod` command no longer exists.
- Members-only videos: detected from the channel list (`availability`) or the yt-dlp error
  (youtube.MembersOnlyError) -> status skipped, kv `members_only:<id>`, no episode number used,
  and the batch moves on to the next video.
- Ep 73 (TpVVcz2viW8) uploaded as draft 2026-11-12; next_episode_number is 74.
- Daily schedule installed 2026-11-12: launchd `com.youtify.daily` at 08:00, max_uploads_per_run: 1
  (Ayush wants 1/day). Output in ~/Youtify/logs/launchd.*.log and youtify.log.
- Code is in a PRIVATE GitHub repo: https://github.com/AkdasHayat/youtify (branch main). Local git
  identity = AkdasHayat + GitHub noreply email. Data folder ~/Youtify is never committed.
- Data/config: `~/Youtify/` (config.yaml, state.sqlite3, review/, cache/, logs/youtify.log,
  logs/failures/*.png|html screenshots, work/downloads/, browser-profile/).

## Channel config (~/Youtify/config.yaml)
- YouTube: https://www.youtube.com/@Gay_Timeline (73 videos) -> Spotify show "Gay Stories",
  id 033Jd5n5gMRRxZuF8JEWOF. No local video files.
- download_missing: true, download_max_height: 1080 (never higher). Downloaded just before upload,
  deleted right after (success or failure).
- description_source: youtube (exact YouTube title + description, no AI).
- season_number: 1, next_episode_number: 72 (numbers stored per video in sqlite kv
  `episode_number:<yt_id>`, counter `next_episode_number:gay-timeline`), episode_type: full.
- Upload boundary: only videos NEWER than the latest Spotify episode ("Inside the Secret Gay World of
  the British Merchant Navy", yt Vzb_tNmKB0g, kv `upload_after:gay-timeline`). 24 newer videos;
  45 older are skipped. Uploads go oldest-first.
- spotify.browser_path = Brave, brave_profile = Personal.

## Features added this session (all have tests; `pytest` = 37 passing)
- youtube.download_video / delete_download / ensure_jpg_thumbnail (webp -> jpg via ffmpeg).
- pipeline.queue_downloads, upload_boundary/apply_upload_boundary, episode_number_for.
- `youtify skip-existing [--dry-run]`: reads Spotify episode titles (embed page + open.spotify.com),
  marks matches uploaded, sets the upload boundary.
- `youtify login --from-brave [--profile]`: copies a Brave profile (see warning below).
- spotify.py for the CURRENT Spotify wizard (Upload > Details > Review):
  description pasted via synthetic paste event (Slate editor; typing scrambled paragraph order);
  fills episode type radio `input[name=podcastEpisodeType]`, `#season-number`, `#episode-number`;
  thumbnail goes into 16:9 `#thumbnail-list` (NOT the square "Episode art" input);
  busy detection uses `#upload-status` text ("Uploading (x%)" vs "Preview ready!") — the video
  player's seek bar has role=progressbar, so never treat that as busy;
  draft = reach Review, wait for "Preview ready!", click X (`button.e-101081-dialog-full-screen__close-button`);
  there is no "Save draft" button. Once the file is sent Spotify already created the episode, so
  failures after that raise UncertainUploadError (no auto-retry -> avoids duplicates).
  A `<id>-details.png` screenshot of the Details step is saved every upload for checking.

## Update 2026-11-11 (evening)
- Descriptions now strip YouTube *bold*/_italic_ markers, lines with YouTube links or about
  memberships/subscribing, and hashtags (metadata.strip_youtube_formatting / strip_youtube_extras).
  Ayush wants this. Episode 72 on Spotify still shows the stars; he fixes that by hand.
- Ep 72 confirmed `uploaded` in sqlite. Ep 73 (TpVVcz2viW8) is `ready`, its review JSON already rebuilt.
- Ep 73 upload failed at the DOWNLOAD step: YouTube "Sign in to confirm you're not a bot" (IP limit;
  yt-dlp 2026.10.26 is latest). Nothing sent to Spotify. Ayush chose to wait and retry 2026-11-12
  (`youtify upload --limit 1`). If it still fails, next option is youtube_cookies_from_browser: brave
  (he was warned this may risk the Google account).

## Status at handoff
- Episode 72 (kNady9_j-QM, "The Secret Gay Magazines Disguised as Bodybuilding Books") uploaded;
  it hung on Review (seek-bar bug, now fixed). Ayush was told to click X, Ctrl+C, then
  `youtify mark-uploaded kNady9_j-QM`, and to add `~/Youtify/Episode 72 thumbnail.jpg` by hand
  (that upload ran before the webp thumbnail fix). Verify in state.sqlite3.
- Mv8orj5ia7o ("45 Real Gay Celebrity Couples") must NOT be uploaded (skipped); 1–2 leftover
  drafts of it may exist on Spotify — Ayush should delete them.
- NEXT: test episode 73 with `youtify prepare --limit 1 && youtify upload --limit 1`, check the
  details screenshot (thumbnail, Full, S1, E73) and that work/downloads is empty; then
  `youtify schedule --time HH:MM` for the daily run (5 uploads/day). Later maybe
  finish_action: publish.

## Warnings
- macOS XProtect showed "Malicious Script Blocked" once when youtify launched Brave with a COPIED
  Brave profile (looks like cookie-stealer behaviour). Not malware, but the copy approach was a mistake.
  `~/Youtify/browser-profile` still contains a copy of his Brave "Personal" cookies/saved passwords;
  recommended: replace with a fresh youtify-only profile (log in once, Spotify email code) and turn
  OFF Full Disk Access for Terminal afterwards. Ayush declined an automatic delete; ask before deleting.
- Spotify sessions expire every few weeks -> re-login.
