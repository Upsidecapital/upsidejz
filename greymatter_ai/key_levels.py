"""
GreymatterAI — Key Price Levels & Market Structure (Fabio Valentini Framework)

Provides the most important contextual levels for every session:

  PDH / PDL / PDC  — Previous Day High / Low / Close
    Fabio considers PDC the single most important level each morning.
    PDH/PDL are the session's breakout reference points.

  Weekly Open (WO)  — First 15-min bar of the week
    Sustained trade above = bullish week bias, below = bearish week bias.

  Monthly Open (MO) — First bar of the calendar month
    Swing-level anchor; rarely relevant intraday but noted.

  Overnight High / Low (Globex)
    The NAS100 trades nearly 24h. The Globex session (16:00 ET prev day →
    09:30 ET today) forms an overnight range. A break of overnight high at
    NY open is a continuation signal; a fade of it is a reversal play.

  VWAP + Standard Deviation Bands (±1σ, ±2σ)
    Fabio uses VWAP with bands as dynamic support/resistance.
    ±1σ = normal range, ±2σ = extended / overextended.

Market Structure (HTF Bias):
  Computes H1 trend direction for intraday trade bias:
    - bullish : recent swing highs and lows are rising (HH/HL pattern)
    - bearish : recent swing highs and lows are falling (LH/LL pattern)
    - neutral : mixed / consolidation
  Only take LONG setups in bullish structure, SHORT in bearish, either in neutral.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from typing import Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# NY open / close in UTC
_NY_OPEN  = time(13, 30)   # 09:30 ET
_NY_CLOSE = time(21, 0)    # 16:00 ET


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class KeyLevels:
    """All key levels relevant to the current trading session."""
    pdh: float              # Previous Day High
    pdl: float              # Previous Day Low
    pdc: float              # Previous Day Close  ← most important
    weekly_open: float      # First M15 bar of the current week
    monthly_open: float     # First M15 bar of the current month
    overnight_high: float   # Globex session high (prev 16:00 ET → today 09:30 ET)
    overnight_low: float    # Globex session low
    vwap: float             # Current session VWAP
    vwap_std: float         # VWAP standard deviation
    vwap_upper1: float      # VWAP + 1σ
    vwap_lower1: float      # VWAP − 1σ
    vwap_upper2: float      # VWAP + 2σ
    vwap_lower2: float      # VWAP − 2σ

    def as_list(self) -> list[float]:
        """All levels as a flat list (for proximity checks)."""
        return [
            self.pdh, self.pdl, self.pdc,
            self.weekly_open, self.overnight_high, self.overnight_low,
            self.vwap, self.vwap_upper1, self.vwap_lower1,
            self.vwap_upper2, self.vwap_lower2,
        ]

    def nearest(self, price: float, max_dist: float = 50.0) -> Optional[tuple[str, float]]:
        """Return (label, level) of the nearest key level within max_dist points."""
        candidates = {
            "PDH": self.pdh, "PDL": self.pdl, "PDC": self.pdc,
            "WO": self.weekly_open, "ONH": self.overnight_high, "ONL": self.overnight_low,
            "VWAP": self.vwap, "VWAP+1σ": self.vwap_upper1, "VWAP-1σ": self.vwap_lower1,
            "VWAP+2σ": self.vwap_upper2, "VWAP-2σ": self.vwap_lower2,
        }
        best_label, best_level, best_dist = None, None, float("inf")
        for label, level in candidates.items():
            if level <= 0:
                continue
            dist = abs(price - level)
            if dist < best_dist:
                best_dist, best_label, best_level = dist, label, level
        if best_dist <= max_dist:
            return best_label, best_level
        return None


@dataclass
class MarketStructure:
    """Higher-timeframe trend context."""
    h1_bias: str        # "bullish", "bearish", "neutral"
    d1_bias: str        # "bullish", "bearish", "neutral"
    combined: str       # combined bias (both aligned = strong, mixed = neutral)
    h1_detail: str      # human-readable detail for logs/dashboard
    d1_detail: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _bar_time(ts) -> time:
    return ts.time() if hasattr(ts, "time") else ts.to_pydatetime().time()


def _bar_date(ts):
    return ts.date() if hasattr(ts, "date") else ts.to_pydatetime().date()


def _bar_datetime(ts) -> datetime:
    if isinstance(ts, datetime):
        return ts
    return ts.to_pydatetime()


def _vwap_and_bands(m15: pd.DataFrame) -> tuple[float, float]:
    """
    Compute session VWAP and its standard deviation from the current NY session.
    Returns (vwap, std).
    """
    ts  = m15["timestamp"]
    n   = len(m15)
    start = n - 1

    for i in range(n - 1, max(0, n - 300), -1):
        t = _bar_time(ts.iloc[i])
        d = _bar_date(ts.iloc[i])
        if t < _NY_OPEN:
            continue
        start = i
        while start > 0:
            if _bar_date(ts.iloc[start - 1]) != d or _bar_time(ts.iloc[start - 1]) < _NY_OPEN:
                break
            start -= 1
        break

    seg = m15.iloc[start:]
    if len(seg) < 2:
        mid = float((m15["high"].iloc[-1] + m15["low"].iloc[-1] + m15["close"].iloc[-1]) / 3.0)
        return mid, 0.0

    tp       = (seg["high"] + seg["low"] + seg["close"]) / 3.0
    cum_vol  = seg["volume"].cumsum().replace(0, np.nan)
    cum_tpv  = (tp * seg["volume"]).cumsum()
    vwap_s   = cum_tpv / cum_vol

    # Standard deviation of typical price from VWAP
    variance = ((tp - vwap_s) ** 2 * seg["volume"]).cumsum() / cum_vol
    std_s    = np.sqrt(variance.fillna(0.0))

    vwap = float(vwap_s.iloc[-1])
    std  = float(std_s.iloc[-1])
    return vwap, std


# ---------------------------------------------------------------------------
# Main compute functions
# ---------------------------------------------------------------------------

def compute_key_levels(
    d1: pd.DataFrame,
    m15: pd.DataFrame,
) -> Optional[KeyLevels]:
    """
    Compute all key levels from daily and 15-min data.
    Returns None if insufficient data.
    """
    if len(d1) < 3 or len(m15) < 30:
        return None

    # ── Previous Day High / Low / Close ──────────────────────────────────
    # d1 index: -1 = today (current, incomplete), -2 = yesterday (complete)
    pdh = float(d1["high"].iloc[-2])
    pdl = float(d1["low"].iloc[-2])
    pdc = float(d1["close"].iloc[-2])

    # ── Weekly Open ───────────────────────────────────────────────────────
    ts_col = m15["timestamp"]
    today  = _bar_date(ts_col.iloc[-1])
    weekly_open = 0.0
    for i in range(len(m15) - 1, max(0, len(m15) - 600), -1):
        bar_dt = _bar_datetime(ts_col.iloc[i])
        if bar_dt.weekday() == 0:  # Monday
            weekly_open = float(m15["open"].iloc[i])
            break
    if weekly_open == 0.0:
        # Fallback: use earliest bar in last 5 days
        cutoff = today - timedelta(days=5)
        hist = m15[m15["timestamp"].apply(_bar_date) >= cutoff]
        if not hist.empty:
            weekly_open = float(hist["open"].iloc[0])

    # ── Monthly Open ─────────────────────────────────────────────────────
    monthly_open = 0.0
    cur_month = today.month
    cur_year  = today.year
    for i in range(len(m15) - 1, max(0, len(m15) - 3000), -1):
        bar_dt = _bar_datetime(ts_col.iloc[i])
        if bar_dt.month == cur_month and bar_dt.year == cur_year:
            monthly_open = float(m15["open"].iloc[i])
    if monthly_open == 0.0:
        monthly_open = float(m15["open"].iloc[0])

    # ── Overnight High / Low (Globex session) ────────────────────────────
    # Find bars between yesterday's NY close (21:00 UTC) and today's NY open (13:30 UTC)
    overnight_highs = []
    overnight_lows  = []
    for i in range(len(m15) - 1, max(0, len(m15) - 100), -1):
        t  = _bar_time(ts_col.iloc[i])
        d  = _bar_date(ts_col.iloc[i])
        if d == today and t < _NY_OPEN:
            # Pre-market today
            overnight_highs.append(float(m15["high"].iloc[i]))
            overnight_lows.append(float(m15["low"].iloc[i]))
        elif d < today and t >= time(21, 0):
            # After-hours yesterday
            overnight_highs.append(float(m15["high"].iloc[i]))
            overnight_lows.append(float(m15["low"].iloc[i]))
    overnight_high = max(overnight_highs) if overnight_highs else pdh
    overnight_low  = min(overnight_lows)  if overnight_lows  else pdl

    # ── VWAP + Bands ─────────────────────────────────────────────────────
    vwap, std = _vwap_and_bands(m15)
    if std < 5.0:
        # Fallback std from ATR proxy
        atr_proxy = float((m15["high"] - m15["low"]).rolling(14).mean().iloc[-1])
        std = atr_proxy * 0.6

    return KeyLevels(
        pdh=pdh, pdl=pdl, pdc=pdc,
        weekly_open=weekly_open,
        monthly_open=monthly_open,
        overnight_high=overnight_high,
        overnight_low=overnight_low,
        vwap=vwap, vwap_std=std,
        vwap_upper1=vwap + std,
        vwap_lower1=vwap - std,
        vwap_upper2=vwap + 2 * std,
        vwap_lower2=vwap - 2 * std,
    )


def compute_market_structure(
    h1: pd.DataFrame,
    d1: pd.DataFrame,
) -> MarketStructure:
    """
    Determine Higher-Timeframe bias using swing high/low structure.

    H1 intraday bias (last 10 H1 bars):
      Compares consecutive pivot highs and lows:
        HH + HL = bullish  |  LH + LL = bearish  |  else = neutral

    D1 swing bias (last 20 D1 bars):
      Uses 20-bar EMA slope as proxy for trend direction.
    """
    # ── H1 Structure ─────────────────────────────────────────────────────
    h1_bias    = "neutral"
    h1_detail  = "mixed"
    try:
        if len(h1) >= 10:
            h = h1["high"].values[-10:]
            l = h1["low"].values[-10:]
            # Compare first half vs second half
            hh = h[-3:].max() > h[-6:-3].max()  # higher highs
            hl = l[-3:].min() > l[-6:-3].min()   # higher lows
            lh = h[-3:].max() < h[-6:-3].max()   # lower highs
            ll = l[-3:].min() < l[-6:-3].min()   # lower lows

            if hh and hl:
                h1_bias   = "bullish"
                h1_detail = "H1 HH+HL — uptrend"
            elif lh and ll:
                h1_bias   = "bearish"
                h1_detail = "H1 LH+LL — downtrend"
            elif hh and ll:
                h1_detail = "H1 expanding — neutral"
            elif lh and hl:
                h1_detail = "H1 contracting — neutral"
    except Exception as exc:
        logger.debug("H1 structure calc error: %s", exc)

    # ── D1 Trend ─────────────────────────────────────────────────────────
    d1_bias   = "neutral"
    d1_detail = "D1 mixed"
    try:
        if len(d1) >= 20:
            closes = d1["close"].values[-20:]
            ema20  = pd.Series(closes).ewm(span=20, adjust=False).mean().values
            slope  = ema20[-1] - ema20[-5]
            price  = closes[-1]
            if slope > 5 and price > ema20[-1]:
                d1_bias   = "bullish"
                d1_detail = f"D1 above 20EMA, slope +{slope:.0f}"
            elif slope < -5 and price < ema20[-1]:
                d1_bias   = "bearish"
                d1_detail = f"D1 below 20EMA, slope {slope:.0f}"
            else:
                d1_detail = f"D1 near 20EMA (slope {slope:.0f})"
    except Exception as exc:
        logger.debug("D1 structure calc error: %s", exc)

    # ── Combined ─────────────────────────────────────────────────────────
    if h1_bias == d1_bias and h1_bias != "neutral":
        combined = h1_bias   # both agree = strong directional bias
    elif h1_bias != "neutral":
        combined = h1_bias   # H1 takes priority for intraday
    elif d1_bias != "neutral":
        combined = d1_bias
    else:
        combined = "neutral"

    return MarketStructure(
        h1_bias=h1_bias, d1_bias=d1_bias,
        combined=combined,
        h1_detail=h1_detail, d1_detail=d1_detail,
    )


def is_ny_morning_session(now_utc: Optional[datetime] = None) -> bool:
    """
    Fabio's primary trading window: 09:30 – 12:00 ET (13:30 – 17:00 UTC).
    A secondary window is 14:00 – 16:00 ET (18:00 – 20:00 UTC).
    """
    if now_utc is None:
        now_utc = datetime.now(timezone.utc)
    t = now_utc.time()
    morning_ok   = time(13, 30) <= t <= time(17, 0)
    afternoon_ok = time(18, 0) <= t <= time(20, 0)
    return morning_ok or afternoon_ok
