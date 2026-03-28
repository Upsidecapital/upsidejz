"""
Fabio Valentini — IVB (Initial Value Balance) for NAS100  [Full Implementation]

KEY DISTINCTION (Fabio's playbook):
  IVB = ORB + Fixed-Range Volume Profile (FRVP) overlay on the IB window.
  It is NOT a standalone indicator — it provides precision entry zones for
  the post-ORB retest.

Full Fabio IVB Rules:
  1. Build the 60-min Initial Balance (9:30–10:30 ET) from M5 bars.
  2. Compute FRVP over that window:
       POC — price with most volume  ("market gravity")
       VAH — upper edge of 70% volume zone
       VAL — lower edge of 70% volume zone
  3. Require price displacement BEYOND the IB before any retest trade.
  4. Two primary entry setups:
       A. VAH/VAL Retest  (highest probability, Fabio's preferred)
          - Price broke out, pulled back to VAH (long) or VAL (short)
          - Entry on M15 close back in breakout direction with OF confirmation
       B. POC Deep Retest (higher R, but tighter entry window)
          - Pulled all the way back to POC — shows strong momentum returning
  5. 80% Rule (AMT — Area of Value):
       If price re-enters the value area and holds ≥ 2 bars, high probability
       it will traverse to the opposite extreme.
       → Re-entry from above VAH: target VAL (short setup)
       → Re-entry from below VAL: target VAH (long setup)
  6. ORDER FLOW CONFIRMATION at levels (real ticks when MT5 connected):
       - Real tick absorption OR delta direction at the level
       - Fallback to momentum candle close
  7. FILTERS:
       - Key levels: PDC/VWAP proximity bonus
       - Market structure: H1/D1 bias alignment
       - Session time window: 09:30–12:00 ET
       - Minimum VA range to avoid dead/too-tight markets

No lookahead — only closed M5 + M15 bars used.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Optional, Tuple

import numpy as np
import pandas as pd

from config import (
    IVBConfig, STRATEGY_CONFIGS,
    NY_OPEN_UTC_HOUR, NY_OPEN_UTC_MINUTE, MT5_SYMBOL,
)
from database import TradeDirection
from key_levels import (
    compute_key_levels, compute_market_structure, is_ny_morning_session,
)
from live_order_flow import live_of

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
        seg = m15.iloc[start:]
        tp = (seg["high"] + seg["low"] + seg["close"]) / 3.0
        cum_vol = seg["volume"].cumsum().replace(0, np.nan)
        vwap_s  = (tp * seg["volume"]).cumsum() / cum_vol
        return float(vwap_s.iloc[-1]) if not vwap_s.empty else None
    return None


def _build_frvp(m5_window: pd.DataFrame, num_bins: int) -> Tuple[np.ndarray, np.ndarray]:
    """Volume-at-price histogram from a M5 slice."""
    lo = float(m5_window["low"].min())
    hi = float(m5_window["high"].max())
    if hi <= lo:
        return np.array([]), np.array([])
    edges    = np.linspace(lo, hi, num_bins + 1)
    mids     = (edges[:-1] + edges[1:]) / 2.0
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
    target  = vols.sum() * pct
    acc     = vols[poc_idx]
    lo_idx  = hi_idx = poc_idx
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
    m5: pd.DataFrame, ib_minutes: int, num_bins: int, va_pct: float,
) -> Optional[Tuple[float, float, float, float, float, int]]:
    ib_bars = ib_minutes // 5
    ts = m5["timestamp"]
    n  = len(m5)
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
        window   = m5.iloc[start: ib_end + 1]
        ib_high  = float(window["high"].max())
        ib_low   = float(window["low"].min())
        mids, vols = _build_frvp(window, num_bins)
        if len(mids) == 0 or vols.sum() == 0:
            return None
        poc, val, vah = _value_area(mids, vols, va_pct)
        return ib_high, ib_low, poc, val, vah, ib_end
    return None


# ---------------------------------------------------------------------------
# Order flow confirmation at a level
# ---------------------------------------------------------------------------

def _of_confirmed_at_level(
    level: float,
    direction: TradeDirection,
    atr_val: float,
    last_bar: pd.Series,
) -> tuple[bool, str]:
    """
    Real tick absorption or delta confirmation at the given level.
    Falls back to momentum candle close.
    """
    # 1. Real tick absorption
    real_abs = live_of.check_absorption(
        MT5_SYMBOL, level, tolerance=atr_val * 0.5, lookback_seconds=900,
    )
    if real_abs is not None:
        dir_match = (direction == TradeDirection.LONG  and real_abs["direction"] == "buying") or \
                    (direction == TradeDirection.SHORT and real_abs["direction"] == "selling")
        if dir_match:
            return True, f"REAL:absorption_{real_abs['direction']}"

    # 2. Real tick delta on the entry bar
    ts    = last_bar["timestamp"]
    bar_dt = ts.to_pydatetime() if hasattr(ts, "to_pydatetime") else ts
    live_bd = live_of.get_bar_delta(MT5_SYMBOL, bar_dt, bar_dt + timedelta(minutes=15))
    if live_bd.is_real:
        if direction == TradeDirection.LONG  and live_bd.delta > 0:
            return True, "REAL:positive_delta"
        if direction == TradeDirection.SHORT and live_bd.delta < 0:
            return True, "REAL:negative_delta"
        return False, "REAL:delta_opposed"

    # 3. Momentum candle fallback
    close = float(last_bar["close"])
    open_ = float(last_bar["open"])
    if direction == TradeDirection.LONG  and close > open_:
        return True, "OHLCV:bull_close"
    if direction == TradeDirection.SHORT and close < open_:
        return True, "OHLCV:bear_close"
    return False, "OHLCV:no_confirmation"


# ---------------------------------------------------------------------------
# Main detect
# ---------------------------------------------------------------------------

def detect(
    m5: pd.DataFrame,
    m15: pd.DataFrame,
    d1: Optional[pd.DataFrame] = None,
    h1: Optional[pd.DataFrame] = None,
    cfg: IVBConfig = CFG,
) -> Optional["IVBSignal"]:
    """
    Detect Fabio Valentini IVB setups on NAS100.
    Uses M5 FRVP for zone calculation, M15 for entry bar confirmation.
    """
    if len(m5) < cfg.ivb_bars_m5 + 20 or len(m15) < 30:
        return None

    # ── Session filter ────────────────────────────────────────────────────
    if not is_ny_morning_session():
        return None

    # ── FRVP ─────────────────────────────────────────────────────────────
    frvp = _get_ib_and_frvp(m5, cfg.ivb_window_minutes, cfg.num_bins, cfg.value_area_pct)
    if frvp is None:
        return None

    ib_high, ib_low, poc, val, vah = frvp[:5]
    va_range = vah - val

    if va_range < cfg.min_va_range_pts:
        logger.debug("IVB skipped — VA range %.1f pts too narrow", va_range)
        return None

    # ── Key levels + market structure ─────────────────────────────────────
    kl = compute_key_levels(d1, m15) if d1 is not None and len(d1) >= 3 else None
    ms = compute_market_structure(h1, d1) if (h1 is not None and len(h1) >= 10
                                              and d1 is not None and len(d1) >= 20) else None

    session_vwap = _session_vwap(m15)
    last      = m15.iloc[-1]
    prev      = m15.iloc[-2]
    close     = float(last["close"])
    open_     = float(last["open"])
    hi        = float(last["high"])
    lo        = float(last["low"])
    prev_close = float(prev["close"])
    body      = abs(close - open_)
    rng       = hi - lo
    bull_bar  = close > open_
    bear_bar  = close < open_
    atr_val   = va_range / 2.0  # proxy ATR from VA range

    vol_ma    = float(m15["volume"].rolling(20).mean().iloc[-1])
    vol_spike = float(last["volume"]) > vol_ma * cfg.volume_confirm_mult

    # Displacement check
    post_ib_m15 = m15[m15["timestamp"] > m5["timestamp"].iloc[0]]
    if post_ib_m15.empty:
        post_ib_m15 = m15.iloc[-20:]

    displaced_long  = any(float(r["close"]) > ib_high for _, r in post_ib_m15.iloc[:-1].iterrows())
    displaced_short = any(float(r["close"]) < ib_low  for _, r in post_ib_m15.iloc[:-1].iterrows())

    def _ms_allows(direction: TradeDirection) -> bool:
        if ms is None:
            return True
        if direction == TradeDirection.LONG  and ms.combined == "bearish":
            return False
        if direction == TradeDirection.SHORT and ms.combined == "bullish":
            return False
        return True

    def _pdc_bonus() -> float:
        if kl and abs(close - kl.pdc) < atr_val * 0.8:
            return 10.0
        return 0.0

    tol = cfg.level_tolerance_pts
    candidates = []

    # -----------------------------------------------------------------------
    # LONG RETEST SETUPS
    # -----------------------------------------------------------------------
    if displaced_long and _ms_allows(TradeDirection.LONG):
        vwap_ok = (session_vwap is None) or (close > session_vwap)

        # A. VAH retest
        if abs(close - vah) <= tol and close > val and bull_bar and vwap_ok:
            of_ok, of_src = _of_confirmed_at_level(vah, TradeDirection.LONG, atr_val, last)
            if of_ok:
                sl   = vah - cfg.sl_buffer_pts
                tp   = ib_high + (ib_high - ib_low) * 1.5
                if tp > close and (tp - close) >= (close - sl) * 1.3:
                    conv = _score(vol_spike, bull_bar, close, vah, va_range, True) + _pdc_bonus()
                    if conv >= 40:
                        candidates.append(IVBSignal(
                            direction=TradeDirection.LONG, entry_price=close,
                            stop_loss=sl, take_profit=tp, conviction=conv,
                            poc=poc, vah=vah, val=val,
                            bar_close_time=last["timestamp"], setup="vah_retest_long",
                            of_source=of_src,
                            notes=f"IVB VAH retest long [{of_src}] | VA {val:.0f}–{vah:.0f} POC {poc:.0f}",
                        ))

        # B. POC deep retest
        if abs(close - poc) <= cfg.poc_tolerance_pts and bull_bar and vwap_ok:
            of_ok, of_src = _of_confirmed_at_level(poc, TradeDirection.LONG, atr_val, last)
            if of_ok:
                sl = poc - cfg.sl_buffer_pts
                tp = ib_high + (ib_high - ib_low) * 1.5
                if tp > close and (tp - close) >= (close - sl) * 1.5:
                    conv = _score(vol_spike, bull_bar, close, poc, va_range, True) + _pdc_bonus()
                    if conv >= 45:
                        candidates.append(IVBSignal(
                            direction=TradeDirection.LONG, entry_price=close,
                            stop_loss=sl, take_profit=tp, conviction=conv,
                            poc=poc, vah=vah, val=val,
                            bar_close_time=last["timestamp"], setup="poc_retest_long",
                            of_source=of_src,
                            notes=f"IVB POC deep retest long [{of_src}] | POC {poc:.0f}",
                        ))

        # C. 80% Rule — re-entry short (price pops back inside from above)
        if prev_close > vah and close <= vah and close >= val and bear_bar:
            of_ok, of_src = _of_confirmed_at_level(vah, TradeDirection.SHORT, atr_val, last)
            if of_ok:
                sl = vah + cfg.sl_buffer_pts
                tp = val
                if (close - tp) >= (sl - close):
                    conv = _score(vol_spike, bear_bar, close, vah, va_range, False) + _pdc_bonus()
                    if conv >= 40:
                        candidates.append(IVBSignal(
                            direction=TradeDirection.SHORT, entry_price=close,
                            stop_loss=sl, take_profit=tp, conviction=conv,
                            poc=poc, vah=vah, val=val,
                            bar_close_time=last["timestamp"], setup="80pct_reentry_short",
                            of_source=of_src,
                            notes=f"IVB 80% rule short VAH→VAL [{of_src}] | VA {val:.0f}–{vah:.0f}",
                        ))

    # -----------------------------------------------------------------------
    # SHORT RETEST SETUPS
    # -----------------------------------------------------------------------
    if displaced_short and _ms_allows(TradeDirection.SHORT):
        vwap_ok = (session_vwap is None) or (close < session_vwap)

        # A. VAL retest
        if abs(close - val) <= tol and close < vah and bear_bar and vwap_ok:
            of_ok, of_src = _of_confirmed_at_level(val, TradeDirection.SHORT, atr_val, last)
            if of_ok:
                sl = val + cfg.sl_buffer_pts
                tp = ib_low - (ib_high - ib_low) * 1.5
                if tp < close and (close - tp) >= (sl - close) * 1.3:
                    conv = _score(vol_spike, bear_bar, close, val, va_range, True) + _pdc_bonus()
                    if conv >= 40:
                        candidates.append(IVBSignal(
                            direction=TradeDirection.SHORT, entry_price=close,
                            stop_loss=sl, take_profit=tp, conviction=conv,
                            poc=poc, vah=vah, val=val,
                            bar_close_time=last["timestamp"], setup="val_retest_short",
                            of_source=of_src,
                            notes=f"IVB VAL retest short [{of_src}] | VA {val:.0f}–{vah:.0f} POC {poc:.0f}",
                        ))

        # B. POC deep retest
        if abs(close - poc) <= cfg.poc_tolerance_pts and bear_bar and vwap_ok:
            of_ok, of_src = _of_confirmed_at_level(poc, TradeDirection.SHORT, atr_val, last)
            if of_ok:
                sl = poc + cfg.sl_buffer_pts
                tp = ib_low - (ib_high - ib_low) * 1.5
                if tp < close and (close - tp) >= (sl - close) * 1.5:
                    conv = _score(vol_spike, bear_bar, close, poc, va_range, True) + _pdc_bonus()
                    if conv >= 45:
                        candidates.append(IVBSignal(
                            direction=TradeDirection.SHORT, entry_price=close,
                            stop_loss=sl, take_profit=tp, conviction=conv,
                            poc=poc, vah=vah, val=val,
                            bar_close_time=last["timestamp"], setup="poc_retest_short",
                            of_source=of_src,
                            notes=f"IVB POC deep retest short [{of_src}] | POC {poc:.0f}",
                        ))

        # C. 80% Rule — re-entry long (price pops back inside from below)
        if prev_close < val and close >= val and close <= vah and bull_bar:
            of_ok, of_src = _of_confirmed_at_level(val, TradeDirection.LONG, atr_val, last)
            if of_ok:
                sl = val - cfg.sl_buffer_pts
                tp = vah
                if (tp - close) >= (close - sl):
                    conv = _score(vol_spike, bull_bar, close, val, va_range, False) + _pdc_bonus()
                    if conv >= 40:
                        candidates.append(IVBSignal(
                            direction=TradeDirection.LONG, entry_price=close,
                            stop_loss=sl, take_profit=tp, conviction=conv,
                            poc=poc, vah=vah, val=val,
                            bar_close_time=last["timestamp"], setup="80pct_reentry_long",
                            of_source=of_src,
                            notes=f"IVB 80% rule long VAL→VAH [{of_src}] | VA {val:.0f}–{vah:.0f}",
                        ))

    if not candidates:
        return None

    best = max(candidates, key=lambda s: s.conviction)
    ms_str = ms.combined if ms else "N/A"
    logger.info(
        "IVB %s | setup=%s POC=%.0f VAL=%.0f VAH=%.0f MS=%s OF=%s conviction=%.0f",
        best.direction, best.setup, poc, val, vah, ms_str, best.of_source, best.conviction,
    )
    return best


def _score(
    vol_spike: bool, momentum_bar: bool,
    close: float, level: float, va_range: float, is_key_level: bool,
) -> float:
    score = 25.0
    if vol_spike:
        score += 25.0
    if momentum_bar:
        score += 20.0
    if is_key_level:
        score += 15.0
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
    of_source: str = ""
    notes: str = ""

    @property
    def atr(self) -> float:
        return self.vah - self.val
