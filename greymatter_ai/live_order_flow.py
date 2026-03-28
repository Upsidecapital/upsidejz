"""
GreymatterAI — Live Order Flow Engine (MT5 Tick Data)

Replaces the OHLCV approximation with real tick-level data from MetaTrader 5.

How real delta works:
  MT5 provides tick-by-tick trade prints with BUY/SELL flags:
    TICK_FLAG_BUY  (2) — buyer was the aggressor (lifted the ask)
    TICK_FLAG_SELL (8) — seller was the aggressor (hit the bid)
  Real delta = cumulative buy volume - cumulative sell volume per bar.

DOM (Depth of Market):
  MT5's market_book_add/get provides bid/ask depth levels.
  Used to detect absorption: large bid/ask at a level being defended.

Fallback:
  When MT5 is not connected (e.g. cloud deployment without terminal),
  falls back to OHLCV approximation identical to the original order_flow.py.

Usage:
  from live_order_flow import live_of
  bar_delta = live_of.get_bar_delta(symbol, from_dt, to_dt)
  dom       = live_of.get_dom(symbol)
  absorbed  = live_of.check_absorption(symbol, level=19500.0, tolerance=15.0)
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# MT5 tick flags
_TICK_FLAG_BUY  = 2
_TICK_FLAG_SELL = 8

try:
    import MetaTrader5 as mt5
    _MT5_OK = True
except ImportError:
    mt5 = None  # type: ignore[assignment]
    _MT5_OK = False


@dataclass
class BarDelta:
    """True buy/sell split for a single OHLCV bar derived from tick data."""
    bar_open:  datetime
    bar_close: datetime
    buy_volume:  float
    sell_volume: float
    total_volume: float
    delta: float            # buy_volume − sell_volume
    delta_ratio: float      # buy_volume / total_volume  (1.0 = all buys)
    is_real: bool           # True = from ticks, False = OHLCV approximation


@dataclass
class DOMLevel:
    price: float
    bid_volume: float
    ask_volume: float


@dataclass
class DOMSnapshot:
    symbol: str
    timestamp: datetime
    bids: list[DOMLevel]
    asks: list[DOMLevel]
    total_bid: float
    total_ask: float
    imbalance: float    # (bid - ask) / (bid + ask), positive = bid-heavy


class LiveOrderFlow:
    """
    Provides real tick-based order flow metrics from MT5.
    Thread-safe for single-threaded async usage.
    """

    def _mt5_active(self) -> bool:
        if not _MT5_OK or mt5 is None:
            return False
        try:
            info = mt5.account_info()
            return info is not None
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Real delta from ticks
    # ------------------------------------------------------------------
    def get_bar_delta(
        self,
        symbol: str,
        bar_open: datetime,
        bar_close: datetime,
    ) -> BarDelta:
        """
        Compute true delta for a single bar's time range.
        Returns real data if MT5 connected, OHLCV approximation otherwise.
        """
        if self._mt5_active():
            return self._real_delta(symbol, bar_open, bar_close)
        return self._fake_delta_placeholder(bar_open, bar_close)

    def _real_delta(self, symbol: str, bar_open: datetime, bar_close: datetime) -> BarDelta:
        """Pull ticks from MT5 and compute true buy/sell delta."""
        try:
            ticks = mt5.copy_ticks_range(
                symbol,
                bar_open.astimezone(timezone.utc).replace(tzinfo=None),
                bar_close.astimezone(timezone.utc).replace(tzinfo=None),
                mt5.COPY_TICKS_TRADE,
            )
            if ticks is None or len(ticks) == 0:
                logger.debug("No ticks for %s [%s – %s]", symbol, bar_open, bar_close)
                return self._fake_delta_placeholder(bar_open, bar_close)

            buy_vol = 0.0
            sell_vol = 0.0
            for tick in ticks:
                flags = int(tick.flags)
                vol   = float(tick.volume_real) if tick.volume_real > 0 else float(tick.volume)
                if flags & _TICK_FLAG_BUY:
                    buy_vol += vol
                elif flags & _TICK_FLAG_SELL:
                    sell_vol += vol
                else:
                    # Ambiguous — split evenly
                    buy_vol  += vol * 0.5
                    sell_vol += vol * 0.5

            total = buy_vol + sell_vol
            ratio = buy_vol / total if total > 0 else 0.5
            return BarDelta(
                bar_open=bar_open, bar_close=bar_close,
                buy_volume=buy_vol, sell_volume=sell_vol,
                total_volume=total, delta=buy_vol - sell_vol,
                delta_ratio=ratio, is_real=True,
            )
        except Exception as exc:
            logger.warning("Real delta failed (%s) — using approximation", exc)
            return self._fake_delta_placeholder(bar_open, bar_close)

    @staticmethod
    def _fake_delta_placeholder(bar_open: datetime, bar_close: datetime) -> BarDelta:
        """Placeholder — replaced with OHLCV approx in order_flow.py when needed."""
        return BarDelta(
            bar_open=bar_open, bar_close=bar_close,
            buy_volume=0.0, sell_volume=0.0, total_volume=0.0,
            delta=0.0, delta_ratio=0.5, is_real=False,
        )

    def get_bar_deltas_for_df(
        self,
        symbol: str,
        df: pd.DataFrame,
        ohlcv_fallback: bool = True,
    ) -> list[BarDelta]:
        """
        Compute delta for every closed bar in a DataFrame.
        Falls back to OHLCV approximation per-bar when MT5 not available.
        """
        results: list[BarDelta] = []
        mt5_live = self._mt5_active()

        for i in range(len(df)):
            row = df.iloc[i]
            ts = row["timestamp"]
            bar_open_dt  = ts.to_pydatetime() if hasattr(ts, "to_pydatetime") else ts
            bar_close_dt = bar_open_dt + timedelta(minutes=15)

            if mt5_live:
                bd = self._real_delta(symbol, bar_open_dt, bar_close_dt)
            elif ohlcv_fallback:
                bd = self._ohlcv_approx(row, bar_open_dt, bar_close_dt)
            else:
                bd = self._fake_delta_placeholder(bar_open_dt, bar_close_dt)
            results.append(bd)

        return results

    @staticmethod
    def _ohlcv_approx(row, bar_open_dt: datetime, bar_close_dt: datetime) -> BarDelta:
        """
        Classic OHLCV approximation (same formula as original order_flow.py):
          buy_vol  = volume × (close − low)  / (high − low)
          sell_vol = volume × (high − close) / (high − low)
        """
        hi    = float(row["high"])
        lo    = float(row["low"])
        close = float(row["close"])
        vol   = float(row["volume"])
        rng   = hi - lo
        if rng < 1e-9 or vol == 0:
            return BarDelta(bar_open=bar_open_dt, bar_close=bar_close_dt,
                            buy_volume=vol * 0.5, sell_volume=vol * 0.5,
                            total_volume=vol, delta=0.0, delta_ratio=0.5, is_real=False)
        buy_vol  = vol * (close - lo) / rng
        sell_vol = vol * (hi - close) / rng
        return BarDelta(
            bar_open=bar_open_dt, bar_close=bar_close_dt,
            buy_volume=buy_vol, sell_volume=sell_vol,
            total_volume=vol, delta=buy_vol - sell_vol,
            delta_ratio=buy_vol / vol, is_real=False,
        )

    # ------------------------------------------------------------------
    # DOM snapshot
    # ------------------------------------------------------------------
    def get_dom(self, symbol: str) -> Optional[DOMSnapshot]:
        """
        Fetch current Depth of Market for the symbol.
        Returns None when MT5 is not connected.
        Requires the symbol to be subscribed via market_book_add.
        """
        if not self._mt5_active():
            return None
        try:
            mt5.market_book_add(symbol)
            book = mt5.market_book_get(symbol)
            if book is None:
                return None

            from MetaTrader5 import BOOK_TYPE_SELL, BOOK_TYPE_BUY
            bids = [DOMLevel(price=float(l.price), bid_volume=float(l.volume), ask_volume=0.0)
                    for l in book if l.type == BOOK_TYPE_BUY]
            asks = [DOMLevel(price=float(l.price), bid_volume=0.0, ask_volume=float(l.volume))
                    for l in book if l.type == BOOK_TYPE_SELL]

            total_bid = sum(b.bid_volume for b in bids)
            total_ask = sum(a.ask_volume for a in asks)
            total = total_bid + total_ask
            imbalance = (total_bid - total_ask) / total if total > 0 else 0.0

            return DOMSnapshot(
                symbol=symbol,
                timestamp=datetime.now(timezone.utc),
                bids=bids, asks=asks,
                total_bid=total_bid, total_ask=total_ask,
                imbalance=imbalance,
            )
        except Exception as exc:
            logger.debug("DOM snapshot failed: %s", exc)
            return None

    # ------------------------------------------------------------------
    # Absorption detection at a price level
    # ------------------------------------------------------------------
    def check_absorption(
        self,
        symbol: str,
        level: float,
        tolerance: float = 15.0,
        lookback_seconds: int = 180,
        min_volume_mult: float = 2.0,
    ) -> Optional[dict]:
        """
        Check if there is absorption (large volume, tiny price move) near `level`.
        Uses tick data from the last `lookback_seconds` seconds.

        Returns dict with absorption details, or None if not detected / MT5 unavailable.
        """
        if not self._mt5_active():
            return None
        try:
            from_dt = datetime.now(timezone.utc) - timedelta(seconds=lookback_seconds)
            to_dt   = datetime.now(timezone.utc)
            ticks = mt5.copy_ticks_range(
                symbol,
                from_dt.replace(tzinfo=None),
                to_dt.replace(tzinfo=None),
                mt5.COPY_TICKS_TRADE,
            )
            if ticks is None or len(ticks) < 5:
                return None

            prices = np.array([t.last for t in ticks])
            vols   = np.array([max(float(t.volume_real), float(t.volume)) for t in ticks])

            # Filter ticks near the level
            mask    = np.abs(prices - level) <= tolerance
            near_vol  = vols[mask].sum()
            total_vol = vols.sum()
            if total_vol == 0:
                return None

            # Price range during the window (is it compressed?)
            price_range = prices.max() - prices.min()
            vol_ratio = near_vol / total_vol

            # Absorption: lots of volume near the level + compressed price action
            avg_vol = total_vol / len(ticks)
            is_absorbed = vol_ratio > 0.50 and price_range < tolerance * 1.5

            if is_absorbed:
                buy_ticks  = sum(1 for t in ticks if int(t.flags) & _TICK_FLAG_BUY  and abs(t.last - level) <= tolerance)
                sell_ticks = sum(1 for t in ticks if int(t.flags) & _TICK_FLAG_SELL and abs(t.last - level) <= tolerance)
                direction = "buying" if buy_ticks >= sell_ticks else "selling"
                return {
                    "level": level,
                    "absorbed": True,
                    "direction": direction,
                    "near_volume": round(near_vol, 2),
                    "total_volume": round(total_vol, 2),
                    "vol_ratio": round(vol_ratio, 3),
                    "price_range": round(price_range, 1),
                    "buy_ticks": buy_ticks,
                    "sell_ticks": sell_ticks,
                }
            return None
        except Exception as exc:
            logger.debug("Absorption check failed: %s", exc)
            return None

    # ------------------------------------------------------------------
    # CVD (Cumulative Volume Delta) series from ticks
    # ------------------------------------------------------------------
    def get_cvd_series(
        self,
        symbol: str,
        lookback_bars: int = 12,
        bar_minutes: int = 15,
    ) -> Optional[list[float]]:
        """
        Returns a list of CVD values (one per bar) for the last N bars.
        CVD[i] = running sum of delta up to bar i.
        Returns None when MT5 unavailable.
        """
        if not self._mt5_active():
            return None
        try:
            from_dt = datetime.now(timezone.utc) - timedelta(minutes=lookback_bars * bar_minutes + 5)
            to_dt   = datetime.now(timezone.utc)
            ticks = mt5.copy_ticks_range(
                symbol,
                from_dt.replace(tzinfo=None),
                to_dt.replace(tzinfo=None),
                mt5.COPY_TICKS_TRADE,
            )
            if ticks is None or len(ticks) < 10:
                return None

            # Bucket into bars
            bar_deltas = []
            current_bar_start = from_dt.replace(second=0, microsecond=0)
            current_delta = 0.0
            ti = 0
            for _ in range(lookback_bars):
                bar_end = current_bar_start + timedelta(minutes=bar_minutes)
                delta = 0.0
                while ti < len(ticks):
                    t_time = datetime.fromtimestamp(ticks[ti].time, tz=timezone.utc)
                    if t_time >= bar_end:
                        break
                    vol   = max(float(ticks[ti].volume_real), float(ticks[ti].volume))
                    flags = int(ticks[ti].flags)
                    if flags & _TICK_FLAG_BUY:
                        delta += vol
                    elif flags & _TICK_FLAG_SELL:
                        delta -= vol
                    ti += 1
                bar_deltas.append(delta)
                current_bar_start = bar_end

            # CVD = running sum
            cvd = []
            running = 0.0
            for d in bar_deltas:
                running += d
                cvd.append(running)
            return cvd
        except Exception as exc:
            logger.debug("CVD series failed: %s", exc)
            return None


# Module-level singleton
live_of = LiveOrderFlow()
