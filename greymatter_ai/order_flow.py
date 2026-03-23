"""
Fabio Valentini — Order Flow for NAS100

Approximates order-flow from OHLCV bars (no tick data required):
  buy_vol  = volume × (close − low)  / (high − low)
  sell_vol = volume × (high − close) / (high − low)
  delta    = buy_vol − sell_vol       (signed; positive = net buying)

Key signals (from Fabio's published playbook and community research):

  1. DELTA FLIP THROUGH ZERO  ← primary trigger
     delta crosses from negative to positive (bullish) or vice versa (bearish)
     after multiple bars of one-sided pressure. The flip shows a change in who
     is in control — the key moment to enter.

  2. ABSORPTION AT KEY LEVEL
     Large volume spike + tiny body (body/range < 20%) = institutions absorbing
     supply/demand. Fade the direction that drove into the level.
     Key levels: prev-day H/L, session VWAP, IB high/low.

  3. STACKED IMBALANCE → CONTINUATION  (Fabio: 2–3 bars required)
     Three consecutive bars all in the same direction with strong delta
     (one side > 60% of total volume). Price pauses (inside/counter bar) → enter.
     Delta imbalance threshold: one-side volume > 60% of bar volume.

  4. CVD / DELTA DIVERGENCE  (exhaustion reversal)
     Price prints new N-bar extreme but cumulative delta fails to confirm
     → shows trapped participants on the wrong side.

VWAP filter (Fabio's rule):
  Long signals: close > session VWAP (or within 0.3 × ATR for absorption)
  Short signals: close < session VWAP (or within 0.3 × ATR for absorption)

Setup grading:
  A (delta_flip + absorption + VWAP): full conviction
  B (two of three): medium conviction
  C (one signal only): below threshold — skipped

No lookahead — only closed M15 bars used.
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
# Core computations
# ---------------------------------------------------------------------------

def _delta(df: pd.DataFrame) -> pd.Series:
    """Signed delta: positive = net buy pressure."""
    rng = (df["high"] - df["low"]).replace(0, np.nan)
    buy_vol  = df["volume"] * (df["close"] - df["low"])  / rng
    sell_vol = df["volume"] * (df["high"]  - df["close"]) / rng
    return (buy_vol - sell_vol).fillna(0.0)


def _delta_ratio(df: pd.DataFrame) -> pd.Series:
    """Fraction of volume that is 'buy': 1.0 = all buy, 0.0 = all sell."""
    rng = (df["high"] - df["low"]).replace(0, np.nan)
    buy_vol = df["volume"] * (df["close"] - df["low"]) / rng
    return (buy_vol / df["volume"].replace(0, np.nan)).fillna(0.5)


def _atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - df["close"].shift()).abs(),
        (df["low"]  - df["close"].shift()).abs(),
    ], axis=1).max(axis=1)
    return tr.rolling(period).mean()


def _session_vwap(m15: pd.DataFrame) -> float | None:
    """Running VWAP from most recent NYSE open."""
    ts = m15["timestamp"]
    n = len(m15)
    for i in range(n - 1, max(0, n - 300), -1):
        t = ts.iloc[i].time() if hasattr(ts.iloc[i], "time") else ts.iloc[i].to_pydatetime().time()
        d = ts.iloc[i].date() if hasattr(ts.iloc[i], "date") else ts.iloc[i].to_pydatetime().date()
        if t < NY_OPEN:
            continue
        start = i
        while start > 0:
            prev_d = ts.iloc[start-1].date() if hasattr(ts.iloc[start-1], "date") else ts.iloc[start-1].to_pydatetime().date()
            prev_t = ts.iloc[start-1].time() if hasattr(ts.iloc[start-1], "time") else ts.iloc[start-1].to_pydatetime().time()
            if prev_d != d or prev_t < NY_OPEN:
                break
            start -= 1
        seg = m15.iloc[start:]
        tp = (seg["high"] + seg["low"] + seg["close"]) / 3.0
        cum_vol = seg["volume"].cumsum().replace(0, np.nan)
        vwap_s = (tp * seg["volume"]).cumsum() / cum_vol
        return float(vwap_s.iloc[-1]) if not vwap_s.empty else None
    return None


# ---------------------------------------------------------------------------
# Main detect
# ---------------------------------------------------------------------------

def detect(
    m15: pd.DataFrame,
    d1: pd.DataFrame,
    cfg: OrderFlowConfig = CFG,
) -> Optional["OrderFlowSignal"]:
    """
    Detect Fabio Valentini order-flow setups on NAS100 M15.
    Returns the highest-conviction signal or None.
    """
    if len(m15) < cfg.lookback_bars + 20 or len(d1) < 2:
        return None

    delta     = _delta(m15)
    dr        = _delta_ratio(m15)
    atr_s     = _atr(m15)
    vol_ma    = m15["volume"].rolling(20).mean()

    atr_val = float(atr_s.iloc[-1])
    if np.isnan(atr_val) or atr_val == 0:
        return None

    session_vwap = _session_vwap(m15)
    prev_high = float(d1["high"].iloc[-2]) if len(d1) >= 2 else None
    prev_low  = float(d1["low"].iloc[-2])  if len(d1) >= 2 else None
    key_levels = [l for l in [prev_high, prev_low, session_vwap] if l is not None]

    last     = m15.iloc[-1]
    prev_bar = m15.iloc[-2]
    close    = float(last["close"])
    open_    = float(last["open"])
    hi       = float(last["high"])
    lo       = float(last["low"])
    body     = abs(close - open_)
    rng      = hi - lo
    vol_last = float(last["volume"])
    vol_avg  = float(vol_ma.iloc[-1])

    delta_last = float(delta.iloc[-1])
    delta_prev = float(delta.iloc[-2])

    above_vwap = (session_vwap is not None) and (close > session_vwap)
    below_vwap = (session_vwap is not None) and (close < session_vwap)
    at_vwap    = (session_vwap is not None) and (abs(close - session_vwap) < atr_val * 0.3)
    bull_bar   = close > open_
    bear_bar   = close < open_

    candidates = []

    # =======================================================================
    # Signal 1: DELTA FLIP THROUGH ZERO  ← Fabio's primary trigger
    # =======================================================================
    # Bullish flip: previous delta was negative, current delta is positive
    delta_flip_bull = delta_prev < 0 and delta_last > 0 and bull_bar
    # Bearish flip: previous delta was positive, current delta is negative
    delta_flip_bear = delta_prev > 0 and delta_last < 0 and bear_bar

    if delta_flip_bull and (not cfg.vwap_filter or above_vwap or at_vwap):
        flip_magnitude = abs(delta_last - delta_prev) / (vol_avg + 1e-9)
        conviction = min(45.0 + flip_magnitude * 20.0, 80.0)
        if vol_last > vol_avg * 1.2:
            conviction += 10.0
        sl = lo - cfg.sl_pts
        tp = close + cfg.sl_pts * cfg.tp_rr
        candidates.append(OrderFlowSignal(
            direction=TradeDirection.LONG,
            entry_price=close, stop_loss=sl, take_profit=tp,
            conviction=min(conviction, 90.0), atr=atr_val,
            bar_close_time=last["timestamp"],
            setup="delta_flip",
            notes=f"OF delta flip BULL | Δ {delta_prev:.0f}→{delta_last:.0f} VWAP={session_vwap:.0f if session_vwap else 'N/A'}",
        ))

    if delta_flip_bear and (not cfg.vwap_filter or below_vwap or at_vwap):
        flip_magnitude = abs(delta_last - delta_prev) / (vol_avg + 1e-9)
        conviction = min(45.0 + flip_magnitude * 20.0, 80.0)
        if vol_last > vol_avg * 1.2:
            conviction += 10.0
        sl = hi + cfg.sl_pts
        tp = close - cfg.sl_pts * cfg.tp_rr
        candidates.append(OrderFlowSignal(
            direction=TradeDirection.SHORT,
            entry_price=close, stop_loss=sl, take_profit=tp,
            conviction=min(conviction, 90.0), atr=atr_val,
            bar_close_time=last["timestamp"],
            setup="delta_flip",
            notes=f"OF delta flip BEAR | Δ {delta_prev:.0f}→{delta_last:.0f} VWAP={session_vwap:.0f if session_vwap else 'N/A'}",
        ))

    # =======================================================================
    # Signal 2: ABSORPTION AT KEY LEVEL
    # =======================================================================
    big_vol   = vol_last > vol_avg * cfg.absorption_volume_mult
    small_body = (body / rng < cfg.absorption_body_pct) if rng > 0 else False

    if big_vol and small_body:
        for level in key_levels:
            near = abs(close - level) < atr_val * 0.6
            if not near:
                continue
            if level == prev_high or (session_vwap is not None and abs(level - session_vwap) < atr_val * 0.3):
                if not cfg.vwap_filter or below_vwap or at_vwap:
                    sl = close + cfg.sl_pts
                    tp = close - cfg.sl_pts * cfg.tp_rr
                    conv = _score_absorption(big_vol, small_body, body, rng)
                    candidates.append(OrderFlowSignal(
                        direction=TradeDirection.SHORT,
                        entry_price=close, stop_loss=sl, take_profit=tp,
                        conviction=conv, atr=atr_val,
                        bar_close_time=last["timestamp"],
                        setup="absorption",
                        notes=f"OF absorption SHORT at {level:.0f} | big vol small body",
                    ))
            if level == prev_low:
                if not cfg.vwap_filter or above_vwap or at_vwap:
                    sl = close - cfg.sl_pts
                    tp = close + cfg.sl_pts * cfg.tp_rr
                    conv = _score_absorption(big_vol, small_body, body, rng)
                    candidates.append(OrderFlowSignal(
                        direction=TradeDirection.LONG,
                        entry_price=close, stop_loss=sl, take_profit=tp,
                        conviction=conv, atr=atr_val,
                        bar_close_time=last["timestamp"],
                        setup="absorption",
                        notes=f"OF absorption LONG at {level:.0f} | big vol small body",
                    ))

    # =======================================================================
    # Signal 3: STACKED IMBALANCE CONTINUATION
    # Fabio: requires 2–3 bars; delta > 60% of volume on each bar
    # =======================================================================
    n_stack = 3
    stack_bars = m15.iloc[-(n_stack + 1):-1]
    stack_dr   = dr.iloc[-(n_stack + 1):-1]

    if len(stack_bars) == n_stack:
        # Bullish stack: all bars close up AND buy ratio > 60%
        all_bull_stack = all(
            float(r["close"]) > float(r["open"]) and float(stack_dr.iloc[j]) > cfg.imbalance_delta_ratio
            for j, (_, r) in enumerate(stack_bars.iterrows())
        )
        # Pause bar (inside / small counter-bar) = the entry bar
        pause_bull = (body < atr_val * 0.4) or (bear_bar and body < atr_val * 0.3)

        if all_bull_stack and pause_bull and (not cfg.vwap_filter or above_vwap):
            stk_conviction = min(50.0 + float(stack_dr.mean()) * 30.0, 85.0)
            sl = lo - cfg.sl_pts
            tp = close + cfg.sl_pts * cfg.tp_rr
            candidates.append(OrderFlowSignal(
                direction=TradeDirection.LONG,
                entry_price=close, stop_loss=sl, take_profit=tp,
                conviction=stk_conviction, atr=atr_val,
                bar_close_time=last["timestamp"],
                setup="stacked_imbalance",
                notes=f"OF stacked imbalance LONG ({n_stack} bars, DR>{cfg.imbalance_delta_ratio:.0%})",
            ))

        # Bearish stack: all bars close down AND sell ratio > 60%
        all_bear_stack = all(
            float(r["close"]) < float(r["open"]) and float(stack_dr.iloc[j]) < (1 - cfg.imbalance_delta_ratio)
            for j, (_, r) in enumerate(stack_bars.iterrows())
        )
        pause_bear = (body < atr_val * 0.4) or (bull_bar and body < atr_val * 0.3)

        if all_bear_stack and pause_bear and (not cfg.vwap_filter or below_vwap):
            stk_conviction = min(50.0 + (1 - float(stack_dr.mean())) * 30.0, 85.0)
            sl = hi + cfg.sl_pts
            tp = close - cfg.sl_pts * cfg.tp_rr
            candidates.append(OrderFlowSignal(
                direction=TradeDirection.SHORT,
                entry_price=close, stop_loss=sl, take_profit=tp,
                conviction=stk_conviction, atr=atr_val,
                bar_close_time=last["timestamp"],
                setup="stacked_imbalance",
                notes=f"OF stacked imbalance SHORT ({n_stack} bars, DR<{1-cfg.imbalance_delta_ratio:.0%})",
            ))

    # =======================================================================
    # Signal 4: DELTA DIVERGENCE / CVD EXHAUSTION
    # =======================================================================
    lb = cfg.lookback_bars
    window   = m15.iloc[-(lb + 1):-1]
    delta_w  = delta.iloc[-(lb + 1):-1]

    if len(window) >= lb:
        new_high = hi > float(window["high"].max())
        d_peak   = float(delta_w.max())
        if new_high and delta_last < d_peak * 0.60 and (not cfg.vwap_filter or below_vwap or at_vwap):
            div_strength = 1.0 - delta_last / (d_peak + 1e-9)
            conv = min(50.0 + div_strength * 30.0, 88.0)
            candidates.append(OrderFlowSignal(
                direction=TradeDirection.SHORT,
                entry_price=close, stop_loss=hi + cfg.sl_pts, take_profit=close - cfg.sl_pts * cfg.tp_rr,
                conviction=conv, atr=atr_val,
                bar_close_time=last["timestamp"],
                setup="delta_divergence",
                notes="OF bearish CVD divergence at new high",
            ))

        new_low  = lo < float(window["low"].min())
        d_trough = float(delta_w.min())
        if new_low and delta_last > d_trough * 0.60 and (not cfg.vwap_filter or above_vwap or at_vwap):
            div_strength = 1.0 - abs(delta_last) / (abs(d_trough) + 1e-9)
            conv = min(50.0 + div_strength * 30.0, 88.0)
            candidates.append(OrderFlowSignal(
                direction=TradeDirection.LONG,
                entry_price=close, stop_loss=lo - cfg.sl_pts, take_profit=close + cfg.sl_pts * cfg.tp_rr,
                conviction=conv, atr=atr_val,
                bar_close_time=last["timestamp"],
                setup="delta_divergence",
                notes="OF bullish CVD divergence at new low",
            ))

    if not candidates:
        return None

    best = max(candidates, key=lambda s: s.conviction)
    if best.conviction < 40:
        return None

    logger.info(
        "ORDER FLOW %s | setup=%s VWAP=%s Δ=%.0f conviction=%.0f",
        best.direction, best.setup,
        f"{session_vwap:.0f}" if session_vwap else "N/A",
        delta_last, best.conviction,
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
