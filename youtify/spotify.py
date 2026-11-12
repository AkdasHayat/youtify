"""Upload episodes to Spotify for Creators with browser automation (Playwright).

Spotify has no public upload API for self-hosted shows, so this drives the normal web uploader
in a dedicated Chrome profile that stays logged in (log in once with `youtify login`).

Web UIs change. Every element is found through a list of candidate selectors which you can
override in <data_dir>/spotify_selectors.yaml without touching code. Failures save a screenshot
and HTML snapshot in <data_dir>/logs/failures/.

Selector syntax (one per list item, first visible match wins):
    css:<css selector>          role:<aria role>:<name or /regex/flags>
    label:<text or /regex/>     text:<text or /regex/>      placeholder:<text or /regex/>
"""
from __future__ import annotations

import logging
import os
import platform
import re
import time
from dataclasses import dataclass
from pathlib import Path

import yaml

log = logging.getLogger(__name__)

CREATORS_HOME = "https://creators.spotify.com/"

DEFAULT_SELECTORS: dict[str, list[str]] = {
    "file_input": ["css:input[type=file]:not([accept*='image'])", "css:input[type=file]"],
    "select_file_button": ["role:button:/select (a )?file|browse|upload/i"],
    "title": ["css:input[name='title']", "label:/^title/i", "css:#title-input", "placeholder:/title/i"],
    "description": [
        "css:div[contenteditable='true'][role='textbox']",
        "css:[name='description'][contenteditable='true']",
        "css:textarea[name='description']",
        "css:div[contenteditable='true']",
        "label:/description/i",
    ],
    "thumbnail_input": ["css:section:has(#thumbnail-title) input[type=file]",
                        "css:#thumbnail-list input[type=file]"],
    "thumbnail_upload_button": ["css:#thumbnail-list > div:first-child button",
                                "css:#thumbnail-list > div:first-child [role=button]",
                                "css:section:has(#thumbnail-title) [aria-label*='upload' i]",
                                "css:#thumbnail-list > div:first-child"],
    "dialog_confirm_button": ["css:[role=dialog] button:text-matches('^(save|apply|done|confirm|upload|crop|use)', 'i')"],
    "season_number": ["css:#season-number", "css:input[name='podcastSeasonNumber']", "label:/season number/i"],
    "episode_number": ["css:#episode-number", "css:input[name='podcastEpisodeNumber']", "label:/episode number/i"],
    "next_button": ["role:button:/^next$/i"],
    "save_draft_button": ["role:button:/save (as )?draft/i", "text:/^save (as )?draft$/i"],
    "close_button": ["css:button.e-101081-dialog-full-screen__close-button", "css:button[aria-label='Close']",
                     "role:button:/^close$/i"],
    "review_page": ["text:/^review and publish$/i", "css:form#review-form"],
    "upload_done": ["css:#upload-status:has-text('ready')", "text:/^preview ready!?$/i"],
    "draft_confirm_button": ["role:button:/save( as)? draft|keep( as)? draft|save (and|&) (exit|close)|^save$/i"],
    "publish_now_option": ["role:radio:/^now$/i", "label:/^now$/i"],
    "publish_button": ["role:button:/^publish( now)?$/i"],
    "busy_indicator": ["css:[role=progressbar]", "text:/^\\s*uploading/i"],
    "error_indicator": ["text:/upload failed|something went wrong|file (is )?not supported/i"],
    "login_form": ["css:input#login-username", "css:input[name=username]", "text:/log in to spotify/i"],
}

SUCCESS_TEXT = re.compile(r"(draft (was )?saved|saved as draft|episode (is )?(published|live)|"
                          r"published|scheduled)", re.I)


class NotLoggedIn(RuntimeError):
    pass


class RetryableUploadError(RuntimeError):
    """Failed before anything could have been saved on Spotify: safe to retry automatically."""


class UncertainUploadError(RuntimeError):
    """Failed after the final click: an episode may exist on Spotify. Needs a human check."""


@dataclass
class UploadJob:
    yt_id: str
    show_id: str
    video_path: Path
    title: str
    description: str
    thumbnail: Path | None = None
    season_number: int | None = None
    episode_number: int | None = None
    episode_type: str = "full"


