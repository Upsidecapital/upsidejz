"""
GreymatterAI — NAS100 Prop Desk
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
MT5_SERVER: str = os.environ.get("MT5_SERVER", "")          # e.g. "ICMarkets-Live"
MT5_SYMBOL: str = os.environ.get("MT5_SYMBOL", "US100")    # broker name: US100 / NAS100 / US100Cash
# NAS100 CFD point value per 1.0 lot (broker-dependent):
#   ICMarkets/Pepperstone: 1 lot = $1/point  → set to 1.0
#   Some brokers:          1 lot = $10/point → set to 10.0
MT5_POINT_VALUE: float = float(os.environ.get("MT5_POINT_VALUE", "1.0"))
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
# Instrument — NAS100 (NASDAQ 100)
# Twelve Data symbol: "QQQ" (ETF, best volume data) or "NDX" (index, no volume)
# For live CFD data: check your broker's Twelve Data symbol mapping
# ---------------------------------------------------------------------------
SYMBOL: str = os.environ.get("TD_SYMBOL", "QQQ")      # Twelve Data ticker
DISPLAY_SYMBOL: str = "NAS100"                          # shown in alerts / dashboard
EXCHANGE: str = "NYSE"

TIMEFRAMES: List[str] = ["5min", "15min", "1h", "1day"]

# Minimum bars required per timeframe before any strategy fires
MIN_BARS: dict[str, int] = {
    "5min":  300,   # ~5 hours of 5-min bars (needed for IVB first-hour VP)
    "15min": 200,
    "1h":    100,
    "1day":  60,
}

# ---------------------------------------------------------------------------
# Session times (UTC) for NAS100
# ---------------------------------------------------------------------------
# Pre-market opens 04:00 ET = 09:00 UTC
# NYSE regular session: 09:30 ET = 13:30 UTC  ← primary ORB session
# NYSE close: 16:00 ET = 21:00 UTC
NY_OPEN_UTC_HOUR: int = 13
NY_OPEN_UTC_MINUTE: int = 30
NY_CLOSE_UTC_HOUR: int = 21
NY_CLOSE_UTC_MINUTE: int = 0

# IVB window: first 60 minutes of NY session (9:30-10:30 ET)
IVB_WINDOW_MINUTES: int = 60

# ORB Initial Balance: first 30 minutes (9:30-10:00 ET = 2 × 15-min bars)
ORB_IB_MINUTES: int = 30

# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------
HEARTBEAT_SECONDS: int = 900   # 15 minutes (aligns with M15 bar close)
OPTIMIZATION_INTERVAL_HOURS: int = 4
OPTIMIZATION_LOOKBACK_DAYS: int = 30

# ---------------------------------------------------------------------------
# Strategy configs (Fabio Valentini ORB / IVB / Order Flow for NAS100)
# ---------------------------------------------------------------------------

@dataclass
class ORBBreakoutConfig:
    """
    Fabio Valentini ORB — NAS100 NY session open.
    IB = first 30 min (9:30-10:00 ET). Breakout of IB with momentum confirmation.
    SL = IB midpoint (not the far side). TP = IB extension × tp_ib_mult.
    """
    ib_minutes: int = 30                   # Initial Balance window
    ib_bars_m15: int = 2                   # 30 min / 15 min = 2 bars
    volume_breakout_mult: float = 1.5      # volume vs 20-bar avg for breakout bar
    body_ratio_min: float = 0.55           # breakout bar body/range ≥ 55 % (momentum)
    sl_at_ib_mid: bool = True              # True = SL at IB midpoint (Fabio style)
    sl_atr_mult: float = 0.5              # extra buffer beyond IB mid (in ATR units)
    tp_ib_mult: float = 1.5               # TP = IB high/low ± IB_range × mult
    min_ib_range_pts: float = 20.0        # ignore sessions with IB range < 20 pts

@dataclass
class IVBConfig:
    """
    Fabio Valentini IVB — Initial Value Balance via first-hour volume profile.
    Build VP from M5 bars of the first 60 min after NY open.
    Two setups:
      A. Rejection from VAH/VAL → trade back to POC
      B. Re-entry into IVB after price has left and returns → trade to opposite extreme
    """
    ivb_window_minutes: int = 60           # first-hour IVB window
    ivb_bars_m5: int = 12                  # 60 min / 5 min
    num_bins: int = 30                     # volume histogram resolution
    value_area_pct: float = 0.70           # 70 % of volume = value area
    poc_tolerance_pts: float = 8.0         # price within 8 pts of POC = "at POC"
    level_tolerance_pts: float = 10.0      # price within 10 pts of VAH/VAL
    volume_confirm_mult: float = 1.3       # volume spike for confirmation
    sl_buffer_pts: float = 8.0            # SL beyond VAL/VAH
    min_va_range_pts: float = 25.0        # skip if value area is too narrow

@dataclass
class OrderFlowConfig:
    """
    NAS100 Order Flow — delta imbalance, absorption, and exhaustion.
    Uses VWAP as intraday bias filter.
    """
    absorption_volume_mult: float = 2.0    # volume spike vs 20-bar avg
    absorption_body_pct: float = 0.20      # body/range < 20 % = absorption candle
    lookback_bars: int = 8                 # bars back for delta divergence / exhaustion
    imbalance_delta_ratio: float = 0.35    # delta/volume ratio for stacked imbalance
    vwap_filter: bool = True               # require VWAP alignment for entries
    sl_pts: float = 25.0                   # fixed SL in NAS100 points
    tp_rr: float = 2.5                     # R:R multiple for TP


@dataclass
class AllStrategyConfigs:
    orb_breakout: ORBBreakoutConfig = field(default_factory=ORBBreakoutConfig)
    ivb: IVBConfig = field(default_factory=IVBConfig)
    order_flow: OrderFlowConfig = field(default_factory=OrderFlowConfig)

STRATEGY_CONFIGS = AllStrategyConfigs()
