"""
SQLite database layer for Forex Edge Finder.
Stores: COT snapshots, forex rates, economic events, signals.
"""

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

DB_PATH = Path(__file__).parent / "forex_edge.db"


@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with get_conn() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS cot_snapshots (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            currency    TEXT NOT NULL,
            report_date TEXT NOT NULL,
            fetched_at  TEXT NOT NULL,
            data        TEXT NOT NULL,  -- JSON blob
            UNIQUE(currency, report_date)
        );

        CREATE TABLE IF NOT EXISTS forex_rates (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            fetched_at  TEXT NOT NULL,
            base        TEXT NOT NULL DEFAULT 'USD',
            rates       TEXT NOT NULL  -- JSON blob
        );

        CREATE TABLE IF NOT EXISTS economic_events (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            event_date  TEXT NOT NULL,
            currency    TEXT NOT NULL,
            event_name  TEXT NOT NULL,
            impact      TEXT NOT NULL,  -- HIGH/MEDIUM/LOW
            forecast    TEXT,
            previous    TEXT,
            actual      TEXT,
            source      TEXT DEFAULT 'manual'
        );

        CREATE TABLE IF NOT EXISTS signals (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at  TEXT NOT NULL,
            pair        TEXT NOT NULL,
            direction   TEXT NOT NULL,  -- BUY/SELL/NEUTRAL
            cot_score   REAL,
            fundamental_score REAL,
            combined_score    REAL,
            rationale   TEXT
        );

        CREATE TABLE IF NOT EXISTS interest_rates (
            currency    TEXT PRIMARY KEY,
            rate        REAL NOT NULL,
            updated_at  TEXT NOT NULL,
            central_bank TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS ai_analyses (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at  TEXT NOT NULL,
            pair        TEXT NOT NULL,
            analysis    TEXT NOT NULL
        );
        """)
    _seed_interest_rates()


def _seed_interest_rates() -> None:
    """Seed current central bank rates (March 2026 approximate values)."""
    rates = [
        ("USD", 4.50, "Federal Reserve"),
        ("EUR", 2.65, "European Central Bank"),
        ("GBP", 4.50, "Bank of England"),
        ("JPY", 0.50, "Bank of Japan"),
        ("CHF", 0.25, "Swiss National Bank"),
        ("CAD", 3.00, "Bank of Canada"),
        ("AUD", 4.10, "Reserve Bank of Australia"),
        ("NZD", 3.75, "Reserve Bank of New Zealand"),
        ("MXN", 9.50, "Banco de Mexico"),
    ]
    with get_conn() as conn:
        for currency, rate, bank in rates:
            conn.execute("""
                INSERT OR IGNORE INTO interest_rates (currency, rate, updated_at, central_bank)
                VALUES (?, ?, ?, ?)
            """, (currency, rate, datetime.utcnow().isoformat(), bank))


# ── COT ──────────────────────────────────────────────────────────────────────

def upsert_cot_snapshot(currency: str, report_date: str, data: dict) -> None:
    with get_conn() as conn:
        conn.execute("""
            INSERT OR REPLACE INTO cot_snapshots (currency, report_date, fetched_at, data)
            VALUES (?, ?, ?, ?)
        """, (currency, report_date, datetime.utcnow().isoformat(), json.dumps(data)))


def get_cot_history(currency: str, limit: int = 52) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT report_date, data FROM cot_snapshots
            WHERE currency = ?
            ORDER BY report_date DESC
            LIMIT ?
        """, (currency, limit)).fetchall()
    result = []
    for row in rows:
        d = json.loads(row["data"])
        d["report_date"] = row["report_date"]
        result.append(d)
    return list(reversed(result))