def load_selectors(path: Path) -> dict[str, list[str]]:
    sel = {k: list(v) for k, v in DEFAULT_SELECTORS.items()}
    if path.exists():
        override = yaml.safe_load(path.read_text()) or {}
        for k, v in override.items():
            sel[k] = [v] if isinstance(v, str) else list(v)
    return sel


def _pattern(value: str):
    m = re.fullmatch(r"/(.*)/([a-z]*)", value, re.S)
    if not m:
        return value
    flags = re.I if "i" in m.group(2) else 0
    return re.compile(m.group(1), flags)


def resolve(page, spec: str):
    kind, _, rest = spec.partition(":")
    if kind == "css":
        return page.locator(rest)
    if kind == "role":
        role, _, name = rest.partition(":")
        return page.get_by_role(role, name=_pattern(name)) if name else page.get_by_role(role)
    if kind == "label":
        return page.get_by_label(_pattern(rest))
    if kind == "text":
        return page.get_by_text(_pattern(rest))
    if kind == "placeholder":
        return page.get_by_placeholder(_pattern(rest))
    return page.locator(spec)


class SpotifyUploader:
    def __init__(self, cfg, logs_dir: Path, profile_dir: Path, selectors_path: Path,
                 headless: bool | None = None):
        self.cfg = cfg  # SpotifyConfig
        self.logs_dir = logs_dir
        self.profile_dir = profile_dir
        self.selectors = load_selectors(selectors_path)
        self.headless = cfg.headless if headless is None else headless
        self._pw = self._ctx = self.page = None

    # ---------- lifecycle ----------
    def __enter__(self):
        from playwright.sync_api import sync_playwright

        self._pw = sync_playwright().start()
        extra = {}
        exe = os.environ.get("YOUTIFY_CHROME_PATH") or getattr(self.cfg, "browser_path", None)
        if exe:  # e.g. Brave. Use the real macOS keychain so cookies copied from that browser work.
            if not Path(exe).exists():
                raise RuntimeError(f"Browser not found: {exe} (fix spotify.browser_path in config.yaml)")
            extra["executable_path"] = exe
            extra["ignore_default_args"] = ["--use-mock-keychain", "--enable-automation"]
        self._ctx = self._pw.chromium.launch_persistent_context(
            str(self.profile_dir), headless=self.headless, viewport={"width": 1400, "height": 950},
            args=["--disable-blink-features=AutomationControlled"], **extra,
        )
        self._ctx.set_default_timeout(20_000)
        self.page = self._ctx.pages[0] if self._ctx.pages else self._ctx.new_page()
        return self

    def __exit__(self, *exc):
        try:
            if self._ctx:
                self._ctx.close()
        finally:
            if self._pw:
                self._pw.stop()

    # ---------- helpers ----------
    def find(self, name: str, timeout: float = 10, visible: bool = True):
        """First matching candidate for selector group `name`, or None."""
        deadline = time.monotonic() + timeout
        while True:
            for spec in self.selectors.get(name, []):
                loc = resolve(self.page, spec)
                try:
                    count = loc.count()
                except Exception:  # noqa: BLE001 (page navigating)
                    count = 0
                for i in range(min(count, 5)):
                    el = loc.nth(i)
                    if not visible or el.is_visible():
                        return el
            if time.monotonic() >= deadline:
                return None
            time.sleep(0.5)

    def require(self, name: str, timeout: float = 20):
        el = self.find(name, timeout=timeout)
        if el is None:
            raise RetryableUploadError(
                f"Could not find '{name}' on {self.page.url}. Spotify's page may have changed; "
                f"adjust it in spotify_selectors.yaml (see logs/failures for a screenshot)."
            )
        return el

    def snapshot(self, label: str) -> Path | None:
        d = self.logs_dir / "failures"
        d.mkdir(parents=True, exist_ok=True)
        base = d / f"{time.strftime('%Y%m%d-%H%M%S')}-{label}"
        try:
            self.page.screenshot(path=str(base.with_suffix(".png")), full_page=True)
            base.with_suffix(".html").write_text(self.page.content())
            return base.with_suffix(".png")
        except Exception:  # noqa: BLE001
            return None

    # ---------- login ----------
    def is_logged_in(self, show_id: str) -> bool:
        self.page.goto(self.cfg.wizard_url.format(show_id=show_id), wait_until="domcontentloaded")
        self.page.wait_for_timeout(4000)
        url = self.page.url
        if "accounts.spotify.com" in url or "/login" in url:
            return False
        return self.find("login_form", timeout=1) is None

    def interactive_login(self) -> None:
        self.page.goto(CREATORS_HOME, wait_until="domcontentloaded")
        print("\nA browser window opened. Log in to Spotify for Creators there.")
        print("When you can see your podcast dashboard, come back here and press Enter.")
        input()

    # ---------- upload ----------
    def upload(self, job: UploadJob, finish_action: str) -> str:
        """Uploads one episode. Returns the resulting page URL."""
        self._file_sent = False
        try:
            self._fill_wizard(job)
        except Exception as e:  # noqa: BLE001
            shot = self.snapshot(f"{job.yt_id}-fill")
            if isinstance(e, NotLoggedIn):
                raise
            if self._file_sent:
                # Spotify creates the episode as soon as the file is sent; keep it as a draft
                # instead of retrying (which would create a duplicate).
                try:
                    self._close_as_draft_quick()
                except Exception:  # noqa: BLE001
                    pass
                raise UncertainUploadError(
                    f"{type(e).__name__}: {e}. A partial draft may exist on Spotify (screenshot: {shot})") from e
            if isinstance(e, RetryableUploadError):
                raise
            raise RetryableUploadError(f"{type(e).__name__}: {e}") from e
        try:
            return self._finish(job, finish_action)
        except Exception as e:  # noqa: BLE001
            shot = self.snapshot(f"{job.yt_id}-finish")
            if isinstance(e, (RetryableUploadError, UncertainUploadError)):
                raise
            raise UncertainUploadError(f"{type(e).__name__}: {e} (screenshot: {shot})") from e

    def _fill_wizard(self, job: UploadJob) -> None:
        page = self.page
        page.goto(self.cfg.wizard_url.format(show_id=job.show_id), wait_until="domcontentloaded")
        page.wait_for_timeout(3000)
        if "accounts.spotify.com" in page.url or self.find("login_form", timeout=1):
            raise NotLoggedIn("Spotify session expired. Run `youtify login`.")

        log.info("Selecting file %s", job.video_path.name)
        file_input = self.find("file_input", timeout=15, visible=False)
        if file_input is not None:
            file_input.set_input_files(str(job.video_path))
            self._file_sent = True
        else:
            button = self.require("select_file_button")
            with page.expect_file_chooser() as fc:
                button.click()
            fc.value.set_files(str(job.video_path))
            self._file_sent = True

        title = self.require("title", timeout=180)  # form appears once the upload starts
        title.fill(job.title)
        if title.input_value().strip() != job.title.strip():
            raise RetryableUploadError("Title did not stick in the form")

        self._fill_description(job.description)

        self._fill_episode_fields(job)

        if job.thumbnail and self.cfg.upload_thumbnail:
            if not self._set_thumbnail(job.thumbnail):
                self.snapshot(f"{job.yt_id}-thumbnail")
                raise RetryableUploadError("Could not set the YouTube thumbnail (screenshot saved)")
        self.snapshot(f"{job.yt_id}-details")  # what the Details step looked like (for checking)

    def _fill_number(self, group: str, value: int) -> None:
        box = self.require(group)
        box.scroll_into_view_if_needed()
        box.fill(str(value))
        box.press("Tab")
        if box.input_value().strip() != str(value):
            raise RetryableUploadError(f"{group.replace('_', ' ').capitalize()} did not stick")

    def _fill_episode_fields(self, job: UploadJob) -> None:
        page = self.page
        etype = (job.episode_type or "full").lower()
        radio = page.locator(f"input[name='podcastEpisodeType'][value='{etype}']")
        if radio.count():
            if not radio.first.is_checked():
                label = page.locator(f"label[for='{etype}-episode'], label:has(input[name='podcastEpisodeType'][value='{etype}'])")
                (label.first if label.count() else page.get_by_text(etype.capitalize(), exact=True).first).click()
                page.wait_for_timeout(300)
            if not radio.first.is_checked():
                radio.first.check(force=True)
            if not radio.first.is_checked():
                raise RetryableUploadError(f"Could not select episode type '{etype}'")
        else:
            log.warning("Episode type field not found; leaving Spotify's default")
        if job.season_number is not None:
            self._fill_number("season_number", job.season_number)
        if job.episode_number is not None:
            self._fill_number("episode_number", job.episode_number)
        log.info("  episode type %s, season %s, episode %s", etype, job.season_number, job.episode_number)

    def _thumb_state(self) -> tuple[int, str]:
        try:
            return self.page.evaluate("""() => {
                const l = document.querySelector('#thumbnail-list');
                if (!l) return [0, ''];
                const imgs = l.querySelectorAll('img').length;
                const checked = l.querySelector('[aria-checked=true]');
                return [imgs, checked ? (checked.getAttribute('aria-label') || '') + (checked.querySelector('img') ? ':img' : '') : ''];
            }""")
        except Exception:  # noqa: BLE001
            return (0, "")

    def _set_thumbnail(self, path: Path) -> bool:
        """Upload the YouTube thumbnail into the 16:9 'Thumbnails' list (not the square episode art)."""
        page = self.page
        try:
            page.locator("#thumbnail-list").wait_for(timeout=120_000)
        except Exception:  # noqa: BLE001
            log.warning("Thumbnails section not found")
            return False
        before = self._thumb_state()
        sent = False
        inp = self.find("thumbnail_input", timeout=2, visible=False)
        if inp is not None:
            inp.set_input_files(str(path))
            sent = True
        else:
            deadline = time.monotonic() + 120  # the upload tile shows "..." until frames are generated
            while not sent and time.monotonic() < deadline:
                for spec in self.selectors["thumbnail_upload_button"]:
                    loc = resolve(page, spec)
                    if not loc.count() or not loc.first.is_visible():
                        continue
                    try:
                        with page.expect_file_chooser(timeout=4000) as fc:
                            loc.first.click()
                        fc.value.set_files(str(path))
                        sent = True
                        break
                    except Exception:  # noqa: BLE001
                        inp = self.find("thumbnail_input", timeout=1, visible=False)
                        if inp is not None:
                            inp.set_input_files(str(path))
                            sent = True
                            break
                if not sent:
                    time.sleep(3)
        if not sent:
            log.warning("No way to upload a thumbnail was found")
            return False
        # Possible crop/confirm dialog.
        for _ in range(3):
            btn = self.find("dialog_confirm_button", timeout=4)
            if btn is None:
                break
            btn.click()
            page.wait_for_timeout(1500)
        for _ in range(20):
            after = self._thumb_state()
            if after[0] > before[0] or (after[1] and after[1] != before[1]):
                log.info("  thumbnail set")
                return True
            page.wait_for_timeout(1000)
        log.warning("Thumbnail sent but no change seen in the Thumbnails list")
        return False

    @staticmethod
    def _norm(t: str) -> str:
        t = t.replace("\ufeff", "").replace("\u200b", "").replace("\xa0", " ")
        return "\n".join(line.strip() for line in t.splitlines() if line.strip())

    def _description_ok(self, box, text: str) -> bool:
        try:
            typed = self._norm(box.inner_text())
        except Exception:  # noqa: BLE001
            return False
        return typed == self._norm(text)

    def _clear_editor(self, box) -> None:
        box.click()
        mod = "Meta" if platform.system() == "Darwin" else "Control"
        self.page.keyboard.press(f"{mod}+A")
        self.page.keyboard.press("Backspace")
        self.page.wait_for_timeout(300)

    def _fill_description(self, text: str) -> None:
        box = self.require("description")
        tag = box.evaluate("e => e.tagName.toLowerCase()")
        if tag in ("textarea", "input"):
            box.fill(text)
            return
        # 1) Paste, like a person would. Rich editors (Spotify uses Slate) keep paragraph order.
        self._clear_editor(box)
        box.evaluate(
            """(el, text) => {
                const dt = new DataTransfer();
                dt.setData('text/plain', text);
                el.dispatchEvent(new ClipboardEvent('paste', {clipboardData: dt, bubbles: true, cancelable: true}));
            }""",
            text,
        )
        self.page.wait_for_timeout(800)
        if self._description_ok(box, text):
            return
        # 2) Fallback: type it with real key presses, line by line.
        log.info("Paste did not work in the description editor; typing it instead")
        self._clear_editor(box)
        lines = text.split("\n")
        for i, line in enumerate(lines):
            if line:
                self.page.keyboard.type(line)
            if i < len(lines) - 1:
                self.page.keyboard.press("Enter")
        self.page.wait_for_timeout(500)
        if not self._description_ok(box, text):
            raise RetryableUploadError("Description did not stick in the editor")

    def _upload_busy(self) -> bool:
        """True while Spotify is still uploading/processing. Uses Spotify's own status text
        ("Uploading (40%)" ... "Preview ready!"); a video player's seek bar is NOT a busy signal."""
        try:
            status = self.page.evaluate(
                "() => { const e = document.querySelector('#upload-status'); return e ? e.innerText : null; }")
        except Exception:  # noqa: BLE001
            status = None
        if status is not None:
            return not re.search(r"ready|complete|uploaded|processed", status, re.I)
        return self.find("busy_indicator", timeout=0) is not None

    def _on_review(self) -> bool:
        return (self.find("review_page", timeout=0) is not None
                or self.find("publish_button", timeout=0) is not None)

    def _wait_until_uploaded(self, button_group: str) -> None:
        """Wait for the video upload/processing to finish (no progress bar, final button enabled)."""
        deadline = time.monotonic() + self.cfg.upload_timeout_minutes * 60
        clean_checks = 0
        last_log = 0.0
        while time.monotonic() < deadline:
            if self.find("error_indicator", timeout=0):
                raise RetryableUploadError("Spotify reported an upload error")
            busy = self._upload_busy()
            button = self.find(button_group, timeout=0)
            if not busy and button is not None and button.is_enabled():
                clean_checks += 1
                if clean_checks >= 3:
                    return
            else:
                clean_checks = 0
            if time.monotonic() - last_log > 60:
                log.info("  still uploading/processing…")
                last_log = time.monotonic()
            time.sleep(3)
        raise RetryableUploadError(f"Upload did not finish within {self.cfg.upload_timeout_minutes} min")

    def _finish(self, job: UploadJob, action: str) -> str:
        page = self.page
        if action == "handoff":
            print(f"\n>>> Review '{job.title}' in the browser, then publish or save it yourself.")
            input(">>> Press Enter here when done (Ctrl+C to abort)… ")
            return page.url

        if action == "draft":
            # Current Spotify wizard: there is no "Save draft" button. Going to the Review step and
            # closing the wizard with X keeps the episode as a draft.
            self._go_to_review()
            return self._close_as_draft()
        else:  # publish
            self._wait_until_uploaded("next_button")
            self.require("next_button").click()
            page.wait_for_timeout(2000)
            now = self.find("publish_now_option", timeout=5)
            if now is not None:
                now.check() if now.get_attribute("type") == "radio" else now.click()
            target = "publish_button"

        self._wait_until_uploaded(target)
        before = page.url
        hits_before = self._success_hits()
        log.info("Clicking final button (%s)", target)
        self.require(target).click()  # ---- point of no return ----

        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            page.wait_for_timeout(1500)
            if page.url != before and "wizard" not in page.url:
                return page.url
            if self._success_hits() > hits_before:
                return page.url
        raise UncertainUploadError("Clicked the final button but could not confirm success")

    def _go_to_review(self) -> None:
        """Click Next until the Review step is shown (also fine if we are already there)."""
        deadline = time.monotonic() + self.cfg.upload_timeout_minutes * 60
        last_log = 0.0
        clicks = 0
        while time.monotonic() < deadline:
            if self._on_review():
                return
            if self.find("error_indicator", timeout=0):
                raise UncertainUploadError("Spotify reported an upload error")
            nxt = self.find("next_button", timeout=0)
            if nxt is not None and nxt.is_enabled() and not self._upload_busy() and clicks < 4:
                nxt.click()
                clicks += 1
                self.page.wait_for_timeout(2500)
                continue
            if time.monotonic() - last_log > 60:
                log.info("  still uploading/processing…")
                last_log = time.monotonic()
            time.sleep(3)
        raise UncertainUploadError("Could not reach the Review step of the wizard")

    def _close_as_draft(self) -> str:
        page = self.page
        # Upload fully processed before leaving: "Preview ready!" shown and Publish enabled.
        deadline = time.monotonic() + self.cfg.upload_timeout_minutes * 60
        last_log = 0.0
        while self.find("upload_done", timeout=0) is None:
            if self.find("error_indicator", timeout=0):
                raise UncertainUploadError("Spotify reported an upload error on the Review step")
            if time.monotonic() > deadline:
                raise UncertainUploadError("Video was still processing when the time limit was reached")
            if time.monotonic() - last_log > 60:
                log.info("  waiting for Spotify to finish processing the video…")
                last_log = time.monotonic()
            time.sleep(3)
        self._wait_until_uploaded("publish_button")
        log.info("Closing the wizard with X (Spotify keeps it as a draft)")
        self.require("close_button").click()  # ---- point of no return ----
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            page.wait_for_timeout(1500)
            confirm = self.find("draft_confirm_button", timeout=0)
            if confirm is not None:
                log.info("  confirming: %s", (confirm.inner_text() or "").strip())
                confirm.click()
                continue
            if "/wizard" not in page.url:
                return page.url
        raise UncertainUploadError("Closed the wizard but could not confirm the draft was saved")

    def _close_as_draft_quick(self) -> None:
        btn = self.find("close_button", timeout=3)
        if btn is not None:
            btn.click()
            self.page.wait_for_timeout(2000)
            confirm = self.find("draft_confirm_button", timeout=3)
            if confirm is not None:
                confirm.click()
                self.page.wait_for_timeout(2000)

    def _success_hits(self) -> int:
        try:
            return len(SUCCESS_TEXT.findall(self.page.inner_text("body") or ""))
        except Exception:  # noqa: BLE001
            return 0

    def debug(self, show_id: str) -> None:
        """Open the wizard with the Playwright Inspector, to find/fix selectors."""
        self.page.goto(self.cfg.wizard_url.format(show_id=show_id), wait_until="domcontentloaded")
        print("Playwright Inspector opened. Use 'Pick locator' to inspect elements; close it to exit.")
        self.page.pause()


