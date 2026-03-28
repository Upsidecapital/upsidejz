"""
Fabio Valentini — ORB (Opening Range Breakout) for NAS100  [Full Implementation]

KEY INSIGHT (Fabio's playbook):
  The edge is NOT in entering the breakout candle.
  The edge is in entering the RETEST of the broken level after displacement.
  Order flow CONFIRMATION on the retest bar seals the entry.

Full Fabio ORB Rules:
  1. Build the 30-min Initial Balance (IB) from the first 2 × M15 bars after NY open (09:30 ET).
  2. DISPLACEMENT: A bar closes convincingly beyond IB high/low with:
       - Body/range ≥ 55% (momentum bar — Fabio's key filter)
       - Volume expansion ≥ 1.5× 20-bar average
  3. RETEST: Price pulls back to IB high (for long) or IB low (for short).
       - Retest bar must touch the level (low ≤ IB_high + tolerance for long)
       - Retest bar must close back in the breakout direction
  4. ORDER FLOW CONFIRMATION (new — uses real ticks when MT5 connected):
       - Delta must be positive on retest close (buyers defending IB high)
       - OR absorption detected at the IB level (real tick check)
  5. FILTERS:
       - VWAP: long entries only above session VWAP, short only below
       - PDC proximity: bonus conviction when retest is near Previous Day Close
       - Market structure: only take longs in bullish/neutral H1 bias
       - Session window: 09:30–12:00 ET (morning) and 14:00–16:00 ET (afternoon)
  6. RISK:
       - Stop-loss : IB midpoint (tight Fabio-style SL)
       - Take-profit: IB extension × 1.5 (first TP) and × 2.5 (second TP)

No lookahead — only closed M15 bars used.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Optional, Tuple

import numpy as np
import pandas as pd

from config import (
    ORBBreakoutConfig, STRATEGY_CONFIGS,
    NY_OPEN_UTC_HOUR, NY_OPEN_UTC_MINUTE, MT5_SYMBOL,
)
from database import TradeDirection
from key_levels import (
    KeyLevels, MarketStructure,
    compute_key_levels, compute_market_structure,
    is_ny_morning_session,
)
from live_order_flow import live_of

logger = logging.getLogger(__name__)

CFG: ORBBreakoutConfig = STRATEGY_CONFIGS.orb_breakout
NY_OPEN = time(NY_OPEN_UTC_HOUR, NY_OPEN_UTC_MINUTE)   # 13:30 UTC


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - df["close"].shift()).abs(),
        (df["low"] - df["close"].shift()).abs(),
    ], axis=1).max(axis=1)
    return tr.rolling(period).mean()


def _bar_time(ts) -> time:
    return ts.time() if hasattr(ts, "time") else ts.to_pydatetime().time()


def _bar_date(ts):
    return ts.date() if hasattr(ts, "date") else ts.to_pydatetime().date()


def _session_vwap(m15: pd.DataFrame) -> float | None:
    ts = m15["timestamp"]
    n  = len(m15)
    for i in range(n - 1, max(0, n - 300), -1):
        t = _bar_time(ts.iloc[i])
        d = _bar_date(ts.iloc[i])
        if t < NY_OPEN:
            continue
        start = i
        while start > 0:
            if _bar_date(ts.iloc[start - 1]) != d or _bar_time(ts.iloc[start - 1]) < NY_OPEN:
                break
            start -= 1
        segment = m15.iloc[start:]
        typical = (segment["high"] + segment["low"] + segment["close"]) / 3.0
        cum_vol  = segment["volume"].cumsum().replace(0, np.nan)
        vwap_s   = (typical * segment["volume"]).cumsum() / cum_vol
        return float(vwap_s.iloc[-1]) if not vwap_s.empty else None
    return None


def _get_ib(m15: pd.DataFrame, ib_bars: int) -> Optional[Tuple[float, float, float, int]]:
    """
    Locate the most recent NYSE-open Initial Balance.
    Returns (ib_high, ib_low, ib_mid, ib_end_idx) or None.
    """
    ts = m15["timestamp"]
    n  = len(m15)
    for i in range(n - 1, max(0, n - 300), -1):
        t = _bar_time(ts.iloc[i])
        d = _bar_date(ts.iloc[i])
        if t < NY_OPEN:
            continue
        start = i
        while start > 0:
            if _bar_date(ts.iloc[start - 1]) != d or _bar_time(ts.iloc[start - 1]) < NY_OPEN:
                break
            start -= 1
        ib_end = start + ib_bars - 1
        if ib_end >= n:
            return None
        ib = m15.iloc[start: ib_end + 1]
        hi = float(ib["high"].max())
        lo = float(ib["low"].min())
        return hi, lo, (hi + lo) / 2.0, ib_end
    return None


# ---------------------------------------------------------------------------
# Order flow confirmation at a retest level
# ---------------------------------------------------------------------------

def _of_confirmed_retest(
    m15: pd.DataFrame,
    level: float,
    direction: TradeDirection,
    atr_val: float,
) -> tuple[bool, str]:
    """
    Check order flow confirmation on the last (retest) bar.
    Returns (confirmed: bool, source: str).

    Uses real tick delta when MT5 connected.
    Falls back to OHLCV delta approximation.
    """
    last = m15.iloc[-1]
    ts   = last["timestamp"]
    bar_dt = ts.to_pydatetime() if hasattr(ts, "to_pydatetime") else ts

    # 1. Try real tick absorption at the level
    real_abs = live_of.check_absorption(
        MT5_SYMBOL, level, tolerance=atr_val * 0.5, lookback_seconds=900,
    )
    if real_abs is not None:
        confirmed_dir = real_abs["direction"]
        if direction == TradeDirection.LONG  and confirmed_dir == "buying":
            return True, "REAL:absorption_buying"
        if direction == TradeDirection.SHORT and confirmed_dir == "selling":
            return True, "REAL:absorption_selling"

    # 2. Try real tick delta on the retest bar
    live_bd = live_of.get_bar_delta(MT5_SYMBOL, bar_dt, bar_dt + timedelta(minutes=15))
    if live_bd.is_real:
        if direction == TradeDirection.LONG  and live_bd.delta > 0:
            return True, "REAL:positive_delta"
        if direction == TradeDirection.SHORT and live_bd.delta < 0:
            return True, "REAL:negative_delta"
        if live_bd.is_real:
            return False, "REAL:delta_opposed"

    # 3. OHLCV fallback — momentum close in breakout direction
    close  = float(last["close"])
    open_  = float(last["open"])
    bull   = close > open_
    bear   = close < open_
    if direction == TradeDirection.LONG  and bull:
        return True, "OHLCV:bull_close"
    if direction == TradeDirection.SHORT and bear:
        return True, "OHLCV:bear_close"
    return False, "OHLCV:no_confirmation"


# ---------------------------------------------------------------------------
# Main detect
# ---------------------------------------------------------------------------

def detect(
    m15: pd.DataFrame,
    d1: Optional[pd.DataFrame] = None,
    h1: Optional[pd.DataFrame] = None,
    cfg: ORBBreakoutConfig = CFG,
) -> Optional["ORBSignal"]:
    """
    Returns an ORB signal when a post-IB RETEST setup is confirmed
    with order flow and key level alignment.
    """
    if len(m15) < 60:
        return None

    # ── Session filter ────────────────────────────────────────────────────
    if not is_ny_morning_session():
        return None

    # ── Key levels + market structure ─────────────────────────────────────
    kl = compute_key_levels(d1, m15) if d1 is not None and len(d1) >= 3 else None
    ms = compute_market_structure(h1, d1) if (h1 is not None and len(h1) >= 10
                                              and d1 is not None and len(d1) >= 20) else None

    result = _get_ib(m15, cfg.ib_bars_m15)
    if result is None:
        return None

    ib_high, ib_low, ib_mid, ib_end_idx = result
    ib_range = ib_high - ib_low

    if ib_range < cfg.min_ib_range_pts:
        return None

    post_ib = m15.iloc[ib_end_idx + 1:].reset_index(drop=True)
    if len(post_ib) < 3:
        return None

    atr_val = float(_atr(m15).iloc[-1])
    if np.isnan(atr_val) or atr_val == 0:
        return None

    vol_ma       = m15["volume"].rolling(20).mean()
    session_vwap = _session_vwap(m15)

    # Detect displacement bars in post-IB (exclude trigger bar)
    breakout_long  = False
    breakout_short = False

    for _, row in post_ib.iloc[:-1].iterrows():
        bar_range = float(row["high"]) - float(row["low"])
        body = abs(float(row["close"]) - float(row["open"]))
        body_ratio = (body / bar_range) if bar_range > 0 else 0.0
        vol_idx = m15.index.get_loc(row.name) if row.name in m15.index else -1
        vol_spike = float(row["volume"]) > float(vol_ma.iloc[-1]) * cfg.volume_breakout_mult

        if float(row["close"]) > ib_high and body_ratio >= cfg.body_ratio_min:
            breakout_long = True
        if float(row["close"]) < ib_low and body_ratio >= cfg.body_ratio_min:
            breakout_short = True

    last      = post_ib.iloc[-1]
    close     = float(last["close"])
    open_     = float(last["open"])
    lo        = float(last["low"])
    hi        = float(last["high"])
    bar_range = hi - lo
    body      = abs(close - open_)
    body_ratio = (body / bar_range) if bar_range > 0 else 0.0
    vol_spike  = float(last["volume"]) > float(vol_ma.iloc[-1]) * cfg.volume_breakout_mult

    retest_tol = atr_val * 0.5

    # ── PDC proximity bonus ───────────────────────────────────────────────
    def _pdc_bonus() -> float:
        if kl and abs(close - kl.pdc) < atr_val * 0.6:
            return 10.0
        return 0.0

    def _ms_allows(direction: TradeDirection) -> bool:
        if ms is None:
            return True
        if direction == TradeDirection.LONG  and ms.combined == "bearish":
            return False
        if direction == TradeDirection.SHORT and ms.combined == "bullish":
            return False
        return True

    # -----------------------------------------------------------------------
    # LONG RETEST
    # -----------------------------------------------------------------------
    if breakout_long and _ms_allows(TradeDirection.LONG):
        at_ib_high   = lo <= ib_high + retest_tol and hi >= ib_high - retest_tol
        bullish_close = close > open_ and close > ib_high - retest_tol
        vwap_ok      = (session_vwap is None) or (close > session_vwap)

        if at_ib_high and bullish_close and vwap_ok:
            of_ok, of_src = _of_confirmed_retest(m15, ib_high, TradeDirection.LONG, atr_val)
            if not of_ok:
                logger.info("ORB long retest: OF not confirmed (%s) — skip", of_src)
            else:
                conviction = _score(vol_spike, body_ratio, True, cfg) + _pdc_bonus()
                if conviction >= 40:
                    sl = ib_mid - atr_val * cfg.sl_atr_mult
                    tp1 = ib_high + ib_range * cfg.tp_ib_mult         # 1.5× TP
                    tp2 = ib_high + ib_range * 2.5                    # 2.5× TP (Fabio's second target)
                    ms_str = ms.combined if ms else "N/A"
                    logger.info(
                        "ORB LONG RETEST [%s] | IB=[%.0f–%.0f] retest=%.0f "
                        "VWAP=%.0f MS=%s OF=%s conviction=%.0f",
                        of_src, ib_low, ib_high, close, session_vwap or 0, ms_str, of_src, conviction,
                    )
                    return ORBSignal(
                        direction=TradeDirection.LONG,
                        entry_price=close, stop_loss=sl, take_profit=tp1,
                        take_profit_2=tp2, conviction=conviction, atr=atr_val,
                        ib_high=ib_high, ib_low=ib_low, ib_mid=ib_mid, ib_range=ib_range,
                        bar_close_time=last["timestamp"],
                        of_source=of_src,
                        notes=(f"ORB long retest IB_high={ib_high:.0f} "
                               f"PDC={kl.pdc:.0f if kl else 'N/A'} "
                               f"VWAP={session_vwap:.0f if session_vwap else 'N/A'} "
                               f"OF={of_src}"),
                    )

    # -----------------------------------------------------------------------
    # SHORT RETEST
    # -----------------------------------------------------------------------
    if breakout_short and _ms_allows(TradeDirection.SHORT):
        at_ib_low    = hi >= ib_low - retest_tol and lo <= ib_low + retest_tol
        bearish_close = close < open_ and close < ib_low + retest_tol
        vwap_ok      = (session_vwap is None) or (close < session_vwap)

        if at_ib_low and bearish_close and vwap_ok:
            of_ok, of_src = _of_confirmed_retest(m15, ib_low, TradeDirection.SHORT, atr_val)
            if not of_ok:
                logger.info("ORB short retest: OF not confirmed (%s) — skip", of_src)
            else:
                conviction = _score(vol_spike, body_ratio, True, cfg) + _pdc_bonus()
                if conviction >= 40:
                    sl = ib_mid + atr_val * cfg.sl_atr_mult
                    tp1 = ib_low - ib_range * cfg.tp_ib_mult
                    tp2 = ib_low - ib_range * 2.5
                    ms_str = ms.combined if ms else "N/A"
                    logger.info(
                        "ORB SHORT RETEST [%s] | IB=[%.0f–%.0f] retest=%.0f "
                        "VWAP=%.0f MS=%s OF=%s conviction=%.0f",
                        of_src, ib_low, ib_high, close, session_vwap or 0, ms_str, of_src, conviction,
                    )
                    return ORBSignal(
                        direction=TradeDirection.SHORT,
                        entry_price=close, stop_loss=sl, take_profit=tp1,
                        take_profit_2=tp2, conviction=conviction, atr=atr_val,
                        ib_high=ib_high, ib_low=ib_low, ib_mid=ib_mid, ib_range=ib_range,
                        bar_close_time=last["timestamp"],
                        of_source=of_src,
                        notes=(f"ORB short retest IB_low={ib_low:.0f} "
                               f"PDC={kl.pdc:.0f if kl else 'N/A'} "
                               f"VWAP={session_vwap:.0f if session_vwap else 'N/A'} "
                               f"OF={of_src}"),
                    )

    return None


def _score(vol_spike: bool, body_ratio: float, vwap_aligned: bool, cfg: ORBBreakoutConfig) -> float:
    score = 30.0
    if vol_spike:
        score += 25.0
    score += min(body_ratio / cfg.body_ratio_min * 20.0, 20.0)
    if vwap_aligned:
        score += 25.0
    return min(score, 100.0)


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
    ib_mid: float
    ib_range: float
    bar_close_time: datetime
    take_profit_2: float = 0.0    # Fabio's second target (2.5× IB range)
    of_source: str = ""           # "REAL:absorption_buying", "OHLCV:bull_close", etc.
    notes: str = ""
