"""Telegram notification system for trade alerts and risk warnings."""

import asyncio
import logging
from datetime import datetime, timezone

import aiohttp

from .config import BotConfig

logger = logging.getLogger(__name__)


class TelegramAlerts:
    """Sends Telegram alerts on every trade and on drawdown thresholds."""

    API_URL = "https://api.telegram.org/bot{token}/sendMessage"

    def __init__(self, config: BotConfig):
        self.config = config
        self._enabled = bool(
            config.api.telegram_bot_token and config.api.telegram_chat_id
        )
        self._session: aiohttp.ClientSession | None = None
        self._rate_limit = asyncio.Semaphore(5)  # Max 5 concurrent sends

        if not self._enabled:
            logger.warning(
                "Telegram alerts disabled - "
                "set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID"
            )

    async def start(self):
        """Initialize the HTTP session."""
        self._session = aiohttp.ClientSession()
        if self._enabled:
            await self.send_message(
                "POLYTRACKER BOT STARTED\n"
                f"Mode: {'PAPER' if not self.config.trading.is_live else 'LIVE'}\n"
                f"Time: {self._now()}"
            )

    async def stop(self):
        """Close the HTTP session."""
        if self._enabled:
            await self.send_message(
                f"POLYTRACKER BOT STOPPED\nTime: {self._now()}"
            )
        if self._session:
            await self._session.close()
            self._session = None

    async def send_message(self, text: str, parse_mode: str = "HTML"):
        """Send a message to Telegram."""
        if not self._enabled:
            logger.debug("[Telegram disabled] %s", text[:100])
            return

        async with self._rate_limit:
            url = self.API_URL.format(token=self.config.api.telegram_bot_token)
            payload = {
                "chat_id": self.config.api.telegram_chat_id,
                "text": text,
                "parse_mode": parse_mode,
                "disable_web_page_preview": True,
            }

            for attempt in range(self.config.max_retries):
                try:
                    async with self._session.post(
                        url, json=payload, timeout=aiohttp.ClientTimeout(total=10)
                    ) as resp:
                        if resp.status == 200:
                            return
                        if resp.status == 429:
                            retry_after = int(
                                resp.headers.get("Retry-After", 5)
                            )
                            logger.warning(
                                "Telegram rate limited, waiting %ds",
                                retry_after,
                            )
                            await asyncio.sleep(retry_after)
                            continue
                        body = await resp.text()
                        logger.warning(
                            "Telegram API error %d: %s", resp.status, body
                        )
                except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                    logger.warning(
                        "Telegram send attempt %d failed: %s",
                        attempt + 1,
                        e,
                    )
                    if attempt < self.config.max_retries - 1:
                        await asyncio.sleep(2 ** attempt)

    async def alert_trade(
        self,
        asset: str,
        timeframe: str,
        direction: str,
        side: str,
        size: float,
        price: float,
        edge_pct: float,
        confidence: float,
        cex_price: float,
        is_paper: bool,
    ):
        """Send a trade execution alert."""
        mode = "PAPER" if is_paper else "LIVE"
        msg = (
            f"{'=' * 25}\n"
            f"<b>TRADE EXECUTED [{mode}]</b>\n"
            f"{'=' * 25}\n"
            f"Asset: <b>{asset}</b>\n"
            f"Contract: {timeframe} {direction.upper()}\n"
            f"Side: <b>{side}</b>\n"
            f"Size: <b>${size:.2f}</b> USDC\n"
            f"Price: {price:.4f}\n"
            f"Edge: {edge_pct:.2f}%\n"
            f"Confidence: {confidence:.1%}\n"
            f"CEX Price: ${cex_price:,.2f}\n"
            f"Time: {self._now()}"
        )
        await self.send_message(msg)

    async def alert_trade_close(
        self,
        asset: str,
        pnl: float,
        exit_price: float,
        is_paper: bool,
    ):
        """Send a trade closure alert."""
        mode = "PAPER" if is_paper else "LIVE"
        emoji_indicator = "PROFIT" if pnl >= 0 else "LOSS"
        msg = (
            f"<b>POSITION CLOSED [{mode}]</b>\n"
            f"Asset: {asset}\n"
            f"P&L: <b>${pnl:+.2f}</b> ({emoji_indicator})\n"
            f"Exit Price: {exit_price:.4f}\n"
            f"Time: {self._now()}"
        )
        await self.send_message(msg)

    async def alert_drawdown(self, level: str, message: str):
        """Send a drawdown/risk alert."""
        msg = (
            f"<b>RISK ALERT [{level}]</b>\n"
            f"{'=' * 25}\n"
            f"{message}\n"
            f"Time: {self._now()}"
        )
        await self.send_message(msg)

    async def alert_error(self, error: str):
        """Send an error alert."""
        msg = (
            f"<b>BOT ERROR</b>\n"
            f"{error[:500]}\n"
            f"Time: {self._now()}"
        )
        await self.send_message(msg)

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