# ---------------------------------------------------------------- existing episodes
def _titles_from_embed(show_id: str) -> list[str]:
    """Episode titles from the public Spotify embed page (no login needed)."""
    import json
    import urllib.request

    req = urllib.request.Request(f"https://open.spotify.com/embed/show/{show_id}",
                                 headers={"User-Agent": "Mozilla/5.0"})
    html = urllib.request.urlopen(req, timeout=30).read().decode("utf-8", "replace")
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
    if not m:
        return []
    titles: list[str] = []

    def walk(o):
        if isinstance(o, dict):
            uri = str(o.get("uri") or "")
            t = o.get("title") or o.get("name")
            if uri.startswith("spotify:episode:") and isinstance(t, str):
                titles.append(t)
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    walk(json.loads(m.group(1)))
    return titles


def _titles_from_show_page(up: "SpotifyUploader", show_id: str) -> list[str]:
    """Episode titles from open.spotify.com, scrolling until the list stops growing."""
    page = up.page
    page.goto(f"https://open.spotify.com/show/{show_id}", wait_until="domcontentloaded")
    page.wait_for_timeout(5000)
    seen: dict[str, str] = {}
    stable = 0
    for _ in range(300):
        for btn in ("Show more episodes", "See all episodes", "Show more", "Load more"):
            try:
                b = page.get_by_role("button", name=btn)
                if b.count() and b.first.is_visible():
                    b.first.click()
                    page.wait_for_timeout(1500)
            except Exception:  # noqa: BLE001
                pass
        links = page.eval_on_selector_all(
            "a[href*='/episode/']",
            "els => els.map(e => [e.getAttribute('href'), (e.innerText || e.getAttribute('aria-label') || '').trim()])",
        )
        before = len(seen)
        for href, text in links:
            if href and text and href not in seen:
                seen[href] = text.split("\n")[0].strip()
        stable = stable + 1 if len(seen) == before else 0
        if stable >= 4:
            break
        page.mouse.wheel(0, 4000)
        page.keyboard.press("End")
        page.wait_for_timeout(1200)
    return list(seen.values())


