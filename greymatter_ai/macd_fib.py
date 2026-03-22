"""
Strategy — MACD + Fibonacci Retracements

Trend filter  : H1 MACD (12, 26, 9) histogram direction
Swing detection: H4 — identifies the last significant swing high & low
                 over the most recent `swing_lookback` bars
Fibonacci grid : 23.6 %, 38.2 %, 50.0 %, 61.8 %, 78.6 % of the swing range
Entry           : M15 price touches a key Fib level (38.2/50/61.8 preferred)
                 while MACD histogram confirms trend direction
Stop-loss       : just beyond the 78.6 % level (one more ATR buffer)
Take-profit     : 0 % extension (swing high for longs, swing low for shorts)

No lookahead: only closed bars used.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional, Tuple

import numpy as np
import pandas as pd

from config import MACDFibConfig, STRATEGY_CONFIGS
from database import TradeDirection

logger = logging.getLogger(__name__)

CFG: MACDFibConfig = STRATEGY_CONFIGS.macd_fib

FIB_LEVELS = [0.0, 0.236, 0.382, 0.500, 0.618, 0.786, 1.0]
KEY_FIBS = {0.382, 0.500, 0.618}   # highest-priority retracement zones


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _macd(series: pd.Series, fast: int, slow: int, signal: int) -> Tuple[pd.Series, pd.Series, pd.Series]:
    ema_fast = series.ewm(span=fast, adjust=False).mean()
    ema_slow = series.ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    histogram = macd_line - signal_line
    return macd_line, signal_line, histogram


def _swing_high_low(h4: pd.DataFrame, lookback: int) -> Tuple[float, float, int, int]:
    """
    Returns (swing_high, swing_low, high_idx, low_idx) from the last `lookback` H4 bars.
    """
    window = h4.iloc[-lookback:]
    high_idx = int(window["high"].idxmax()) if hasattr(window["high"].idxmax(), "__index__") else int(window["high"].values.argmax())
    low_idx = int(window["low"].idxmin()) if hasattr(window["low"].idxmin(), "__index__") else int(window["low"].values.argmin())
    return float(window["high"].max()), float(window["low"].min()), high_idx, low_idx


def _fib_price(swing_high: float, swing_low: float, level: float, direction: str) -> float:
    """
    For a LONG (retracement from high down to low then back up):
      fib_price = swing_high - level × (swing_high - swing_low)
    For a SHORT (retracement from low up to high then back down):
      fib_price = swing_low + level × (swing_high - swing_low)
    """
    rng = swing_high - swing_low
    if direction == "long":
        return swing_high - level * rng
    return swing_low + level * rng


def _atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - df["close"].shift()).abs(),
        (df["low"] - df["close"].shift()).abs(),
    ], axis=1).max(axis=1)
    return tr.rolling(period).mean()


# ---------------------------------------------------------------------------
# Main detect
# ---------------------------------------------------------------------------

def detect(
    m15: pd.DataFrame,
    h1: pd.DataFrame,
    h4: pd.DataFrame,
    cfg: MACDFibConfig = CFG,
) -> Optional["MACDFibSignal"]:
    """
    Returns a signal when M15 price is at a key Fibonacci retracement level
    AND H1 MACD histogram confirms the trade direction.
    """
    min_h4 = cfg.swing_lookback + 5
    if len(h4) < min_h4 or len(h1) < cfg.macd_slow + 10 or len(m15) < 30:
        return None

    # --- Step 1: MACD on H1 ---
    _, _, hist = _macd(h1["close"], cfg.macd_fast, cfg.macd_slow, cfg.macd_signal)
    macd_bull = float(hist.iloc[-1]) > 0 and float(hist.iloc[-1]) > float(hist.iloc[-2])   # rising positive
    macd_bear = float(hist.iloc[-1]) < 0 and float(hist.iloc[-1]) < float(hist.iloc[-2])   # falling negative

    if not (macd_bull or macd_bear):
        return None

    # --- Step 2: Swing high/low on H4 ---
    swing_high, swing_low, hi_idx, lo_idx = _swing_high_low(h4, cfg.swing_lookback)
    swing_range = swing_high - swing_low
    if swing_range < 1e-4:
        return None

    # Determine trend direction based on which swing is more recent
    # If swing high is more recent → last move was up → look for short retracement
    # If swing low is more recent  → last move was down → look for long retracement
    trend = "long" if lo_idx > hi_idx else "short"

    if macd_bull and trend != "long":
        return None
    if macd_bear and trend != "short":
        return None

    atr_val = float(_atr(m15).iloc[-1])
    if np.isnan(atr_val) or atr_val == 0:
        return None

    last = m15.iloc[-1]
    close = float(last["close"])
    tolerance = swing_range * cfg.fib_tolerance_pct

    # --- Step 3: Check if M15 close is at any key Fibonacci level ---
    hit_fib: Optional[float] = None
    hit_priority = False
    for lvl in sorted(FIB_LEVELS[1:-1], key=lambda x: abs(x - 0.5)):   # scan 50 first
        fib_price = _fib_price(swing_high, swing_low, lvl, trend)
        if abs(close - fib_price) <= tolerance:
            hit_fib = lvl
            hit_priority = lvl in KEY_FIBS
            break

    if hit_fib is None:
        return None

    # SL just past 78.6 % retrace (the "last line of defence")
    sl_fib = _fib_price(swing_high, swing_low, 0.786, trend)
    tp_fib = _fib_price(swing_high, swing_low, 0.0, trend)   # swing extreme = 0 %

    if trend == "long":
        sl = min(sl_fib, close) - atr_val * 0.3
        tp = tp_fib   # swing high
        if tp <= close:
            return None
    else:
        sl = max(sl_fib, close) + atr_val * 0.3
        tp = tp_fib   # swing low
        if tp >= close:
            return None

    # Minimum R:R = 1.5
    risk = abs(close - sl)
    reward = abs(tp - close)
    if risk <= 0 or reward / risk < 1.5:
        return None

    conviction = _score(hit_priority, macd_bull or macd_bear, hit_fib, reward / risk)
    if conviction < 40:
        return None

    logger.info(
        "MACD+FIB %s | fib=%.3f swing_range=%.2f conviction=%.0f",
        trend.upper(), hit_fib, swing_range, conviction,
    )
    return MACDFibSignal(
        direction=TradeDirection.LONG if trend == "long" else TradeDirection.SHORT,
        entry_price=close,
        stop_loss=sl,
        take_profit=tp,
        conviction=conviction,
        atr=atr_val,
        bar_close_time=last["timestamp"],
        fib_level=hit_fib,
        swing_high=swing_high,
        swing_low=swing_low,
        notes=f"Fib {hit_fib*100:.1f}% | swing {swing_low:.2f}–{swing_high:.2f}",
    )


def _score(key_fib: bool, macd_strong: bool, fib_level: float, rr: float) -> float:
    score = 30.0
    if key_fib:
        score += 25.0   # 38.2, 50, 61.8 are the golden zones
    if macd_strong:
        score += 20.0
    # 61.8 golden ratio gets bonus
    if abs(fib_level - 0.618) < 0.01:
        score += 10.0
    # R:R bonus
    score += min((rr - 1.5) * 5.0, 15.0)
    return min(score, 100.0)


@dataclass
class MACDFibSignal:
    direction: TradeDirection
    entry_price: float
    stop_loss: float
    take_profit: float
    conviction: float
    atr: float
    bar_close_time: datetime
    fib_level: float
    swing_high: float
    swing_low: float
    notes: str = ""