def get_latest_cot(currency: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute("""
            SELECT report_date, data FROM cot_snapshots
            WHERE currency = ?
            ORDER BY report_date DESC
            LIMIT 1
        """, (currency,)).fetchone()
    if not row:
        return None
    d = json.loads(row["data"])
    d["report_date"] = row["report_date"]
    return d


def get_all_latest_cot() -> dict[str, dict]:
    currencies = ["EUR", "GBP", "JPY", "CHF", "CAD", "AUD", "NZD", "MXN"]
    return {c: get_latest_cot(c) for c in currencies}


# ── Forex Rates ───────────────────────────────────────────────────────────────

def store_forex_rates(rates: dict) -> None:
    with get_conn() as conn:
        conn.execute("""
            INSERT INTO forex_rates (fetched_at, base, rates)
            VALUES (?, 'USD', ?)
        """, (datetime.utcnow().isoformat(), json.dumps(rates)))
        # Keep only last 100 snapshots
        conn.execute("""
            DELETE FROM forex_rates WHERE id NOT IN (
                SELECT id FROM forex_rates ORDER BY id DESC LIMIT 100
            )
        """)


def get_latest_forex_rates() -> dict | None:
    with get_conn() as conn:
        row = conn.execute("""
            SELECT fetched_at, rates FROM forex_rates
            ORDER BY id DESC LIMIT 1
        """).fetchone()
    if not row:
        return None
    return {"fetched_at": row["fetched_at"], "rates": json.loads(row["rates"])}


# ── Economic Events ───────────────────────────────────────────────────────────

def upsert_economic_events(events: list[dict]) -> None:
    with get_conn() as conn:
        for ev in events:
            conn.execute("""
                INSERT OR REPLACE INTO economic_events
                (event_date, currency, event_name, impact, forecast, previous, actual, source)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                ev.get("event_date"), ev.get("currency"), ev.get("event_name"),
                ev.get("impact", "MEDIUM"), ev.get("forecast"), ev.get("previous"),
                ev.get("actual"), ev.get("source", "api")
            ))


def get_upcoming_events(days_ahead: int = 7) -> list[dict]:
    from datetime import timedelta
    today = datetime.utcnow().date().isoformat()
    future = (datetime.utcnow().date() + timedelta(days=days_ahead)).isoformat()
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT * FROM economic_events
            WHERE event_date BETWEEN ? AND ?
            ORDER BY event_date, impact DESC
        """, (today, future)).fetchall()
    return [dict(r) for r in rows]


# ── Interest Rates ────────────────────────────────────────────────────────────

def get_interest_rates() -> dict[str, dict]:
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM interest_rates").fetchall()
    return {r["currency"]: dict(r) for r in rows}


def update_interest_rate(currency: str, rate: float) -> None:
    with get_conn() as conn:
        conn.execute("""
            UPDATE interest_rates SET rate = ?, updated_at = ?
            WHERE currency = ?
        """, (rate, datetime.utcnow().isoformat(), currency))


# ── Signals ───────────────────────────────────────────────────────────────────

def store_signal(pair: str, direction: str, cot_score: float,
                 fundamental_score: float, combined_score: float,
                 rationale: str) -> int:
    with get_conn() as conn:
        cur = conn.execute("""
            INSERT INTO signals (created_at, pair, direction, cot_score,
                                 fundamental_score, combined_score, rationale)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (datetime.utcnow().isoformat(), pair, direction,
              cot_score, fundamental_score, combined_score, rationale))
        return cur.lastrowid


def get_latest_signals(limit: int = 20) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT * FROM signals ORDER BY created_at DESC LIMIT ?
        """, (limit,)).fetchall()
    return [dict(r) for r in rows]


# ── AI Analyses ───────────────────────────────────────────────────────────────

def store_ai_analysis(pair: str, analysis: str) -> None:
    with get_conn() as conn:
        conn.execute("""
            INSERT INTO ai_analyses (created_at, pair, analysis)
            VALUES (?, ?, ?)
        """, (datetime.utcnow().isoformat(), pair, analysis))
        conn.execute("""
            DELETE FROM ai_analyses WHERE id NOT IN (
                SELECT id FROM ai_analyses ORDER BY id DESC LIMIT 50
            )
        """)


def get_ai_analysis(pair: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute("""
            SELECT * FROM ai_analyses WHERE pair = ?
            ORDER BY created_at DESC LIMIT 1
        """, (pair,)).fetchone()
    return dict(row) if row else None
