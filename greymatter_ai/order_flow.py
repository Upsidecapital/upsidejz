"""
Strategy — Order Flow (Delta Imbalance & Absorption)

Approximates order-flow delta from OHLCV bars (no tick data required):
  buy_vol  = volume × (close − low)  / (high − low)
  sell_vol = volume × (high − close) / (high − low)
  delta    = buy_vol − sell_vol

Three setups:

1. ABSORPTION  – price is at a key level (prev-day H/L or session extreme)
   with a spike in volume but tiny body (institutions absorbing).
   Fades the direction that drove price to the level.

2. DELTA EXHAUSTION – price prints a new N-bar high/low but cumulative
   delta fails to confirm (divergence). Signals exhaustion & reversal.

3. DELTA IMBALANCE  – three consecutive bars with strong one-sided delta
   (all same direction, delta/volume ratio > threshold). Trend continuation
   after a brief pause bar.

No lookahead: all signals derived from fully-closed bars only.
Entry executes at next bar open (handled by orchestrator).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

import numpy as np
import pandas as pd

from config import OrderFlowConfig, STRATEGY_CONFIGS
from database import TradeDirection

logger = logging.getLogger(__name__)

CFG: OrderFlowConfig = STRATEGY_CONFIGS.order_flow


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _delta(df: pd.DataFrame) -> pd.Series:
    """Signed delta: positive = net buy pressure, negative = net sell."""
    rng = (df["high"] - df["low"]).replace(0, np.nan)
    buy_vol = df["volume"] * (df["close"] - df["low"]) / rng
    sell_vol = df["volume"] * (df["high"] - df["close"]) / rng
    return (buy_vol - sell_vol).fillna(0)


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
    d1: pd.DataFrame,
    cfg: OrderFlowConfig = CFG,
) -> Optional["OrderFlowSignal"]:
    """
    Scans the last few M15 bars for absorption, delta exhaustion, or
    delta imbalance. Returns the highest-conviction signal found, or None.
    """
    if len(m15) < cfg.lookback_bars + 20 or len(d1) < 3:
        return None

    delta = _delta(m15)
    atr_series = _atr(m15)
    vol_ma = m15["volume"].rolling(20).mean()

    atr_val = float(atr_series.iloc[-1])
    if np.isnan(atr_val) or atr_val == 0:
        return None

    # Key levels from previous D1 bars
    prev_high = float(d1["high"].iloc[-2])
    prev_low = float(d1["low"].iloc[-2])
    key_levels = [prev_high, prev_low]

    last = m15.iloc[-1]
    close = float(last["close"])
    body = abs(float(last["close"]) - float(last["open"]))
    rng = float(last["high"]) - float(last["low"])

    candidates = []

    # ------------------------------------------------------------------
    # Setup 1: Absorption
    # ------------------------------------------------------------------
    for level in key_levels:
        near_level = abs(close - level) <= atr_val * 0.5
        if not near_level:
            continue

        big_volume = float(last["volume"]) > float(vol_ma.iloc[-1]) * cfg.absorption_volume_mult
        small_body = (body / rng < cfg.absorption_body_pct) if rng > 0 else False

        if big_volume and small_body:
            # Fade the side: if level is a high → short; if level is a low → long
            if level == prev_high:
                direction = TradeDirection.SHORT
                sl = level + atr_val * cfg.sl_atr_mult
                tp = close - atr_val * cfg.tp_atr_mult
            else:
                direction = TradeDirection.LONG
                sl = level - atr_val * cfg.sl_atr_mult
                tp = close + atr_val * cfg.tp_atr_mult

            conviction = _score_absorption(big_volume, small_body, body, rng)
            candidates.append(OrderFlowSignal(
                direction=direction,
                entry_price=close,
                stop_loss=sl,
                take_profit=tp,
                conviction=conviction,
                atr=atr_val,
                bar_close_time=last["timestamp"],
                setup="absorption",
                notes=f"Absorption at level {level:.2f}",
            ))

    # ------------------------------------------------------------------
    # Setup 2: Delta Exhaustion (divergence)
    # ------------------------------------------------------------------
    lookback = cfg.lookback_bars
    window = m15.iloc[-(lookback + 1):-1]   # exclude the trigger bar itself
    delta_window = delta.iloc[-(lookback + 1):-1]

    if len(window) >= lookback:
        price_new_high = float(last["high"]) > float(window["high"].max())
        cum_delta_last = float(delta.iloc[-1])
        cum_delta_prev_max = float(delta_window.max())
        delta_divergence_bear = price_new_high and cum_delta_last < cum_delta_prev_max * 0.7

        price_new_low = float(last["low"]) < float(window["low"].min())
        cum_delta_prev_min = float(delta_window.min())
        delta_divergence_bull = price_new_low and cum_delta_last > cum_delta_prev_min * 0.7

        if delta_divergence_bear:
            conviction = 55.0 + (1.0 - cum_delta_last / (cum_delta_prev_max + 1e-9)) * 20.0
            conviction = min(conviction, 90.0)
            candidates.append(OrderFlowSignal(
                direction=TradeDirection.SHORT,
                entry_price=close,
                stop_loss=float(last["high"]) + atr_val * cfg.sl_atr_mult,
                take_profit=close - atr_val * cfg.tp_atr_mult,
                conviction=conviction,
                atr=atr_val,
                bar_close_time=last["timestamp"],
                setup="delta_exhaustion",
                notes="Bearish delta divergence at new high",
            ))

        if delta_divergence_bull:
            conviction = 55.0 + (1.0 - abs(cum_delta_last) / (abs(cum_delta_prev_min) + 1e-9)) * 20.0
            conviction = min(conviction, 90.0)
            candidates.append(OrderFlowSignal(
                direction=TradeDirection.LONG,
                entry_price=close,
                stop_loss=float(last["low"]) - atr_val * cfg.sl_atr_mult,
                take_profit=close + atr_val * cfg.tp_atr_mult,
                conviction=conviction,
                atr=atr_val,
                bar_close_time=last["timestamp"],
                setup="delta_exhaustion",
                notes="Bullish delta divergence at new low",
            ))

    # ------------------------------------------------------------------
    # Setup 3: Delta Imbalance (3 consecutive strong one-sided bars)
    # ------------------------------------------------------------------
    last3 = m15.iloc[-4:-1]   # 3 bars before the trigger bar
    delta3 = delta.iloc[-4:-1]

    if len(last3) == 3:
        all_bull = all(float(b["close"]) > float(b["open"]) for _, b in last3.iterrows())
        all_bear = all(float(b["close"]) < float(b["open"]) for _, b in last3.iterrows())
        avg_delta_ratio = float((delta3 / (last3["volume"] + 1e-9)).mean())

        if all_bull and avg_delta_ratio > cfg.imbalance_delta_ratio:
            # Trend continuation long — enter on a pause/pullback bar
            pause = float(last["close"]) < float(m15.iloc[-2]["close"])  # slight pullback
            if pause:
                conviction = 50.0 + min(avg_delta_ratio * 30.0, 30.0)
                candidates.append(OrderFlowSignal(
                    direction=TradeDirection.LONG,
                    entry_price=close,
                    stop_loss=close - atr_val * cfg.sl_atr_mult,
                    take_profit=close + atr_val * cfg.tp_atr_mult,
                    conviction=min(conviction, 85.0),
                    atr=atr_val,
                    bar_close_time=last["timestamp"],
                    setup="delta_imbalance",
                    notes="Bullish delta imbalance continuation",
                ))

        if all_bear and avg_delta_ratio < -cfg.imbalance_delta_ratio:
            pause = float(last["close"]) > float(m15.iloc[-2]["close"])
            if pause:
                conviction = 50.0 + min(abs(avg_delta_ratio) * 30.0, 30.0)
                candidates.append(OrderFlowSignal(
                    direction=TradeDirection.SHORT,
                    entry_price=close,
                    stop_loss=close + atr_val * cfg.sl_atr_mult,
                    take_profit=close - atr_val * cfg.tp_atr_mult,
                    conviction=min(conviction, 85.0),
                    atr=atr_val,
                    bar_close_time=last["timestamp"],
                    setup="delta_imbalance",
                    notes="Bearish delta imbalance continuation",
                ))

    if not candidates:
        return None

    best = max(candidates, key=lambda s: s.conviction)
    if best.conviction < 40:
        return None

    logger.info(
        "ORDER FLOW %s | setup=%s conviction=%.0f atr=%.2f",
        best.direction, best.setup, best.conviction, best.atr,
    )
    return best


def _score_absorption(big_vol: bool, small_body: bool, body: float, rng: float) -> float:
    score = 35.0
    if big_vol:
        score += 30.0
    if small_body:
        score += 20.0
    # Smaller body relative to range = stronger absorption
    if rng > 0:
        score += max(0.0, 15.0 * (1.0 - body / rng))
    return min(score, 100.0)


@dataclass
class OrderFlowSignal:
    direction: TradeDirection
    entry_price: float
    stop_loss: float
    take_profit: float
    conviction: float
    atr: float
    bar_close_time: datetime
    setup: str = ""
    notes: str = ""
