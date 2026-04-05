"""SQLite trade logging with full position history."""

import logging
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class TradeRecord:
    """A single trade record for the database."""

    trade_id: str
    timestamp: float
    asset: str
    timeframe: str
    direction: str
    side: str  # "YES" or "NO"
    entry_price: float
    size_usdc: float
    edge_pct: float
    confidence: float
    cex_price: float
    polymarket_price: float
    pnl: float = 0.0
    exit_price: float = 0.0
    exit_timestamp: float = 0.0
    status: str = "OPEN"  # OPEN, CLOSED, CANCELLED
    is_paper: bool = True


class TradeLogger:
    """Logs all trades to SQLite with full position history."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._conn: sqlite3.Connection | None = None

    def initialize(self):
        """Create database and tables."""
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)

        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")

        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                trade_id TEXT UNIQUE NOT NULL,
                timestamp REAL NOT NULL,
                asset TEXT NOT NULL,
                timeframe TEXT NOT NULL,
                direction TEXT NOT NULL,
                side TEXT NOT NULL,
                entry_price REAL NOT NULL,
                size_usdc REAL NOT NULL,
                edge_pct REAL NOT NULL,
                confidence REAL NOT NULL,
                cex_price REAL NOT NULL,
                polymarket_price REAL NOT NULL,
                pnl REAL DEFAULT 0.0,
                exit_price REAL DEFAULT 0.0,
                exit_timestamp REAL DEFAULT 0.0,
                status TEXT DEFAULT 'OPEN',
                is_paper INTEGER DEFAULT 1,
                created_at REAL DEFAULT (strftime('%s', 'now'))
            );

            CREATE INDEX IF NOT EXISTS idx_trades_asset
                ON trades(asset);
            CREATE INDEX IF NOT EXISTS idx_trades_status
                ON trades(status);
            CREATE INDEX IF NOT EXISTS idx_trades_timestamp
                ON trades(timestamp);

            CREATE TABLE IF NOT EXISTS portfolio_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL NOT NULL,
                portfolio_value REAL NOT NULL,
                daily_pnl REAL NOT NULL,
                total_pnl REAL NOT NULL,
                open_positions INTEGER NOT NULL,
                win_rate REAL NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_snapshots_timestamp
                ON portfolio_snapshots(timestamp);
        """)

        self._conn.commit()
        logger.info("Trade database initialized at %s", self.db_path)

    def log_trade(self, record: TradeRecord):
        """Insert a new trade record."""
        try:
            self._conn.execute(
                """
                INSERT INTO trades (
                    trade_id, timestamp, asset, timeframe, direction,
                    side, entry_price, size_usdc, edge_pct, confidence,
                    cex_price, polymarket_price, pnl, exit_price,
                    exit_timestamp, status, is_paper
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.trade_id,
                    record.timestamp,
                    record.asset,
                    record.timeframe,
                    record.direction,
                    record.side,
                    record.entry_price,
                    record.size_usdc,
                    record.edge_pct,
                    record.confidence,
                    record.cex_price,
                    record.polymarket_price,
                    record.pnl,
                    record.exit_price,
                    record.exit_timestamp,
                    record.status,
                    1 if record.is_paper else 0,
                ),
            )
            self._conn.commit()
            logger.debug("Trade logged: %s", record.trade_id)
        except sqlite3.IntegrityError:
            logger.warning("Duplicate trade ID: %s", record.trade_id)
        except Exception as e:
            logger.error("Failed to log trade: %s", e)

    def update_trade(
        self,
        trade_id: str,
        pnl: float,
        exit_price: float,
        status: str = "CLOSED",
    ):
        """Update a trade with exit info."""
        try:
            self._conn.execute(
                """
                UPDATE trades
                SET pnl = ?, exit_price = ?, exit_timestamp = ?, status = ?
                WHERE trade_id = ?
                """,
                (pnl, exit_price, time.time(), status, trade_id),
            )
            self._conn.commit()
        except Exception as e:
            logger.error("Failed to update trade %s: %s", trade_id, e)

    def log_portfolio_snapshot(
        self,
        portfolio_value: float,
        daily_pnl: float,
        total_pnl: float,
        open_positions: int,
        win_rate: float,
    ):
        """Log a portfolio state snapshot."""
        try:
            self._conn.execute(
                """
                INSERT INTO portfolio_snapshots
                    (timestamp, portfolio_value, daily_pnl, total_pnl,
                     open_positions, win_rate)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    time.time(),
                    portfolio_value,
                    daily_pnl,
                    total_pnl,
                    open_positions,
                    win_rate,
                ),
            )
            self._conn.commit()
        except Exception as e:
            logger.error("Failed to log snapshot: %s", e)

    def get_recent_trades(self, limit: int = 10) -> list[dict]:
        """Get the most recent trades."""
        try:
            cursor = self._conn.execute(
                """
                SELECT * FROM trades
                ORDER BY timestamp DESC
                LIMIT ?
                """,
                (limit,),
            )
            return [dict(row) for row in cursor.fetchall()]
        except Exception as e:
            logger.error("Failed to fetch recent trades: %s", e)
            return []

    def get_open_positions(self) -> list[dict]:
        """Get all open positions."""
        try:
            cursor = self._conn.execute(
                "SELECT * FROM trades WHERE status = 'OPEN' ORDER BY timestamp DESC"
            )
            return [dict(row) for row in cursor.fetchall()]
        except Exception as e:
            logger.error("Failed to fetch open positions: %s", e)
            return []

    def get_stats(self) -> dict:
        """Get aggregate trading statistics."""
        try:
            cursor = self._conn.execute("""
                SELECT
                    COUNT(*) as total_trades,
                    SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as wins,
                    SUM(CASE WHEN pnl < 0 THEN 1 ELSE 0 END) as losses,
                    SUM(CASE WHEN status = 'OPEN' THEN 1 ELSE 0 END) as open_count,
                    COALESCE(SUM(pnl), 0) as total_pnl,
                    COALESCE(AVG(pnl), 0) as avg_pnl,
                    COALESCE(MAX(pnl), 0) as best_trade,
                    COALESCE(MIN(pnl), 0) as worst_trade,
                    COALESCE(AVG(edge_pct), 0) as avg_edge,
                    COALESCE(SUM(size_usdc), 0) as total_volume
                FROM trades
                WHERE status = 'CLOSED'
            """)
            row = cursor.fetchone()
            if row:
                stats = dict(row)
                total = stats["wins"] + stats["losses"]
                stats["win_rate"] = (
                    stats["wins"] / total * 100 if total > 0 else 0
                )
                return stats
            return {}
        except Exception as e:
            logger.error("Failed to fetch stats: %s", e)
            return {}

    def close(self):
        """Close the database connection."""
        if self._conn:
            self._conn.close()
            self._conn = None
