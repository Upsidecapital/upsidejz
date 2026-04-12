"""
Real-time BTC and ETH price feed with multi-exchange support.

Tries exchanges in order until one connects:
  1. Binance WebSocket (international + US)
  2. Coinbase WebSocket
  3. Kraken WebSocket
  4. CoinGecko REST polling (final fallback — always works)

All endpoints are public and require no API keys.
"""

import asyncio
import json
import logging
import ssl
import time
import urllib.request
from dataclasses import dataclass

import websockets
from websockets.exceptions import ConnectionClosed

from .config import BotConfig

logger = logging.getLogger(__name__)


@dataclass
class PriceUpdate:
    """A single price update from an exchange."""

    symbol: str  # "BTC" or "ETH"
    price: float
    timestamp: float
    volume_24h: float


def _ssl_context() -> ssl.SSLContext:
    """Build an SSL context, preferring certifi certs on Windows."""
    ctx = ssl.create_default_context()
    try:
        import certifi
        ctx.load_verify_locations(certifi.where())
    except Exception:
        pass
    return ctx


class BinanceFeed:
    """Multi-exchange price feed with automatic failover.

    Despite the class name (kept for backwards-compat with bot.py),
    this now tries Binance -> Coinbase -> Kraken -> CoinGecko REST.
    """

    SYMBOLS = {
        "btcusdt": "BTC",
        "ethusdt": "ETH",
    }

    def __init__(self, config: BotConfig):
        self.config = config
        self._prices: dict[str, PriceUpdate] = {}
        self._running = False
        self._ws = None
        self._callbacks: list = []
        self._reconnect_count = 0
        self._max_reconnects = 50
        self._lock = asyncio.Lock()
        self._active_source: str = ""

    @property
    def prices(self) -> dict[str, PriceUpdate]:
        return dict(self._prices)

    def get_price(self, asset: str) -> float | None:
        update = self._prices.get(asset)
        return update.price if update else None

    def on_price_update(self, callback):
        """Register a callback for price updates: callback(PriceUpdate)."""
        self._callbacks.append(callback)

    async def _emit(self, update: PriceUpdate):
        """Store price and notify all callbacks."""
        async with self._lock:
            self._prices[update.symbol] = update
        for cb in self._callbacks:
            try:
                if asyncio.iscoroutinefunction(cb):
                    await cb(update)
                else:
                    cb(update)
            except Exception as e:
                logger.error("Price callback error: %s", e)

    # ==================================================================
    # Main entry point
    # ==================================================================
    async def start(self):
        """Try each exchange in order; restart from top on disconnect."""
        self._running = True

        # Each entry: (human name, coroutine factory)
        sources = [
            ("Binance", self._connect_binance),
            ("Coinbase", self._connect_coinbase),
            ("Kraken", self._connect_kraken),
            ("CoinGecko REST", self._poll_coingecko),
        ]

        while self._running:
            for name, connect_fn in sources:
                if not self._running:
                    return
                try:
                    logger.info("Trying price feed: %s", name)
                    await connect_fn()
                    # If connect_fn returns cleanly, we were stopped
                    return
                except asyncio.CancelledError:
                    logger.info("Feed cancelled")
                    return
                except Exception as e:
                    logger.warning(
                        "%s feed failed: %s — trying next source", name, e
                    )

            # All sources exhausted — wait and retry from top
            logger.error(
                "All price feeds unreachable. Retrying in 15s..."
            )
            await asyncio.sleep(15)

    # ==================================================================
    # 1. Binance WebSocket
    # ==================================================================
    BINANCE_ENDPOINTS = [
        "wss://stream.binance.com:9443",
        "wss://stream.binance.com:443",
        "wss://fstream.binance.com",
        "wss://stream.binance.us:9443",
    ]

    async def _connect_binance(self):
        streams = "/".join(f"{sym}@miniTicker" for sym in self.SYMBOLS)
        ssl_ctx = _ssl_context()

        configured = self.config.api.binance_ws_url.rstrip("/")
        candidates = [configured]
        for ep in self.BINANCE_ENDPOINTS:
            if ep not in candidates:
                candidates.append(ep)

        last_err: Exception | None = None
        for base in candidates:
            url = f"{base}/stream?streams={streams}"
            try:
                logger.info("  Binance endpoint: %s", base)
                async with websockets.connect(
                    url, ssl=ssl_ctx,
                    ping_interval=20, ping_timeout=10,
                    close_timeout=5, open_timeout=10,
                ) as ws:
                    self._ws = ws
                    self._active_source = f"Binance ({base})"
                    logger.info("Connected to %s", self._active_source)
                    async for msg in ws:
                        if not self._running:
                            return
                        await self._handle_binance(msg)
                    return
            except (OSError, ConnectionError, asyncio.TimeoutError) as e:
                last_err = e
                logger.debug("  %s: %s", base, e)
        raise ConnectionError(f"All Binance endpoints failed: {last_err}")

    async def _handle_binance(self, raw: str):
        try:
            data = json.loads(raw)
            payload = data.get("data", data)
            sym = payload.get("s", "").lower()
            asset = self.SYMBOLS.get(sym)
            if not asset:
                return
            price = float(payload.get("c", 0))
            volume = float(payload.get("v", 0))
            if price <= 0:
                return
            await self._emit(PriceUpdate(asset, price, time.time(), volume))
        except (json.JSONDecodeError, KeyError, ValueError) as e:
            logger.debug("Binance parse error: %s", e)

    # ==================================================================
    # 2. Coinbase WebSocket
    # ==================================================================
    async def _connect_coinbase(self):
        url = "wss://ws-feed.exchange.coinbase.com"
        ssl_ctx = _ssl_context()

        subscribe = json.dumps({
            "type": "subscribe",
            "channels": [
                {
                    "name": "ticker",
                    "product_ids": ["BTC-USD", "ETH-USD"],
                }
            ],
        })

        product_map = {"BTC-USD": "BTC", "ETH-USD": "ETH"}

        logger.info("  Coinbase endpoint: %s", url)
        async with websockets.connect(
            url, ssl=ssl_ctx,
            ping_interval=20, ping_timeout=10,
            close_timeout=5, open_timeout=10,
        ) as ws:
            self._ws = ws
            self._active_source = "Coinbase"
            await ws.send(subscribe)
            logger.info("Connected to Coinbase WebSocket")

            async for msg in ws:
                if not self._running:
                    return
                try:
                    data = json.loads(msg)
                    if data.get("type") != "ticker":
                        continue
                    product = data.get("product_id", "")
                    asset = product_map.get(product)
                    if not asset:
                        continue
                    price = float(data.get("price", 0))
                    volume = float(data.get("volume_24h", 0))
                    if price <= 0:
                        continue
                    await self._emit(
                        PriceUpdate(asset, price, time.time(), volume)
                    )
                except (json.JSONDecodeError, KeyError, ValueError) as e:
                    logger.debug("Coinbase parse error: %s", e)

    # ==================================================================
    # 3. Kraken WebSocket
    # ==================================================================
    async def _connect_kraken(self):
        url = "wss://ws.kraken.com"
        ssl_ctx = _ssl_context()

        subscribe = json.dumps({
            "event": "subscribe",
            "pair": ["XBT/USD", "ETH/USD"],
            "subscription": {"name": "ticker"},
        })

        pair_map = {
            "XBT/USD": "BTC",
            "BTC/USD": "BTC",
            "ETH/USD": "ETH",
        }

        logger.info("  Kraken endpoint: %s", url)
        async with websockets.connect(
            url, ssl=ssl_ctx,
            ping_interval=20, ping_timeout=10,
            close_timeout=5, open_timeout=10,
        ) as ws:
            self._ws = ws
            self._active_source = "Kraken"
            await ws.send(subscribe)
            logger.info("Connected to Kraken WebSocket")

            async for msg in ws:
                if not self._running:
                    return
                try:
                    data = json.loads(msg)
                    # Kraken sends arrays for ticker data:
                    # [channelID, tickerData, channelName, pair]
                    if not isinstance(data, list) or len(data) < 4:
                        continue
                    pair = data[-1]
                    asset = pair_map.get(pair)
                    if not asset:
                        continue
                    ticker = data[1]
                    # "c" = close [price, lot_volume]
                    price = float(ticker.get("c", [0])[0])
                    # "v" = volume [today, last24h]
                    vol_arr = ticker.get("v", [0, 0])
                    volume = float(vol_arr[1]) if len(vol_arr) > 1 else 0.0
                    if price <= 0:
                        continue
                    await self._emit(
                        PriceUpdate(asset, price, time.time(), volume)
                    )
                except (json.JSONDecodeError, KeyError, ValueError,
                        TypeError, IndexError) as e:
                    logger.debug("Kraken parse error: %s", e)

    # ==================================================================
    # 4. CoinGecko REST polling (always-available fallback)
    # ==================================================================
    COINGECKO_URLS = [
        "https://api.coingecko.com/api/v3/simple/price"
        "?ids=bitcoin,ethereum&vs_currencies=usd&include_24hr_vol=true",
    ]

    async def _poll_coingecko(self):
        ssl_ctx = _ssl_context()
        poll_interval = 5.0  # CoinGecko free tier: ~10-30 req/min
        id_map = {"bitcoin": "BTC", "ethereum": "ETH"}

        self._active_source = "CoinGecko REST"
        logger.info("Using CoinGecko REST polling (every %.0fs)", poll_interval)
        first = True

        while self._running:
            try:
                url = self.COINGECKO_URLS[0]
                loop = asyncio.get_event_loop()
                req = urllib.request.Request(
                    url, headers={"User-Agent": "Polytracker/1.0"}
                )
                raw = await loop.run_in_executor(
                    None,
                    lambda: urllib.request.urlopen(
                        req, context=ssl_ctx, timeout=10
                    ).read(),
                )
                data = json.loads(raw)
                # data = {"bitcoin": {"usd": 60000, "usd_24h_vol": ...}, ...}
                for coin_id, asset in id_map.items():
                    info = data.get(coin_id, {})
                    price = float(info.get("usd", 0))
                    volume = float(info.get("usd_24h_vol", 0))
                    if price > 0:
                        await self._emit(
                            PriceUpdate(asset, price, time.time(), volume)
                        )
                if first:
                    logger.info("CoinGecko REST feed active")
                    first = False
            except Exception as e:
                logger.warning("CoinGecko poll failed: %s", e)

            await asyncio.sleep(poll_interval)

    # ==================================================================
    # Shutdown
    # ==================================================================
    async def stop(self):
        """Gracefully stop the feed."""
        self._running = False
        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None
        logger.info("Price feed stopped (was: %s)", self._active_source)
