# youtify

Turns the videos from your YouTube channels (already saved on your Mac) into **Spotify video podcast
episodes**. Each episode gets the same title, description and thumbnail as on YouTube. It is uploaded to the right Spotify show and saved as a **draft**. You
review the draft and click Publish.

Everything runs locally on a MacBook Air M2 with 8 GB of RAM. It costs **$0**: no subscriptions,
no cloud server, no paid API keys.

```
 YouTube (public data)          Your Mac                                   Spotify for Creators
 ───────────────────            ─────────────────────────────────────────  ─────────────────────
 title, description, ─yt-dlp─▶  match files ─▶ copy title + description, ─▶ browser automation
 tags, thumbnail                in your        fit to Spotify's limits        uploads the video and
                                folders                                       metadata as a DRAFT
                                     │                │                            │
                                     └──── SQLite state, review files ─────────────┘
                                                                         you: review ▸ Publish
```

## Why it's built this way

| Need | Choice | Why |
|---|---|---|
| Spotify upload | **Spotify for Creators** (free hosting) + **Playwright** browser automation | Spotify has no public upload API for creators. Its video Distribution API only works through partner hosts, and those cost money. Automating the normal web uploader is the only free option. |
| Video info | **yt-dlp** (no API key) | Gets each video's exact title, full description, chapters, tags and thumbnail. No video is downloaded; your local files are used. (Algrow's video data has no descriptions or chapters, and it can't be called from a script on your Mac, so it isn't used.) |
| Description | **Copied from YouTube** (default) | It's your own text, and it's already written. No AI is installed or run. |
| Optional AI | **Ollama + qwen2.5:3b**, captions or **mlx-whisper** | Only for channels set to `description_source: ai` / `youtube_or_ai`. |
| Video format | **ffmpeg** with Apple's hardware encoder | Converts only files Spotify won't accept (e.g. `.webm`/VP9). It leaves MP4/H.264 untouched. |
| Scheduling | **launchd** + `caffeinate` | Built into macOS. If the Mac was asleep at the scheduled time, the run happens when it wakes. It doesn't need to run 24/7. |
| Low RAM | Steps run one after another | In the default mode there is no AI, so only the browser uses memory. In AI mode the model is unloaded before the browser opens. |

## One-time setup (about 20 minutes)

