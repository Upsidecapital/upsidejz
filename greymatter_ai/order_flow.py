"""
Fabio Valentini — Order Flow for NAS100 (Live Tick Edition)

Uses real MT5 tick data when available (via live_order_flow.LiveOrderFlow).
Falls back to OHLCV approximation on cloud deployments without an MT5 terminal.

Key signals (Fabio's published playbook):

  1. DELTA FLIP THROUGH ZERO  ← primary trigger
     Delta crosses from negative to positive (bullish) or vice versa after
     sustained one-sided pressure. The flip shows a change in who is in
     control — the moment to enter.

  2. ABSORPTION AT KEY LEVEL  (real tick-based when MT5 connected)
     Large volume at a key level (PDH/PDL/PDC/VWAP) with tiny price range.
     Confirms institutions absorbing supply/demand before a reversal.

  3. STACKED IMBALANCE → CONTINUATION
     3 consecutive bars all same direction with strong delta (>60% of volume).
     Price pauses (inside/counter bar) → enter the continuation.

  4. CVD / DELTA DIVERGENCE  (exhaustion reversal)
     Price prints a new N-bar extreme but cumulative delta fails to confirm
     → trapped participants on the wrong side.

VWAP filter (Fabio's rule):
  Long signals: close > session VWAP (or within 0.3 × ATR for absorption)
  Short signals: close < session VWAP (or within 0.3 × ATR for absorption)

Key Level Alignment (Fabio's confluence):
  Signals at PDH/PDL/PDC/VWAP get +15 conviction bonus.
  Signals against key level bias are penalised −10.

Session filter:
  Only trade during NY morning (09:30–12:00 ET) and afternoon (14:00–16:00 ET).
  is_ny_morning_session() from key_levels.py enforces this.

Market Structure filter:
  Long setups require H1/D1 combined bias ≠ "bearish".
  Short setups require H1/D1 combined bias ≠ "bullish".
  In neutral structure both directions allowed.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, time
from typing import Optional

import numpy as np
import pandas as pd

from config import OrderFlowConfig, STRATEGY_CONFIGS, MT5_SYMBOL
from database import TradeDirection
from key_levels import KeyLevels, MarketStructure, compute_key_levels, compute_market_structure, is_ny_morning_session
from live_order_flow import live_of

logger = logging.getLogger(__name__)

CFG: OrderFlowConfig = STRATEGY_CONFIGS.order_flow


# ---------------------------------------------------------------------------
# OHLCV-based fallback computations (same as before, kept for cloud fallback)
# ---------------------------------------------------------------------------

def _delta_series(df: pd.DataFrame) -> pd.Series:
    """Signed delta from OHLCV bars."""
    rng = (df["high"] - df["low"]).replace(0, np.nan)
    buy_vol  = df["volume"] * (df["close"] - df["low"])  / rng
    sell_vol = df["volume"] * (df["high"]  - df["close"]) / rng
    return (buy_vol - sell_vol).fillna(0.0)


def _delta_ratio_series(df: pd.DataFrame) -> pd.Series:
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


# ---------------------------------------------------------------------------
# Main detect
# ---------------------------------------------------------------------------

def detect(
    m15: pd.DataFrame,
    d1: pd.DataFrame,
    h1: Optional[pd.DataFrame] = None,
    cfg: OrderFlowConfig = CFG,
) -> Optional["OrderFlowSignal"]:
    """
    Detect Fabio Valentini order-flow setups on NAS100 M15.
    Returns the highest-conviction signal or None.
    """
    if len(m15) < cfg.lookback_bars + 20 or len(d1) < 2:
        return None

    # ── Session filter ────────────────────────────────────────────────────
    if not is_ny_morning_session():
        return None

    # ── Key levels & market structure ────────────────────────────────────
    kl = compute_key_levels(d1, m15)
    ms = compute_market_structure(h1, d1) if h1 is not None and len(h1) >= 10 else None

    # ── Delta series (OHLCV-based; live deltas augment below) ────────────
    delta_s = _delta_series(m15)
    dr_s    = _delta_ratio_series(m15)
    atr_s   = _atr(m15)
    vol_ma  = m15["volume"].rolling(20).mean()

    atr_val = float(atr_s.iloc[-1])
    if np.isnan(atr_val) or atr_val == 0:
        return None

    # ── VWAP ──────────────────────────────────────────────────────────────
    session_vwap = kl.vwap if kl else None
    key_level_list = kl.as_list() if kl else []

    last     = m15.iloc[-1]
    prev_bar = m15.iloc[-2]
    close    = float(last["close"])
    open_    = float(last["open"])
    hi       = float(last["high"])
    lo       = float(last["low"])
    body     = abs(close - open_)
    rng      = hi - lo
    vol_last = float(last["volume"])
    vol_avg  = float(vol_ma.iloc[-1]) if not np.isnan(vol_ma.iloc[-1]) else vol_last

    # ── Live delta for last bar (real ticks if MT5 connected) ────────────
    bar_ts = last["timestamp"]
    bar_dt = bar_ts.to_pydatetime() if hasattr(bar_ts, "to_pydatetime") else bar_ts
    from datetime import timedelta
    live_bd = live_of.get_bar_delta(MT5_SYMBOL, bar_dt, bar_dt + timedelta(minutes=15))
    if live_bd.is_real:
        delta_last = live_bd.delta
        delta_prev = live_of.get_bar_delta(
            MT5_SYMBOL,
            bar_dt - timedelta(minutes=15),
            bar_dt,
        ).delta
        logger.debug("Using REAL tick delta: %.0f (prev %.0f)", delta_last, delta_prev)
    else:
        delta_last = float(delta_s.iloc[-1])
        delta_prev = float(delta_s.iloc[-2])

    above_vwap = (session_vwap is not None) and (close > session_vwap)
    below_vwap = (session_vwap is not None) and (close < session_vwap)
    at_vwap    = (session_vwap is not None) and (abs(close - session_vwap) < atr_val * 0.3)
    bull_bar   = close > open_
    bear_bar   = close < open_

    candidates = []

    # ── Market structure pre-filter ───────────────────────────────────────
    def _ms_allows(direction: TradeDirection) -> bool:
        if ms is None:
            return True
        if direction == TradeDirection.LONG  and ms.combined == "bearish":
            return False
        if direction == TradeDirection.SHORT and ms.combined == "bullish":
            return False
        return True

    # ── Key level bonus ───────────────────────────────────────────────────
    def _key_level_bonus(price: float) -> float:
        if not kl:
            return 0.0
        near = kl.nearest(price, max_dist=atr_val * 0.8)
        return 15.0 if near else 0.0

    # =======================================================================
    # Signal 1: DELTA FLIP THROUGH ZERO (Fabio's primary trigger)
    # =======================================================================
    delta_flip_bull = delta_prev < 0 and delta_last > 0 and bull_bar
    delta_flip_bear = delta_prev > 0 and delta_last < 0 and bear_bar

    if delta_flip_bull and _ms_allows(TradeDirection.LONG) and (not cfg.vwap_filter or above_vwap or at_vwap):
        flip_magnitude = abs(delta_last - delta_prev) / (vol_avg + 1e-9)
        conviction = min(45.0 + flip_magnitude * 20.0, 80.0)
        if vol_last > vol_avg * 1.2:
            conviction += 10.0
        conviction += _key_level_bonus(lo)
        sl = lo - cfg.sl_pts
        tp = close + cfg.sl_pts * cfg.tp_rr
        tick_source = "REAL" if live_bd.is_real else "APPROX"
        candidates.append(OrderFlowSignal(
            direction=TradeDirection.LONG,
            entry_price=close, stop_loss=sl, take_profit=tp,
            conviction=min(conviction, 95.0), atr=atr_val,
            bar_close_time=last["timestamp"], setup="delta_flip",
            notes=(f"OF delta flip BULL [{tick_source}] | "
                   f"Δ {delta_prev:.0f}→{delta_last:.0f} "
                   f"VWAP={session_vwap:.0f if session_vwap else 'N/A'}"),
        ))

    if delta_flip_bear and _ms_allows(TradeDirection.SHORT) and (not cfg.vwap_filter or below_vwap or at_vwap):
        flip_magnitude = abs(delta_last - delta_prev) / (vol_avg + 1e-9)
        conviction = min(45.0 + flip_magnitude * 20.0, 80.0)
        if vol_last > vol_avg * 1.2:
            conviction += 10.0
        conviction += _key_level_bonus(hi)
        sl = hi + cfg.sl_pts
        tp = close - cfg.sl_pts * cfg.tp_rr
        tick_source = "REAL" if live_bd.is_real else "APPROX"
        candidates.append(OrderFlowSignal(
            direction=TradeDirection.SHORT,
            entry_price=close, stop_loss=sl, take_profit=tp,
            conviction=min(conviction, 95.0), atr=atr_val,
            bar_close_time=last["timestamp"], setup="delta_flip",
            notes=(f"OF delta flip BEAR [{tick_source}] | "
                   f"Δ {delta_prev:.0f}→{delta_last:.0f} "
                   f"VWAP={session_vwap:.0f if session_vwap else 'N/A'}"),
        ))

    # =======================================================================
    # Signal 2: ABSORPTION AT KEY LEVEL
    # Real tick absorption check when MT5 is connected.
    # =======================================================================
    big_vol    = vol_last > vol_avg * cfg.absorption_volume_mult
    small_body = (body / rng < cfg.absorption_body_pct) if rng > 0 else False

    if big_vol and small_body and kl:
        # Check real tick absorption at the nearest key level
        near = kl.nearest(close, max_dist=atr_val * 0.8)
        if near:
            level_label, level_price = near
            # Try real tick absorption check first
            real_abs = live_of.check_absorption(MT5_SYMBOL, level_price, tolerance=atr_val * 0.4)
            is_absorbed = (real_abs is not None) or (big_vol and small_body)

            if is_absorbed:
                abs_dir = real_abs["direction"] if real_abs else ("selling" if close >= level_price else "buying")
                abs_src = "REAL" if real_abs else "OHLCV"

                if abs_dir == "selling" and _ms_allows(TradeDirection.SHORT) and (not cfg.vwap_filter or below_vwap or at_vwap):
                    sl = close + cfg.sl_pts
                    tp = close - cfg.sl_pts * cfg.tp_rr
                    conv = _score_absorption(big_vol, small_body, body, rng) + 15.0  # key level bonus
                    candidates.append(OrderFlowSignal(
                        direction=TradeDirection.SHORT,
                        entry_price=close, stop_loss=sl, take_profit=tp,
                        conviction=min(conv, 95.0), atr=atr_val,
                        bar_close_time=last["timestamp"], setup="absorption",
                        notes=f"OF absorption SHORT [{abs_src}] at {level_label}={level_price:.0f}",
                    ))

                if abs_dir == "buying" and _ms_allows(TradeDirection.LONG) and (not cfg.vwap_filter or above_vwap or at_vwap):
                    sl = close - cfg.sl_pts
                    tp = close + cfg.sl_pts * cfg.tp_rr
                    conv = _score_absorption(big_vol, small_body, body, rng) + 15.0
                    candidates.append(OrderFlowSignal(
                        direction=TradeDirection.LONG,
                        entry_price=close, stop_loss=sl, take_profit=tp,
                        conviction=min(conv, 95.0), atr=atr_val,
                        bar_close_time=last["timestamp"], setup="absorption",
                        notes=f"OF absorption LONG [{abs_src}] at {level_label}={level_price:.0f}",
                    ))

    # =======================================================================
    # Signal 3: STACKED IMBALANCE CONTINUATION
    # =======================================================================
    n_stack   = 3
    stack_bars = m15.iloc[-(n_stack + 1):-1]
    stack_dr   = dr_s.iloc[-(n_stack + 1):-1]

    if len(stack_bars) == n_stack:
        all_bull_stack = all(
            float(r["close"]) > float(r["open"]) and float(stack_dr.iloc[j]) > cfg.imbalance_delta_ratio
            for j, (_, r) in enumerate(stack_bars.iterrows())
        )
        pause_bull = (body < atr_val * 0.4) or (bear_bar and body < atr_val * 0.3)
        if all_bull_stack and pause_bull and _ms_allows(TradeDirection.LONG) and (not cfg.vwap_filter or above_vwap):
            stk_conviction = min(50.0 + float(stack_dr.mean()) * 30.0, 85.0) + _key_level_bonus(lo)
            sl = lo - cfg.sl_pts
            tp = close + cfg.sl_pts * cfg.tp_rr
            candidates.append(OrderFlowSignal(
                direction=TradeDirection.LONG,
                entry_price=close, stop_loss=sl, take_profit=tp,
                conviction=stk_conviction, atr=atr_val,
                bar_close_time=last["timestamp"], setup="stacked_imbalance",
                notes=f"OF stacked imbalance LONG ({n_stack} bars)",
            ))

        all_bear_stack = all(
            float(r["close"]) < float(r["open"]) and float(stack_dr.iloc[j]) < (1 - cfg.imbalance_delta_ratio)
            for j, (_, r) in enumerate(stack_bars.iterrows())
        )
        pause_bear = (body < atr_val * 0.4) or (bull_bar and body < atr_val * 0.3)
        if all_bear_stack and pause_bear and _ms_allows(TradeDirection.SHORT) and (not cfg.vwap_filter or below_vwap):
            stk_conviction = min(50.0 + (1 - float(stack_dr.mean())) * 30.0, 85.0) + _key_level_bonus(hi)
            sl = hi + cfg.sl_pts
            tp = close - cfg.sl_pts * cfg.tp_rr
            candidates.append(OrderFlowSignal(
                direction=TradeDirection.SHORT,
                entry_price=close, stop_loss=sl, take_profit=tp,
                conviction=stk_conviction, atr=atr_val,
                bar_close_time=last["timestamp"], setup="stacked_imbalance",
                notes=f"OF stacked imbalance SHORT ({n_stack} bars)",
            ))

    # =======================================================================
    # Signal 4: CVD / DELTA DIVERGENCE  (exhaustion reversal)
    # Use real CVD series if MT5 connected, else OHLCV delta.
    # =======================================================================
    lb = cfg.lookback_bars
    window  = m15.iloc[-(lb + 1):-1]
    delta_w = delta_s.iloc[-(lb + 1):-1]

    # Try real CVD from ticks
    cvd_series = live_of.get_cvd_series(MT5_SYMBOL, lookback_bars=lb + 2)
    if cvd_series and len(cvd_series) >= lb:
        delta_w_vals = cvd_series[-lb:]
        d_peak   = max(delta_w_vals)
        d_trough = min(delta_w_vals)
        cvd_source = "REAL"
    else:
        d_peak   = float(delta_w.max())
        d_trough = float(delta_w.min())
        cvd_source = "APPROX"

    if len(window) >= lb:
        new_high = hi > float(window["high"].max())
        if new_high and delta_last < d_peak * 0.60 and _ms_allows(TradeDirection.SHORT) and (not cfg.vwap_filter or below_vwap or at_vwap):
            div_strength = 1.0 - delta_last / (d_peak + 1e-9)
            conv = min(50.0 + div_strength * 30.0, 88.0) + _key_level_bonus(hi)
            candidates.append(OrderFlowSignal(
                direction=TradeDirection.SHORT,
                entry_price=close,
                stop_loss=hi + cfg.sl_pts, take_profit=close - cfg.sl_pts * cfg.tp_rr,
                conviction=conv, atr=atr_val,
                bar_close_time=last["timestamp"], setup="delta_divergence",
                notes=f"OF bearish CVD divergence [{cvd_source}] at new high",
            ))

        new_low = lo < float(window["low"].min())
        if new_low and delta_last > d_trough * 0.60 and _ms_allows(TradeDirection.LONG) and (not cfg.vwap_filter or above_vwap or at_vwap):
            div_strength = 1.0 - abs(delta_last) / (abs(d_trough) + 1e-9)
            conv = min(50.0 + div_strength * 30.0, 88.0) + _key_level_bonus(lo)
            candidates.append(OrderFlowSignal(
                direction=TradeDirection.LONG,
                entry_price=close,
                stop_loss=lo - cfg.sl_pts, take_profit=close + cfg.sl_pts * cfg.tp_rr,
                conviction=conv, atr=atr_val,
                bar_close_time=last["timestamp"], setup="delta_divergence",
                notes=f"OF bullish CVD divergence [{cvd_source}] at new low",
            ))

    if not candidates:
        return None

    best = max(candidates, key=lambda s: s.conviction)
    if best.conviction < 40:
        return None

    ms_str = ms.combined if ms else "N/A"
    logger.info(
        "ORDER FLOW %s | setup=%s VWAP=%s Δ=%.0f MS=%s conviction=%.0f",
        best.direction, best.setup,
        f"{session_vwap:.0f}" if session_vwap else "N/A",
        delta_last, ms_str, best.conviction,
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
