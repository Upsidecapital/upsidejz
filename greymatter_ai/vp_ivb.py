"""
Fabio Valentini — IVB (Initial Value Balance) for NAS100

Fabio's IVB is the volume profile of the FIRST HOUR of the NYSE session
(09:30–10:30 ET = 13:30–14:30 UTC), built from M5 bars.

Key levels:
  POC – Point of Control: price bin with the most volume in the first hour
  VAH – Value Area High:  upper edge of the 70% volume zone (Fabio's "fair value" top)
  VAL – Value Area Low:   lower edge of the 70% volume zone (Fabio's "fair value" bottom)

Two setups (Fabio Valentini):

  A. VAH / VAL REJECTION  (mean-reversion, higher probability)
     Price pokes beyond VAH or VAL, then closes back inside the value area.
     → LONG if price rejects VAL from below (dip into VAL, close back above)
       Entry ≈ close, TP = POC, SL = VAL − sl_buffer
     → SHORT if price rejects VAH from above (spike into VAH, close back below)
       Entry ≈ close, TP = POC, SL = VAH + sl_buffer

  B. IVB RE-ENTRY  (price left IVB and is returning to it)
     Price traded outside IVB and now the first bar to RE-ENTER the value area
     fires a mean-reversion trade toward the opposite extreme.
     → LONG re-entry through VAL (from below): TP = VAH, SL = below entry
     → SHORT re-entry through VAH (from above): TP = VAL, SL = above entry

Confirmation for both: volume spike + momentum bar in the direction of the trade.
No lookahead — only closed M5 bars are used.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, time
from typing import Optional, Tuple

import numpy as np
import pandas as pd

from config import (
    IVBConfig, STRATEGY_CONFIGS,
    NY_OPEN_UTC_HOUR, NY_OPEN_UTC_MINUTE,
    IVB_WINDOW_MINUTES,
)
from database import TradeDirection

logger = logging.getLogger(__name__)

CFG: IVBConfig = STRATEGY_CONFIGS.ivb

NY_OPEN = time(NY_OPEN_UTC_HOUR, NY_OPEN_UTC_MINUTE)   # 13:30 UTC


# ---------------------------------------------------------------------------
# Volume Profile helpers
# ---------------------------------------------------------------------------

def _bar_time(ts) -> time:
    if hasattr(ts, "time"):
        return ts.time()
    return ts.to_pydatetime().time()


def _bar_date(ts):
    if hasattr(ts, "date"):
        return ts.date()
    return ts.to_pydatetime().date()


def _build_profile(m5_window: pd.DataFrame, num_bins: int) -> Tuple[np.ndarray, np.ndarray]:
    """
    Build a volume-at-price histogram from a slice of M5 bars.
    Returns (bin_mids, bin_volumes).
    """
    lo = float(m5_window["low"].min())
    hi = float(m5_window["high"].max())
    if hi <= lo:
        return np.array([]), np.array([])

    edges = np.linspace(lo, hi, num_bins + 1)
    mids = (edges[:-1] + edges[1:]) / 2.0
    bin_vols = np.zeros(num_bins)

    for _, row in m5_window.iterrows():
        bar_lo = float(row["low"])
        bar_hi = float(row["high"])
        vol = float(row["volume"])
        bar_range = bar_hi - bar_lo
        if bar_range < 1e-6:
            idx = int(np.clip(np.searchsorted(edges, (bar_lo + bar_hi) / 2) - 1, 0, num_bins - 1))
            bin_vols[idx] += vol
            continue
        for b in range(num_bins):
            overlap = min(edges[b + 1], bar_hi) - max(edges[b], bar_lo)
            if overlap > 0:
                bin_vols[b] += vol * (overlap / bar_range)

    return mids, bin_vols


def _value_area(mids: np.ndarray, bin_vols: np.ndarray, pct: float = 0.70) -> Tuple[float, float, float]:
    """
    Expand outward from POC until `pct` of total volume is captured.
    Returns (POC, VAL, VAH).
    """
    poc_idx = int(np.argmax(bin_vols))
    poc = float(mids[poc_idx])
    target = bin_vols.sum() * pct
    accumulated = bin_vols[poc_idx]
    lo_idx = poc_idx
    hi_idx = poc_idx

    while accumulated < target:
        can_lo = lo_idx > 0
        can_hi = hi_idx < len(bin_vols) - 1
        if not can_lo and not can_hi:
            break
        vol_lo = bin_vols[lo_idx - 1] if can_lo else -1.0
        vol_hi = bin_vols[hi_idx + 1] if can_hi else -1.0
        if vol_lo >= vol_hi:
            lo_idx -= 1
            accumulated += bin_vols[lo_idx]
        else:
            hi_idx += 1
            accumulated += bin_vols[hi_idx]

    return poc, float(mids[lo_idx]), float(mids[hi_idx])


def _get_ivb_levels(
    m5: pd.DataFrame,
    window_minutes: int,
    num_bins: int,
    value_area_pct: float,
) -> Optional[Tuple[float, float, float, int]]:
    """
    Build the IVB from the most recent day's first `window_minutes` of M5 data.
    Returns (POC, VAL, VAH, ivb_end_idx) or None.
    """
    ts = m5["timestamp"]
    n = len(m5)
    ivb_bars = window_minutes // 5  # e.g. 60 min → 12 bars

    for i in range(n - 1, max(0, n - 400), -1):
        t = _bar_time(ts.iloc[i])
        d = _bar_date(ts.iloc[i])
        if t < NY_OPEN:
            continue

        # Walk back to session start
        start = i
        while start > 0:
            prev_d = _bar_date(ts.iloc[start - 1])
            prev_t = _bar_time(ts.iloc[start - 1])
            if prev_d != d or prev_t < NY_OPEN:
                break
            start -= 1

        ivb_end = start + ivb_bars - 1
        if ivb_end >= n:
            return None  # IVB window not yet complete

        window = m5.iloc[start: ivb_end + 1]
        mids, vols = _build_profile(window, num_bins)
        if len(mids) == 0 or vols.sum() == 0:
            return None

        poc, val, vah = _value_area(mids, vols, value_area_pct)
        return poc, val, vah, ivb_end

    return None


# ---------------------------------------------------------------------------
# Main detect
# ---------------------------------------------------------------------------

def detect(
    m5: pd.DataFrame,
    m15: pd.DataFrame,
    cfg: IVBConfig = CFG,
) -> Optional["IVBSignal"]:
    """
    Detect Fabio Valentini IVB setups on NAS100.
    Uses M5 bars to build the volume profile, M15 for entry confirmation.
    """
    min_m5 = cfg.ivb_bars_m5 + 20
    if len(m5) < min_m5 or len(m15) < 30:
        return None

    result = _get_ivb_levels(m5, cfg.ivb_window_minutes, cfg.num_bins, cfg.value_area_pct)
    if result is None:
        return None

    poc, val, vah, ivb_end_idx = result
    va_range = vah - val

    if va_range < cfg.min_va_range_pts:
        logger.debug("IVB skipped — VA range %.1f pts < min %.1f", va_range, cfg.min_va_range_pts)
        return None

    # Use M15 for entry signal (confirms after IVB is established)
    last = m15.iloc[-1]
    prev = m15.iloc[-2]
    close = float(last["close"])
    prev_close = float(prev["close"])
    body = abs(close - float(last["open"]))
    rng = float(last["high"]) - float(last["low"])
    vol_ma = float(m15["volume"].rolling(20).mean().iloc[-1])
    vol_spike = float(last["volume"]) > vol_ma * cfg.volume_confirm_mult
    bull_bar = close > float(last["open"])
    bear_bar = close < float(last["open"])

    candidates = []

    # ------------------------------------------------------------------
    # Setup A: VAL Rejection (LONG) — price dips to VAL then closes back inside
    # ------------------------------------------------------------------
    # Previous bar touched or poked below VAL, current bar closes above VAL
    val_rejected_long = (
        float(prev["low"]) <= val + cfg.level_tolerance_pts and
        close > val and
        bull_bar
    )
    if val_rejected_long:
        sl = val - cfg.sl_buffer_pts
        tp = poc
        if tp > close and (tp - close) > (close - sl):   # minimum 1:1 R:R
            conviction = _score_rejection(vol_spike, bull_bar, close, val, va_range)
            if conviction >= 40:
                candidates.append(IVBSignal(
                    direction=TradeDirection.LONG,
                    entry_price=close,
                    stop_loss=sl,
                    take_profit=tp,
                    conviction=conviction,
                    poc=poc, vah=vah, val=val,
                    bar_close_time=last["timestamp"],
                    setup="val_rejection",
                    notes=f"IVB VAL rejection → POC | VA {val:.0f}–{vah:.0f} POC {poc:.0f}",
                ))

    # ------------------------------------------------------------------
    # Setup A: VAH Rejection (SHORT) — price pokes above VAH, closes back inside
    # ------------------------------------------------------------------
    vah_rejected_short = (
        float(prev["high"]) >= vah - cfg.level_tolerance_pts and
        close < vah and
        bear_bar
    )
    if vah_rejected_short:
        sl = vah + cfg.sl_buffer_pts
        tp = poc
        if tp < close and (close - tp) > (sl - close):
            conviction = _score_rejection(vol_spike, bear_bar, close, vah, va_range)
            if conviction >= 40:
                candidates.append(IVBSignal(
                    direction=TradeDirection.SHORT,
                    entry_price=close,
                    stop_loss=sl,
                    take_profit=tp,
                    conviction=conviction,
                    poc=poc, vah=vah, val=val,
                    bar_close_time=last["timestamp"],
                    setup="vah_rejection",
                    notes=f"IVB VAH rejection → POC | VA {val:.0f}–{vah:.0f} POC {poc:.0f}",
                ))

    # ------------------------------------------------------------------
    # Setup B: IVB Re-entry from below (LONG) — was below VAL, now re-enters IVB
    # ------------------------------------------------------------------
    reentry_long = (
        prev_close < val and
        close >= val and
        close <= vah and
        bull_bar
    )
    if reentry_long:
        sl = val - cfg.sl_buffer_pts
        tp = vah
        conviction = _score_reentry(vol_spike, bull_bar, (close - val) / va_range)
        if conviction >= 40 and (tp - close) >= (close - sl):
            candidates.append(IVBSignal(
                direction=TradeDirection.LONG,
                entry_price=close,
                stop_loss=sl,
                take_profit=tp,
                conviction=conviction,
                poc=poc, vah=vah, val=val,
                bar_close_time=last["timestamp"],
                setup="ivb_reentry_long",
                notes=f"IVB re-entry long VAL→VAH | VA {val:.0f}–{vah:.0f}",
            ))

    # ------------------------------------------------------------------
    # Setup B: IVB Re-entry from above (SHORT) — was above VAH, now re-enters IVB
    # ------------------------------------------------------------------
    reentry_short = (
        prev_close > vah and
        close <= vah and
        close >= val and
        bear_bar
    )
    if reentry_short:
        sl = vah + cfg.sl_buffer_pts
        tp = val
        conviction = _score_reentry(vol_spike, bear_bar, (vah - close) / va_range)
        if conviction >= 40 and (close - tp) >= (sl - close):
            candidates.append(IVBSignal(
                direction=TradeDirection.SHORT,
                entry_price=close,
                stop_loss=sl,
                take_profit=tp,
                conviction=conviction,
                poc=poc, vah=vah, val=val,
                bar_close_time=last["timestamp"],
                setup="ivb_reentry_short",
                notes=f"IVB re-entry short VAH→VAL | VA {val:.0f}–{vah:.0f}",
            ))

    if not candidates:
        return None

    best = max(candidates, key=lambda s: s.conviction)
    logger.info(
        "IVB %s | setup=%s POC=%.0f VAL=%.0f VAH=%.0f conviction=%.0f",
        best.direction, best.setup, poc, val, vah, best.conviction,
    )
    return best


def _score_rejection(vol_spike: bool, momentum_bar: bool, close: float, level: float, va_range: float) -> float:
    score = 30.0
    if vol_spike:
        score += 25.0
    if momentum_bar:
        score += 20.0
    # Closer to the level = sharper rejection
    dist = abs(close - level)
    score += max(0.0, 25.0 - (dist / va_range) * 50.0)
    return min(score, 100.0)


def _score_reentry(vol_spike: bool, momentum_bar: bool, penetration_ratio: float) -> float:
    score = 30.0
    if vol_spike:
        score += 25.0
    if momentum_bar:
        score += 20.0
    # Shallow re-entry (close near VAL/VAH) = stronger signal
    score += max(0.0, 25.0 - penetration_ratio * 40.0)
    return min(score, 100.0)


@dataclass
class IVBSignal:
    direction: TradeDirection
    entry_price: float
    stop_loss: float
    take_profit: float
    conviction: float
    poc: float
    vah: float
    val: float
    bar_close_time: datetime
    setup: str = ""
    notes: str = ""

    # These two attributes make IVBSignal compatible with the orchestrator
    # (which reads .atr — not used for IVB sizing but kept for consistency)
    @property
    def atr(self) -> float:
        return self.vah - self.val   # use VA range as proxy
