"""Shared state store for the dashboard - bot publishes, web subscribes."""

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class DashboardState:
    """Snapshot of bot state for dashboard rendering."""

    # Mode & status
    is_paper: bool = True
    trading_halted: bool = False
    halt_reason: str = ""
    bot_status: str = "INITIALIZING"  # INITIALIZING, RUNNING, HALTED, STOPPED
    uptime_seconds: float = 0.0
    started_at: float = field(default_factory=time.time)

    # Portfolio
    initial_portfolio: float = 0.0
    portfolio_value: float = 0.0
    peak_portfolio_value: float = 0.0
    daily_pnl: float = 0.0
    total_pnl: float = 0.0
    total_pnl_pct: float = 0.0

    # Drawdown
    daily_drawdown_pct: float = 0.0
    total_drawdown_pct: float = 0.0
    daily_drawdown_limit: float = 20.0
    total_drawdown_limit: float = 40.0

    # Performance
    total_trades: int = 0
    wins: int = 0
    losses: int = 0
    win_rate: float = 0.0
    trades_today: int = 0
    wins_today: int = 0
    losses_today: int = 0
    avg_edge: float = 0.0
    avg_pnl: float = 0.0
    best_trade: float = 0.0
    worst_trade: float = 0.0
    open_positions_count: int = 0

    # Asset performance (per contract type)
    asset_stats: dict[str, dict[str, Any]] = field(default_factory=dict)

    # Live prices
    prices: dict[str, float] = field(default_factory=dict)
    price_changes: dict[str, float] = field(default_factory=dict)

    # Recent trades + open positions
    recent_trades: list[dict] = field(default_factory=list)
    open_positions: list[dict] = field(default_factory=list)

    # Equity curve (time series)
    equity_curve: list[dict] = field(default_factory=list)

    # Activity log
    activity: list[dict] = field(default_factory=list)

    # Timestamp
    updated_at: float = field(default_factory=time.time)


class StateStore:
    """Thread-safe state store with pub/sub for WebSocket broadcasts."""

    def __init__(self, max_activity: int = 50, max_equity_points: int = 500):
        self.state = DashboardState()
        self._subscribers: set[asyncio.Queue] = set()
        self._lock = asyncio.Lock()
        self._activity_buffer: deque = deque(maxlen=max_activity)
        self._equity_buffer: deque = deque(maxlen=max_equity_points)

    async def update(self, **kwargs):
        """Update state fields and broadcast to subscribers."""
        async with self._lock:
            for k, v in kwargs.items():
                if hasattr(self.state, k):
                    setattr(self.state, k, v)
            self.state.updated_at = time.time()
            self.state.uptime_seconds = time.time() - self.state.started_at
        await self._broadcast()

    async def log_activity(self, level: str, message: str):
        """Add an entry to the activity log."""
        entry = {
            "timestamp": time.time(),
            "level": level,
            "message": message,
        }
        async with self._lock:
            self._activity_buffer.append(entry)
            self.state.activity = list(self._activity_buffer)
        await self._broadcast()

    async def record_equity_point(self, value: float):
        """Record a point on the equity curve."""
        point = {"t": time.time(), "v": round(value, 2)}
        async with self._lock:
            self._equity_buffer.append(point)
            self.state.equity_curve = list(self._equity_buffer)

    def snapshot(self) -> dict:
        """Get a JSON-serializable snapshot of current state."""
        s = self.state
        return {
            "is_paper": s.is_paper,
            "trading_halted": s.trading_halted,
            "halt_reason": s.halt_reason,
            "bot_status": s.bot_status,
            "uptime_seconds": round(s.uptime_seconds, 1),
            "started_at": s.started_at,
            "initial_portfolio": round(s.initial_portfolio, 2),
            "portfolio_value": round(s.portfolio_value, 2),
            "peak_portfolio_value": round(s.peak_portfolio_value, 2),
            "daily_pnl": round(s.daily_pnl, 2),
            "total_pnl": round(s.total_pnl, 2),
            "total_pnl_pct": round(s.total_pnl_pct, 2),
            "daily_drawdown_pct": round(s.daily_drawdown_pct, 2),
            "total_drawdown_pct": round(s.total_drawdown_pct, 2),
            "daily_drawdown_limit": s.daily_drawdown_limit,
            "total_drawdown_limit": s.total_drawdown_limit,
            "total_trades": s.total_trades,
            "wins": s.wins,
            "losses": s.losses,
            "win_rate": round(s.win_rate, 2),
            "trades_today": s.trades_today,
            "wins_today": s.wins_today,
            "losses_today": s.losses_today,
            "avg_edge": round(s.avg_edge, 2),
            "avg_pnl": round(s.avg_pnl, 2),
            "best_trade": round(s.best_trade, 2),
            "worst_trade": round(s.worst_trade, 2),
            "open_positions_count": s.open_positions_count,
            "asset_stats": s.asset_stats,
            "prices": s.prices,
            "price_changes": s.price_changes,
            "recent_trades": s.recent_trades,
            "open_positions": s.open_positions,
            "equity_curve": s.equity_curve,
            "activity": s.activity,
            "updated_at": s.updated_at,
        }

    async def subscribe(self) -> asyncio.Queue:
        """Subscribe a WebSocket client to state updates."""
        queue: asyncio.Queue = asyncio.Queue(maxsize=32)
        async with self._lock:
            self._subscribers.add(queue)
        # Send current snapshot on connect
        try:
            queue.put_nowait(self.snapshot())
        except asyncio.QueueFull:
            pass
        return queue

    async def unsubscribe(self, queue: asyncio.Queue):
        """Remove a WebSocket subscriber."""
        async with self._lock:
            self._subscribers.discard(queue)

    async def _broadcast(self):
        """Send current snapshot to all subscribers."""
        snap = self.snapshot()
        dead: list[asyncio.Queue] = []
        for q in list(self._subscribers):
            try:
                q.put_nowait(snap)
            except asyncio.QueueFull:
                # Drop oldest
                try:
                    q.get_nowait()
                    q.put_nowait(snap)
                except Exception:
                    dead.append(q)
        for q in dead:
            self._subscribers.discard(q)


# Global singleton store
store = StateStore()
