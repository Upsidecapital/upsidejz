"""
GreymatterAI — Database models (SQLAlchemy async + PostgreSQL)
"""
from __future__ import annotations

import enum
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    BigInteger, Boolean, Column, DateTime, Enum, Float,
    Integer, String, Text, func,
)
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from config import DATABASE_URL

# ---------------------------------------------------------------------------
# Engine & Session
# ---------------------------------------------------------------------------
engine = create_async_engine(DATABASE_URL, echo=False, pool_pre_ping=True)

AsyncSessionLocal = sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
)

async def get_db() -> AsyncSession:  # type: ignore[return]
    async with AsyncSessionLocal() as session:
        yield session

async def init_db() -> None:
    """Create all tables on startup and migrate enums if needed."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # PostgreSQL requires explicit ALTER TYPE to add new enum values.
        # These are idempotent: IF NOT EXISTS was added in PostgreSQL 9.6.
        new_values = ["volume_profile", "order_flow", "macd_fib", "ivb"]
        for val in new_values:
            try:
                await conn.execute(
                    __import__("sqlalchemy").text(
                        f"ALTER TYPE strategyname ADD VALUE IF NOT EXISTS '{val}'"
                    )
                )
            except Exception:
                pass  # enum type may not exist yet (fresh DB) — create_all will handle it


# ---------------------------------------------------------------------------
# Base
# ---------------------------------------------------------------------------
class Base(DeclarativeBase):
    pass


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------
class TradeStatus(str, enum.Enum):
    PENDING = "pending"
    OPEN = "open"
    CLOSED_WIN = "closed_win"
    CLOSED_LOSS = "closed_loss"
    CANCELLED = "cancelled"

class TradeDirection(str, enum.Enum):
    LONG = "long"
    SHORT = "short"

class StrategyName(str, enum.Enum):
    LIQUIDITY_SWEEP = "liquidity_sweep"
    EMA_PULLBACK = "ema_pullback"
    ORB_BREAKOUT = "orb_breakout"
    EMA_MOMENTUM = "ema_momentum"
    VOLUME_PROFILE = "volume_profile"
    ORDER_FLOW = "order_flow"
    MACD_FIB = "macd_fib"
    IVB = "ivb"


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------
class Signal(Base):
    """Every raw signal emitted by a strategy, regardless of execution."""
    __tablename__ = "signals"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    strategy = Column(Enum(StrategyName), nullable=False)
    direction = Column(Enum(TradeDirection), nullable=False)
    conviction = Column(Float, nullable=False)          # 0–100 (after adaptive weight applied)
    raw_conviction = Column(Float, nullable=True)       # conviction before adaptive weight
    entry_price = Column(Float, nullable=False)
    stop_loss = Column(Float, nullable=False)
    take_profit = Column(Float, nullable=False)
    atr = Column(Float, nullable=True)
    timeframe = Column(String(10), nullable=False)
    bar_close_time = Column(DateTime(timezone=True), nullable=False)
    executed = Column(Boolean, default=False, nullable=False)
    setup = Column(String(60), nullable=True)           # e.g. "retest_long", "delta_flip"
    notes = Column(Text, nullable=True)


class Trade(Base):
    """Executed trades with full lifecycle tracking."""
    __tablename__ = "trades"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    signal_id = Column(BigInteger, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    opened_at = Column(DateTime(timezone=True), nullable=True)
    closed_at = Column(DateTime(timezone=True), nullable=True)
    strategy = Column(Enum(StrategyName), nullable=False)
    setup = Column(String(60), nullable=True)           # setup label for outcome tracking
    direction = Column(Enum(TradeDirection), nullable=False)
    entry_price = Column(Float, nullable=False)
    stop_loss = Column(Float, nullable=False)
    take_profit = Column(Float, nullable=False)
    close_price = Column(Float, nullable=True)
    lot_size = Column(Float, nullable=False)             # position size in oz
    risk_usd = Column(Float, nullable=False)
    pnl_usd = Column(Float, nullable=True)
    pnl_r = Column(Float, nullable=True)                # result in R multiples
    status = Column(Enum(TradeStatus), default=TradeStatus.PENDING, nullable=False)
    conviction = Column(Float, nullable=False)
    mt5_ticket = Column(BigInteger, nullable=True)   # MT5 position ticket (None if MT5 not used)
    notes = Column(Text, nullable=True)


class SetupPerformance(Base):
    """
    Per-setup adaptive weights — updated after every trade closes.
    Tracks EWMA win rate and translates it to a conviction multiplier
    that scales future signals from the same setup (0.6–1.4×).
    """
    __tablename__ = "setup_performance"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    strategy = Column(String(40), nullable=False)       # e.g. "orb_breakout"
    setup = Column(String(60), nullable=False)          # e.g. "retest_long"
    trades_total = Column(Integer, default=0, nullable=False)
    trades_win = Column(Integer, default=0, nullable=False)
    ewma_win_rate = Column(Float, default=0.5, nullable=False)  # EWMA; starts neutral
    avg_pnl_r = Column(Float, default=0.0, nullable=False)      # simple running avg R
    conviction_multiplier = Column(Float, default=1.0, nullable=False)  # applied to raw conviction
    last_updated = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class EquitySnapshot(Base):
    """Periodic equity curve snapshots for charting & Sharpe calculation."""
    __tablename__ = "equity_snapshots"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    recorded_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    equity_usd = Column(Float, nullable=False)
    open_trades = Column(Integer, default=0)
    daily_pnl_usd = Column(Float, default=0.0)
    consecutive_losers = Column(Integer, default=0)
    system_active = Column(Boolean, default=True)


class SystemEvent(Base):
    """Audit log for kill-switches, restarts, and optimisation cycles."""
    __tablename__ = "system_events"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    event_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    event_type = Column(String(50), nullable=False)   # e.g. KILL_SWITCH, RESTART, OPTIMISE
    detail = Column(Text, nullable=True)


class ClaudeInsight(Base):
    """
    Stored output from Claude AI trade analysis.
    Each row represents one analysis run covering the last N closed trades.
    The setup_adjustments and risk_adjustment are applied by the orchestrator
    on top of the EWMA adaptive weights.
    """
    __tablename__ = "claude_insights"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    setup_adjustments = Column(Text, nullable=False)   # JSON: {"orb_breakout/retest_long": 1.2}
    risk_adjustment   = Column(Float, default=1.0, nullable=False)  # 0.50–1.00
    avoid_conditions  = Column(Text, nullable=True)    # JSON array of strings
    focus_setups      = Column(Text, nullable=True)    # JSON array of strings
    summary           = Column(Text, nullable=True)    # Human-readable analysis
    confidence        = Column(Float, default=0.5, nullable=False)
    raw_response      = Column(Text, nullable=True)
