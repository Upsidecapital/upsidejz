"""
GreymatterAI — Async multi-timeframe data fetcher (Twelve Data)
Fetches OHLCV for M15 / H1 / H4 / D1 with rate-limit handling.
All bars are returned bar-closed (no lookahead): the current (incomplete) bar
is always dropped so strategies only see fully-formed candles.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, List, Optional

import aiohttp
import pandas as pd

from config import TWELVE_DATA_API_KEY, SYMBOL, TIMEFRAMES, MIN_BARS

logger = logging.getLogger(__name__)

BASE_URL = "https://api.twelvedata.com"
RATE_LIMIT_CALLS = 8        # free tier: 8 req/min
RATE_LIMIT_WINDOW = 60.0    # seconds


@dataclass
class OHLCVBar:
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


class RateLimiter:
    """Token-bucket rate limiter for the Twelve Data free tier."""

    def __init__(self, max_calls: int, window: float) -> None:
        self._max = max_calls
        self._window = window
        self._calls: List[float] = []
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            now = time.monotonic()
            self._calls = [t for t in self._calls if now - t < self._window]
            if len(self._calls) >= self._max:
                sleep_for = self._window - (now - self._calls[0]) + 0.1
                logger.debug("Rate limit reached, sleeping %.1fs", sleep_for)
                await asyncio.sleep(sleep_for)
                self._calls = []
            self._calls.append(time.monotonic())


_rate_limiter = RateLimiter(RATE_LIMIT_CALLS, RATE_LIMIT_WINDOW)


async def _fetch_timeseries(
    session: aiohttp.ClientSession,
    symbol: str,
    interval: str,
    outputsize: int,
    retries: int = 5,
) -> List[dict]:
    """Single Twelve Data /time_series call with exponential back-off."""
    params = {
        "symbol": symbol,
        "interval": interval,
        "outputsize": outputsize,
        "apikey": TWELVE_DATA_API_KEY,
        "format": "JSON",
        "timezone": "UTC",
    }
    delay = 2.0
    for attempt in range(1, retries + 1):
        await _rate_limiter.acquire()
        try:
            async with session.get(
                f"{BASE_URL}/time_series", params=params, timeout=aiohttp.ClientTimeout(total=30)
            ) as resp:
                data = await resp.json()
                if resp.status != 200 or "values" not in data:
                    raise ValueError(f"Bad response [{resp.status}]: {data.get('message', data)}")
                return data["values"]
        except (aiohttp.ClientError, ValueError, asyncio.TimeoutError) as exc:
            if attempt == retries:
                logger.error("Failed to fetch %s %s after %d attempts: %s", symbol, interval, retries, exc)
                raise
            logger.warning("Attempt %d/%d failed (%s), retrying in %.1fs", attempt, retries, exc, delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 60.0)
    return []  # unreachable


def _parse_bars(raw: List[dict]) -> pd.DataFrame:
    """Convert Twelve Data JSON list → tidy DataFrame (oldest first, bar-closed)."""
    records = []
    for row in raw:
        records.append({
            "timestamp": pd.to_datetime(row["datetime"], utc=True),
            "open": float(row["open"]),
            "high": float(row["high"]),
            "low": float(row["low"]),
            "close": float(row["close"]),
            "volume": float(row.get("volume", 0)),
        })
    df = pd.DataFrame(records).sort_values("timestamp").reset_index(drop=True)
    # Drop the last (current, incomplete) bar — critical for no lookahead
    if len(df) > 1:
        df = df.iloc[:-1]
    return df


class DataFetcher:
    """
    Fetches and caches OHLCV data for all required timeframes.
    Call `refresh()` at each scheduler heartbeat.
    """

    def __init__(self) -> None:
        self._cache: Dict[str, pd.DataFrame] = {}
        self._session: Optional[aiohttp.ClientSession] = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            connector = aiohttp.TCPConnector(ssl=True)
            self._session = aiohttp.ClientSession(connector=connector)
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def refresh(self) -> Dict[str, pd.DataFrame]:
        """Fetch all timeframes concurrently and update the cache."""
        session = await self._get_session()
        tasks = {
            tf: _fetch_timeseries(session, SYMBOL, tf, outputsize=MIN_BARS[tf] + 10)
            for tf in TIMEFRAMES
        }
        results = await asyncio.gather(*tasks.values(), return_exceptions=True)
        for tf, result in zip(tasks.keys(), results):
            if isinstance(result, Exception):
                logger.error("Timeframe %s fetch failed: %s", tf, result)
                continue
            df = _parse_bars(result)
            if len(df) < MIN_BARS[tf]:
                logger.warning("Timeframe %s only has %d bars (need %d)", tf, len(df), MIN_BARS[tf])
            self._cache[tf] = df
            logger.info("Cached %d bars for %s", len(df), tf)
        return self._cache

    def get(self, timeframe: str) -> Optional[pd.DataFrame]:
        """Return the latest cached DataFrame for a timeframe (or None)."""
        return self._cache.get(timeframe)

    def all(self) -> Dict[str, pd.DataFrame]:
        return dict(self._cache)

    def compute_atr(self, timeframe: str, period: int = 14) -> Optional[float]:
        """True Range ATR of the last `period` closed bars."""
        df = self.get(timeframe)
        if df is None or len(df) < period + 1:
            return None
        tr = pd.concat([
            df["high"] - df["low"],
            (df["high"] - df["close"].shift()).abs(),
            (df["low"] - df["close"].shift()).abs(),
        ], axis=1).max(axis=1)
        return float(tr.iloc[-period:].mean())
