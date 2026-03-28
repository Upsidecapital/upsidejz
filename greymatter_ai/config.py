"""
GreymatterAI — NAS100 Prop Desk
Central configuration. All secrets are loaded from environment variables.
Never commit real values to source control.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import List

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# API Credentials
# ---------------------------------------------------------------------------
TWELVE_DATA_API_KEY: str = os.environ.get("TWELVE_DATA_API_KEY", "")
TELEGRAM_BOT_TOKEN:  str = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID:    str = os.environ.get("TELEGRAM_CHAT_ID", "")
CLAUDE_API_KEY:      str = os.environ.get("CLAUDE_API_KEY", "")   # for AI learning

# ---------------------------------------------------------------------------
# MetaTrader 5 Credentials
# ---------------------------------------------------------------------------
MT5_LOGIN:    str   = os.environ.get("MT5_LOGIN", "")
MT5_PASSWORD: str   = os.environ.get("MT5_PASSWORD", "")
MT5_SERVER:   str   = os.environ.get("MT5_SERVER", "")        # e.g. "ICMarkets-Live"
MT5_SYMBOL:   str   = os.environ.get("MT5_SYMBOL", "US100")  # broker name: US100 / NAS100
# NAS100 CFD point value per 1.0 lot (broker-dependent):
#   ICMarkets/Pepperstone: 1 lot = $1/point → MT5_POINT_VALUE=1.0
#   Some brokers:          1 lot = $10/point → MT5_POINT_VALUE=10.0
MT5_POINT_VALUE: float = float(os.environ.get("MT5_POINT_VALUE", "1.0"))
DATABASE_URL: str = os.environ.get(
    "DATABASE_URL",
    "postgresql+asyncpg://user:password@localhost:5432/greymatter",
)

# ---------------------------------------------------------------------------
# Account & Risk
# ---------------------------------------------------------------------------
ACCOUNT_SIZE_USD: float = float(os.environ.get("ACCOUNT_SIZE_USD", "250"))

# ── Small account detection ──────────────────────────────────────────────
# Accounts < SMALL_ACCOUNT_THRESHOLD get tighter risk parameters automatically.
SMALL_ACCOUNT_THRESHOLD: float = 1000.0
IS_SMALL_ACCOUNT: bool = ACCOUNT_SIZE_USD < SMALL_ACCOUNT_THRESHOLD

if IS_SMALL_ACCOUNT:
    # Tight limits for micro accounts (<$1000)
    MAX_DAILY_DRAWDOWN_PCT: float   = 0.03      # 3% DD cap ($7.50 on $250)
    MAX_RISK_PER_TRADE_PCT: float   = 0.01      # 1% per trade ($2.50 on $250)
    MAX_CONSECUTIVE_LOSERS: int     = 3         # halt after 3 losers (protect small capital)
    MIN_SHARPE_THRESHOLD:   float   = 0.3       # looser Sharpe gate (fewer trades = noisy)
    MAX_LOT_SIZE:           float   = 0.10      # hard cap — prevents oversizing on small margin
else:
    MAX_DAILY_DRAWDOWN_PCT: float   = 0.02      # 2% for normal accounts
    MAX_RISK_PER_TRADE_PCT: float   = 0.01      # 1% per trade
    MAX_CONSECUTIVE_LOSERS: int     = 8
    MIN_SHARPE_THRESHOLD:   float   = 0.5
    MAX_LOT_SIZE:           float   = 10.0

# Absolute minimum lot size (broker floor — never go below this)
MIN_LOT_SIZE: float = float(os.environ.get("MIN_LOT_SIZE", "0.01"))

# ── Margin warning threshold ─────────────────────────────────────────────
# NAS100 requires roughly $100–$500 margin per 0.01 lot depending on leverage.
# At $250 with 1:500 leverage: 0.01 lot NAS100 ≈ $4–$6 margin. Fine.
# At $250 with 1:30 leverage:  0.01 lot NAS100 ≈ $60 margin. Very tight.
# We warn if margin-per-trade might be > 10% of account.
LEVERAGE: int = int(os.environ.get("LEVERAGE", "500"))   # set to your broker's leverage

# ---------------------------------------------------------------------------
# Instrument — NAS100 (NASDAQ 100)
# ---------------------------------------------------------------------------
SYMBOL:         str = os.environ.get("TD_SYMBOL", "QQQ")
DISPLAY_SYMBOL: str = "NAS100"
EXCHANGE:       str = "NYSE"

TIMEFRAMES:  List[str]       = ["5min", "15min", "1h", "1day"]
MIN_BARS:    dict[str, int]  = {
    "5min":  300,
    "15min": 200,
    "1h":    100,
    "1day":  60,
}

# ---------------------------------------------------------------------------
# Session times (UTC)
# ---------------------------------------------------------------------------
NY_OPEN_UTC_HOUR:    int = 13
NY_OPEN_UTC_MINUTE:  int = 30
NY_CLOSE_UTC_HOUR:   int = 21
NY_CLOSE_UTC_MINUTE: int = 0

IVB_WINDOW_MINUTES: int = 60
ORB_IB_MINUTES:     int = 30

# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------
HEARTBEAT_SECONDS:          int = 900  # 15 min (M15 bar boundary)
OPTIMIZATION_INTERVAL_HOURS: int = 4
OPTIMIZATION_LOOKBACK_DAYS:  int = 30
CLAUDE_ANALYSIS_INTERVAL:    int = 5   # run Claude analysis every N closed trades

# ---------------------------------------------------------------------------
# Strategy configs
# ---------------------------------------------------------------------------

@dataclass
class ORBBreakoutConfig:
    """Fabio Valentini ORB — NAS100 NY session open."""
    ib_minutes:           int   = 30
    ib_bars_m15:          int   = 2
    volume_breakout_mult: float = 1.5
    body_ratio_min:       float = 0.55
    sl_at_ib_mid:         bool  = True
    sl_atr_mult:          float = 0.5
    tp_ib_mult:           float = 1.5
    min_ib_range_pts:     float = 20.0


@dataclass
class IVBConfig:
    """Fabio Valentini IVB — first-hour volume profile."""
    ivb_window_minutes:  int   = 60
    ivb_bars_m5:         int   = 12
    num_bins:            int   = 30
    value_area_pct:      float = 0.70
    poc_tolerance_pts:   float = 8.0
    level_tolerance_pts: float = 10.0
    volume_confirm_mult: float = 1.3
    sl_buffer_pts:       float = 8.0
    min_va_range_pts:    float = 25.0


@dataclass
class OrderFlowConfig:
    """NAS100 Order Flow — delta imbalance, absorption, exhaustion."""
    absorption_volume_mult: float = 2.0
    absorption_body_pct:    float = 0.20
    lookback_bars:          int   = 8
    imbalance_delta_ratio:  float = 0.35
    vwap_filter:            bool  = True
    sl_pts:                 float = 25.0
    tp_rr:                  float = 2.5


@dataclass
class AllStrategyConfigs:
    orb_breakout: ORBBreakoutConfig = field(default_factory=ORBBreakoutConfig)
    ivb:          IVBConfig         = field(default_factory=IVBConfig)
    order_flow:   OrderFlowConfig   = field(default_factory=OrderFlowConfig)


STRATEGY_CONFIGS = AllStrategyConfigs()

# ---------------------------------------------------------------------------
# Startup warnings
# ---------------------------------------------------------------------------
def print_account_warnings() -> None:
    """Log important warnings about the current account configuration."""
    if IS_SMALL_ACCOUNT:
        risk_usd  = ACCOUNT_SIZE_USD * MAX_RISK_PER_TRADE_PCT
        daily_cap = ACCOUNT_SIZE_USD * MAX_DAILY_DRAWDOWN_PCT
        logger.warning("=" * 60)
        logger.warning("SMALL ACCOUNT MODE — $%.2f", ACCOUNT_SIZE_USD)
        logger.warning("  Risk per trade : $%.2f (%.0f%%)", risk_usd, MAX_RISK_PER_TRADE_PCT * 100)
        logger.warning("  Daily DD cap   : $%.2f (%.0f%%)", daily_cap, MAX_DAILY_DRAWDOWN_PCT * 100)
        logger.warning("  Max lot size   : %.2f lots", MAX_LOT_SIZE)
        logger.warning("  Halt after     : %d consecutive losses", MAX_CONSECUTIVE_LOSERS)
        logger.warning("  Leverage set   : 1:%d", LEVERAGE)
        est_margin = (ACCOUNT_SIZE_USD / LEVERAGE) * 100  # very rough NAS100 estimate
        logger.warning("  Est. margin/0.01 lot ≈ $%.2f", est_margin)
        if ACCOUNT_SIZE_USD < 100:
            logger.critical(
                "ACCOUNT TOO SMALL ($%.2f) — NAS100 minimum margin requirements "
                "may exceed account balance. Ensure your broker offers 1:500+ leverage "
                "and allows micro lots (0.01). Bot will generate signals but orders "
                "may be rejected by MT5.", ACCOUNT_SIZE_USD
            )
        logger.warning("=" * 60)