def existing_episode_titles(up: "SpotifyUploader", show_id: str) -> list[str]:
    titles: list[str] = []
    try:
        titles += _titles_from_embed(show_id)
    except Exception as e:  # noqa: BLE001
        log.warning("Could not read the Spotify embed page: %s", e)
    try:
        titles += _titles_from_show_page(up, show_id)
    except Exception as e:  # noqa: BLE001
        log.warning("Could not read the Spotify show page: %s", e)
    out, seen = [], set()
    for t in titles:
        if t and t not in seen:
            seen.add(t)
            out.append(t)
    return out


# ---------------------------------------------------------------- reuse an existing Brave login
BRAVE_APP = "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser"
BRAVE_DATA = Path("~/Library/Application Support/BraveSoftware/Brave-Browser").expanduser()
_SKIP_DIRS = {"Cache", "Code Cache", "GPUCache", "DawnCache", "DawnGraphiteCache", "DawnWebGPUCache",
              "GrShaderCache", "ShaderCache", "CacheStorage", "ScriptCache", "blob_storage",
              "Service Worker", "File System", "IndexedDB", "optimization_guide_model_store"}


def brave_running() -> bool:
    import subprocess

    return subprocess.run(["pgrep", "-x", "Brave Browser"], capture_output=True).returncode == 0


