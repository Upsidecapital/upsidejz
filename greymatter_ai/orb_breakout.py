"""
Fabio Valentini — ORB (Opening Range Breakout) for NAS100

KEY INSIGHT (from Fabio's playbook):
  The edge is NOT in entering the breakout candle.
  The edge is in entering the RETEST of the broken level after displacement.

Flow:
  1. Build the 30-min Initial Balance (9:30–10:00 ET) from M15 bars.
  2. Wait for price to DISPLACE beyond IB_high or IB_low with a strong close
     and volume expansion (the "breakout bar").
  3. Wait for price to PULL BACK and retest IB_high (now support) or IB_low
     (now resistance).
  4. Enter the RETEST BAR when it closes with momentum back in the
     breakout direction and price is on the correct side of session VWAP.

Stop-loss  : IB midpoint (Fabio's tight SL).
Take-profit: breakout_level ± IB_range × tp_ib_mult (default 1.5).

Filters:
  - VWAP: long entries only above session VWAP; short entries only below.
  - Skip if IB range < min_ib_range_pts (dead/too-tight market).
  - Breakout bar must have body/range ≥ body_ratio_min to prove momentum.

No lookahead — only closed M15 bars used.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, time
from typing import Optional, Tuple

import numpy as np
import pandas as pd

from config import (
    ORBBreakoutConfig, STRATEGY_CONFIGS,
    NY_OPEN_UTC_HOUR, NY_OPEN_UTC_MINUTE,
)
from database import TradeDirection

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
    """Running VWAP from the most recent NYSE open bar."""
    ts = m15["timestamp"]
    n = len(m15)
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
        cum_vol = segment["volume"].cumsum().replace(0, np.nan)
        vwap_series = (typical * segment["volume"]).cumsum() / cum_vol
        return float(vwap_series.iloc[-1]) if not vwap_series.empty else None
    return None


def _get_ib(m15: pd.DataFrame, ib_bars: int) -> Optional[Tuple[float, float, float, int]]:
    """
    Locate the most recent NYSE-open Initial Balance.
    Returns (ib_high, ib_low, ib_mid, ib_end_idx) or None.
    """
    ts = m15["timestamp"]
    n = len(m15)
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
# Main detect
# ---------------------------------------------------------------------------

def detect(
    m15: pd.DataFrame,
    cfg: ORBBreakoutConfig = CFG,
) -> Optional["ORBSignal"]:
    """
    Returns an ORB signal when a post-IB RETEST setup is confirmed.
    No signal is generated on the raw breakout bar — only on the retest.
    """
    if len(m15) < 60:
        return None

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

    vol_ma = m15["volume"].rolling(20).mean()
    session_vwap = _session_vwap(m15)

    # Detect if a breakout occurred anywhere in post_ib (not just the last bar)
    # "Breakout" = any bar that closed beyond IB with body ≥ body_ratio_min
    breakout_long  = False
    breakout_short = False

    for _, row in post_ib.iloc[:-1].iterrows():   # exclude the trigger bar
        bar_range = float(row["high"]) - float(row["low"])
        body = abs(float(row["close"]) - float(row["open"]))
        body_ratio = (body / bar_range) if bar_range > 0 else 0.0
        if float(row["close"]) > ib_high and body_ratio >= cfg.body_ratio_min:
            breakout_long = True
        if float(row["close"]) < ib_low and body_ratio >= cfg.body_ratio_min:
            breakout_short = True

    last = post_ib.iloc[-1]
    close = float(last["close"])
    open_ = float(last["open"])
    lo    = float(last["low"])
    hi    = float(last["high"])
    bar_range = hi - lo
    body = abs(close - open_)
    body_ratio = (body / bar_range) if bar_range > 0 else 0.0
    vol_spike = float(last["volume"]) > float(vol_ma.iloc[-1]) * cfg.volume_breakout_mult

    retest_tolerance = atr_val * 0.5

    # -----------------------------------------------------------------------
    # LONG RETEST: breakout above IB high already happened,
    # last bar returns to IB high (now support) and closes bullish
    # VWAP filter: close must be above session VWAP
    # -----------------------------------------------------------------------
    if breakout_long:
        at_ib_high = lo <= ib_high + retest_tolerance and hi >= ib_high - retest_tolerance
        bullish_close = close > open_ and close > ib_high - retest_tolerance
        vwap_ok = (session_vwap is None) or (close > session_vwap)

        if at_ib_high and bullish_close and vwap_ok:
            conviction = _score(vol_spike, body_ratio, True, cfg)
            if conviction >= 40:
                sl = ib_mid - atr_val * cfg.sl_atr_mult
                tp = ib_high + ib_range * cfg.tp_ib_mult
                logger.info(
                    "ORB LONG RETEST | IB=[%.0f–%.0f] retest=%.0f VWAP=%.0f conviction=%.0f",
                    ib_low, ib_high, close, session_vwap or 0, conviction,
                )
                return ORBSignal(
                    direction=TradeDirection.LONG,
                    entry_price=close,
                    stop_loss=sl,
                    take_profit=tp,
                    conviction=conviction,
                    atr=atr_val,
                    ib_high=ib_high, ib_low=ib_low, ib_mid=ib_mid, ib_range=ib_range,
                    bar_close_time=last["timestamp"],
                    notes=f"ORB long retest IB_high={ib_high:.0f} VWAP={session_vwap:.0f if session_vwap else 'N/A'}",
                )

    # -----------------------------------------------------------------------
    # SHORT RETEST: breakout below IB low already happened,
    # last bar returns to IB low (now resistance) and closes bearish
    # VWAP filter: close must be below session VWAP
    # -----------------------------------------------------------------------
    if breakout_short:
        at_ib_low = hi >= ib_low - retest_tolerance and lo <= ib_low + retest_tolerance
        bearish_close = close < open_ and close < ib_low + retest_tolerance
        vwap_ok = (session_vwap is None) or (close < session_vwap)

        if at_ib_low and bearish_close and vwap_ok:
            conviction = _score(vol_spike, body_ratio, True, cfg)
            if conviction >= 40:
                sl = ib_mid + atr_val * cfg.sl_atr_mult
                tp = ib_low - ib_range * cfg.tp_ib_mult
                logger.info(
                    "ORB SHORT RETEST | IB=[%.0f–%.0f] retest=%.0f VWAP=%.0f conviction=%.0f",
                    ib_low, ib_high, close, session_vwap or 0, conviction,
                )
                return ORBSignal(
                    direction=TradeDirection.SHORT,
                    entry_price=close,
                    stop_loss=sl,
                    take_profit=tp,
                    conviction=conviction,
                    atr=atr_val,
                    ib_high=ib_high, ib_low=ib_low, ib_mid=ib_mid, ib_range=ib_range,
                    bar_close_time=last["timestamp"],
                    notes=f"ORB short retest IB_low={ib_low:.0f} VWAP={session_vwap:.0f if session_vwap else 'N/A'}",
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
    notes: str = ""
