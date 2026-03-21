"""
Strategy 2.4 — EMA Momentum (Aggressive Delta Rusher)
Strong EMA slope + price leaving an imbalance zone, confirmed by H4/D1
alignment. Only takes trades when higher-timeframe bias is clear.

No lookahead. Entry = next bar open.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

import numpy as np
import pandas as pd

from config import EMAMomentumConfig, STRATEGY_CONFIGS
from database import TradeDirection

logger = logging.getLogger(__name__)

CFG: EMAMomentumConfig = STRATEGY_CONFIGS.ema_momentum


@dataclass
class EMAMomentumSignal:
    direction: TradeDirection
    entry_price: float
    stop_loss: float
    take_profit: float
    conviction: float
    atr: float
    bar_close_time: datetime
    notes: str = ""


def _atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - df["close"].shift()).abs(),
        (df["low"] - df["close"].shift()).abs(),
    ], axis=1).max(axis=1)
    return tr.rolling(period).mean()


def _ema_slope(series: pd.Series, span: int, lookback: int = 3) -> float:
    """Average price-change-per-bar of the EMA over the last `lookback` bars."""
    ema = series.ewm(span=span, adjust=False).mean()
    if len(ema) < lookback + 1:
        return 0.0
    delta = float(ema.iloc[-1]) - float(ema.iloc[-lookback - 1])
    avg_price = float(ema.iloc[-1])
    return delta / avg_price if avg_price else 0.0


def _imbalance_zone(df: pd.DataFrame, lookback: int = 20) -> tuple[float, float]:
    """
    Very simple imbalance detection: find the largest gap between consecutive
    bars in the lookback window (fair value gap proxy).
    Returns (imbalance_low, imbalance_high) or (0, 0) if none found.
    """
    if len(df) < lookback + 1:
        return 0.0, 0.0
    recent = df.iloc[-lookback:]
    best_gap = 0.0
    iz_low, iz_high = 0.0, 0.0
    for i in range(1, len(recent)):
        prev = recent.iloc[i - 1]
        curr = recent.iloc[i]
        # Bullish FVG: low of current bar > high of bar two back
        if i >= 2:
            two_back = recent.iloc[i - 2]
            bull_gap = float(curr["low"]) - float(two_back["high"])
            if bull_gap > best_gap:
                best_gap = bull_gap
                iz_low = float(two_back["high"])
                iz_high = float(curr["low"])
    return iz_low, iz_high


def detect(
    m15: pd.DataFrame,
    h4: pd.DataFrame,
    d1: pd.DataFrame,
    cfg: EMAMomentumConfig = CFG,
) -> Optional[EMAMomentumSignal]:
    """
    Returns a signal when M15 shows strong EMA momentum, leaving an imbalance,
    confirmed by H4 and D1 bias.
    """
    if len(h4) < cfg.h4_ema + 5 or len(d1) < cfg.d1_ema + 5 or len(m15) < 50:
        return None

    # --- H4 & D1 bias ---
    h4_ema = float(h4["close"].ewm(span=cfg.h4_ema, adjust=False).mean().iloc[-1])
    d1_ema = float(d1["close"].ewm(span=cfg.d1_ema, adjust=False).mean().iloc[-1])
    h4_price = float(h4["close"].iloc[-1])
    d1_price = float(d1["close"].iloc[-1])

    htf_bullish = h4_price > h4_ema and d1_price > d1_ema
    htf_bearish = h4_price < h4_ema and d1_price < d1_ema

    if not (htf_bullish or htf_bearish):
        return None

    # --- M15 slope ---
    slope = _ema_slope(m15["close"], cfg.slope_ema)
    strong_bull_slope = slope >= cfg.slope_threshold
    strong_bear_slope = slope <= -cfg.slope_threshold

    atr_val = float(_atr(m15).iloc[-1])
    if np.isnan(atr_val) or atr_val == 0:
        return None

    last_m15 = m15.iloc[-1]
    vol_ma = m15["volume"].rolling(20).mean()
    vol_ok = float(last_m15["volume"]) > float(vol_ma.iloc[-1]) * cfg.volume_aggression_mult

    # --- Imbalance zone (price leaving it = momentum confirmation) ---
    iz_low, iz_high = _imbalance_zone(m15)
    price_above_iz = iz_high > 0 and float(last_m15["close"]) > iz_high
    price_below_iz = iz_low > 0 and float(last_m15["close"]) < iz_low

    if htf_bullish and strong_bull_slope:
        conviction = _score(vol_ok, price_above_iz, abs(slope) / cfg.slope_threshold)
        if conviction < 40:
            return None
        entry = float(last_m15["close"])
        sl = entry - atr_val * cfg.sl_atr_mult
        tp = entry + atr_val * cfg.tp_atr_mult
        logger.info("EMA MOMENTUM LONG | slope=%.5f conviction=%.0f", slope, conviction)
        return EMAMomentumSignal(
            direction=TradeDirection.LONG,
            entry_price=entry,
            stop_loss=sl,
            take_profit=tp,
            conviction=conviction,
            atr=atr_val,
            bar_close_time=last_m15["timestamp"],
            notes=f"slope={slope:.5f} HTF_bull",
        )

    if htf_bearish and strong_bear_slope:
        conviction = _score(vol_ok, price_below_iz, abs(slope) / cfg.slope_threshold)
        if conviction < 40:
            return None
        entry = float(last_m15["close"])
        sl = entry + atr_val * cfg.sl_atr_mult
        tp = entry - atr_val * cfg.tp_atr_mult
        logger.info("EMA MOMENTUM SHORT | slope=%.5f conviction=%.0f", slope, conviction)
        return EMAMomentumSignal(
            direction=TradeDirection.SHORT,
            entry_price=entry,
            stop_loss=sl,
            take_profit=tp,
            conviction=conviction,
            atr=atr_val,
            bar_close_time=last_m15["timestamp"],
            notes=f"slope={slope:.5f} HTF_bear",
        )

    return None


def _score(vol_ok: bool, leaving_imbalance: bool, slope_ratio: float) -> float:
    score = 35.0
    if vol_ok:
        score += 30.0
    if leaving_imbalance:
        score += 20.0
    score += min((slope_ratio - 1.0) * 10.0, 15.0)
    return min(score, 100.0)
