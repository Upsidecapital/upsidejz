"""
GreymatterAI — XAUUSD Prop Desk
Central configuration. All secrets are loaded from environment variables.
Never commit real values to source control.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import List


# ---------------------------------------------------------------------------
# API Credentials
# ---------------------------------------------------------------------------
TWELVE_DATA_API_KEY: str = os.environ.get("TWELVE_DATA_API_KEY", "")
TELEGRAM_BOT_TOKEN: str = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID: str = os.environ.get("TELEGRAM_CHAT_ID", "")

# ---------------------------------------------------------------------------
# MetaTrader 5 Credentials
# ---------------------------------------------------------------------------
MT5_LOGIN: str = os.environ.get("MT5_LOGIN", "")
MT5_PASSWORD: str = os.environ.get("MT5_PASSWORD", "")
MT5_SERVER: str = os.environ.get("MT5_SERVER", "")        # e.g. "ICMarkets-Live"
MT5_SYMBOL: str = os.environ.get("MT5_SYMBOL", "XAUUSD")  # some brokers use "GOLD"
MT5_LOT_DIVISOR: float = float(os.environ.get("MT5_LOT_DIVISOR", "100"))  # 1 MT5 lot = 100 oz
DATABASE_URL: str = os.environ.get(
    "DATABASE_URL",
    "postgresql+asyncpg://user:password@localhost:5432/greymatter",
)

# ---------------------------------------------------------------------------
# Account & Risk
# ---------------------------------------------------------------------------
ACCOUNT_SIZE_USD: float = float(os.environ.get("ACCOUNT_SIZE_USD", "200000"))
MAX_DAILY_DRAWDOWN_PCT: float = 0.02          # 2 % of equity
MAX_RISK_PER_TRADE_PCT: float = 0.01          # 1 R = 1 % of equity
MAX_CONSECUTIVE_LOSERS: int = 8
MIN_SHARPE_THRESHOLD: float = 0.5

# ---------------------------------------------------------------------------
# Instrument
# ---------------------------------------------------------------------------
SYMBOL: str = "XAU/USD"
EXCHANGE: str = "FOREX"

TIMEFRAMES: List[str] = ["15min", "1h", "4h", "1day"]

# Minimum bars required per timeframe before any strategy fires
MIN_BARS: dict[str, int] = {
    "15min": 200,
    "1h": 200,
    "4h": 100,
    "1day": 100,
}

# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------
HEARTBEAT_SECONDS: int = 1800  # 30 minutes
OPTIMIZATION_INTERVAL_HOURS: int = 4
OPTIMIZATION_LOOKBACK_DAYS: int = 30

# ---------------------------------------------------------------------------
# Strategy defaults (overridden by walk-forward optimiser)
# ---------------------------------------------------------------------------

@dataclass
class LiquiditySweepConfig:
    volume_spike_multiplier: float = 1.8   # relative to 20-bar avg
    absorption_rejection_pct: float = 0.40  # candle wick ratio
    lookback_bars_m15: int = 96            # ~1 trading day on M15
    sl_atr_mult: float = 1.5
    tp_atr_mult: float = 3.0

@dataclass
class EMAPullbackConfig:
    fast_ema: int = 9
    slow_ema: int = 21
    pullback_tolerance_pct: float = 0.002  # 0.2 % of price
    volume_confirmation_mult: float = 1.3
    sl_atr_mult: float = 1.2
    tp_atr_mult: float = 2.5

@dataclass
class ORBBreakoutConfig:
    ib_minutes: int = 30           # Initial Balance window
    atr_compression_ratio: float = 0.6  # current ATR / 20-bar ATR
    volume_breakout_mult: float = 1.5
    sl_atr_mult: float = 1.0
    tp_atr_mult: float = 3.0

@dataclass
class EMAMomentumConfig:
    slope_ema: int = 21
    slope_threshold: float = 0.0015   # min price change per bar
    h4_ema: int = 50
    d1_ema: int = 200
    volume_aggression_mult: float = 1.6
    sl_atr_mult: float = 1.8
    tp_atr_mult: float = 3.5

@dataclass
class AllStrategyConfigs:
    liquidity_sweep: LiquiditySweepConfig = field(default_factory=LiquiditySweepConfig)
    ema_pullback: EMAPullbackConfig = field(default_factory=EMAPullbackConfig)
    orb_breakout: ORBBreakoutConfig = field(default_factory=ORBBreakoutConfig)
    ema_momentum: EMAMomentumConfig = field(default_factory=EMAMomentumConfig)

STRATEGY_CONFIGS = AllStrategyConfigs()
