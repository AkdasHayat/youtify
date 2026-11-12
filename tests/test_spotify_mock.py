"""Drives the real uploader code against a local mock of the Spotify wizard in headless Chromium."""
import os
import shutil
from pathlib import Path

import pytest

from youtify.config import SpotifyConfig
from youtify.spotify import SpotifyUploader, UploadJob

FIXTURE = Path(__file__).parent / "fixtures" / "mock_wizard.html"
CHROME = os.environ.get("YOUTIFY_CHROME_PATH") or shutil.which("chromium") or shutil.which("google-chrome")

pytest.importorskip("playwright")


@pytest.fixture
def uploader(tmp_path, monkeypatch):
    if not CHROME and not Path("/opt/pw-browsers").exists():
        pytest.skip("no chromium available")
    if CHROME:
        monkeypatch.setenv("YOUTIFY_CHROME_PATH", CHROME)
    cfg = SpotifyConfig(headless=True, wizard_url=FIXTURE.as_uri() + "?show={show_id}",
                        upload_timeout_minutes=1)
    with SpotifyUploader(cfg, tmp_path / "logs", tmp_path / "profile", tmp_path / "none.yaml") as up:
        yield up


@pytest.mark.parametrize("action", ["draft", "publish"])
def test_full_wizard_flow(uploader, tmp_path, action):
    video = tmp_path / "ep.mp4"
    video.write_bytes(b"\0" * 1024)
    thumb = tmp_path / "t.jpg"
    thumb.write_bytes(b"\xff\xd8\xff")
    desc = "First paragraph here.\n\nChapters:\n00:00 Intro\n05:00 Soil\n\nWatch: https://youtu.be/x"
    job = UploadJob("abcdefghijk", "SHOW", video, "My Title: Part 1", desc, thumb)
    uploader.upload(job, action)
    r = uploader.page.evaluate("window.result")
    assert r["kind"] == action
    assert r["title"] == "My Title: Part 1"
    assert r["file"] == "ep.mp4" and r["thumb"] == 1
    lines = [l for l in r["description"].replace("\ufeff", "").splitlines() if l.strip()]
    assert lines == ["First paragraph here.", "Chapters:", "00:00 Intro", "05:00 Soil", "Watch: https://youtu.be/x"]
    if action == "draft":
        assert r["status"] == "Preview ready!"  # closed only after Spotify finished processing