def _brave_data_dir() -> Path:
    base = Path("~/Library/Application Support/BraveSoftware").expanduser()
    for name in ("Brave-Browser", "Brave-Browser-Beta", "Brave-Browser-Nightly"):
        if (base / name).is_dir():
            return base / name
    return BRAVE_DATA


def brave_profiles() -> dict[str, str]:
    """{display name: folder name} of the Brave profiles, e.g. {"Personal": "Profile 1"}."""
    import json

    data = _brave_data_dir()
    out: dict[str, str] = {}
    try:
        cache = json.loads((data / "Local State").read_text(errors="replace"))["profile"]["info_cache"]
        out.update({(v.get("name") or k): k for k, v in cache.items()})
    except Exception as e:  # noqa: BLE001
        log.info("Could not read Brave's Local State (%s); checking profile folders", e)
    # Also read each profile folder's own Preferences (works even if Local State is unreadable).
    try:
        entries = sorted(data.iterdir()) if data.is_dir() else []
    except PermissionError:
        raise PermissionError(
            "macOS is blocking Terminal from reading Brave's data. Open System Settings > Privacy & "
            "Security > Full Disk Access, turn ON Terminal, quit Terminal (Cmd+Q), reopen it and run "
            "the command again.") from None
    if entries:
        for d in entries:
            prefs = d / "Preferences"
            if not (d.is_dir() and (d.name == "Default" or d.name.startswith("Profile")) and prefs.exists()):
                continue
            try:
                name = json.loads(prefs.read_text(errors="replace")).get("profile", {}).get("name") or d.name
            except Exception:  # noqa: BLE001
                name = d.name
            if d.name not in out.values():
                out[name] = d.name
    return out


