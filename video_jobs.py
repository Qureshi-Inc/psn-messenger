"""Persistent SQLite job store for PSN → WhatsApp video forwarding."""
import logging
import sqlite3
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)

_DB_PATH = Path("/data/video_jobs.db")
_lock = threading.Lock()

# Statuses: pending → downloading → sending → delivered | failed
PENDING = "pending"
DOWNLOADING = "downloading"
SENDING = "sending"
DELIVERED = "delivered"
FAILED = "failed"
TERMINAL = {DELIVERED, FAILED}


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(_DB_PATH, check_same_thread=False)
    c.row_factory = sqlite3.Row
    return c


def init() -> None:
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _lock, _conn() as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS video_jobs (
                message_uid     TEXT PRIMARY KEY,
                group_id        TEXT NOT NULL,
                ugc_id          TEXT NOT NULL,
                sender          TEXT NOT NULL,
                created_at      REAL NOT NULL,
                status          TEXT NOT NULL DEFAULT 'pending',
                attempts        INTEGER NOT NULL DEFAULT 0,
                next_attempt_at REAL NOT NULL DEFAULT 0.0,
                last_error      TEXT
            )
        """)
        db.commit()
    logger.info("video_jobs: DB ready at %s", _DB_PATH)


def claim(message_uid: str, group_id: str, ugc_id: str, sender: str) -> bool:
    """Insert job if not already known. Returns True if this is a new job."""
    with _lock, _conn() as db:
        db.execute(
            "INSERT OR IGNORE INTO video_jobs "
            "(message_uid, group_id, ugc_id, sender, created_at, next_attempt_at) "
            "VALUES (?, ?, ?, ?, ?, 0.0)",
            (message_uid, group_id, ugc_id, sender, time.time()),
        )
        db.commit()
        return db.execute("SELECT changes()").fetchone()[0] > 0


def get(message_uid: str) -> dict | None:
    with _conn() as db:
        row = db.execute(
            "SELECT * FROM video_jobs WHERE message_uid=?", (message_uid,)
        ).fetchone()
    return dict(row) if row else None


def mark(message_uid: str, status: str, error: str | None = None,
         next_attempt_at: float | None = None) -> None:
    with _lock, _conn() as db:
        if next_attempt_at is not None:
            db.execute(
                "UPDATE video_jobs SET status=?, last_error=?, attempts=attempts+1, "
                "next_attempt_at=? WHERE message_uid=?",
                (status, error, next_attempt_at, message_uid),
            )
        else:
            db.execute(
                "UPDATE video_jobs SET status=?, last_error=? WHERE message_uid=?",
                (status, error, message_uid),
            )
        db.commit()


def recoverable_jobs() -> list[dict]:
    """Jobs not in a terminal state that are past their next_attempt_at."""
    now = time.time()
    with _conn() as db:
        rows = db.execute(
            "SELECT * FROM video_jobs "
            "WHERE status NOT IN ('delivered', 'failed') AND next_attempt_at <= ? "
            "ORDER BY created_at",
            (now,),
        ).fetchall()
    return [dict(r) for r in rows]


def stats() -> dict:
    with _conn() as db:
        row = db.execute(
            "SELECT COUNT(*) AS total, "
            "SUM(status='delivered') AS delivered, "
            "SUM(status='failed') AS failed, "
            "SUM(status NOT IN ('delivered','failed')) AS active "
            "FROM video_jobs"
        ).fetchone()
    return dict(row) if row else {}
