"""
Fabio Valentini — IVB (Initial Value Balance) for NAS100

KEY DISTINCTION (from research):
  IVB = ORB + Fixed-Range Volume Profile (FRVP) overlay on the IB window.
  It is NOT a standalone indicator — it provides precision entry zones for
  the post-ORB retest.

Flow:
  1. Build the 30-min Initial Balance (9:30–10:00 ET), identical to ORB.
  2. Compute FRVP over that same 30-min window from M5 bars:
       POC — price with most volume (acts as "market gravity")
       VAH — upper edge of 70% volume zone
       VAL — lower edge of 70% volume zone
  3. Wait for price to BREAK OUT of the IB (upside or downside).
  4. Wait for the PULLBACK into FRVP levels:
       - Pullback to VAH (long after upside break)
       - Pullback to VAL (short after downside break)
       - Deep pullback to POC (higher risk, higher R)
  5. Enter on M15 close that bounces off the FRVP zone with:
       - Momentum candle in breakout direction
       - Volume confirmation
       - VWAP alignment (above VWAP for longs, below for shorts)

"80% Rule" from AMT (Fabio uses this):
  If price re-enters the value area and holds for 2+ bars, there is high
  probability it traverses the entire value area to the opposite extreme.
  → Used here: if price re-enters IVB from above (bearish re-entry), target VAL.
              If price re-enters IVB from below (bullish re-entry), target VAH.

Stop-loss : sl_buffer_pts beyond the FRVP level.
No lookahead — only closed M5 + M15 bars used.
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
)
from database import TradeDirection

logger = logging.getLogger(__name__)

CFG: IVBConfig = STRATEGY_CONFIGS.ivb
NY_OPEN = time(NY_OPEN_UTC_HOUR, NY_OPEN_UTC_MINUTE)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

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
        seg = m15.iloc[start:]
        tp = (seg["high"] + seg["low"] + seg["close"]) / 3.0
        cum_vol = seg["volume"].cumsum().replace(0, np.nan)
        vwap_s = (tp * seg["volume"]).cumsum() / cum_vol
        return float(vwap_s.iloc[-1]) if not vwap_s.empty else None
    return None


def _build_frvp(m5_window: pd.DataFrame, num_bins: int) -> Tuple[np.ndarray, np.ndarray]:
    """Volume-at-price histogram from a M5 slice."""
    lo = float(m5_window["low"].min())
    hi = float(m5_window["high"].max())
    if hi <= lo:
        return np.array([]), np.array([])
    edges = np.linspace(lo, hi, num_bins + 1)
    mids = (edges[:-1] + edges[1:]) / 2.0
    bin_vols = np.zeros(num_bins)
    for _, row in m5_window.iterrows():
        blo, bhi, vol = float(row["low"]), float(row["high"]), float(row["volume"])
        rng = bhi - blo
        if rng < 1e-6:
            idx = int(np.clip(np.searchsorted(edges, (blo + bhi) / 2) - 1, 0, num_bins - 1))
            bin_vols[idx] += vol
            continue
        for b in range(num_bins):
            overlap = min(edges[b + 1], bhi) - max(edges[b], blo)
            if overlap > 0:
                bin_vols[b] += vol * (overlap / rng)
    return mids, bin_vols


def _value_area(mids: np.ndarray, vols: np.ndarray, pct: float = 0.70) -> Tuple[float, float, float]:
    """Returns (POC, VAL, VAH) expanding outward from POC."""
    poc_idx = int(np.argmax(vols))
    target = vols.sum() * pct
    acc = vols[poc_idx]
    lo_idx = hi_idx = poc_idx
    while acc < target:
        can_lo = lo_idx > 0
        can_hi = hi_idx < len(vols) - 1
        if not can_lo and not can_hi:
            break
        vlo = vols[lo_idx - 1] if can_lo else -1.0
        vhi = vols[hi_idx + 1] if can_hi else -1.0
        if vlo >= vhi:
            lo_idx -= 1; acc += vols[lo_idx]
        else:
            hi_idx += 1; acc += vols[hi_idx]
    return float(mids[poc_idx]), float(mids[lo_idx]), float(mids[hi_idx])


def _get_ib_and_frvp(
    m5: pd.DataFrame,
    ib_minutes: int,
    num_bins: int,
    va_pct: float,
) -> Optional[Tuple[float, float, float, float, float, int]]:
    """
    Find the most recent NYSE-session Initial Balance from M5 bars.
    Returns (ib_high, ib_low, poc, val, vah, ib_end_m5_idx) or None.
    IB = first `ib_minutes` minutes from NY open.
    """
    ib_bars = ib_minutes // 5
    ts = m5["timestamp"]
    n = len(m5)
    for i in range(n - 1, max(0, n - 600), -1):
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
        window = m5.iloc[start: ib_end + 1]
        ib_high = float(window["high"].max())
        ib_low = float(window["low"].min())
        mids, vols = _build_frvp(window, num_bins)
        if len(mids) == 0 or vols.sum() == 0:
            return None
        poc, val, vah = _value_area(mids, vols, va_pct)
        return ib_high, ib_low, poc, val, vah, ib_end
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
    Uses M5 FRVP for zone calculation, M15 for entry bar confirmation.
    """
    if len(m5) < cfg.ivb_bars_m5 + 20 or len(m15) < 30:
        return None

    frvp = _get_ib_and_frvp(m5, cfg.ivb_window_minutes, cfg.num_bins, cfg.value_area_pct)
    if frvp is None:
        return None

    ib_high, ib_low, poc, val, vah = frvp[:5]
    va_range = vah - val

    if va_range < cfg.min_va_range_pts:
        logger.debug("IVB skipped — VA range %.1f pts too narrow", va_range)
        return None

    session_vwap = _session_vwap(m15)
    last = m15.iloc[-1]
    prev = m15.iloc[-2]
    close = float(last["close"])
    open_ = float(last["open"])
    hi    = float(last["hi"]) if "hi" in last else float(last["high"])
    lo    = float(last["lo"]) if "lo" in last else float(last["low"])
    prev_close = float(prev["close"])

    body = abs(close - open_)
    rng = float(last["high"]) - float(last["low"])
    bull_bar = close > open_
    bear_bar = close < open_

    vol_ma = float(m15["volume"].rolling(20).mean().iloc[-1])
    vol_spike = float(last["volume"]) > vol_ma * cfg.volume_confirm_mult

    # Has price displaced beyond the IB? (required before any retest trade)
    post_ib_m15 = m15[m15["timestamp"] > m5["timestamp"].iloc[0]]
    if post_ib_m15.empty:
        post_ib_m15 = m15.iloc[-20:]

    displaced_long  = any(float(r["close"]) > ib_high for _, r in post_ib_m15.iloc[:-1].iterrows())
    displaced_short = any(float(r["close"]) < ib_low  for _, r in post_ib_m15.iloc[:-1].iterrows())

    candidates = []
    tol = cfg.level_tolerance_pts

    # -----------------------------------------------------------------------
    # LONG RETEST SETUPS (after upside displacement)
    # -----------------------------------------------------------------------
    if displaced_long:
        vwap_ok = (session_vwap is None) or (close > session_vwap)

        # Retest VAH (first pullback target, highest probability)
        if abs(close - vah) <= tol and close > val and bull_bar and vwap_ok:
            sl = vah - cfg.sl_buffer_pts
            tp = ib_high + (ib_high - ib_low) * 1.5   # TP = ib_high + 1.5×range
            if tp > close and (tp - close) >= (close - sl) * 1.3:
                conv = _score(vol_spike, bull_bar, close, vah, va_range, is_key_level=True)
                if conv >= 40:
                    candidates.append(IVBSignal(
                        direction=TradeDirection.LONG, entry_price=close,
                        stop_loss=sl, take_profit=tp, conviction=conv,
                        poc=poc, vah=vah, val=val,
                        bar_close_time=last["timestamp"],
                        setup="vah_retest_long",
                        notes=f"IVB VAH retest long | VA {val:.0f}–{vah:.0f} POC {poc:.0f}",
                    ))

        # Deep retest to POC (higher risk, higher R)
        if abs(close - poc) <= cfg.poc_tolerance_pts and bull_bar and vwap_ok:
            sl = poc - cfg.sl_buffer_pts
            tp = ib_high + (ib_high - ib_low) * 1.5
            if tp > close and (tp - close) >= (close - sl) * 1.5:
                conv = _score(vol_spike, bull_bar, close, poc, va_range, is_key_level=True)
                if conv >= 45:
                    candidates.append(IVBSignal(
                        direction=TradeDirection.LONG, entry_price=close,
                        stop_loss=sl, take_profit=tp, conviction=conv,
                        poc=poc, vah=vah, val=val,
                        bar_close_time=last["timestamp"],
                        setup="poc_retest_long",
                        notes=f"IVB POC deep retest long | POC {poc:.0f}",
                    ))

        # 80% Rule — re-entry from above: price pops back inside value area
        # → high probability of traversal to VAL
        if prev_close > vah and close <= vah and close >= val and bear_bar:
            sl = vah + cfg.sl_buffer_pts
            tp = val
            if (close - tp) >= (sl - close):
                conv = _score(vol_spike, bear_bar, close, vah, va_range, is_key_level=False)
                if conv >= 40:
                    candidates.append(IVBSignal(
                        direction=TradeDirection.SHORT, entry_price=close,
                        stop_loss=sl, take_profit=tp, conviction=conv,
                        poc=poc, vah=vah, val=val,
                        bar_close_time=last["timestamp"],
                        setup="80pct_reentry_short",
                        notes=f"IVB 80% rule re-entry short VAH→VAL | VA {val:.0f}–{vah:.0f}",
                    ))

    # -----------------------------------------------------------------------
    # SHORT RETEST SETUPS (after downside displacement)
    # -----------------------------------------------------------------------
    if displaced_short:
        vwap_ok = (session_vwap is None) or (close < session_vwap)

        # Retest VAL (first pullback target)
        if abs(close - val) <= tol and close < vah and bear_bar and vwap_ok:
            sl = val + cfg.sl_buffer_pts
            tp = ib_low - (ib_high - ib_low) * 1.5
            if tp < close and (close - tp) >= (sl - close) * 1.3:
                conv = _score(vol_spike, bear_bar, close, val, va_range, is_key_level=True)
                if conv >= 40:
                    candidates.append(IVBSignal(
                        direction=TradeDirection.SHORT, entry_price=close,
                        stop_loss=sl, take_profit=tp, conviction=conv,
                        poc=poc, vah=vah, val=val,
                        bar_close_time=last["timestamp"],
                        setup="val_retest_short",
                        notes=f"IVB VAL retest short | VA {val:.0f}–{vah:.0f} POC {poc:.0f}",
                    ))

        # Deep retest to POC
        if abs(close - poc) <= cfg.poc_tolerance_pts and bear_bar and vwap_ok:
            sl = poc + cfg.sl_buffer_pts
            tp = ib_low - (ib_high - ib_low) * 1.5
            if tp < close and (close - tp) >= (sl - close) * 1.5:
                conv = _score(vol_spike, bear_bar, close, poc, va_range, is_key_level=True)
                if conv >= 45:
                    candidates.append(IVBSignal(
                        direction=TradeDirection.SHORT, entry_price=close,
                        stop_loss=sl, take_profit=tp, conviction=conv,
                        poc=poc, vah=vah, val=val,
                        bar_close_time=last["timestamp"],
                        setup="poc_retest_short",
                        notes=f"IVB POC deep retest short | POC {poc:.0f}",
                    ))

        # 80% Rule — re-entry from below: price pops back inside value area
        if prev_close < val and close >= val and close <= vah and bull_bar:
            sl = val - cfg.sl_buffer_pts
            tp = vah
            if (tp - close) >= (close - sl):
                conv = _score(vol_spike, bull_bar, close, val, va_range, is_key_level=False)
                if conv >= 40:
                    candidates.append(IVBSignal(
                        direction=TradeDirection.LONG, entry_price=close,
                        stop_loss=sl, take_profit=tp, conviction=conv,
                        poc=poc, vah=vah, val=val,
                        bar_close_time=last["timestamp"],
                        setup="80pct_reentry_long",
                        notes=f"IVB 80% rule re-entry long VAL→VAH | VA {val:.0f}–{vah:.0f}",
                    ))

    if not candidates:
        return None

    best = max(candidates, key=lambda s: s.conviction)
    logger.info(
        "IVB %s | setup=%s POC=%.0f VAL=%.0f VAH=%.0f conviction=%.0f",
        best.direction, best.setup, poc, val, vah, best.conviction,
    )
    return best


def _score(vol_spike: bool, momentum_bar: bool, close: float, level: float, va_range: float, is_key_level: bool) -> float:
    score = 25.0
    if vol_spike:
        score += 25.0
    if momentum_bar:
        score += 20.0
    if is_key_level:
        score += 15.0
    # Proximity to level: tighter = stronger
    dist_pct = abs(close - level) / (va_range + 1e-9)
    score += max(0.0, 15.0 * (1.0 - dist_pct * 5.0))
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

    @property
    def atr(self) -> float:
        return self.vah - self.val   # VA range as ATR proxy for orchestrator