1. **Spotify:** in [creators.spotify.com](https://creators.spotify.com), create one show per YouTube channel
   (or use your existing ones). Open each show and copy its id from the address bar:
   `https://creators.spotify.com/pod/show/<THIS-IS-THE-ID>/home`
2. **Install:**
   ```bash
   cd youtify
   ./scripts/setup_mac.sh
   ```
   (Add `--with-ai` only if you want the optional AI-written descriptions.)
3. **Configure:** edit `~/Youtify/config.yaml`. Add one block per channel with its YouTube URL, the
   folder(s) holding its videos on your Mac, and the Spotify show id. A footer is optional.
4. **Check:**
   ```bash
   youtify doctor
   ```
5. **Log in to Spotify once.** A browser window opens; log in. The session is saved.
   ```bash
   youtify login
   ```
6. **Try a small batch first:**
   ```bash
   youtify sync
   youtify scan
   youtify prepare --limit 3
   ```
   Read the generated files in `~/Youtify/review/<channel>/`, then upload one:
   ```bash
   youtify upload --limit 1
   ```
   Check the draft on Spotify. If everything looks right:
7. **Turn on the daily automatic run:**
   ```bash
   youtify schedule --time 10:41
   ```

## Daily use

You do **nothing** except review. Each day the scheduled run:

1. refreshes each channel's YouTube video list,
2. finds new video files in your folders and matches them to YouTube videos,
3. prepares metadata for the next episodes (oldest videos first, so the podcast follows your YouTube order),
4. uploads up to `max_uploads_per_run` episodes as Spotify **drafts**,
5. uses the remaining time to prepare more episodes ahead,
6. shows a macOS notification such as "5 uploaded to Spotify. Review drafts and publish."

Then open Spotify for Creators → Episodes → Drafts, check each one, and click **Publish**.

Commands you might use:

```bash
youtify status -v     # per-channel counts, failures and unmatched files
youtify list --status ready
youtify show <youtube_id>
youtify missing       # YouTube videos with no file on your Mac
```

### Want to edit before uploading?
Each episode has `~/Youtify/review/<channel>/<date>_<id>.json`. Whatever is in its `description`
field is what gets uploaded, so edit it freely. To review **every** description before upload, set
`run.require_metadata_approval: true`. Then nothing uploads until you run
`youtify approve <id>` or `youtify approve --all`.

### How fully automatic should it be?
`spotify.finish_action`:
- `draft` (default): uploads and saves a draft; you publish.
- `publish`: publishes immediately, fully hands-off. Use it once you trust the output.
- `handoff`: fills everything in and leaves the browser open for you to review and click Publish.

## Videos you no longer have on your Mac

Set `download_missing: true` on a channel. Every YouTube video with no file on your Mac is then
downloaded from YouTube just before its upload (at most `download_max_height`, default 1080p, H.264
preferred so no re-encoding is needed), uploaded, and the downloaded copy is deleted right away,
whether the upload worked or not. Only one video is on disk at a time. Videos you do have locally
always use your own file.

Already uploaded some episodes by hand? Run `youtify skip-existing` once (add `--dry-run` to only
see what it would do). It reads your show's episode titles from Spotify and marks the matching
YouTube videos as uploaded, so they are never uploaded twice.

## How files are matched to YouTube videos

1. **The most reliable way:** keep the YouTube id in the filename, e.g. `My Video [dQw4w9WgXcQ].mp4`
   (yt-dlp names files this way by default).
2. Otherwise it matches by title. Files exported with the YouTube title as their name work
   well, even with `:`/`|` replaced by look-alike characters. The video's length is used to tell
   similar titles apart (e.g. "Part 1" and "Part 2").
3. Anything it isn't sure about is **never guessed**. It shows up in `youtify status -v` with a best guess.
   Link it by hand:
   ```bash
   youtify map "/path/file.mp4" <youtube_id>
   ```

Files still being copied (changed in the last 2 minutes) are ignored until the copy finishes.

## What goes into each episode

- **Title:** exactly the YouTube title.
- **Description:** exactly the YouTube description, including links, hashtags and any timestamps.
  The only change: Spotify allows 4,000 characters and YouTube allows 5,000, so a longer one is cut
  at a paragraph break and ends with "…".
- **Chapters:** nothing is added; Spotify generates chapters automatically. (Set
  `append_chapters: true` if you ever want your YouTube chapters written into the description.)
- **Thumbnail:** your YouTube thumbnail, uploaded as the episode image if the form offers it.
- **Keywords:** your YouTube tags are saved in the review JSON. Spotify has no keywords field;
  set `keywords_in_description: true` to add them as a "Topics:" line.
- **Footer (optional):** extra text at the end, e.g. `Watch on YouTube: {youtube_url}`.
- **Empty YouTube description:** the title is used, or set `description_source: youtube_or_ai` to let
  the local AI write one only for those videos.

Changed a description on YouTube after it was prepared? Run `youtify regenerate <id>` (before upload).

### Optional: AI-written descriptions
Set `description_source: ai` on a channel to have a local AI write a fresh 90–200 word description
from the video's transcript, plus generated chapters and keywords. Install it with
`./scripts/setup_mac.sh --with-ai`. It runs offline and is free.

## Reliability and safety

- **No duplicate uploads.** Every episode's state is stored in SQLite. If something fails *before* the
  final Save/Publish click, it is retried automatically next run (3 attempts). If it fails *after* that
  click and success can't be confirmed, it stops and asks you. Check Spotify, then run
  `youtify mark-uploaded <id>` or `youtify retry <id>`.
- **Screenshots on failure:** `~/Youtify/logs/failures/` gets a screenshot and an HTML snapshot.
- **Logs:** `~/Youtify/logs/youtify.log`
- **Only one run at a time** (lock file). Every run stops itself at `max_runtime_minutes`.
- Your original video files are never modified or deleted. Converted copies are temporary.

## Rough throughput on an M2 Air (8 GB)

- Metadata: a few seconds per episode (just a YouTube lookup). AI mode takes minutes per episode.
- Upload: limited by your internet upload speed (1 GB takes about 7 minutes at 20 Mbps).
- A 4-hour daily window is mostly upload time: 5–15 episodes depending on file sizes. Raise
  `max_uploads_per_run` gradually. Ramping up slowly is kinder to Spotify's systems than dumping
  hundreds of episodes on day one.

## Troubleshooting

- **"Could not find 'title'/'description'/… on the page".** Spotify changed its web page. Run
  `youtify debug-spotify`, use **Pick locator** in the inspector, and put the new selector in
  `~/Youtify/spotify_selectors.yaml`. No code change is needed. Example:
  ```yaml
  title: ["css:input[data-testid='episode-title']"]
  save_draft_button: ["role:button:/save draft/i"]
  ```
  Selector names and defaults are listed at the top of `youtify/spotify.py`.
- **"Not logged in".** Run `youtify login` again (Spotify sessions expire every few weeks).
- **YouTube errors / HTTP 429.** Update yt-dlp (`.venv/bin/pip install -U yt-dlp`). If it continues, set
  `run.youtube_cookies_from_browser: chrome`.
- **The scheduled run can't see my files.** macOS protects Desktop, Documents and Downloads. Keep
  videos in `~/Movies/...`, or give the venv's Python *Full Disk Access* in System Settings → Privacy &
  Security. External drives must be plugged in during the run.
- **AI mode feels slow.** Use the default `qwen2.5:3b` model and close heavy apps during the run, or
  `llm.model: qwen2.5:7b` for better writing if you have about 5 GB free RAM.

## A note on terms of use

This automates *your* actions on *your* account to upload content *you* own, at a low daily rate.
That is ordinary personal automation. Spotify doesn't publish rules on browser automation, so keep
the volume modest. Draft mode means a person (you) approves everything before it goes public.

## Development

```bash
pip install -e '.[dev]'
pytest
```
The tests cover matching, caption parsing, YouTube-description copying and trimming, chapter rules,
the metadata pipeline in every description mode (with a fake LLM), ffmpeg conversion, the daily-run logic, and the full Spotify upload flow driven in real
Chromium against a local mock of the upload wizard.
