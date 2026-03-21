"""
Strategy 2.2 — EMA Pullbacks (Auction Market Theory)
H1 EMA 9/21 as dynamic POC proxy. Looks for price pulling back into the
EMA zone (low-value area) then continuing with M15 delta aggression.

No lookahead: bar close only. Entry = next bar open.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

import numpy as np
import pandas as pd

from config import EMAPullbackConfig, STRATEGY_CONFIGS
from database import TradeDirection

logger = logging.getLogger(__name__)

CFG: EMAPullbackConfig = STRATEGY_CONFIGS.ema_pullback


@dataclass
class EMAPullbackSignal:
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


def detect(
    m15: pd.DataFrame,
    h1: pd.DataFrame,
    cfg: EMAPullbackConfig = CFG,
) -> Optional[EMAPullbackSignal]:
    """
    Returns a signal when a confirmed EMA pullback entry condition exists.
    Trend determined on H1; entry confirmed on M15.
    """
    if len(h1) < cfg.slow_ema + 10 or len(m15) < 30:
        return None

    # --- H1 EMAs ---
    h1 = h1.copy()
    h1["ema_fast"] = h1["close"].ewm(span=cfg.fast_ema, adjust=False).mean()
    h1["ema_slow"] = h1["close"].ewm(span=cfg.slow_ema, adjust=False).mean()

    last_h1 = h1.iloc[-1]
    prev_h1 = h1.iloc[-2]

    bullish_trend = (
        float(last_h1["ema_fast"]) > float(last_h1["ema_slow"]) and
        float(last_h1["close"]) > float(last_h1["ema_fast"])
    )
    bearish_trend = (
        float(last_h1["ema_fast"]) < float(last_h1["ema_slow"]) and
        float(last_h1["close"]) < float(last_h1["ema_fast"])
    )

    if not (bullish_trend or bearish_trend):
        return None

    ema_zone_high = max(float(last_h1["ema_fast"]), float(last_h1["ema_slow"]))
    ema_zone_low = min(float(last_h1["ema_fast"]), float(last_h1["ema_slow"]))
    tolerance = ema_zone_high * cfg.pullback_tolerance_pct

    atr_val = float(_atr(m15).iloc[-1])
    if np.isnan(atr_val) or atr_val == 0:
        return None

    # M15 confirmation: last M15 bar closed back above/below EMA zone after dipping in
    last_m15 = m15.iloc[-1]
    prev_m15 = m15.iloc[-2]

    vol_ma = m15["volume"].rolling(20).mean()
    vol_aggression = last_m15["volume"] > float(vol_ma.iloc[-1]) * cfg.volume_confirmation_mult

    if bullish_trend:
        # Pullback: prev close dipped into EMA zone (or below)
        touched_zone = float(prev_m15["low"]) <= ema_zone_high + tolerance
        # Continuation: last M15 close is above ema_zone_high
        continuation = float(last_m15["close"]) > ema_zone_high
        # Bullish momentum bar
        bull_bar = float(last_m15["close"]) > float(last_m15["open"])

        if not (touched_zone and continuation):
            return None

        conviction = _score(vol_aggression, bull_bar, bullish_trend)
        if conviction < 30:
            return None

        entry = float(last_m15["close"])
        sl = ema_zone_low - atr_val * cfg.sl_atr_mult
        tp = entry + atr_val * cfg.tp_atr_mult

        logger.info("EMA PULLBACK LONG | conviction=%.0f atr=%.2f", conviction, atr_val)
        return EMAPullbackSignal(
            direction=TradeDirection.LONG,
            entry_price=entry,
            stop_loss=sl,
            take_profit=tp,
            conviction=conviction,
            atr=atr_val,
            bar_close_time=last_m15["timestamp"],
            notes=f"EMA zone {ema_zone_low:.2f}–{ema_zone_high:.2f}",
        )

    if bearish_trend:
        touched_zone = float(prev_m15["high"]) >= ema_zone_low - tolerance
        continuation = float(last_m15["close"]) < ema_zone_low
        bear_bar = float(last_m15["close"]) < float(last_m15["open"])

        if not (touched_zone and continuation):
            return None

        conviction = _score(vol_aggression, bear_bar, True)
        if conviction < 30:
            return None

        entry = float(last_m15["close"])
        sl = ema_zone_high + atr_val * cfg.sl_atr_mult
        tp = entry - atr_val * cfg.tp_atr_mult

        logger.info("EMA PULLBACK SHORT | conviction=%.0f atr=%.2f", conviction, atr_val)
        return EMAPullbackSignal(
            direction=TradeDirection.SHORT,
            entry_price=entry,
            stop_loss=sl,
            take_profit=tp,
            conviction=conviction,
            atr=atr_val,
            bar_close_time=last_m15["timestamp"],
            notes=f"EMA zone {ema_zone_low:.2f}–{ema_zone_high:.2f}",
        )

    return None


def _score(vol_aggression: bool, momentum_bar: bool, strong_trend: bool) -> float:
    score = 35.0
    if vol_aggression:
        score += 30.0
    if momentum_bar:
        score += 20.0
    if strong_trend:
        score += 15.0
    return min(score, 100.0)
