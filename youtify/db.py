"""SQLite state: YouTube catalog, episodes queue, unmatched files."""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path

PENDING = "pending"              # matched to a YouTube video, metadata not generated yet
AWAITING_APPROVAL = "awaiting_approval"
READY = "ready"                  # metadata done, waiting for upload
UPLOADED = "uploaded"            # on Spotify (draft or published)
FAILED = "failed"                # gave up after max attempts / needs a human
SKIPPED = "skipped"

SCHEMA = """
CREATE TABLE IF NOT EXISTS catalog(
  yt_id TEXT PRIMARY KEY,
  channel TEXT NOT NULL,
  title TEXT NOT NULL,
  duration REAL,
  kind TEXT,
  position INTEGER,
  seen_at REAL
);
CREATE INDEX IF NOT EXISTS idx_catalog_channel ON catalog(channel);
CREATE TABLE IF NOT EXISTS episodes(
  yt_id TEXT PRIMARY KEY,
  channel TEXT NOT NULL,
  title TEXT NOT NULL,
  file_path TEXT NOT NULL,
  status TEXT NOT NULL,
  position INTEGER,
  upload_date TEXT,
  metadata_path TEXT,
  attempts INTEGER NOT NULL DEFAULT 0,
  last_error TEXT,
  spotify_url TEXT,
  created_at REAL,
  updated_at REAL,
  uploaded_at REAL
);
CREATE INDEX IF NOT EXISTS idx_episodes_status ON episodes(status);
CREATE TABLE IF NOT EXISTS unmatched(
  file_path TEXT PRIMARY KEY,
  channel TEXT NOT NULL,
  size INTEGER,
  mtime REAL,
  best_guess TEXT,
  score REAL,
  seen_at REAL
);
CREATE TABLE IF NOT EXISTS kv(key TEXT PRIMARY KEY, value TEXT);
"""


class DB:
    def __init__(self, path: Path | str):
        self.conn = sqlite3.connect(str(path), timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self):
        self.conn.close()

    # ---- kv ----
    def get_kv(self, key: str, default: str | None = None) -> str | None:
        row = self.conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def set_kv(self, key: str, value: str) -> None:
        self.conn.execute("INSERT OR REPLACE INTO kv(key,value) VALUES(?,?)", (key, value))
        self.conn.commit()

    # ---- catalog ----
    def replace_catalog(self, channel: str, entries: list[dict]) -> None:
        now = time.time()
        with self.conn:
            for e in entries:
                self.conn.execute(
                    """INSERT INTO catalog(yt_id,channel,title,duration,kind,position,seen_at)
                       VALUES(?,?,?,?,?,?,?)
                       ON CONFLICT(yt_id) DO UPDATE SET channel=excluded.channel, title=excluded.title,
                         duration=COALESCE(excluded.duration, catalog.duration), kind=excluded.kind,
                         position=excluded.position, seen_at=excluded.seen_at""",
                    (e["id"], channel, e["title"], e.get("duration"), e.get("kind"), e.get("position"), now),
                )
        self.set_kv(f"catalog_refreshed:{channel}", str(now))

    def catalog(self, channel: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM catalog WHERE channel=? ORDER BY position", (channel,)
        ).fetchall()

    def catalog_entry(self, yt_id: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM catalog WHERE yt_id=?", (yt_id,)).fetchone()

    def catalog_refreshed_at(self, channel: str) -> float:
        return float(self.get_kv(f"catalog_refreshed:{channel}", "0"))

    # ---- episodes ----
    def episode(self, yt_id: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM episodes WHERE yt_id=?", (yt_id,)).fetchone()

    def episode_by_path(self, path: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM episodes WHERE file_path=?", (path,)).fetchone()

    def add_episode(self, yt_id: str, channel: str, title: str, file_path: str,
                    position: int | None, status: str = PENDING) -> None:
        now = time.time()
        with self.conn:
            self.conn.execute(
                """INSERT INTO episodes(yt_id,channel,title,file_path,status,position,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (yt_id, channel, title, file_path, status, position, now, now),
            )
            self.conn.execute("DELETE FROM unmatched WHERE file_path=?", (file_path,))

    def update_episode(self, yt_id: str, **fields) -> None:
        fields["updated_at"] = time.time()
        cols = ", ".join(f"{k}=?" for k in fields)
        with self.conn:
            self.conn.execute(f"UPDATE episodes SET {cols} WHERE yt_id=?", (*fields.values(), yt_id))

    def record_failure(self, yt_id: str, error: str, max_attempts: int, give_up: bool = False) -> int:
        ep = self.episode(yt_id)
        attempts = (ep["attempts"] or 0) + 1
        fields = {"attempts": attempts, "last_error": error[:2000]}
        if give_up or attempts >= max_attempts:
            fields["status"] = FAILED
        self.update_episode(yt_id, **fields)
        return attempts

    def episodes(self, status: str | None = None, channel: str | None = None) -> list[sqlite3.Row]:
        q, args = "SELECT * FROM episodes WHERE 1=1", []
        if status:
            q += " AND status=?"
            args.append(status)
        if channel:
            q += " AND channel=?"
            args.append(channel)
        q += " ORDER BY channel, upload_date IS NULL, upload_date, position DESC"
        return self.conn.execute(q, args).fetchall()

    def needing_metadata(self, limit: int, channel: str | None = None) -> list[sqlite3.Row]:
        # Oldest YouTube videos first (catalog position 0 = newest).
        return self.conn.execute(
            "SELECT * FROM episodes WHERE status=? AND (? IS NULL OR channel=?) "
            "ORDER BY position DESC LIMIT ?",
            (PENDING, channel, channel, limit),
        ).fetchall()

    def ready_for_upload(self, limit: int, channel: str | None = None) -> list[sqlite3.Row]:
        return self.conn.execute(
            """SELECT * FROM episodes WHERE status=? AND (? IS NULL OR channel=?)
               ORDER BY upload_date IS NULL, upload_date, position DESC LIMIT ?""",
            (READY, channel, channel, limit),
        ).fetchall()

    def counts(self) -> dict[tuple[str, str], int]:
        rows = self.conn.execute(
            "SELECT channel, status, COUNT(*) n FROM episodes GROUP BY channel, status"
        ).fetchall()
        return {(r["channel"], r["status"]): r["n"] for r in rows}

    # ---- unmatched ----
    def unmatched(self, channel: str | None = None) -> list[sqlite3.Row]:
        if channel:
            return self.conn.execute("SELECT * FROM unmatched WHERE channel=?", (channel,)).fetchall()
        return self.conn.execute("SELECT * FROM unmatched ORDER BY channel, file_path").fetchall()

    def unmatched_entry(self, path: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM unmatched WHERE file_path=?", (path,)).fetchone()

    def set_unmatched(self, path: str, channel: str, size: int, mtime: float,
                      best_guess: str | None, score: float) -> None:
        with self.conn:
            self.conn.execute(
                """INSERT OR REPLACE INTO unmatched(file_path,channel,size,mtime,best_guess,score,seen_at)
                   VALUES(?,?,?,?,?,?,?)""",
                (path, channel, size, mtime, best_guess, score, time.time()),
            )
