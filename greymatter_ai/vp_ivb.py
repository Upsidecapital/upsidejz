"""
Strategy — Volume Profile / Initial Value Balance (IVB)

Builds a volume-at-price histogram from the last N H1 bars.
Identifies:
  POC – Point of Control: price level with the highest traded volume
  VAH – Value Area High:  upper bound of the 70 % volume zone
  VAL – Value Area Low:   lower bound of the 70 % volume zone

Trade logic (per spec):
  LONG  – price is near POC coming from the VAL side
          Entry ≈ POC,  TP = VAH,  SL = VAL
  SHORT – price is near POC coming from the VAH side
          Entry ≈ POC,  TP = VAL,  SL = VAH

Confirmation: M15 momentum candle + volume above average.
No lookahead: only closed bars used.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Tuple

import numpy as np
import pandas as pd

from config import VolumeProfileConfig, STRATEGY_CONFIGS
from database import TradeDirection

logger = logging.getLogger(__name__)

CFG: VolumeProfileConfig = STRATEGY_CONFIGS.volume_profile


# ---------------------------------------------------------------------------
# Volume profile helpers
# ---------------------------------------------------------------------------

def _build_profile(h1: pd.DataFrame, num_bins: int) -> Tuple[np.ndarray, np.ndarray]:
    """
    Returns (bin_prices, bin_volumes) where bin_prices are the mid-prices
    of each bin and bin_volumes are the total volume traded in that bin.
    """
    lo = float(h1["low"].min())
    hi = float(h1["high"].max())
    if hi <= lo:
        return np.array([]), np.array([])

    edges = np.linspace(lo, hi, num_bins + 1)
    mids = (edges[:-1] + edges[1:]) / 2.0
    bin_vols = np.zeros(num_bins)

    for _, row in h1.iterrows():
        # Distribute the bar's volume proportionally across the bins it spans
        bar_lo, bar_hi, vol = float(row["low"]), float(row["high"]), float(row["volume"])
        bar_range = bar_hi - bar_lo
        if bar_range < 1e-8:
            # Single-tick bar: all volume to nearest bin
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
    Returns (POC, VAL, VAH).
    Expands outward from POC until 70 % of total volume is captured.
    """
    poc_idx = int(np.argmax(bin_vols))
    poc = float(mids[poc_idx])

    target = bin_vols.sum() * pct
    accumulated = bin_vols[poc_idx]
    lo_idx = poc_idx
    hi_idx = poc_idx

    while accumulated < target:
        expand_lo = lo_idx > 0
        expand_hi = hi_idx < len(bin_vols) - 1
        if not expand_lo and not expand_hi:
            break
        vol_lo = bin_vols[lo_idx - 1] if expand_lo else -1
        vol_hi = bin_vols[hi_idx + 1] if expand_hi else -1
        if vol_lo >= vol_hi:
            lo_idx -= 1
            accumulated += bin_vols[lo_idx]
        else:
            hi_idx += 1
            accumulated += bin_vols[hi_idx]

    return poc, float(mids[lo_idx]), float(mids[hi_idx])


def _atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - df["close"].shift()).abs(),
        (df["low"] - df["close"].shift()).abs(),
    ], axis=1).max(axis=1)
    return tr.rolling(period).mean()


# ---------------------------------------------------------------------------
# Main detect function
# ---------------------------------------------------------------------------

def detect(
    m15: pd.DataFrame,
    h1: pd.DataFrame,
    cfg: VolumeProfileConfig = CFG,
) -> Optional["VolumeProfileSignal"]:
    """
    Returns a signal when M15 price touches POC and has momentum
    confirming direction (towards VAH for LONG, towards VAL for SHORT).
    """
    if len(h1) < cfg.lookback_bars_h1 + 5 or len(m15) < 30:
        return None

    # Build volume profile from last N H1 bars
    h1_window = h1.iloc[-cfg.lookback_bars_h1:]
    mids, bin_vols = _build_profile(h1_window, cfg.num_bins)
    if len(mids) == 0:
        return None

    poc, val, vah = _value_area(mids, bin_vols, cfg.value_area_pct)

    if vah <= val or poc <= val or poc >= vah:
        return None

    atr_val = float(_atr(m15).iloc[-1])
    if np.isnan(atr_val) or atr_val == 0:
        return None

    last = m15.iloc[-1]
    prev = m15.iloc[-2]
    close = float(last["close"])
    tolerance = poc * cfg.poc_tolerance_pct

    vol_ma = float(m15["volume"].rolling(20).mean().iloc[-1])
    vol_spike = float(last["volume"]) > vol_ma * 1.2

    # --- LONG: price approaching POC from below (was near VAL, now at POC) ---
    approaching_from_below = float(prev["close"]) < poc and abs(close - poc) <= tolerance
    bull_bar = close > float(last["open"])

    if approaching_from_below and bull_bar:
        # SL = VAL, TP = VAH
        if close <= val:
            return None  # price still below VAL, not yet at POC
        conviction = _score(vol_spike, bull_bar, abs(close - poc) / (poc * 0.01))
        if conviction < 35:
            return None
        logger.info("VP/IVB LONG | POC=%.2f VAL=%.2f VAH=%.2f conviction=%.0f", poc, val, vah, conviction)
        return VolumeProfileSignal(
            direction=TradeDirection.LONG,
            entry_price=close,
            stop_loss=val - atr_val * 0.3,
            take_profit=vah,
            conviction=conviction,
            atr=atr_val,
            bar_close_time=last["timestamp"],
            poc=poc, vah=vah, val=val,
            notes=f"POC={poc:.2f} VAL={val:.2f} VAH={vah:.2f}",
        )

    # --- SHORT: price approaching POC from above (was near VAH, now at POC) ---
    approaching_from_above = float(prev["close"]) > poc and abs(close - poc) <= tolerance
    bear_bar = close < float(last["open"])

    if approaching_from_above and bear_bar:
        if close >= vah:
            return None
        conviction = _score(vol_spike, bear_bar, abs(close - poc) / (poc * 0.01))
        if conviction < 35:
            return None
        logger.info("VP/IVB SHORT | POC=%.2f VAL=%.2f VAH=%.2f conviction=%.0f", poc, val, vah, conviction)
        return VolumeProfileSignal(
            direction=TradeDirection.SHORT,
            entry_price=close,
            stop_loss=vah + atr_val * 0.3,
            take_profit=val,
            conviction=conviction,
            atr=atr_val,
            bar_close_time=last["timestamp"],
            poc=poc, vah=vah, val=val,
            notes=f"POC={poc:.2f} VAL={val:.2f} VAH={vah:.2f}",
        )

    return None


def _score(vol_spike: bool, momentum_bar: bool, poc_proximity_pct: float) -> float:
    """0–100 conviction. poc_proximity_pct: how close price is to POC (lower = better)."""
    score = 30.0
    if vol_spike:
        score += 25.0
    if momentum_bar:
        score += 20.0
    # Closer to POC = higher conviction (max +25)
    score += max(0.0, 25.0 - poc_proximity_pct * 10.0)
    return min(score, 100.0)


@dataclass
class VolumeProfileSignal:
    direction: TradeDirection
    entry_price: float
    stop_loss: float
    take_profit: float
    conviction: float
    atr: float
    bar_close_time: datetime
    poc: float
    vah: float
    val: float
    notes: str = ""
