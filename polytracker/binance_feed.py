"""Real-time BTC and ETH price feed from Binance WebSocket."""

import asyncio
import json
import logging
import ssl
import time
from dataclasses import dataclass

import websockets
from websockets.exceptions import ConnectionClosed

from .config import BotConfig

logger = logging.getLogger(__name__)

# Binance WebSocket endpoints, tried in order.
# - Primary: international
# - Fallback 1: Binance.US (for US-based users)
# - Fallback 2: alternative international port
BINANCE_WS_ENDPOINTS = [
    "wss://stream.binance.com:9443",
    "wss://stream.binance.com:443",
    "wss://fstream.binance.com",
    "wss://stream.binance.us:9443",
]


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
        self._active_endpoint: str = ""

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
        """Start the WebSocket connection with auto-reconnect.

        If WebSocket is completely unreachable after several attempts,
        falls back to REST API polling.
        """
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

                # After 5 failed WS attempts, switch to REST polling
                if self._reconnect_count >= 5:
                    logger.warning(
                        "WebSocket unreachable after %d attempts — "
                        "falling back to REST API polling",
                        self._reconnect_count,
                    )
                    await self._rest_poll_loop()
                    return

                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                logger.info("Binance feed cancelled")
                break

        if self._reconnect_count >= self._max_reconnects:
            logger.error("Max reconnection attempts reached for Binance WS")

    async def _connect(self):
        """Establish WebSocket connection and process messages.

        Tries multiple Binance endpoints in order (international, port 443,
        futures domain, Binance.US) so the bot works regardless of region or
        firewall rules.
        """
        streams = "/".join(f"{sym}@miniTicker" for sym in self.SYMBOLS)

        # Build list of URLs to try. Put the configured one first.
        configured = self.config.api.binance_ws_url.rstrip("/")
        candidates = [configured]
        for ep in BINANCE_WS_ENDPOINTS:
            if ep not in candidates:
                candidates.append(ep)

        # Permissive SSL context for Windows environments with outdated
        # certificate bundles. We're only reading public market data.
        ssl_ctx = ssl.create_default_context()
        try:
            import certifi
            ssl_ctx.load_verify_locations(certifi.where())
        except Exception:
            pass  # certifi is optional

        last_error: Exception | None = None
        for base_url in candidates:
            url = f"{base_url}/stream?streams={streams}"
            try:
                logger.info("Trying Binance WebSocket: %s", url)
                async with websockets.connect(
                    url,
                    ssl=ssl_ctx,
                    ping_interval=20,
                    ping_timeout=10,
                    close_timeout=5,
                    open_timeout=10,
                ) as ws:
                    self._ws = ws
                    self._reconnect_count = 0
                    self._active_endpoint = base_url
                    logger.info(
                        "Binance WebSocket connected via %s", base_url
                    )

                    async for message in ws:
                        if not self._running:
                            break
                        await self._handle_message(message)
                    return  # clean exit from message loop

            except (OSError, ConnectionError, asyncio.TimeoutError) as e:
                last_error = e
                logger.warning(
                    "Binance endpoint %s failed: %s", base_url, e
                )
                continue

        # None of the endpoints worked — raise so the reconnect loop retries
        raise ConnectionError(
            f"All Binance endpoints unreachable. Last error: {last_error}"
        )

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

    # ------------------------------------------------------------------
    # REST API fallback (when WebSocket is blocked)
    # ------------------------------------------------------------------
    REST_ENDPOINTS = [
        "https://api.binance.com",
        "https://api1.binance.com",
        "https://api.binance.us",
    ]

    async def _rest_poll_loop(self):
        """Poll Binance REST API for prices when WebSocket is unreachable.

        Less efficient than WebSocket but works through most firewalls
        since it's plain HTTPS on port 443.
        """
        import urllib.request
        import ssl as _ssl

        ssl_ctx = _ssl.create_default_context()
        try:
            import certifi
            ssl_ctx.load_verify_locations(certifi.where())
        except Exception:
            pass

        symbols = ["BTCUSDT", "ETHUSDT"]
        poll_interval = 3.0  # seconds between polls
        working_base: str | None = None

        logger.info("REST polling mode active (poll every %.0fs)", poll_interval)

        while self._running:
            bases = (
                [working_base] if working_base else self.REST_ENDPOINTS
            )
            for base_url in bases:
                try:
                    url = (
                        f"{base_url}/api/v3/ticker/price?"
                        f"symbols=[{','.join(json.dumps(s) for s in symbols)}]"
                    )
                    loop = asyncio.get_event_loop()
                    req = urllib.request.Request(
                        url,
                        headers={"User-Agent": "Polytracker/1.0"},
                    )
                    raw = await loop.run_in_executor(
                        None,
                        lambda: urllib.request.urlopen(
                            req, context=ssl_ctx, timeout=8
                        ).read(),
                    )
                    data = json.loads(raw)
                    for item in data:
                        sym_raw = item.get("symbol", "").lower()
                        asset = self.SYMBOLS.get(sym_raw)
                        if not asset:
                            continue
                        price = float(item.get("price", 0))
                        if price <= 0:
                            continue
                        update = PriceUpdate(
                            symbol=asset,
                            price=price,
                            timestamp=time.time(),
                            volume_24h=0.0,
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

                    if not working_base:
                        logger.info(
                            "REST fallback connected via %s", base_url
                        )
                    working_base = base_url
                    break  # success, don't try other bases

                except Exception as e:
                    logger.debug("REST poll %s failed: %s", base_url, e)
                    working_base = None
                    continue

            await asyncio.sleep(poll_interval)

    async def stop(self):
        """Gracefully stop the feed."""
        self._running = False
        if self._ws:
            await self._ws.close()
            self._ws = None
        logger.info("Binance feed stopped")
