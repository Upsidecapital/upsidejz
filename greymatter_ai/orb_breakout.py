"""
Fabio Valentini — ORB (Opening Range Breakout) for NAS100

Rules (Fabio Valentini methodology):
  Session   : NYSE regular open — 09:30 ET (13:30 UTC)
  IB window : first 30 minutes = 2 × M15 bars (09:30–10:00 ET)
  Breakout  : M15 bar closes OUTSIDE the Initial Balance with:
                • strong body (body/range ≥ body_ratio_min)
                • volume expansion vs 20-bar average
  Stop-loss : IB midpoint (Fabio's tight SL) ± sl_atr_mult × ATR buffer
  Take-profit: breakout side ± IB_range × tp_ib_mult
              (e.g. IB = 50 pts, tp_ib_mult=1.5 → TP = IB_high + 75 pts)
  Filter    : skip sessions where IB range < min_ib_range_pts (dead market)

No lookahead — only closed M15 bars are used.
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
    if hasattr(ts, "time"):
        return ts.time()
    return ts.to_pydatetime().time()


def _bar_date(ts):
    if hasattr(ts, "date"):
        return ts.date()
    return ts.to_pydatetime().date()


def _get_todays_ib(m15: pd.DataFrame, ib_bars: int) -> Optional[Tuple[float, float, int]]:
    """
    Locate the most recent NYSE open IB on the M15 series.
    Returns (ib_high, ib_low, ib_end_idx) — the index of the LAST IB bar.
    Searches from the tail backwards to handle intraday / multi-day frames.
    """
    ts = m15["timestamp"]
    n = len(m15)

    for i in range(n - 1, max(0, n - 300), -1):
        t = _bar_time(ts.iloc[i])
        d = _bar_date(ts.iloc[i])

        if t < NY_OPEN:
            continue

        # Find the first M15 bar of this NY session day
        session_start = i
        while session_start > 0:
            prev_d = _bar_date(ts.iloc[session_start - 1])
            prev_t = _bar_time(ts.iloc[session_start - 1])
            if prev_d != d or prev_t < NY_OPEN:
                break
            session_start -= 1

        # IB = first `ib_bars` bars of this session
        ib_end = session_start + ib_bars - 1
        if ib_end >= n:
            return None  # IB hasn't closed yet

        ib_slice = m15.iloc[session_start: ib_end + 1]
        ib_high = float(ib_slice["high"].max())
        ib_low = float(ib_slice["low"].min())
        return ib_high, ib_low, ib_end

    return None


# ---------------------------------------------------------------------------
# Main detect
# ---------------------------------------------------------------------------

def detect(
    m15: pd.DataFrame,
    cfg: ORBBreakoutConfig = CFG,
) -> Optional["ORBSignal"]:
    """
    Scan M15 for a confirmed Fabio Valentini ORB breakout on NAS100.
    Returns an ORBSignal or None.
    """
    if len(m15) < 50:
        return None

    result = _get_todays_ib(m15, cfg.ib_bars_m15)
    if result is None:
        return None

    ib_high, ib_low, ib_end_idx = result
    ib_range = ib_high - ib_low
    ib_mid = (ib_high + ib_low) / 2.0

    # Skip dead-market sessions
    if ib_range < cfg.min_ib_range_pts:
        logger.debug("ORB skipped — IB range %.1f pts < min %.1f", ib_range, cfg.min_ib_range_pts)
        return None

    # Only look at post-IB bars
    post_ib = m15.iloc[ib_end_idx + 1:]
    if len(post_ib) == 0:
        return None

    atr_val = float(_atr(m15).iloc[-1])
    if np.isnan(atr_val) or atr_val == 0:
        return None

    vol_ma = m15["volume"].rolling(20).mean()

    last = post_ib.iloc[-1]
    close = float(last["close"])
    open_ = float(last["open"])
    hi = float(last["high"])
    lo = float(last["low"])
    bar_range = hi - lo
    body = abs(close - open_)
    body_ratio = (body / bar_range) if bar_range > 0 else 0.0
    vol_spike = float(last["volume"]) > float(vol_ma.iloc[-1]) * cfg.volume_breakout_mult

    # --- LONG breakout: close above IB high ---
    if close > ib_high and open_ <= ib_high:
        conviction = _score(vol_spike, body_ratio, (close - ib_high) / ib_range, cfg)
        if conviction < 40:
            return None

        sl = (ib_mid - atr_val * cfg.sl_atr_mult) if cfg.sl_at_ib_mid else (ib_low - atr_val * cfg.sl_atr_mult)
        tp = ib_high + ib_range * cfg.tp_ib_mult

        logger.info(
            "ORB LONG  | IB=[%.0f–%.0f] range=%.0f pts SL=%.0f TP=%.0f conviction=%.0f",
            ib_low, ib_high, ib_range, sl, tp, conviction,
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
            notes=f"ORB long IB {ib_low:.0f}–{ib_high:.0f} ({ib_range:.0f}pts)",
        )

    # --- SHORT breakout: close below IB low ---
    if close < ib_low and open_ >= ib_low:
        conviction = _score(vol_spike, body_ratio, (ib_low - close) / ib_range, cfg)
        if conviction < 40:
            return None

        sl = (ib_mid + atr_val * cfg.sl_atr_mult) if cfg.sl_at_ib_mid else (ib_high + atr_val * cfg.sl_atr_mult)
        tp = ib_low - ib_range * cfg.tp_ib_mult

        logger.info(
            "ORB SHORT | IB=[%.0f–%.0f] range=%.0f pts SL=%.0f TP=%.0f conviction=%.0f",
            ib_low, ib_high, ib_range, sl, tp, conviction,
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
            notes=f"ORB short IB {ib_low:.0f}–{ib_high:.0f} ({ib_range:.0f}pts)",
        )

    return None


def _score(vol_spike: bool, body_ratio: float, extension_ratio: float, cfg: ORBBreakoutConfig) -> float:
    """0–100 conviction."""
    score = 30.0
    if vol_spike:
        score += 25.0
    score += min(body_ratio / cfg.body_ratio_min * 20.0, 25.0)
    score += min(extension_ratio * 60.0, 20.0)
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