def copy_brave_profile(dest: Path, profile: str | None = None) -> str:
    """Copy a Brave profile (cookies = your logins) into youtify's own browser profile.

    `profile` is the name shown in Brave (e.g. "personal") or its folder name; default: last used.
    Brave itself is never changed. Returns the name of the copied profile.
    """
    import json
    import shutil

    data = _brave_data_dir()
    if not data.exists():
        raise RuntimeError(f"Brave data not found at {data}")
    local_state = data / "Local State"
    profiles = brave_profiles()
    if profile:
        by_lower = {k.lower(): v for k, v in profiles.items()}
        folder = by_lower.get(profile.lower()) or (profile if (data / profile).is_dir() else None)
        if not folder:
            found = ", ".join(f"{k} ({v})" for k, v in profiles.items()) or "none"
            try:
                listing = ", ".join(sorted(p.name for p in data.iterdir()))
            except Exception as e:  # noqa: BLE001
                listing = f"cannot list folder: {e}"
            raise RuntimeError(f"No Brave profile named '{profile}'. Found: {found}. "
                               f"Brave folder {data} contains: {listing}")
        name = profile
    else:
        try:
            folder = json.loads(local_state.read_text()).get("profile", {}).get("last_used") or "Default"
        except Exception:  # noqa: BLE001
            folder = "Default"
        name = folder
    src = data / folder
    if not src.exists():
        raise RuntimeError(f"Brave profile folder not found: {src}")
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    if local_state.exists():
        shutil.copy2(local_state, dest / "Local State")
    shutil.copytree(src, dest / "Default", symlinks=True,
                    ignore=lambda d, names: [n for n in names if n in _SKIP_DIRS or n.startswith("Singleton")],
                    ignore_dangling_symlinks=True)
    return name
