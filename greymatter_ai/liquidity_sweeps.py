"""
Strategy 2.1 — Liquidity Sweeps
Detects sweeps of previous-day/week highs/lows (equal highs/lows),
confirms absorption via volume spike + price rejection, approximates
order-flow delta via volume aggression, then scores conviction 0-100.

No lookahead: all signals are derived from fully-closed bars only.
Entry executes at next bar open (handled by orchestrator).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

import numpy as np
import pandas as pd

from config import LiquiditySweepConfig, STRATEGY_CONFIGS
from database import TradeDirection

logger = logging.getLogger(__name__)

CFG: LiquiditySweepConfig = STRATEGY_CONFIGS.liquidity_sweep


@dataclass
class LiquiditySweepSignal:
    direction: TradeDirection
    entry_price: float          # next M15 bar open (approximated as last close)
    stop_loss: float
    take_profit: float
    conviction: float           # 0–100
    atr: float
    bar_close_time: datetime
    notes: str = ""


def _equal_level(series: pd.Series, tolerance_pct: float = 0.001) -> pd.Series:
    """Mark bars where value is within `tolerance_pct` of the previous bar."""
    return (series - series.shift(1)).abs() / series.shift(1) < tolerance_pct


def _wick_rejection_ratio(df: pd.DataFrame, direction: str) -> pd.Series:
    """
    For a bullish absorption candle: upper_wick / total_range.
    For a bearish absorption candle: lower_wick / total_range.
    """
    rng = (df["high"] - df["low"]).replace(0, np.nan)
    if direction == "bull":
        wick = df["high"] - df[["open", "close"]].max(axis=1)
    else:
        wick = df[["open", "close"]].min(axis=1) - df["low"]
    return wick / rng


def _volume_ma(df: pd.DataFrame, period: int = 20) -> pd.Series:
    return df["volume"].rolling(period).mean()


def _atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - df["close"].shift()).abs(),
        (df["low"] - df["close"].shift()).abs(),
    ], axis=1).max(axis=1)
    return tr.rolling(period).mean()


def detect(
    m15: pd.DataFrame,
    d1: pd.DataFrame,
    cfg: LiquiditySweepConfig = CFG,
) -> Optional[LiquiditySweepSignal]:
    """
    Returns a signal if a valid liquidity sweep + absorption is detected on M15.
    `m15` and `d1` must be sorted oldest-first with no lookahead (last bar = last closed bar).
    """
    if len(m15) < cfg.lookback_bars_m15 + 20 or len(d1) < 5:
        return None

    # --- Step 1: Identify key levels from D1 (previous day's high/low) ---
    prev_day_high = float(d1["high"].iloc[-2])
    prev_day_low = float(d1["low"].iloc[-2])
    prev_week_high = float(d1["high"].iloc[-6:-1].max())
    prev_week_low = float(d1["low"].iloc[-6:-1].min())

    # Collect all key levels
    key_highs = sorted({prev_day_high, prev_week_high}, reverse=True)
    key_lows = sorted({prev_day_low, prev_week_low})

    atr_series = _atr(m15)
    vol_ma = _volume_ma(m15)
    last = m15.iloc[-1]
    atr_val = float(atr_series.iloc[-1])

    if np.isnan(atr_val) or atr_val == 0:
        return None

    tolerance = atr_val * 0.3   # sweep must reach within 30 % of ATR of the level

    # --- Step 2: Check for sweep of key HIGH → bearish reversal ---
    for level in key_highs:
        swept = last["high"] >= level - tolerance
        if not swept:
            continue
        # Absorption: price closed back below the level
        rejected = last["close"] < level
        if not rejected:
            continue
        # Volume spike
        vol_spike = last["volume"] > float(vol_ma.iloc[-1]) * cfg.volume_spike_multiplier
        # Wick rejection (upper wick dominant)
        wick_ratio = float(_wick_rejection_ratio(m15, "bull").iloc[-1])
        wick_ok = wick_ratio >= cfg.absorption_rejection_pct
        # Delta aggression proxy: volume on the sweep bar is high but close is lower
        delta_flip = last["close"] < (last["open"] + last["close"]) / 2

        conviction = _score(vol_spike, wick_ok, delta_flip, wick_ratio)
        if conviction < 30:
            continue

        sl = last["high"] + atr_val * 0.3
        tp = last["close"] - atr_val * cfg.tp_atr_mult
        entry = last["close"]

        logger.info(
            "LIQUIDITY SWEEP SHORT | level=%.2f conviction=%.0f atr=%.2f",
            level, conviction, atr_val,
        )
        return LiquiditySweepSignal(
            direction=TradeDirection.SHORT,
            entry_price=entry,
            stop_loss=sl,
            take_profit=tp,
            conviction=conviction,
            atr=atr_val,
            bar_close_time=last["timestamp"],
            notes=f"Sweep of D1/W1 high {level:.2f}",
        )

    # --- Step 3: Check for sweep of key LOW → bullish reversal ---
    for level in key_lows:
        swept = last["low"] <= level + tolerance
        if not swept:
            continue
        rejected = last["close"] > level
        if not rejected:
            continue
        vol_spike = last["volume"] > float(vol_ma.iloc[-1]) * cfg.volume_spike_multiplier
        wick_ratio = float(_wick_rejection_ratio(m15, "bear").iloc[-1])
        wick_ok = wick_ratio >= cfg.absorption_rejection_pct
        delta_flip = last["close"] > (last["open"] + last["close"]) / 2

        conviction = _score(vol_spike, wick_ok, delta_flip, wick_ratio)
        if conviction < 30:
            continue

        sl = last["low"] - atr_val * 0.3
        tp = last["close"] + atr_val * cfg.tp_atr_mult
        entry = last["close"]

        logger.info(
            "LIQUIDITY SWEEP LONG | level=%.2f conviction=%.0f atr=%.2f",
            level, conviction, atr_val,
        )
        return LiquiditySweepSignal(
            direction=TradeDirection.LONG,
            entry_price=entry,
            stop_loss=sl,
            take_profit=tp,
            conviction=conviction,
            atr=atr_val,
            bar_close_time=last["timestamp"],
            notes=f"Sweep of D1/W1 low {level:.2f}",
        )

    return None


def _score(vol_spike: bool, wick_ok: bool, delta_flip: bool, wick_ratio: float) -> float:
    """Conviction 0–100 based on confirmation factors."""
    score = 30.0
    if vol_spike:
        score += 25.0
    if wick_ok:
        score += 20.0 + min(wick_ratio * 20.0, 15.0)  # up to 35
    if delta_flip:
        score += 10.0
    return min(score, 100.0)
