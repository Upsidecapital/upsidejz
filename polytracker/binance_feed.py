"""Real-time BTC and ETH price feed from Binance WebSocket."""

import asyncio
import json
import logging
import time
from dataclasses import dataclass

import websockets
from websockets.exceptions import ConnectionClosed

from .config import BotConfig

logger = logging.getLogger(__name__)


@dataclass
class PriceUpdate:
    """A single price update from Binance."""

    symbol: str  # "BTC" or "ETH"
    price: float
    timestamp: float  # Unix timestamp
    volume_24h: float


class BinanceFeed:
    """Manages WebSocket connection to Binance for real-time prices."""

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

    @property
    def prices(self) -> dict[str, PriceUpdate]:
        return dict(self._prices)

    def get_price(self, asset: str) -> float | None:
        update = self._prices.get(asset)
        return update.price if update else None

    def on_price_update(self, callback):
        """Register a callback for price updates: callback(PriceUpdate)."""
        self._callbacks.append(callback)

    async def start(self):
        """Start the WebSocket connection with auto-reconnect."""
        self._running = True
        self._reconnect_count = 0
        while self._running and self._reconnect_count < self._max_reconnects:
            try:
                await self._connect()
            except (ConnectionClosed, ConnectionError, OSError) as e:
                self._reconnect_count += 1
                delay = min(
                    self.config.ws_reconnect_delay * (2 ** (self._reconnect_count - 1)),
                    60.0,
                )
                logger.warning(
                    "Binance WS disconnected (%s), reconnecting in %.1fs "
                    "(attempt %d/%d)",
                    e,
                    delay,
                    self._reconnect_count,
                    self._max_reconnects,
                )
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                logger.info("Binance feed cancelled")
                break

        if self._reconnect_count >= self._max_reconnects:
            logger.error("Max reconnection attempts reached for Binance WS")

    async def _connect(self):
        """Establish WebSocket connection and process messages."""
        streams = "/".join(f"{sym}@miniTicker" for sym in self.SYMBOLS)
        url = f"{self.config.api.binance_ws_url}/stream?streams={streams}"

        logger.info("Connecting to Binance WebSocket: %s", url)

        async with websockets.connect(
            url,
            ping_interval=20,
            ping_timeout=10,
            close_timeout=5,
        ) as ws:
            self._ws = ws
            self._reconnect_count = 0
            logger.info("Binance WebSocket connected")

            async for message in ws:
                if not self._running:
                    break
                await self._handle_message(message)

    async def _handle_message(self, raw: str):
        """Parse and process a WebSocket message."""
        try:
            data = json.loads(raw)
            payload = data.get("data", data)

            symbol_raw = payload.get("s", "").lower()
            asset = self.SYMBOLS.get(symbol_raw)
            if not asset:
                return

            price = float(payload.get("c", 0))  # Close price
            volume = float(payload.get("v", 0))  # 24h volume

            if price <= 0:
                return

            update = PriceUpdate(
                symbol=asset,
                price=price,
                timestamp=time.time(),
                volume_24h=volume,
            )

            async with self._lock:
                self._prices[asset] = update

            for cb in self._callbacks:
                try:
                    if asyncio.iscoroutinefunction(cb):
                        await cb(update)
                    else:
                        cb(update)
                except Exception as e:
                    logger.error("Price callback error: %s", e)

        except (json.JSONDecodeError, KeyError, ValueError) as e:
            logger.debug("Failed to parse Binance message: %s", e)

    async def stop(self):
        """Gracefully stop the feed."""
        self._running = False
        if self._ws:
            await self._ws.close()
            self._ws = None
        logger.info("Binance feed stopped")
