"""
Fabio Valentini — Order Flow for NAS100

Approximates order-flow delta from OHLCV bars (no tick data required):
  buy_vol  = volume × (close − low)  / (high − low)
  sell_vol = volume × (high − close) / (high − low)
  delta    = buy_vol − sell_vol

VWAP is computed for the current session (intraday running VWAP from NY open)
and used as a bias filter — Fabio uses VWAP to determine whether bulls or bears
are in control for the day.

Three setups (Fabio Valentini order-flow logic):

1. ABSORPTION at key level
   Large volume, tiny body (institutions absorbing supply/demand) at:
   - Previous day high/low
   - IB high/IB low (if passed from orchestrator context)
   - VWAP
   → Fade the direction that drove into the level

2. DELTA EXHAUSTION (divergence)
   Price prints a new N-bar extreme but cumulative delta fails to follow.
   Classic sign of institutional supply/demand entering against retail.

3. STACKED DELTA IMBALANCE (continuation)
   3+ consecutive bars all same direction with high delta/volume ratio.
   Price pauses (inside bar or small counter-bar) → enter continuation.

VWAP filter:
  - LONG signals only taken when close > session VWAP (or at VWAP for absorption)
  - SHORT signals only taken when close < session VWAP (or at VWAP for absorption)

No lookahead: all signals use only closed bars.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, time
from typing import Optional

import numpy as np
import pandas as pd

from config import OrderFlowConfig, STRATEGY_CONFIGS, NY_OPEN_UTC_HOUR, NY_OPEN_UTC_MINUTE
from database import TradeDirection

logger = logging.getLogger(__name__)

CFG: OrderFlowConfig = STRATEGY_CONFIGS.order_flow

NY_OPEN = time(NY_OPEN_UTC_HOUR, NY_OPEN_UTC_MINUTE)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _delta(df: pd.DataFrame) -> pd.Series:
    """Signed delta per bar: positive = net buying, negative = net selling."""
    rng = (df["high"] - df["low"]).replace(0, np.nan)
    buy_vol = df["volume"] * (df["close"] - df["low"]) / rng
    sell_vol = df["volume"] * (df["high"] - df["close"]) / rng
    return (buy_vol - sell_vol).fillna(0.0)


def _session_vwap(m15: pd.DataFrame) -> pd.Series:
    """
    Running intraday VWAP from the most recent NYSE open.
    Returns a Series aligned to m15 index. Pre-session bars get NaN.
    """
    ts = m15["timestamp"]
    typical = (m15["high"] + m15["low"] + m15["close"]) / 3.0
    tpv = typical * m15["volume"]

    vwap = pd.Series(np.nan, index=m15.index)
    n = len(m15)

    # Find the most recent session start
    session_start = None
    for i in range(n - 1, max(0, n - 300), -1):
        t = ts.iloc[i].time() if hasattr(ts.iloc[i], "time") else ts.iloc[i].to_pydatetime().time()
        d = ts.iloc[i].date() if hasattr(ts.iloc[i], "date") else ts.iloc[i].to_pydatetime().date()
        if t < NY_OPEN:
            continue
        j = i
        while j > 0:
            prev_d = ts.iloc[j - 1].date() if hasattr(ts.iloc[j - 1], "date") else ts.iloc[j - 1].to_pydatetime().date()
            prev_t = ts.iloc[j - 1].time() if hasattr(ts.iloc[j - 1], "time") else ts.iloc[j - 1].to_pydatetime().time()
            if prev_d != d or prev_t < NY_OPEN:
                break
            j -= 1
        session_start = j
        break

    if session_start is None:
        return vwap

    cum_tpv = tpv.iloc[session_start:].cumsum()
    cum_vol = m15["volume"].iloc[session_start:].cumsum().replace(0, np.nan)
    vwap.iloc[session_start:] = cum_tpv / cum_vol
    return vwap


def _atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - df["close"].shift()).abs(),
        (df["low"] - df["close"].shift()).abs(),
    ], axis=1).max(axis=1)
    return tr.rolling(period).mean()


def _prev_day_levels(d1: pd.DataFrame):
    """Returns (prev_high, prev_low) from the last fully-closed D1 bar."""
    if len(d1) < 2:
        return None, None
    return float(d1["high"].iloc[-2]), float(d1["low"].iloc[-2])


# ---------------------------------------------------------------------------
# Main detect
# ---------------------------------------------------------------------------

def detect(
    m15: pd.DataFrame,
    d1: pd.DataFrame,
    cfg: OrderFlowConfig = CFG,
) -> Optional["OrderFlowSignal"]:
    """
    Scan M15 bars for the highest-conviction order-flow setup.
    Returns the best signal or None.
    """
    if len(m15) < cfg.lookback_bars + 20 or len(d1) < 2:
        return None

    delta = _delta(m15)
    atr_series = _atr(m15)
    vol_ma = m15["volume"].rolling(20).mean()
    vwap = _session_vwap(m15)

    atr_val = float(atr_series.iloc[-1])
    if np.isnan(atr_val) or atr_val == 0:
        return None

    last = m15.iloc[-1]
    close = float(last["close"])
    open_ = float(last["open"])
    hi = float(last["high"])
    lo = float(last["low"])
    body = abs(close - open_)
    rng = hi - lo
    vol_last = float(last["volume"])
    vol_avg = float(vol_ma.iloc[-1])

    session_vwap = float(vwap.iloc[-1]) if not np.isnan(float(vwap.iloc[-1])) else None
    above_vwap = (session_vwap is not None) and (close > session_vwap)
    below_vwap = (session_vwap is not None) and (close < session_vwap)
    at_vwap = (session_vwap is not None) and (abs(close - session_vwap) < atr_val * 0.3)

    prev_high, prev_low = _prev_day_levels(d1)
    key_levels = [l for l in [prev_high, prev_low, session_vwap] if l is not None]

    candidates = []

    # ------------------------------------------------------------------
    # Setup 1: Absorption at key level
    # ------------------------------------------------------------------
    big_volume = vol_last > vol_avg * cfg.absorption_volume_mult
    small_body = (body / rng < cfg.absorption_body_pct) if rng > 0 else False

    if big_volume and small_body:
        for level in key_levels:
            near = abs(close - level) < atr_val * 0.6
            if not near:
                continue

            # Fade into the level
            if level == prev_high or (session_vwap and abs(level - session_vwap) < 1):
                # At resistance or VWAP → short fade
                if not cfg.vwap_filter or below_vwap or at_vwap:
                    sl = close + cfg.sl_pts
                    tp = close - cfg.sl_pts * cfg.tp_rr
                    conviction = _score_absorption(big_volume, small_body, body, rng)
                    candidates.append(OrderFlowSignal(
                        direction=TradeDirection.SHORT,
                        entry_price=close, stop_loss=sl, take_profit=tp,
                        conviction=conviction, atr=atr_val,
                        bar_close_time=last["timestamp"],
                        setup="absorption",
                        notes=f"OF absorption SHORT at level {level:.0f} (VWAP={session_vwap:.0f if session_vwap else 'N/A'})",
                    ))
            if level == prev_low:
                # At support → long fade
                if not cfg.vwap_filter or above_vwap or at_vwap:
                    sl = close - cfg.sl_pts
                    tp = close + cfg.sl_pts * cfg.tp_rr
                    conviction = _score_absorption(big_volume, small_body, body, rng)
                    candidates.append(OrderFlowSignal(
                        direction=TradeDirection.LONG,
                        entry_price=close, stop_loss=sl, take_profit=tp,
                        conviction=conviction, atr=atr_val,
                        bar_close_time=last["timestamp"],
                        setup="absorption",
                        notes=f"OF absorption LONG at level {level:.0f}",
                    ))

    # ------------------------------------------------------------------
    # Setup 2: Delta Exhaustion (divergence at N-bar extreme)
    # ------------------------------------------------------------------
    lb = cfg.lookback_bars
    window_m15 = m15.iloc[-(lb + 1):-1]
    delta_window = delta.iloc[-(lb + 1):-1]
    delta_last = float(delta.iloc[-1])

    if len(window_m15) >= lb:
        # Bearish exhaustion: new high but delta falls
        new_high = hi > float(window_m15["high"].max())
        delta_bear_div = delta_last < float(delta_window.max()) * 0.65
        if new_high and delta_bear_div and (not cfg.vwap_filter or below_vwap or at_vwap):
            sl = hi + cfg.sl_pts
            tp = close - cfg.sl_pts * cfg.tp_rr
            div_strength = 1.0 - delta_last / (float(delta_window.max()) + 1e-9)
            conviction = min(50.0 + div_strength * 30.0, 90.0)
            candidates.append(OrderFlowSignal(
                direction=TradeDirection.SHORT,
                entry_price=close, stop_loss=sl, take_profit=tp,
                conviction=conviction, atr=atr_val,
                bar_close_time=last["timestamp"],
                setup="delta_exhaustion",
                notes="OF bearish delta divergence at new high",
            ))

        # Bullish exhaustion: new low but delta rises
        new_low = lo < float(window_m15["low"].min())
        delta_bull_div = delta_last > float(delta_window.min()) * 0.65
        if new_low and delta_bull_div and (not cfg.vwap_filter or above_vwap or at_vwap):
            sl = lo - cfg.sl_pts
            tp = close + cfg.sl_pts * cfg.tp_rr
            div_strength = 1.0 - abs(delta_last) / (abs(float(delta_window.min())) + 1e-9)
            conviction = min(50.0 + div_strength * 30.0, 90.0)
            candidates.append(OrderFlowSignal(
                direction=TradeDirection.LONG,
                entry_price=close, stop_loss=sl, take_profit=tp,
                conviction=conviction, atr=atr_val,
                bar_close_time=last["timestamp"],
                setup="delta_exhaustion",
                notes="OF bullish delta divergence at new low",
            ))

    # ------------------------------------------------------------------
    # Setup 3: Stacked Delta Imbalance → continuation after pause
    # ------------------------------------------------------------------
    last3 = m15.iloc[-4:-1]   # 3 bars prior to trigger
    delta3 = delta.iloc[-4:-1]

    if len(last3) == 3:
        all_bull = all(float(r["close"]) > float(r["open"]) for _, r in last3.iterrows())
        all_bear = all(float(r["close"]) < float(r["open"]) for _, r in last3.iterrows())
        avg_dr = float((delta3 / (last3["volume"].replace(0, np.nan))).mean())

        if all_bull and avg_dr > cfg.imbalance_delta_ratio:
            pause = close < float(m15.iloc[-2]["close"])  # slight pullback = entry
            if pause and (not cfg.vwap_filter or above_vwap):
                conviction = min(48.0 + avg_dr * 35.0, 85.0)
                sl = close - cfg.sl_pts
                tp = close + cfg.sl_pts * cfg.tp_rr
                candidates.append(OrderFlowSignal(
                    direction=TradeDirection.LONG,
                    entry_price=close, stop_loss=sl, take_profit=tp,
                    conviction=conviction, atr=atr_val,
                    bar_close_time=last["timestamp"],
                    setup="delta_imbalance",
                    notes="OF bullish delta imbalance continuation",
                ))

        if all_bear and avg_dr < -cfg.imbalance_delta_ratio:
            pause = close > float(m15.iloc[-2]["close"])
            if pause and (not cfg.vwap_filter or below_vwap):
                conviction = min(48.0 + abs(avg_dr) * 35.0, 85.0)
                sl = close + cfg.sl_pts
                tp = close - cfg.sl_pts * cfg.tp_rr
                candidates.append(OrderFlowSignal(
                    direction=TradeDirection.SHORT,
                    entry_price=close, stop_loss=sl, take_profit=tp,
                    conviction=conviction, atr=atr_val,
                    bar_close_time=last["timestamp"],
                    setup="delta_imbalance",
                    notes="OF bearish delta imbalance continuation",
                ))

    if not candidates:
        return None

    best = max(candidates, key=lambda s: s.conviction)
    if best.conviction < 40:
        return None

    logger.info(
        "ORDER FLOW %s | setup=%s VWAP=%s conviction=%.0f",
        best.direction, best.setup,
        f"{session_vwap:.0f}" if session_vwap else "N/A",
        best.conviction,
    )
    return best


def _score_absorption(big_vol: bool, small_body: bool, body: float, rng: float) -> float:
    score = 35.0
    if big_vol:
        score += 30.0
    if small_body:
        score += 20.0
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
