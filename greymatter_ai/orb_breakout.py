"""
Strategy 2.3 — ORB Breakout Expansion (Opening Range Breakout)
Flagship strategy. Uses London or NY open Initial Balance (first 30 min),
detects ATR compression, then trades the breakout with volume confirmation.
IVB-style filter: the breakout candle must close convincingly outside the range.

No lookahead. Entry = next bar open after confirmed breakout close.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, time, timezone
from typing import Optional

import numpy as np
import pandas as pd

from config import ORBBreakoutConfig, STRATEGY_CONFIGS
from database import TradeDirection

logger = logging.getLogger(__name__)

CFG: ORBBreakoutConfig = STRATEGY_CONFIGS.orb_breakout

# London open: 08:00 UTC | NY open: 13:30 UTC
LONDON_OPEN = time(8, 0)
NY_OPEN = time(13, 30)
SESSION_END_OFFSET_BARS = 2   # IB = first 2× M15 bars = 30 min


@dataclass
class ORBSignal:
    direction: TradeDirection
    entry_price: float
    stop_loss: float
    take_profit: float
    conviction: float
    atr: float
    ib_high: float
    ib_low: float
    bar_close_time: datetime
    notes: str = ""


def _atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - df["close"].shift()).abs(),
        (df["low"] - df["close"].shift()).abs(),
    ], axis=1).max(axis=1)
    return tr.rolling(period).mean()


def _get_initial_balance(m15: pd.DataFrame, session_open: time) -> Optional[tuple[float, float, int]]:
    """
    Find the most recent Initial Balance for the given session_open time.
    Returns (ib_high, ib_low, ib_end_idx) or None.
    """
    ts = m15["timestamp"]
    # Find the most recent bar at or after session_open today
    for i in range(len(m15) - 1, max(0, len(m15) - 100), -1):
        bar_time = ts.iloc[i].time() if hasattr(ts.iloc[i], "time") else ts.iloc[i].to_pydatetime().time()
        if bar_time >= session_open:
            # Walk back to the first bar of this session
            session_date = ts.iloc[i].date() if hasattr(ts.iloc[i], "date") else ts.iloc[i].to_pydatetime().date()
            first_idx = i
            while first_idx > 0:
                prev_date = ts.iloc[first_idx - 1].date() if hasattr(ts.iloc[first_idx - 1], "date") else ts.iloc[first_idx - 1].to_pydatetime().date()
                prev_time = ts.iloc[first_idx - 1].time() if hasattr(ts.iloc[first_idx - 1], "time") else ts.iloc[first_idx - 1].to_pydatetime().time()
                if prev_date != session_date or prev_time < session_open:
                    break
                first_idx -= 1
            ib_end = first_idx + SESSION_END_OFFSET_BARS - 1
            if ib_end >= len(m15):
                return None
            ib_slice = m15.iloc[first_idx:ib_end + 1]
            return float(ib_slice["high"].max()), float(ib_slice["low"].min()), ib_end
    return None


def detect(
    m15: pd.DataFrame,
    cfg: ORBBreakoutConfig = CFG,
) -> Optional[ORBSignal]:
    """
    Check M15 bars for a confirmed ORB breakout.
    """
    if len(m15) < 50:
        return None

    atr_series = _atr(m15)
    current_atr = float(atr_series.iloc[-1])
    baseline_atr = float(atr_series.iloc[-20:].mean())

    if np.isnan(current_atr) or np.isnan(baseline_atr) or baseline_atr == 0:
        return None

    # --- Volatility compression filter ---
    compressed = (current_atr / baseline_atr) <= cfg.atr_compression_ratio

    # Try both sessions, prefer the more recent one
    for session_open in (NY_OPEN, LONDON_OPEN):
        result = _get_initial_balance(m15, session_open)
        if result is None:
            continue
        ib_high, ib_low, ib_end_idx = result
        ib_range = ib_high - ib_low
        if ib_range <= 0:
            continue

        # Only look at bars after the IB
        post_ib = m15.iloc[ib_end_idx + 1:]
        if len(post_ib) == 0:
            continue

        last = post_ib.iloc[-1]
        vol_ma = m15["volume"].rolling(20).mean()
        vol_spike = float(last["volume"]) > float(vol_ma.iloc[-1]) * cfg.volume_breakout_mult

        # Bullish breakout: close > IB high (IVB: must close convincingly outside)
        if float(last["close"]) > ib_high and float(last["open"]) <= ib_high * 1.002:
            breakout_strength = (float(last["close"]) - ib_high) / ib_range
            conviction = _score(compressed, vol_spike, breakout_strength)
            if conviction < 35:
                continue
            entry = float(last["close"])
            sl = ib_high - current_atr * cfg.sl_atr_mult
            tp = entry + current_atr * cfg.tp_atr_mult
            logger.info("ORB BREAKOUT LONG | IB=%.2f–%.2f conviction=%.0f", ib_low, ib_high, conviction)
            return ORBSignal(
                direction=TradeDirection.LONG,
                entry_price=entry,
                stop_loss=sl,
                take_profit=tp,
                conviction=conviction,
                atr=current_atr,
                ib_high=ib_high,
                ib_low=ib_low,
                bar_close_time=last["timestamp"],
                notes=f"ORB {'compressed' if compressed else ''} session={session_open}",
            )

        # Bearish breakout: close < IB low
        if float(last["close"]) < ib_low and float(last["open"]) >= ib_low * 0.998:
            breakout_strength = (ib_low - float(last["close"])) / ib_range
            conviction = _score(compressed, vol_spike, breakout_strength)
            if conviction < 35:
                continue
            entry = float(last["close"])
            sl = ib_low + current_atr * cfg.sl_atr_mult
            tp = entry - current_atr * cfg.tp_atr_mult
            logger.info("ORB BREAKOUT SHORT | IB=%.2f–%.2f conviction=%.0f", ib_low, ib_high, conviction)
            return ORBSignal(
                direction=TradeDirection.SHORT,
                entry_price=entry,
                stop_loss=sl,
                take_profit=tp,
                conviction=conviction,
                atr=current_atr,
                ib_high=ib_high,
                ib_low=ib_low,
                bar_close_time=last["timestamp"],
                notes=f"ORB {'compressed' if compressed else ''} session={session_open}",
            )

    return None


def _score(compressed: bool, vol_spike: bool, breakout_strength: float) -> float:
    score = 30.0
    if compressed:
        score += 25.0
    if vol_spike:
        score += 25.0
    score += min(breakout_strength * 50.0, 20.0)
    return min(score, 100.0)
