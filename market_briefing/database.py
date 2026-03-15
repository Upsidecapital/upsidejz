"""SQLite persistence layer for market briefings."""

import sqlite3
from datetime import datetime
from pathlib import Path

DB_PATH = Path(__file__).parent / "briefings.db"


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _init_db() -> None:
    with _get_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS briefings (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                date          TEXT    NOT NULL,
                generated_at  TEXT    NOT NULL,
                content       TEXT    NOT NULL DEFAULT '',
                status        TEXT    NOT NULL DEFAULT 'generating',
                error         TEXT
            )
        """)
        conn.commit()


class BriefingDB:
    def __init__(self) -> None:
        _init_db()

    # ── write ──────────────────────────────────────────────────────────────

    def create_briefing(self, date: str) -> int:
        """Insert a new 'generating' record and return its id."""
        with _get_conn() as conn:
            cur = conn.execute(
                "INSERT INTO briefings (date, generated_at) VALUES (?, ?)",
                (date, datetime.utcnow().isoformat(timespec="seconds")),
            )
            conn.commit()
            return cur.lastrowid  # type: ignore[return-value]

    def complete_briefing(self, briefing_id: int, content: str) -> None:
        with _get_conn() as conn:
            conn.execute(
                "UPDATE briefings SET content = ?, status = 'complete' WHERE id = ?",
                (content, briefing_id),
            )
            conn.commit()

    def fail_briefing(self, briefing_id: int, error: str) -> None:
        with _get_conn() as conn:
            conn.execute(
                "UPDATE briefings SET status = 'error', error = ? WHERE id = ?",
                (error, briefing_id),
            )
            conn.commit()

    # ── read ───────────────────────────────────────────────────────────────

    def get_briefing(self, briefing_id: int) -> dict | None:
        with _get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM briefings WHERE id = ?", (briefing_id,)
            ).fetchone()
            return dict(row) if row else None

    def list_briefings(self) -> list[dict]:
        with _get_conn() as conn:
            rows = conn.execute(
                """SELECT id, date, generated_at, status
                   FROM briefings
                   ORDER BY id DESC
                   LIMIT 60"""
            ).fetchall()
            return [dict(r) for r in rows]

    def get_latest_complete(self) -> dict | None:
        with _get_conn() as conn:
            row = conn.execute(
                """SELECT * FROM briefings
                   WHERE status = 'complete'
                   ORDER BY id DESC LIMIT 1"""
            ).fetchone()
            return dict(row) if row else None
