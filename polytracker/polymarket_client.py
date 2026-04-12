"""Polymarket CLOB API client for monitoring and trading contracts."""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Optional

try:
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import OrderArgs, OrderType
    from py_clob_client.order_builder.constants import BUY, SELL
    HAS_CLOB_CLIENT = True
except Exception:  # pragma: no cover - optional dep in paper mode
    ClobClient = None  # type: ignore
    OrderArgs = None  # type: ignore
    OrderType = None  # type: ignore
    BUY = "BUY"  # type: ignore
    SELL = "SELL"  # type: ignore
    HAS_CLOB_CLIENT = False

from .config import BotConfig

if TYPE_CHECKING:
    from .polysimulator import PolySimulator

logger = logging.getLogger(__name__)


@dataclass
class Contract:
    """Represents a Polymarket binary contract."""

    token_id: str
    condition_id: str
    question: str
    asset: str  # "BTC" or "ETH"
    timeframe: str  # "5m" or "15m"
    direction: str  # "up" or "down"
    yes_price: float  # Current YES price (0-1)
    no_price: float  # Current NO price (0-1)
    liquidity: float
    last_updated: float = 0.0


@dataclass
class TradeOrder:
    """A trade order to place on Polymarket."""

    contract: Contract
    side: str  # "YES" or "NO"
    size: float  # USDC amount
    price: float
    order_type: str = "LIMIT"


@dataclass
class TradeResult:
    """Result of a trade execution."""

    success: bool
    order_id: str = ""
    filled_size: float = 0.0
    filled_price: float = 0.0
    pnl: float = 0.0
    exit_price: float = 0.0
    error: str = ""
    timestamp: float = field(default_factory=time.time)


class PolymarketClient:
    """Client for interacting with Polymarket's CLOB API."""

    def __init__(
        self,
        config: BotConfig,
        simulator: Optional["PolySimulator"] = None,
    ):
        self.config = config
        self._client: ClobClient | None = None
        self._simulator = simulator
        self._contracts: dict[str, Contract] = {}
        self._last_api_call: float = 0.0
        self._lock = asyncio.Lock()

    @property
    def is_simulated(self) -> bool:
        """True when running against the PolySimulator instead of live CLOB."""
        return self._simulator is not None and self._client is None

    async def initialize(self):
        """Initialize the CLOB client with API credentials.

        In paper mode without credentials (or without the py_clob_client
        package), we fall back to the PolySimulator transparently.
        """
        api = self.config.api

        # Simulator mode: no real CLOB client needed
        if self._simulator is not None and (
            not HAS_CLOB_CLIENT or not api.polymarket_api_key
        ):
            logger.info(
                "Polymarket client running in SIMULATOR mode "
                "(no CLOB credentials required)"
            )
            self._client = None
            return

        if not HAS_CLOB_CLIENT:
            logger.warning(
                "py_clob_client not installed - Polymarket client disabled"
            )
            self._client = None
            return

        if not api.polymarket_api_key:
            logger.warning(
                "No Polymarket API key configured - running in read-only mode"
            )

        try:
            self._client = ClobClient(
                api.polymarket_base_url,
                key=api.polymarket_api_key,
                chain_id=137,  # Polygon mainnet
                signature_type=2,  # POLY_GNOSIS_SAFE
                funder=api.polymarket_wallet_address or None,
                private_key=api.polymarket_private_key or None,
            )

            if api.polymarket_api_key:
                self._client.set_api_creds(
                    self._client.create_or_derive_api_creds()
                )

            logger.info("Polymarket client initialized")
        except Exception as e:
            logger.error("Failed to initialize Polymarket client: %s", e)
            if self._simulator is not None:
                logger.warning("Falling back to SIMULATOR mode after CLOB failure")
                self._client = None
                return
            raise

    async def _rate_limit(self):
        """Enforce rate limiting between API calls."""
        async with self._lock:
            now = time.time()
            elapsed = now - self._last_api_call
            if elapsed < self.config.api_rate_limit:
                await asyncio.sleep(self.config.api_rate_limit - elapsed)
            self._last_api_call = time.time()

    async def fetch_crypto_contracts(self) -> list[Contract]:
        """Fetch BTC/ETH short-duration up/down contracts."""
        contracts: list[Contract] = []

        # Simulator path
        if self.is_simulated:
            try:
                markets = await self._simulator.fetch_contracts()
                for market in markets:
                    contract = self._parse_crypto_contract(market)
                    if contract:
                        contracts.append(contract)
                        self._contracts[contract.token_id] = contract
                logger.debug("Fetched %d simulated contracts", len(contracts))
            except Exception as e:
                logger.error("Failed to fetch simulated contracts: %s", e)
            return contracts

        if self._client is None:
            logger.warning(
                "Polymarket client not initialized - no contracts available"
            )
            return contracts

        await self._rate_limit()

        try:
            loop = asyncio.get_event_loop()
            markets = await loop.run_in_executor(
                None, self._client.get_markets
            )

            for market in markets:
                contract = self._parse_crypto_contract(market)
                if contract:
                    contracts.append(contract)
                    self._contracts[contract.token_id] = contract

            logger.info("Fetched %d crypto contracts", len(contracts))

        except Exception as e:
            logger.error("Failed to fetch contracts: %s", e)

        return contracts

    def _parse_crypto_contract(self, market: dict) -> Contract | None:
        """Parse a market into a Contract if it matches our criteria."""
        # Fast path: simulator markets carry their SimContract back-reference
        sim_contract = market.get("_sim_contract")
        if sim_contract is not None:
            tokens = market.get("tokens", [])
            yes_token = next(
                (t for t in tokens if t.get("outcome") == "Yes"), None
            )
            no_token = next(
                (t for t in tokens if t.get("outcome") == "No"), None
            )
            yes_price = float(yes_token.get("price", 0.5)) if yes_token else 0.5
            no_price = float(no_token.get("price", 0.5)) if no_token else 0.5
            return Contract(
                token_id=sim_contract.token_id,
                condition_id=sim_contract.condition_id,
                question=sim_contract.question,
                asset=sim_contract.asset,
                timeframe=sim_contract.timeframe,
                direction=sim_contract.direction,
                yes_price=yes_price,
                no_price=no_price,
                liquidity=sim_contract.liquidity,
                last_updated=time.time(),
            )

        question = market.get("question", "").upper()

        asset = None
        if "BTC" in question or "BITCOIN" in question:
            asset = "BTC"
        elif "ETH" in question or "ETHEREUM" in question:
            asset = "ETH"
        else:
            return None

        timeframe = None
        for tf in self.config.timeframes:
            tf_label = tf.replace("m", " MINUTE").replace("h", " HOUR")
            if tf_label in question or tf.upper() in question:
                timeframe = tf
                break

        if not timeframe:
            if "5" in question and "MINUTE" in question:
                timeframe = "5m"
            elif "15" in question and "MINUTE" in question:
                timeframe = "15m"
            else:
                return None

        direction = "up" if "UP" in question or "ABOVE" in question else "down"

        tokens = market.get("tokens", [])
        if not tokens:
            return None

        yes_token = next((t for t in tokens if t.get("outcome") == "Yes"), None)
        no_token = next((t for t in tokens if t.get("outcome") == "No"), None)

        yes_price = float(yes_token.get("price", 0.5)) if yes_token else 0.5
        no_price = float(no_token.get("price", 0.5)) if no_token else 0.5
        token_id = yes_token.get("token_id", "") if yes_token else ""

        liquidity = float(market.get("volume", 0))

        return Contract(
            token_id=token_id,
            condition_id=market.get("condition_id", ""),
            question=market.get("question", ""),
            asset=asset,
            timeframe=timeframe,
            direction=direction,
            yes_price=yes_price,
            no_price=no_price,
            liquidity=liquidity,
            last_updated=time.time(),
        )

    async def get_orderbook(self, token_id: str) -> dict:
        """Fetch the order book for a specific token."""
        await self._rate_limit()
        try:
            loop = asyncio.get_event_loop()
            book = await loop.run_in_executor(
                None, self._client.get_order_book, token_id
            )
            return book
        except Exception as e:
            logger.error("Failed to fetch orderbook for %s: %s", token_id, e)
            return {}

    async def get_contract_price(self, token_id: str) -> float | None:
        """Get the current mid-price for a contract."""
        book = await self.get_orderbook(token_id)
        if not book:
            return None

        bids = book.get("bids", [])
        asks = book.get("asks", [])

        if bids and asks:
            best_bid = float(bids[0].get("price", 0))
            best_ask = float(asks[0].get("price", 0))
            return (best_bid + best_ask) / 2

        return None

    async def place_order(self, order: TradeOrder) -> TradeResult:
        """Place a trade order on Polymarket."""
        if not self.config.trading.is_live:
            return self._paper_trade(order)

        if self.is_simulated:
            return self._paper_trade(order)

        if not self._client:
            return TradeResult(success=False, error="Client not initialized")

        for attempt in range(self.config.max_retries):
            try:
                await self._rate_limit()

                side = BUY if order.side == "YES" else SELL
                order_args = OrderArgs(
                    price=order.price,
                    size=order.size,
                    side=side,
                    token_id=order.contract.token_id,
                )

                loop = asyncio.get_event_loop()
                signed = await loop.run_in_executor(
                    None,
                    self._client.create_and_post_order,
                    order_args,
                )

                order_id = signed.get("orderID", signed.get("id", ""))

                logger.info(
                    "Order placed: %s %s %.2f @ %.4f (ID: %s)",
                    order.side,
                    order.contract.asset,
                    order.size,
                    order.price,
                    order_id,
                )

                return TradeResult(
                    success=True,
                    order_id=order_id,
                    filled_size=order.size,
                    filled_price=order.price,
                )

            except Exception as e:
                logger.warning(
                    "Order attempt %d/%d failed: %s",
                    attempt + 1,
                    self.config.max_retries,
                    e,
                )
                if attempt < self.config.max_retries - 1:
                    await asyncio.sleep(2 ** attempt)

        return TradeResult(
            success=False,
            error=f"Failed after {self.config.max_retries} attempts",
        )

    def _paper_trade(self, order: TradeOrder) -> TradeResult:
        """Open a paper position that will be held until round expiry.

        Instead of resolving immediately, the position stays OPEN and
        the PolySimulator resolves it when the round expires — just
        like real Polymarket binary contracts.
        """
        trade_id = f"PAPER-{int(time.time() * 1000)}"

        logger.info(
            "[PAPER OPEN] %s %s %.2f USDC @ %.4f | %s %s %s",
            order.side,
            order.contract.asset,
            order.size,
            order.price,
            order.contract.timeframe,
            order.contract.direction,
            order.contract.question[:60],
        )

        if self._simulator is not None:
            try:
                opened = self._simulator.open_position(
                    trade_id=trade_id,
                    asset=order.contract.asset,
                    timeframe=order.contract.timeframe,
                    direction=order.contract.direction,
                    side=order.side,
                    entry_price=order.price,
                    size=order.size,
                )
                if not opened:
                    return TradeResult(
                        success=False,
                        error="No active round for position",
                    )
            except Exception as e:
                logger.error("Simulator open_position failed: %s", e)
                return TradeResult(success=False, error=str(e))

        # Position is OPEN — pnl=0 until the round resolves
        return TradeResult(
            success=True,
            order_id=trade_id,
            filled_size=order.size,
            filled_price=order.price,
            pnl=0.0,
            exit_price=0.0,
        )

    async def cancel_all_orders(self):
        """Cancel all open orders."""
        if not self._client or not self.config.trading.is_live:
            return

        try:
            await self._rate_limit()
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, self._client.cancel_all)
            logger.info("All orders cancelled")
        except Exception as e:
            logger.error("Failed to cancel orders: %s", e)

    def get_cached_contracts(self) -> dict[str, Contract]:
        return dict(self._contracts)
